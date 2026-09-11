from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from zq_arb.domain.models import HedgeObligationView
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import (
    ExecutionRecord,
    HedgeObligationRecord,
    OrderRecord,
    VenueEventRecord,
)

ZERO = Decimal("0")
TERMINAL_ORDERS = frozenset({"CANCELLED", "FILLED", "MATCHED", "REJECTED", "EXPIRED"})
TRADE_STATES = {"MATCHED": 1, "MINED": 2, "RETRYING": 2, "CONFIRMED": 3, "FAILED": 4}


def normalize_status(status: str) -> str:
    value = status.upper().removeprefix("ORDER_STATUS_").removeprefix("TRADE_STATUS_")
    return {"CANCELED": "CANCELLED", "CANCELLATION": "CANCELLED"}.get(value, value)


def order_resolved(order: OrderRecord) -> bool:
    return order.state in TERMINAL_ORDERS and bool(order.details.get("terminal_verified"))


def safe_json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class HedgeLedger:
    database: Database

    async def _refresh_batch_state(self, session: Any, batch_id: str) -> None:
        raise NotImplementedError

    @staticmethod
    def obligation_view(item: HedgeObligationRecord) -> HedgeObligationView:
        return HedgeObligationView(
            obligation_id=item.obligation_id,
            batch_id=item.batch_id,
            exec_id=item.exec_id,
            token_id=item.token_id,
            due_shares=item.due_shares,
            confirmed_shares=item.confirmed_shares,
            pending_shares=Decimal(str(item.details.get("pending_shares", "0"))),
            excess_shares=Decimal(str(item.details.get("excess_shares", "0"))),
            state=item.state,
            latest_order_id=str(item.details.get("latest_order_id") or "") or None,
            latest_limit_price=(
                Decimal(str(item.details["latest_limit_price"]))
                if item.details.get("latest_limit_price") is not None
                else None
            ),
            reprice_count=int(item.details.get("reprice_count", 0)),
        )

    async def all_hedge_obligations(self) -> tuple[HedgeObligationView, ...]:
        async with self.database.session() as session:
            records = (
                await session.scalars(
                    select(HedgeObligationRecord).order_by(HedgeObligationRecord.id)
                )
            ).all()
            return tuple(self.obligation_view(item) for item in records)

    async def hedge_obligation(self, obligation_id: str) -> HedgeObligationView:
        async with self.database.session() as session:
            item = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id == obligation_id
                )
            )
            if item is None:
                raise RuntimeError("hedge obligation is missing")
            return self.obligation_view(item)

    async def ledger_orders(self, venue: str | None = None) -> tuple[OrderRecord, ...]:
        async with self.database.session() as session:
            query = select(OrderRecord).order_by(OrderRecord.id)
            if venue is not None:
                query = query.where(OrderRecord.venue == venue)
            return tuple((await session.scalars(query)).all())

    async def create_hedge_order_intent(
        self,
        *,
        obligation_id: str,
        idempotency_key: str,
        shares: Decimal,
        limit_price: Decimal,
        attempt: int,
        signed_payload: dict[str, Any] | None,
        order_id: str | None = None,
    ) -> bool:
        await self.database.validate_execution_environment()
        simulated = self.database.settings.simulate_polymarket_fills
        if signed_payload is None and not simulated:
            raise RuntimeError("Unsigned hedge intent is forbidden outside simulation")
        if signed_payload is not None and simulated:
            raise RuntimeError("Simulation refuses a real signed hedge intent")
        if not simulated and (not order_id or order_id.startswith(("INTENT:", "SIM-"))):
            raise RuntimeError("Real hedge intent requires its signed order hash before submission")
        async with self.database.session() as session:
            orders = tuple(
                (
                    await session.scalars(
                        select(OrderRecord).where(OrderRecord.venue == "POLYMARKET")
                    )
                ).all()
            )
            if any(item.idempotency_key == idempotency_key for item in orders):
                return False
            if any(
                item.details.get("obligation_id") == obligation_id and not order_resolved(item)
                for item in orders
            ):
                return False
            obligation = await session.scalar(
                select(HedgeObligationRecord).where(
                    HedgeObligationRecord.obligation_id == obligation_id
                )
            )
            if obligation is None:
                raise RuntimeError("hedge obligation is missing")
            available = max(
                ZERO,
                obligation.due_shares
                - obligation.confirmed_shares
                - Decimal(str(obligation.details.get("pending_shares", "0"))),
            )
            if shares <= 0 or shares > available:
                raise RuntimeError("hedge order exceeds the unreserved obligation")
            session.add(
                OrderRecord(
                    batch_id=obligation.batch_id,
                    venue="POLYMARKET",
                    venue_order_id=order_id or f"INTENT:{idempotency_key}",
                    idempotency_key=idempotency_key,
                    state="INTENT",
                    side="BUY",
                    quantity=shares,
                    price=limit_price,
                    details=safe_json(
                        {
                            "obligation_id": obligation_id,
                            "token_id": obligation.token_id,
                            "attempt": attempt,
                            "signed_payload": signed_payload,
                            "execution_environment": self.database.identity,
                            "simulated": simulated,
                            "terminal_verified": False,
                        }
                    ),
                )
            )
            obligation.state = "ORDER_INTENT"
            obligation.details = {
                **obligation.details,
                "latest_idempotency_key": idempotency_key,
                "latest_order_id": order_id,
                "latest_limit_price": str(limit_price),
                "reprice_count": attempt,
            }
            return True

    async def pending_polymarket_intents(self) -> tuple[dict[str, Any], ...]:
        orders = await self.ledger_orders("POLYMARKET")
        return tuple(
            {
                **item.details,
                "idempotency_key": item.idempotency_key,
                "order_id": item.venue_order_id,
                "state": item.state,
                "shares": item.quantity,
                "limit_price": item.price,
            }
            for item in orders
            if item.state in {"INTENT", "SUBMITTING", "OUTCOME_UNKNOWN"}
        )

    async def set_hedge_submission_state(self, key: str, state: str) -> None:
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(OrderRecord.idempotency_key == key)
            )
            if order is None:
                raise RuntimeError("durable hedge intent is missing")
            if order.state in {"INTENT", "SUBMITTING", "OUTCOME_UNKNOWN"}:
                order.state = state
                order.details = {**order.details, "last_submit_at": datetime.now(UTC).isoformat()}

    async def accept_hedge_order(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        state: str,
        simulated: bool,
    ) -> None:
        if simulated != self.database.settings.simulate_polymarket_fills:
            raise RuntimeError("Hedge acknowledgement has incompatible simulation provenance")
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(OrderRecord.idempotency_key == idempotency_key)
            )
            if order is None:
                raise RuntimeError("hedge order intent is missing")
            if not order.venue_order_id.startswith("INTENT:") and order.venue_order_id != order_id:
                raise RuntimeError("Venue acknowledgement does not match the persisted order hash")
            order.venue_order_id = order_id
            if not order_resolved(order):
                order.state = normalize_status(state)
            order.details = {**order.details, "simulated": simulated}
            await self._refresh_hedge(session, str(order.details["obligation_id"]))

    async def mark_polymarket_order_cancelled(self, order_id: str) -> None:
        # A cancellation ACK is not proof that its fills have all been received.
        await self.update_polymarket_order_status(order_id, "CANCEL_PENDING")

    async def update_polymarket_order_status(self, order_id: str, status: str) -> None:
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET", OrderRecord.venue_order_id == order_id
                )
            )
            if order is None or order_resolved(order):
                return
            order.state = normalize_status(status)
            order.details = {**order.details, "terminal_verified": False}
            await self._refresh_hedge(session, str(order.details["obligation_id"]))

    async def verify_polymarket_order(self, order_id: str, snapshot: dict[str, Any]) -> bool:
        """Release a reservation only with terminal status AND cumulative fill evidence."""
        status = normalize_status(str(snapshot.get("status") or "UNKNOWN"))
        raw_matched = snapshot.get("size_matched")
        if raw_matched is None:
            return False
        matched = Decimal(str(raw_matched))
        if not matched.is_finite() or matched < 0:
            raise ValueError("invalid cumulative matched quantity")
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET", OrderRecord.venue_order_id == order_id
                )
            )
            if order is None:
                return False
            executions = tuple(
                (
                    await session.scalars(
                        select(ExecutionRecord).where(
                            ExecutionRecord.venue == "POLYMARKET",
                            ExecutionRecord.venue_order_id == order_id,
                        )
                    )
                ).all()
            )
            recorded = sum((item.quantity for item in executions), ZERO)
            known_trades = {
                str(item.details.get("trade_id") or item.execution_id) for item in executions
            }
            associated = set(snapshot.get("associate_trades") or ())
            verified = (
                status in TERMINAL_ORDERS
                and recorded == matched
                and matched <= order.quantity
                and associated <= known_trades
            )
            if (
                order_resolved(order)
                and not verified
                and matched <= Decimal(str(order.details.get("venue_matched_shares", "0")))
                and recorded <= matched
            ):
                # A stale live update cannot reopen a reconciled cancelled order.
                return True
            order.state = status
            order.details = {
                **order.details,
                "venue_matched_shares": str(matched),
                "terminal_verified": verified,
                "last_order_check": datetime.now(UTC).isoformat(),
            }
            await self._refresh_hedge(session, str(order.details["obligation_id"]))
            return verified

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
        if not quantity.is_finite() or quantity <= 0 or not price.is_finite() or price < 0:
            raise ValueError("invalid execution quantity or price")
        simulated = bool(details.get("simulated"))
        if simulated != self.database.settings.simulate_polymarket_fills:
            raise RuntimeError("Execution has incompatible simulation provenance")
        status = (
            "CONFIRMED" if simulated else normalize_status(str(details.get("status", "UNKNOWN")))
        )
        async with self.database.session() as session:
            order = await session.scalar(
                select(OrderRecord).where(
                    OrderRecord.venue == "POLYMARKET", OrderRecord.venue_order_id == order_id
                )
            )
            if order is None:
                return False  # The durable venue receipt remains unprocessed for recovery.
            execution = await session.scalar(
                select(ExecutionRecord).where(
                    ExecutionRecord.venue == "POLYMARKET",
                    ExecutionRecord.execution_id == execution_id,
                )
            )
            if execution is None:
                execution = ExecutionRecord(
                    batch_id=order.batch_id,
                    venue="POLYMARKET",
                    execution_id=execution_id,
                    venue_order_id=order_id,
                    quantity=quantity,
                    price=price,
                    executed_at=executed_at,
                    details={},
                )
                session.add(execution)
            else:
                if (execution.venue_order_id, execution.quantity, execution.price) != (
                    order_id,
                    quantity,
                    price,
                ):
                    raise RuntimeError("Conflicting payload for an existing execution identity")
                previous = str(execution.details.get("status", "UNKNOWN"))
                if previous == "CONFLICT" or {previous, status} == {"CONFIRMED", "FAILED"}:
                    execution.details = {
                        **execution.details,
                        "status": "CONFLICT",
                        "terminal_failed": False,
                        "conflicting_status": status,
                    }
                    await session.flush()
                    await self._refresh_hedge(session, str(order.details["obligation_id"]))
                    return True
                if previous == status or TRADE_STATES.get(previous, 0) > TRADE_STATES.get(
                    status, 0
                ):
                    return True
            execution.details = safe_json(
                {
                    **execution.details,
                    **details,
                    "status": status,
                    "terminal_failed": status == "FAILED",
                    "simulated": simulated,
                    "token_id": order.details["token_id"],
                    "execution_environment": self.database.identity,
                }
            )
            if simulated:
                order.state = "FILLED"
                order.details = {**order.details, "terminal_verified": True}
            await session.flush()
            await self._refresh_hedge(session, str(order.details["obligation_id"]))
            return True

    async def _refresh_hedge(self, session: Any, obligation_id: str) -> None:
        obligation = await session.scalar(
            select(HedgeObligationRecord).where(
                HedgeObligationRecord.obligation_id == obligation_id
            )
        )
        if obligation is None:
            raise RuntimeError("Polymarket execution has no hedge obligation")
        orders = tuple(
            (
                await session.scalars(
                    select(OrderRecord).where(
                        OrderRecord.venue == "POLYMARKET",
                        OrderRecord.batch_id == obligation.batch_id,
                    )
                )
            ).all()
        )
        orders = tuple(
            item for item in orders if item.details.get("obligation_id") == obligation_id
        )
        ids = [item.venue_order_id for item in orders]
        executions = tuple(
            (
                await session.scalars(
                    select(ExecutionRecord)
                    .where(
                        ExecutionRecord.venue == "POLYMARKET",
                        ExecutionRecord.venue_order_id.in_(ids),
                    )
                    .order_by(ExecutionRecord.id)
                )
            ).all()
        )
        for order in orders:
            recorded = sum(
                (
                    item.quantity
                    for item in executions
                    if item.venue_order_id == order.venue_order_id
                ),
                ZERO,
            )
            if (
                order_resolved(order)
                and not order.details.get("simulated")
                and recorded > Decimal(str(order.details.get("venue_matched_shares", "0")))
            ):
                order.details = {**order.details, "terminal_verified": False}
        confirmed = ZERO
        pending = ZERO
        for item in executions:
            status = item.details.get("status", "UNKNOWN")
            allocated = ZERO
            if status == "CONFIRMED":
                allocated = min(item.quantity, max(ZERO, obligation.due_shares - confirmed))
                confirmed += item.quantity
            elif status != "FAILED":
                pending += item.quantity
            item.details = {**item.details, "allocated_shares": str(allocated)}
        excess = max(ZERO, confirmed + pending - obligation.due_shares)
        obligation.confirmed_shares = min(confirmed, obligation.due_shares)
        obligation.details = {
            **obligation.details,
            "actual_confirmed_shares": str(confirmed),
            "pending_shares": str(pending),
            "excess_shares": str(excess),
            "order_attempts": ids,
            "latest_order_id": ids[-1] if ids else None,
        }
        if excess:
            obligation.state = "EXCESS"
        elif any(not order_resolved(item) for item in orders):
            obligation.state = "ORDER_UNRESOLVED"
        elif confirmed >= obligation.due_shares:
            obligation.state = "HEDGED"
        elif pending:
            obligation.state = "SETTLEMENT_PENDING"
        else:
            obligation.state = "PENDING"
        await session.flush()
        await self._refresh_batch_state(session, obligation.batch_id)

    async def save_venue_receipt(self, venue: str, payload: dict[str, Any]) -> str:
        payload = safe_json(payload)
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        async with self.database.session() as session:
            existing = await session.scalar(
                select(VenueEventRecord).where(
                    VenueEventRecord.venue == venue, VenueEventRecord.event_key == key
                )
            )
            if existing is None:
                session.add(VenueEventRecord(venue=venue, event_key=key, payload=payload))
        return key

    async def finish_venue_receipt(self, venue: str, key: str) -> None:
        async with self.database.session() as session:
            item = await session.scalar(
                select(VenueEventRecord).where(
                    VenueEventRecord.venue == venue, VenueEventRecord.event_key == key
                )
            )
            if item is not None:
                item.processed = True

    async def pending_venue_receipts(self, venue: str) -> tuple[dict[str, Any], ...]:
        async with self.database.session() as session:
            items = (
                await session.scalars(
                    select(VenueEventRecord)
                    .where(VenueEventRecord.venue == venue, VenueEventRecord.processed.is_(False))
                    .order_by(VenueEventRecord.id)
                )
            ).all()
            return tuple(item.payload for item in items)

    async def hedge_safety_differences(self) -> dict[str, Any]:
        obligations = await self.all_hedge_obligations()
        orders = await self.ledger_orders("POLYMARKET")
        differences: dict[str, Any] = {}
        unresolved = [item.venue_order_id for item in orders if not order_resolved(item)]
        excess = {
            item.obligation_id: str(item.excess_shares)
            for item in obligations
            if item.excess_shares
        }
        pending = {
            item.obligation_id: str(item.pending_shares)
            for item in obligations
            if item.pending_shares
        }
        if unresolved:
            differences["unresolved_polymarket_orders"] = unresolved
        if excess:
            differences["excess_hedges"] = excess
        if pending:
            differences["pending_settlement"] = pending
        for venue in ("IBKR", "POLYMARKET"):
            if await self.pending_venue_receipts(venue):
                differences[f"unprocessed_{venue.lower()}_events"] = True
        return differences
