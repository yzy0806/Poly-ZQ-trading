from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from zq_arb.config import Settings
from zq_arb.domain.enums import BatchState
from zq_arb.domain.models import (
    BatchView,
    HedgeObligationView,
    PortfolioPositionView,
    StrategyRiskView,
)
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import (
    AuditLogRecord,
    BatchRecord,
    ConfigVersion,
    ExecutionRecord,
    HedgeObligationRecord,
    OrderRecord,
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
            "ibkr_commission_estimate_per_contract_round_trip": (settings.ibkr_commission_estimate),
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

    async def create_zq_batch_intent(
        self,
        *,
        batch_id: str,
        order_id: int,
        contract_month: str,
        quantity: int,
        limit_price: Decimal,
        strategy_version: str,
        snapshot_id: int,
    ) -> None:
        """Atomically persist the batch and reserved IBKR id before placeOrder."""

        idempotency_key = f"{batch_id}:IBKR:ENTRY"
        async with self.database.session() as session:
            existing = await session.scalar(
                select(BatchRecord).where(BatchRecord.batch_id == batch_id)
            )
            if existing is not None:
                return
            session.add(
                BatchRecord(
                    batch_id=batch_id,
                    strategy_version=strategy_version,
                    state=BatchState.QUALIFIED.value,
                    zq_quantity=quantity,
                    filled_quantity=Decimal("0"),
                    details={
                        "zq_order_id": order_id,
                        "zq_order_status": "INTENT",
                        "contract_month": contract_month,
                        "limit_price": str(limit_price),
                        "remaining_quantity": str(quantity),
                        "entry_snapshot_id": snapshot_id,
                    },
                )
            )
            session.add(
                OrderRecord(
                    batch_id=batch_id,
                    venue="IBKR",
                    venue_order_id=str(order_id),
                    idempotency_key=idempotency_key,
                    state="INTENT",
                    side="BUY",
                    quantity=Decimal(quantity),
                    price=limit_price,
                    details={
                        "contract_month": contract_month,
                        "snapshot_id": snapshot_id,
                        "order_ref": batch_id,
                    },
                )
            )

    async def strategy_zq_quantity(self, contract_month: str) -> Decimal:
        """Return the signed ZQ quantity attributable to the durable strategy ledger."""

        async with self.database.session() as session:
            batches = tuple((await session.scalars(select(BatchRecord))).all())
            batch_ids = tuple(
                item.batch_id
                for item in batches
                if str(item.details.get("contract_month") or "") == contract_month
            )
            if not batch_ids:
                return Decimal("0")
            executions = tuple(
                (
                    await session.scalars(
                        select(ExecutionRecord).where(
                            ExecutionRecord.venue == "IBKR",
                            ExecutionRecord.batch_id.in_(batch_ids),
                        )
                    )
                ).all()
            )
            return sum((item.quantity for item in executions), Decimal("0"))

    async def strategy_portfolio_positions(
        self, settings: Settings
    ) -> tuple[PortfolioPositionView, ...]:
        """Aggregate the strategy's durable executions into open long position lots."""

        async with self.database.session() as session:
            executions = tuple((await session.scalars(select(ExecutionRecord))).all())
            if not executions:
                return ()
            batches = {
                item.batch_id: item
                for item in tuple((await session.scalars(select(BatchRecord))).all())
            }
            orders = tuple((await session.scalars(select(OrderRecord))).all())
            polymarket_orders = {
                item.venue_order_id: item for item in orders if item.venue == "POLYMARKET"
            }

        token_labels = {
            leg.yes_token_id: f"{leg.code} YES" for leg in settings.market_legs
        } | {leg.no_token_id: f"{leg.code} NO" for leg in settings.market_legs}
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        for execution in executions:
            if bool(execution.details.get("terminal_failed")):
                continue
            instrument = ""
            label = ""
            simulated = False
            if execution.venue == "IBKR":
                batch = batches.get(execution.batch_id)
                if batch is None:
                    continue
                instrument = str(batch.details.get("contract_month") or "")
                if not instrument:
                    continue
                label = f"ZQ {instrument}"
            elif execution.venue == "POLYMARKET":
                order = polymarket_orders.get(execution.venue_order_id)
                if order is None:
                    continue
                instrument = str(order.details.get("token_id") or "")
                if not instrument:
                    continue
                label = token_labels.get(instrument, f"Token {instrument[:12]}")
                simulated = bool(order.details.get("simulated"))
            else:
                continue
            key = (execution.venue, instrument)
            aggregate = aggregates.setdefault(
                key,
                {
                    "quantity": Decimal("0"),
                    "weighted_price": Decimal("0"),
                    "label": label,
                    "simulated_flags": [],
                },
            )
            aggregate["quantity"] += execution.quantity
            aggregate["weighted_price"] += execution.quantity * execution.price
            if execution.venue == "POLYMARKET":
                aggregate["simulated_flags"].append(simulated)

        positions: list[PortfolioPositionView] = []
        for (venue, instrument), aggregate in sorted(aggregates.items()):
            quantity = Decimal(aggregate["quantity"])
            if quantity <= 0:
                continue
            simulated_flags = list(aggregate["simulated_flags"])
            positions.append(
                PortfolioPositionView(
                    venue=venue,
                    instrument=instrument,
                    label=str(aggregate["label"]),
                    strategy_quantity=quantity,
                    average_entry_price=Decimal(aggregate["weighted_price"]) / quantity,
                    multiplier=Decimal("4167") if venue == "IBKR" else Decimal("1"),
                    simulated=bool(simulated_flags) and all(simulated_flags),
                )
            )
        return tuple(positions)

    async def mark_zq_submitted(self, batch_id: str) -> None:
        async with self.database.session() as session:
            batch = await session.scalar(
                select(BatchRecord).where(BatchRecord.batch_id == batch_id)
            )
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.batch_id == batch_id,
                    OrderRecord.venue == "IBKR",
                )
            )
            if batch is None or order is None:
                raise RuntimeError("durable ZQ order intent is missing")
            if batch.state == BatchState.QUALIFIED.value:
                batch.state = BatchState.ZQ_SUBMITTED.value
            if str(batch.details.get("zq_order_status") or "").upper() == "INTENT":
                batch.details = {**batch.details, "zq_order_status": "SUBMITTED"}
            batch.updated_at = datetime.now(UTC)
            if order.state == "INTENT":
                order.state = "SUBMITTED"

    async def record_ibkr_execution_and_obligations(
        self,
        *,
        order_id: int,
        execution_id: str,
        quantity: Decimal,
        price: Decimal,
        executed_at: datetime,
        token_shares: dict[str, Decimal],
        details: dict[str, Any],
    ) -> tuple[HedgeObligationView, ...]:
        """Persist one unique execId and both hedge obligations in one commit."""

        async with self.database.session() as session:
            duplicate = await session.scalar(
                select(ExecutionRecord).where(
                    ExecutionRecord.venue == "IBKR",
                    ExecutionRecord.execution_id == execution_id,
                )
            )
            if duplicate is not None:
                return ()
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "IBKR",
                    OrderRecord.venue_order_id == str(order_id),
                )
            )
            if order is None:
                return ()
            batch = await session.scalar(
                select(BatchRecord).where(BatchRecord.batch_id == order.batch_id)
            )
            if batch is None:
                raise RuntimeError("execution references a missing batch")
            if quantity <= 0 or batch.filled_quantity + quantity > batch.zq_quantity:
                raise ValueError("IBKR execution quantity is outside the batch")
            session.add(
                ExecutionRecord(
                    batch_id=batch.batch_id,
                    venue="IBKR",
                    execution_id=execution_id,
                    venue_order_id=str(order_id),
                    quantity=quantity,
                    price=price,
                    executed_at=executed_at,
                    details=json_safe(details),
                )
            )
            batch.filled_quantity += quantity
            remaining = max(Decimal("0"), Decimal(batch.zq_quantity) - batch.filled_quantity)
            batch.state = BatchState.ZQ_PARTIAL.value
            batch.details = {
                **batch.details,
                "remaining_quantity": str(remaining),
                "last_exec_id": execution_id,
            }
            batch.updated_at = datetime.now(UTC)
            obligations: list[HedgeObligationView] = []
            for token_id, due_shares in token_shares.items():
                obligation_id = f"{batch.batch_id}:{execution_id}:{token_id}"
                session.add(
                    HedgeObligationRecord(
                        obligation_id=obligation_id,
                        batch_id=batch.batch_id,
                        exec_id=execution_id,
                        token_id=token_id,
                        due_shares=due_shares,
                        confirmed_shares=Decimal("0"),
                        state="PENDING",
                        details={"order_attempts": []},
                    )
                )
                obligations.append(
                    HedgeObligationView(
                        obligation_id=obligation_id,
                        batch_id=batch.batch_id,
                        exec_id=execution_id,
                        token_id=token_id,
                        due_shares=due_shares,
                        state="PENDING",
                    )
                )
            return tuple(obligations)

    async def create_hedge_order_intent(
        self,
        *,
        obligation_id: str,
        idempotency_key: str,
        shares: Decimal,
        limit_price: Decimal,
        attempt: int,
        signed_payload: dict[str, Any] | None,
    ) -> bool:
        async with self.database.session() as session:
            existing = await session.scalar(
                select(OrderRecord).where(OrderRecord.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return False
            obligation = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id == obligation_id
                )
            )
            if obligation is None:
                raise RuntimeError("hedge obligation is missing")
            session.add(
                OrderRecord(
                    batch_id=obligation.batch_id,
                    venue="POLYMARKET",
                    venue_order_id=f"INTENT:{idempotency_key}",
                    idempotency_key=idempotency_key,
                    state="INTENT",
                    side="BUY",
                    quantity=shares,
                    price=limit_price,
                    details={
                        "obligation_id": obligation_id,
                        "token_id": obligation.token_id,
                        "attempt": attempt,
                        "signed_payload": json_safe(signed_payload),
                    },
                )
            )
            obligation.state = "ORDER_INTENT"
            obligation.details = {
                **obligation.details,
                "latest_idempotency_key": idempotency_key,
                "latest_limit_price": str(limit_price),
                "reprice_count": attempt,
            }
            return True

    async def pending_polymarket_intents(self) -> tuple[dict[str, Any], ...]:
        async with self.database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(OrderRecord).where(
                            OrderRecord.venue == "POLYMARKET",
                            OrderRecord.state == "INTENT",
                        )
                    )
                ).all()
            )
            return tuple(
                {
                    "idempotency_key": item.idempotency_key,
                    "token_id": str(item.details.get("token_id") or ""),
                    "shares": item.quantity,
                    "limit_price": item.price,
                    "signed_payload": item.details.get("signed_payload"),
                }
                for item in records
            )

    async def accept_hedge_order(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        state: str,
        simulated: bool,
    ) -> None:
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(OrderRecord.idempotency_key == idempotency_key)
            )
            if order is None:
                raise RuntimeError("hedge order intent is missing")
            order.venue_order_id = order_id
            order.state = state.upper()
            order.details = {**order.details, "simulated": simulated}
            obligation_id = str(order.details["obligation_id"])
            obligation = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id == obligation_id
                )
            )
            if obligation is not None:
                attempts = list(obligation.details.get("order_attempts") or [])
                attempts.append(order_id)
                obligation.details = {
                    **obligation.details,
                    "latest_order_id": order_id,
                    "order_attempts": attempts,
                }
                obligation.state = "ORDER_LIVE" if state.lower() == "live" else "MATCHED"

    async def mark_polymarket_order_cancelled(self, order_id: str) -> None:
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET",
                    OrderRecord.venue_order_id == order_id,
                )
            )
            if order is not None:
                order.state = "CANCELLED"
                obligation = await session.scalar(
                    select(HedgeObligationRecord).where(
                        HedgeObligationRecord.obligation_id
                        == str(order.details.get("obligation_id") or "")
                    )
                )
                if obligation is not None and obligation.confirmed_shares < obligation.due_shares:
                    obligation.state = "PENDING"

    async def update_polymarket_order_status(self, order_id: str, status: str) -> None:
        normalized = status.upper()
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET",
                    OrderRecord.venue_order_id == order_id,
                )
            )
            if order is None:
                return
            order.state = normalized
            obligation = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id
                    == str(order.details.get("obligation_id") or "")
                )
            )
            if obligation is None or obligation.confirmed_shares >= obligation.due_shares:
                return
            if normalized in {"CANCELED", "CANCELLED", "UNMATCHED"}:
                obligation.state = "PENDING"
            elif normalized == "LIVE":
                obligation.state = "ORDER_LIVE"
            elif normalized == "MATCHED":
                obligation.state = "MATCHED"

    async def record_polymarket_execution(
        self,
        *,
        execution_id: str,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
        executed_at: datetime,
        details: dict[str, Any],
    ) -> bool:
        """Credit a unique trade id to exactly one obligation."""

        async with self.database.session() as session:
            duplicate = await session.scalar(
                select(ExecutionRecord).where(
                    ExecutionRecord.venue == "POLYMARKET",
                    ExecutionRecord.execution_id == execution_id,
                )
            )
            if duplicate is not None:
                return False
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET",
                    OrderRecord.venue_order_id == order_id,
                )
            )
            if order is None:
                return False
            obligation = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id
                    == str(order.details.get("obligation_id") or "")
                )
            )
            if obligation is None:
                raise RuntimeError("Polymarket execution has no hedge obligation")
            credit = min(
                quantity,
                max(Decimal("0"), obligation.due_shares - obligation.confirmed_shares),
            )
            if credit <= 0:
                return False
            session.add(
                ExecutionRecord(
                    batch_id=order.batch_id,
                    venue="POLYMARKET",
                    execution_id=execution_id,
                    venue_order_id=order_id,
                    quantity=credit,
                    price=price,
                    executed_at=executed_at,
                    details=json_safe(details),
                )
            )
            obligation.confirmed_shares += credit
            obligation.state = (
                "HEDGED" if obligation.confirmed_shares >= obligation.due_shares else "PARTIAL"
            )
            order.state = "MATCHED"
            await session.flush()
            await self._refresh_batch_state(session, order.batch_id)
            return True

    async def fail_polymarket_execution(self, execution_id: str) -> bool:
        """Reverse a previously credited match when Polymarket reports terminal FAILED."""

        async with self.database.session() as session:
            execution = await session.scalar(
                select(ExecutionRecord).where(
                    ExecutionRecord.venue == "POLYMARKET",
                    ExecutionRecord.execution_id == execution_id,
                )
            )
            if execution is None or execution.details.get("terminal_failed"):
                return False
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET",
                    OrderRecord.venue_order_id == execution.venue_order_id,
                )
            )
            if order is None:
                return False
            obligation = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id
                    == str(order.details.get("obligation_id") or "")
                )
            )
            if obligation is None:
                return False
            obligation.confirmed_shares = max(
                Decimal("0"), obligation.confirmed_shares - execution.quantity
            )
            obligation.state = "PENDING"
            order.state = "FAILED"
            execution.details = {**execution.details, "terminal_failed": True}
            await session.flush()
            await self._refresh_batch_state(session, order.batch_id)
            return True

    async def update_zq_order_status(
        self,
        *,
        order_id: int,
        status: str,
        filled: Decimal | None = None,
        remaining: Decimal | None = None,
        permanent_id: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "IBKR",
                    OrderRecord.venue_order_id == str(order_id),
                )
            )
            if order is None:
                return
            order.state = status.upper()
            order.permanent_id = permanent_id or order.permanent_id
            batch = await session.scalar(
                select(BatchRecord).where(BatchRecord.batch_id == order.batch_id)
            )
            if batch is None:
                return
            details = {**batch.details, "zq_order_status": status}
            if remaining is not None:
                details["remaining_quantity"] = str(max(Decimal("0"), remaining))
            if filled is not None:
                details["reported_filled_quantity"] = str(filled)
            batch.details = details
            batch.updated_at = datetime.now(UTC)
            if status.upper() in {"CANCELLED", "CANCELED", "APICANCELLED", "INACTIVE"}:
                await self._refresh_batch_state(session, batch.batch_id)

    async def set_batch_cancel_pending(
        self,
        *,
        batch_id: str,
        reason: str,
        residual_minimum_net_profit: Decimal | None,
        required_profit: Decimal,
        residual_return_bps: Decimal | None,
    ) -> bool:
        async with self.database.session() as session:
            batch = await session.scalar(
                select(BatchRecord).where(BatchRecord.batch_id == batch_id)
            )
            if batch is None or batch.state == BatchState.CANCEL_PENDING.value:
                return False
            batch.state = BatchState.CANCEL_PENDING.value
            batch.details = {
                **batch.details,
                "cancel_reason": reason,
                "residual_minimum_net_profit": (
                    str(residual_minimum_net_profit)
                    if residual_minimum_net_profit is not None
                    else None
                ),
                "residual_required_profit": str(required_profit),
                "residual_return_on_capital_bps": (
                    str(residual_return_bps) if residual_return_bps is not None else None
                ),
            }
            batch.updated_at = datetime.now(UTC)
            return True

    async def active_batch_view(self) -> BatchView:
        async with self.database.session() as session:
            batch = await session.scalar(
                select(BatchRecord)
                .where(BatchRecord.state != BatchState.COMPLETE.value)
                .order_by(BatchRecord.created_at.desc())
                .limit(1)
            )
            if batch is None:
                return BatchView()
            orders = tuple(
                (
                    await session.scalars(
                        select(OrderRecord).where(OrderRecord.batch_id == batch.batch_id)
                    )
                ).all()
            )
            obligation_records = tuple(
                (
                    await session.scalars(
                        select(HedgeObligationRecord)
                        .where(HedgeObligationRecord.batch_id == batch.batch_id)
                        .order_by(HedgeObligationRecord.id)
                    )
                ).all()
            )
            ibkr_order = next((item for item in orders if item.venue == "IBKR"), None)
            obligations = tuple(
                HedgeObligationView(
                    obligation_id=item.obligation_id,
                    batch_id=item.batch_id,
                    exec_id=item.exec_id,
                    token_id=item.token_id,
                    due_shares=item.due_shares,
                    confirmed_shares=item.confirmed_shares,
                    state=item.state,
                    latest_order_id=str(item.details.get("latest_order_id") or "") or None,
                    latest_limit_price=self._optional_decimal(
                        item.details.get("latest_limit_price")
                    ),
                    reprice_count=int(item.details.get("reprice_count") or 0),
                )
                for item in obligation_records
            )
            details = batch.details
            return BatchView(
                batch_id=batch.batch_id,
                state=BatchState(batch.state),
                zq_order_id=(int(ibkr_order.venue_order_id) if ibkr_order is not None else None),
                original_quantity=batch.zq_quantity,
                filled_quantity=batch.filled_quantity,
                remaining_quantity=self._optional_decimal(details.get("remaining_quantity"))
                or Decimal("0"),
                limit_price=ibkr_order.price if ibkr_order is not None else None,
                zq_order_status=str(details.get("zq_order_status") or "") or None,
                cancel_reason=str(details.get("cancel_reason") or "") or None,
                residual_minimum_net_profit=self._optional_decimal(
                    details.get("residual_minimum_net_profit")
                ),
                residual_required_profit=self._optional_decimal(
                    details.get("residual_required_profit")
                ),
                residual_return_on_capital_bps=self._optional_decimal(
                    details.get("residual_return_on_capital_bps")
                ),
                obligations=obligations,
                updated_at=batch.updated_at,
            )

    async def pending_obligations(self) -> tuple[HedgeObligationView, ...]:
        view = await self.active_batch_view()
        return tuple(item for item in view.obligations if item.deficit_shares > 0)

    async def _refresh_batch_state(self, session: Any, batch_id: str) -> None:
        batch = await session.scalar(select(BatchRecord).where(BatchRecord.batch_id == batch_id))
        if batch is None:
            return
        obligations = tuple(
            (
                await session.scalars(
                    select(HedgeObligationRecord).where(HedgeObligationRecord.batch_id == batch_id)
                )
            ).all()
        )
        hedged = bool(obligations) and all(
            item.confirmed_shares >= item.due_shares for item in obligations
        )
        status = str(batch.details.get("zq_order_status") or "").upper()
        terminal_zq = status in {
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "APICANCELLED",
            "INACTIVE",
        }
        cancel_requested = bool(batch.details.get("cancel_reason"))
        reported_filled = self._optional_decimal(batch.details.get("reported_filled_quantity"))
        executions_caught_up = (
            reported_filled is not None and batch.filled_quantity >= reported_filled
        )
        hedge_caught_up = reported_filled == 0 or hedged
        if terminal_zq and executions_caught_up and hedge_caught_up:
            batch.state = BatchState.COMPLETE.value
            batch.details = {**batch.details, "remaining_quantity": "0"}
        elif cancel_requested:
            batch.state = BatchState.CANCEL_PENDING.value
        elif hedged:
            batch.state = BatchState.HEDGED.value
        elif any(item.confirmed_shares > 0 for item in obligations):
            batch.state = BatchState.PARTIALLY_HEDGED.value
        elif obligations:
            batch.state = BatchState.POLY_HEDGE_PENDING.value
        batch.updated_at = datetime.now(UTC)

    @staticmethod
    def _optional_decimal(value: Any) -> Decimal | None:
        return Decimal(str(value)) if value not in {None, ""} else None

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

    async def record_automated_reconciliation(
        self,
        *,
        clean: bool,
        expected: dict[str, Any],
        observed: dict[str, Any],
        differences: dict[str, Any],
    ) -> str:
        reconciliation_id = str(uuid4())
        async with self.database.session() as session:
            session.add(
                ReconciliationRecord(
                    reconciliation_id=reconciliation_id,
                    clean=clean,
                    expected=json_safe(expected),
                    observed=json_safe(observed),
                    differences=json_safe(differences),
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
