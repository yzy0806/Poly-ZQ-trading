from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from zq_arb.config import Settings
from zq_arb.domain.models import StrategyRiskView
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import (
    AuditLogRecord,
    ConfigVersion,
    ReconciliationRecord,
    StrategyRiskRecord,
)


def canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def json_safe(value: Any) -> Any:
    """Round-trip through the canonical encoder before storing in a JSON column."""

    return json.loads(json.dumps(value, default=str))


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def ensure_config_version(self, settings: Settings) -> None:
        safe_payload = {
            "config_version": settings.config_version,
            "strategy_version": settings.strategy_version,
            "run_mode": settings.run_mode,
            "zq_child_quantity": settings.ibkr_zq_child_order_quantity,
            "zq_subscription_months": settings.subscription_contract_months,
            "effr": {
                "source": settings.effr_source,
                "manual_percent": settings.pre_meeting_effr_percent,
                "nyfed_api_url": settings.nyfed_effr_api_url,
                "maximum_age_days": settings.nyfed_effr_max_age_days,
            },
            "max_zq_position": settings.max_zq_position,
            "min_profit": settings.min_net_profit_usd,
            "min_return_bps": settings.min_return_on_capital_bps,
            "reserve_values": {
                "model": settings.model_risk_reserve_usd,
                "operational": settings.operational_risk_reserve_usd,
                "effr_basis": settings.effr_basis_reserve_usd,
            },
        }
        async with self.database.session() as session:
            existing = await session.scalar(
                select(ConfigVersion).where(ConfigVersion.config_version == settings.config_version)
            )
            if existing is None:
                session.add(
                    ConfigVersion(
                        config_version=settings.config_version,
                        strategy_version=settings.strategy_version,
                        config_hash=canonical_hash(safe_payload),
                        approved_by=settings.operator_approval_id or None,
                        details=json_safe(safe_payload),
                    )
                )

    async def audit(
        self,
        *,
        actor: str,
        action: str,
        reason: str,
        correlation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid4())
        async with self.database.session() as session:
            session.add(
                AuditLogRecord(
                    event_id=event_id,
                    actor=actor,
                    action=action,
                    reason=reason,
                    correlation_id=correlation_id,
                    details=json_safe(details or {}),
                )
            )
        return event_id

    async def load_or_create_strategy_risk(self, settings: Settings) -> StrategyRiskView:
        async with self.database.session() as session:
            record = await session.get(StrategyRiskRecord, 1)
            if record is None:
                now = datetime.now(UTC)
                capital = settings.strategy_allocated_capital_usd
                record = StrategyRiskRecord(
                    id=1,
                    allocated_capital=capital,
                    cumulative_realized_pnl=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                    fees=Decimal("0"),
                    equity=capital,
                    high_water_mark=capital,
                    drawdown=Decimal("0"),
                    daily_pnl=Decimal("0"),
                    trading_day=now.date().isoformat(),
                    source="PERSISTED_STRATEGY_LEDGER",
                    valued_at=now,
                )
                session.add(record)
                await session.flush()
            elif record.allocated_capital != settings.strategy_allocated_capital_usd:
                raise RuntimeError(
                    "configured strategy capital differs from the persisted risk ledger; "
                    "an audited capital reset is required"
                )
            return self._strategy_risk_view(record)

    async def save_strategy_risk(self, risk: StrategyRiskView) -> None:
        async with self.database.session() as session:
            record = await session.get(StrategyRiskRecord, 1)
            if record is None:
                record = StrategyRiskRecord(id=1)
                session.add(record)
            record.allocated_capital = risk.allocated_capital
            record.cumulative_realized_pnl = risk.cumulative_realized_pnl
            record.unrealized_pnl = risk.unrealized_pnl
            record.fees = risk.fees
            record.equity = risk.equity
            record.high_water_mark = risk.high_water_mark
            record.drawdown = risk.drawdown
            record.daily_pnl = risk.daily_pnl
            record.trading_day = risk.trading_day
            record.source = risk.source
            record.valued_at = risk.valued_at

    async def record_reconciliation(
        self,
        *,
        actor: str,
        reason: str,
        expected: dict[str, Any],
        observed: dict[str, Any],
    ) -> str:
        reconciliation_id = str(uuid4())
        async with self.database.session() as session:
            session.add(
                ReconciliationRecord(
                    reconciliation_id=reconciliation_id,
                    clean=True,
                    expected=json_safe({"operator": actor, "reason": reason, **expected}),
                    observed=json_safe(observed),
                    differences={},
                )
            )
        return reconciliation_id

    @staticmethod
    def _strategy_risk_view(record: StrategyRiskRecord) -> StrategyRiskView:
        valued_at = record.valued_at
        if valued_at.tzinfo is None:
            valued_at = valued_at.replace(tzinfo=UTC)
        return StrategyRiskView(
            allocated_capital=record.allocated_capital,
            cumulative_realized_pnl=record.cumulative_realized_pnl,
            unrealized_pnl=record.unrealized_pnl,
            fees=record.fees,
            equity=record.equity,
            high_water_mark=record.high_water_mark,
            drawdown=record.drawdown,
            daily_pnl=record.daily_pnl,
            trading_day=record.trading_day,
            source=record.source,
            valued_at=valued_at,
        )
