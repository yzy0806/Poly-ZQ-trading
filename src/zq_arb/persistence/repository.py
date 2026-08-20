from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from zq_arb.config import Settings
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import AuditLogRecord, ConfigVersion


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
