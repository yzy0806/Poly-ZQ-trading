from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

import structlog

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import (
    PolymarketAdapter,
    PreparedPolymarketOrder,
)
from zq_arb.analytics.payoff import (
    CostInputs,
    build_three_state_opportunity,
    conservative_ibkr_round_trip_commission,
    hedge_shares_per_contract,
    round_shares_up,
    walk_asks,
)
from zq_arb.analytics.portfolio import value_strategy_portfolio
from zq_arb.config import Settings
from zq_arb.domain.enums import AlertSeverity, BatchState, MarginPreviewStatus
from zq_arb.domain.models import (
    EngineSnapshot,
    HedgeObligationView,
    Opportunity,
    PortfolioPositionView,
    PortfolioView,
    utc_now,
)
from zq_arb.persistence.repository import Repository
from zq_arb.services.state import StateStore

LOGGER = structlog.get_logger(__name__)


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _timestamp(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return fallback.astimezone(UTC)


class ExecutionCoordinator:
    """Durable one-batch coordinator from IBKR execId to confirmed hedge trades."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        state: StateStore,
        ibkr: IbkrAdapter,
        polymarket: PolymarketAdapter,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.state = state
        self.ibkr = ibkr
        self.polymarket = polymarket
        self._lock = asyncio.Lock()
        self._order_sent_at: dict[str, datetime] = {}
        self._ibkr_open_order_ids: set[int] = set()
        self._ibkr_open_orders_complete = False
        self._ibkr_executions_complete = False
        self._ibkr_positions: dict[str, Decimal] = {}
        self._ibkr_positions_complete = False
        self._polymarket_open_order_ids: set[str] = set()
        self._polymarket_positions: dict[str, tuple[Decimal, Decimal | None]] = {}
        self._polymarket_positions_complete = False
        self._polymarket_snapshot_complete = False
        self._last_reconciliation_signature: tuple[Any, ...] | None = None

    async def recover(self) -> None:
        """Restore dashboard state and replay only already-signed outbox intents."""

        async with self._lock:
            await self.repository.normalize_active_batch_state()
            await self._publish()
            if not self._polymarket_routing_enabled():
                return
            for intent in await self.repository.pending_polymarket_intents():
                prepared = PreparedPolymarketOrder(
                    token_id=str(intent["token_id"]),
                    limit_price=Decimal(intent["limit_price"]),
                    shares=Decimal(intent["shares"]),
                    idempotency_key=str(intent["idempotency_key"]),
                    signed_payload=intent.get("signed_payload"),
                )
                try:
                    await self._post_prepared(prepared)
                except Exception as exc:
                    LOGGER.exception(
                        "polymarket_outbox_replay_failed",
                        idempotency_key=prepared.idempotency_key,
                        error=str(exc),
                    )
                    await self.state.add_alert(
                        AlertSeverity.CRITICAL,
                        "POLYMARKET_OUTBOX_RECOVERY_FAILED",
                        "A persisted Polymarket hedge intent could not be reconciled or replayed",
                        flashing=True,
                    )
            if not self.settings.simulate_polymarket_fills:
                await self._reconcile_polymarket_account_unlocked()

    async def begin_ibkr_reconciliation(self) -> None:
        async with self._lock:
            self._ibkr_open_order_ids.clear()
            self._ibkr_open_orders_complete = False
            self._ibkr_executions_complete = False
            self._ibkr_positions.clear()
            self._ibkr_positions_complete = False
            self._last_reconciliation_signature = None

    async def handle_ibkr_event(self, event: VenueEvent) -> None:
        async with self._lock:
            if event.kind == "execution":
                await self._handle_ibkr_execution(event)
            elif event.kind == "order_status":
                await self._handle_ibkr_order_status(event)
            elif event.kind == "open_order":
                await self._handle_ibkr_open_order(event)
            elif event.kind == "open_order_end":
                self._ibkr_open_orders_complete = True
            elif event.kind == "execution_end":
                self._ibkr_executions_complete = True
            elif event.kind == "position":
                await self._handle_ibkr_position(event)
                if self._ibkr_positions_complete:
                    await self.state.set_zq_position(self._observed_zq_position())
            elif event.kind == "position_end":
                self._ibkr_positions_complete = True
                await self.state.set_zq_position(self._observed_zq_position())
            await self._attempt_automated_reconciliation()
            await self._publish()

    async def handle_polymarket_event(self, event: VenueEvent) -> None:
        if not event.kind.startswith("user_"):
            return
        async with self._lock:
            if event.kind == "user_trade":
                await self._handle_polymarket_trade(event.payload, event.received_at)
            elif event.kind == "user_order":
                await self.repository.update_polymarket_order_status(
                    str(event.payload.get("id") or ""),
                    str(
                        event.payload.get("status")
                        or event.payload.get("order_event_type")
                        or "UNKNOWN"
                    ),
                )
            await self._publish()

    async def cycle(self, snapshot: EngineSnapshot) -> None:
        """Advance hedges, test residual economics, and submit a fresh qualified batch."""

        async with self._lock:
            batch = await self.repository.active_batch_view()
            if batch.batch_id is not None:
                await self._route_pending_hedges(snapshot, batch.obligations)
                batch = await self.repository.active_batch_view()
                await self._cancel_unprofitable_residual(snapshot, batch)
            elif self._new_entry_authorized(snapshot):
                opportunity = next(
                    (item for item in snapshot.opportunities if item.tradeable),
                    None,
                )
                if opportunity is not None:
                    await self._submit_new_batch(snapshot, opportunity)
            await self._publish()

    async def reconcile_polymarket_account(self) -> None:
        async with self._lock:
            await self._reconcile_polymarket_account_unlocked()

    async def _reconcile_polymarket_account_unlocked(self) -> None:
        account = await self.polymarket.account_snapshot()
        for trade in account.trades:
            await self._handle_polymarket_trade(trade, account.captured_at)
        self._polymarket_positions = {}
        for item in account.positions:
            token_id = str(item.get("asset") or "")
            quantity = _decimal(item.get("size"))
            if not token_id or quantity is None:
                continue
            self._polymarket_positions[token_id] = (quantity, _decimal(item.get("avgPrice")))
        self._polymarket_positions_complete = True
        self._polymarket_open_order_ids = {
            str(item.get("id") or "") for item in account.open_orders if item.get("id")
        }
        batch = await self.repository.active_batch_view()
        for obligation in batch.obligations:
            if (
                obligation.deficit_shares > 0
                and obligation.latest_order_id is not None
                and obligation.latest_order_id not in self._polymarket_open_order_ids
                and obligation.state == "ORDER_LIVE"
            ):
                await self.repository.mark_polymarket_order_cancelled(obligation.latest_order_id)
        self._polymarket_snapshot_complete = True
        await self._attempt_automated_reconciliation()

    async def cancel_unfilled(self, reason: str) -> None:
        async with self._lock:
            batch = await self.repository.active_batch_view()
            if batch.batch_id is None or batch.zq_order_id is None or batch.remaining_quantity <= 0:
                raise ValueError("no active unfilled ZQ order")
            required = (
                self.settings.min_net_profit_usd
                * batch.remaining_quantity
                / Decimal(batch.original_quantity)
            )
            await self._request_zq_cancel(
                batch=batch,
                reason=f"operator requested cancellation: {reason}",
                residual=None,
                required=required,
            )
            await self._publish()

    async def _submit_new_batch(self, snapshot: EngineSnapshot, opportunity: Opportunity) -> None:
        if opportunity.zq_price is None:
            return
        current = await self.state.get()
        target = current.quotes.get(self.settings.ibkr_zq_contract_month)
        if (
            current.snapshot_id != snapshot.snapshot_id
            or target is None
            or target.bid is None
            or target.bid != opportunity.zq_price
        ):
            return
        if (
            opportunity.calculation is None
            or opportunity.calculation.costs.polymarket_fees is None
        ):
            return
        required_cash = (
            opportunity.calculation.emergency_hedge_cash
            + opportunity.calculation.costs.polymarket_fees
        )
        await self.polymarket.trading_preflight(required_cash)
        rechecked = await self.state.get()
        rechecked_target = rechecked.quotes.get(self.settings.ibkr_zq_contract_month)
        if (
            rechecked.snapshot_id != current.snapshot_id
            or rechecked_target is None
            or rechecked_target.bid != opportunity.zq_price
        ):
            return
        batch_id = f"ZQ-{uuid4()}"
        order_id = self.ibkr.reserve_order_id()
        await self.repository.create_zq_batch_intent(
            batch_id=batch_id,
            order_id=order_id,
            contract_month=self.settings.ibkr_zq_contract_month,
            quantity=self.settings.ibkr_zq_child_order_quantity,
            limit_price=opportunity.zq_price,
            strategy_version=self.settings.strategy_version,
            snapshot_id=snapshot.snapshot_id,
        )
        await self._publish()
        self.ibkr.submit_zq_limit_day(
            month=self.settings.ibkr_zq_contract_month,
            limit_price=opportunity.zq_price,
            quantity=self.settings.ibkr_zq_child_order_quantity,
            order_ref=batch_id,
            order_id=order_id,
        )
        await self.repository.mark_zq_submitted(batch_id)
        await self.repository.audit(
            actor="SYSTEM",
            action="ZQ_ENTRY_SUBMITTED",
            reason="armed qualified opportunity",
            correlation_id=batch_id,
            details={
                "order_id": order_id,
                "side": "BUY",
                "quantity": self.settings.ibkr_zq_child_order_quantity,
                "limit_price": opportunity.zq_price,
                "price_source": "CURRENT_BEST_BID",
                "snapshot_id": snapshot.snapshot_id,
            },
        )

    async def _handle_ibkr_execution(self, event: VenueEvent) -> None:
        payload = event.payload
        side = str(payload.get("side") or "").upper()
        if side not in {"BOT", "BUY"}:
            return
        execution_id = str(payload.get("exec_id") or "")
        quantity = _decimal(payload.get("shares"))
        price = _decimal(payload.get("price"))
        order_id_value = payload.get("order_id")
        if not execution_id or quantity is None or price is None or order_id_value is None:
            raise ValueError("IBKR execution is missing its authoritative identity or quantity")
        token_shares = {
            self._token_id("INC25"): round_shares_up(hedge_shares_per_contract(25) * quantity),
            self._token_id("INC50PLUS"): round_shares_up(hedge_shares_per_contract(50) * quantity),
        }
        obligations = await self.repository.record_ibkr_execution_and_obligations(
            order_id=int(order_id_value),
            execution_id=execution_id,
            quantity=quantity,
            price=price,
            executed_at=_timestamp(payload.get("time"), event.received_at),
            token_shares=token_shares,
            details=payload,
        )
        if not obligations:
            return
        await self.repository.audit(
            actor="SYSTEM",
            action="IBKR_EXECUTION_OBLIGATIONS_CREATED",
            reason="unique IBKR execDetails event",
            correlation_id=obligations[0].batch_id,
            details={
                "exec_id": execution_id,
                "zq_fill_quantity": quantity,
                "obligations": [item.model_dump(mode="json") for item in obligations],
            },
        )
        snapshot = await self.state.get()
        results = await asyncio.gather(
            *(self._route_one_hedge(snapshot, obligation) for obligation in obligations),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, BaseException)]
        if failures:
            await self.state.add_alert(
                AlertSeverity.CRITICAL,
                "HEDGE_ROUTING_FAILED",
                f"IBKR fill {execution_id} created a hedge deficit; "
                f"{len(failures)} hedge order(s) failed to route",
                flashing=True,
            )
            for failure in failures:
                LOGGER.exception("incremental_hedge_failed", error=str(failure))

    async def _handle_ibkr_order_status(self, event: VenueEvent) -> None:
        payload = event.payload
        order_id = _decimal(payload.get("order_id"))
        if order_id is None:
            return
        await self.repository.update_zq_order_status(
            order_id=int(order_id),
            status=str(payload.get("status") or "UNKNOWN"),
            filled=_decimal(payload.get("filled")),
            remaining=_decimal(payload.get("remaining")),
            permanent_id=str(payload.get("perm_id") or "") or None,
        )

    async def _handle_ibkr_open_order(self, event: VenueEvent) -> None:
        payload = event.payload
        order_id = _decimal(payload.get("order_id"))
        if order_id is None:
            return
        self._ibkr_open_order_ids.add(int(order_id))
        await self.repository.update_zq_order_status(
            order_id=int(order_id),
            status=str(payload.get("status") or "OPEN"),
        )

    async def _handle_ibkr_position(self, event: VenueEvent) -> None:
        payload = event.payload
        if (
            str(payload.get("symbol") or "").upper() != "ZQ"
            or str(payload.get("security_type") or "").upper() != "FUT"
            or not str(payload.get("contract_month") or "").startswith(
                self.settings.ibkr_zq_contract_month
            )
        ):
            return
        quantity = _decimal(payload.get("position"))
        if quantity is None:
            raise ValueError("IBKR position snapshot contains an invalid ZQ quantity")
        contract_id = str(payload.get("contract_id") or "")
        identity = contract_id or (
            f"ZQ:{payload.get('contract_month')}:{payload.get('account_fingerprint')}"
        )
        self._ibkr_positions[identity] = quantity

    def _observed_zq_position(self) -> Decimal:
        return sum(self._ibkr_positions.values(), Decimal("0"))

    async def _attempt_automated_reconciliation(self) -> None:
        if not (
            self._ibkr_open_orders_complete
            and self._ibkr_executions_complete
            and self._ibkr_positions_complete
            and self._polymarket_snapshot_complete
        ):
            return
        batch = await self.repository.active_batch_view()
        unresolved_hedges = await self.repository.unresolved_hedge_obligation_count()
        differences: dict[str, Any] = {}
        expected_zq_position = await self.repository.strategy_zq_quantity(
            self.settings.ibkr_zq_contract_month
        )
        observed_zq_position = self._observed_zq_position()
        if observed_zq_position != expected_zq_position:
            differences["zq_position_mismatch"] = {
                "expected": str(expected_zq_position),
                "observed": str(observed_zq_position),
            }
        expected_ibkr_order: int | None = None
        if batch.batch_id is not None and batch.zq_order_id is not None:
            terminal = str(batch.zq_order_status or "").upper() in {
                "FILLED",
                "CANCELLED",
                "CANCELED",
                "APICANCELLED",
                "INACTIVE",
            }
            if not terminal and batch.zq_order_id not in self._ibkr_open_order_ids:
                recovered_status = (
                    "Filled"
                    if batch.filled_quantity >= Decimal(batch.original_quantity)
                    else "Cancelled"
                )
                await self.repository.update_zq_order_status(
                    order_id=batch.zq_order_id,
                    status=recovered_status,
                    filled=batch.filled_quantity,
                    remaining=Decimal("0"),
                )
                batch = await self.repository.active_batch_view()
                terminal = True
            if not terminal:
                expected_ibkr_order = batch.zq_order_id
                if expected_ibkr_order not in self._ibkr_open_order_ids:
                    differences["missing_ibkr_order"] = expected_ibkr_order
        expected_polymarket_orders = {
            item.latest_order_id
            for item in batch.obligations
            if item.deficit_shares > 0 and item.latest_order_id is not None
        }
        missing_poly = sorted(expected_polymarket_orders - self._polymarket_open_order_ids)
        if missing_poly:
            differences["missing_polymarket_orders"] = missing_poly
        obligations_without_orders = [
            item.obligation_id
            for item in batch.obligations
            if item.deficit_shares > 0 and item.latest_order_id is None
        ]
        if obligations_without_orders:
            differences["unrouted_obligations"] = obligations_without_orders
        if unresolved_hedges > 0:
            differences["unresolved_hedge_obligations"] = unresolved_hedges
        clean = not differences
        signature = (
            batch.batch_id,
            expected_ibkr_order,
            str(expected_zq_position),
            str(observed_zq_position),
            tuple(sorted(expected_polymarket_orders)),
            unresolved_hedges,
            repr(differences),
            clean,
        )
        if signature == self._last_reconciliation_signature:
            return
        self._last_reconciliation_signature = signature
        expected = {
            "batch_id": batch.batch_id,
            "zq_contract_month": self.settings.ibkr_zq_contract_month,
            "zq_position": str(expected_zq_position),
            "ibkr_open_order_id": expected_ibkr_order,
            "polymarket_open_order_ids": sorted(expected_polymarket_orders),
            "unresolved_obligations": [
                item.obligation_id for item in batch.obligations if item.deficit_shares > 0
            ],
            "unresolved_hedge_obligation_count": unresolved_hedges,
        }
        observed = {
            "ibkr_open_order_ids": sorted(self._ibkr_open_order_ids),
            "zq_position": str(observed_zq_position),
            "polymarket_open_order_ids": sorted(self._polymarket_open_order_ids),
            "ibkr_open_orders_complete": self._ibkr_open_orders_complete,
            "ibkr_executions_complete": self._ibkr_executions_complete,
            "ibkr_positions_complete": self._ibkr_positions_complete,
        }
        reconciliation_id = await self.repository.record_automated_reconciliation(
            clean=clean,
            expected=expected,
            observed=observed,
            differences=differences,
        )
        snapshot = await self.state.get()
        await self.state.set_automated_reconciliation(
            clean=clean,
            reason=(
                f"automated order/execution/position ledger reconciliation "
                f"{reconciliation_id} is clean"
                if clean
                else f"automated reconciliation {reconciliation_id} differences: {differences}"
            ),
            snapshot_id=snapshot.snapshot_id,
        )

    async def _route_pending_hedges(
        self,
        snapshot: EngineSnapshot,
        obligations: tuple[HedgeObligationView, ...],
    ) -> None:
        pending = tuple(item for item in obligations if item.deficit_shares > 0)
        if not pending or not self._polymarket_routing_enabled():
            return
        await asyncio.gather(*(self._route_one_hedge(snapshot, item) for item in pending))

    async def _route_one_hedge(
        self, snapshot: EngineSnapshot, obligation: HedgeObligationView
    ) -> None:
        if obligation.deficit_shares <= 0:
            return
        if obligation.latest_order_id and obligation.state in {
            "ORDER_LIVE",
            "MATCHED",
            "PARTIAL",
        }:
            if obligation.state == "MATCHED":
                return
            sent_at = self._order_sent_at.setdefault(obligation.latest_order_id, utc_now())
            age = (utc_now() - sent_at).total_seconds()
            if (
                obligation.state == "ORDER_LIVE"
                and age < self.settings.polymarket_hedge_reprice_seconds
            ):
                return
            if obligation.reprice_count >= self.settings.polymarket_hedge_max_reprices:
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "HEDGE_REPRICE_EXHAUSTED",
                    f"Hedge {obligation.obligation_id} remains unfilled after "
                    f"{obligation.reprice_count} reprices",
                    flashing=True,
                )
            cancelled = await self.polymarket.cancel_order(obligation.latest_order_id)
            if not cancelled:
                return
            await self.repository.mark_polymarket_order_cancelled(obligation.latest_order_id)
        snapshot = await self.state.get()
        book = snapshot.books.get(obligation.token_id)
        limit_price = book.best_ask if book is not None else None
        if limit_price is None:
            raise RuntimeError("current lowest ask is unavailable for an incremental hedge")
        if limit_price > self.settings.polymarket_emergency_max_price:
            raise RuntimeError("current lowest ask exceeds the approved emergency hedge cap")
        attempt = obligation.reprice_count + 1
        key = f"{obligation.obligation_id}:ATTEMPT:{attempt}"
        prepared = await self.polymarket.prepare_hedge_limit(
            token_id=obligation.token_id,
            limit_price=limit_price,
            shares=obligation.deficit_shares,
            idempotency_key=key,
        )
        rechecked_book = (await self.state.get()).books.get(obligation.token_id)
        if rechecked_book is None or rechecked_book.best_ask != limit_price:
            return
        created = await self.repository.create_hedge_order_intent(
            obligation_id=obligation.obligation_id,
            idempotency_key=key,
            shares=obligation.deficit_shares,
            limit_price=limit_price,
            attempt=attempt,
            signed_payload=prepared.signed_payload,
        )
        if created:
            await self._post_prepared(prepared)
            return
        intents = await self.repository.pending_polymarket_intents()
        existing = next(
            (item for item in intents if item["idempotency_key"] == key),
            None,
        )
        if existing is not None:
            await self._post_prepared(
                PreparedPolymarketOrder(
                    token_id=str(existing["token_id"]),
                    limit_price=Decimal(existing["limit_price"]),
                    shares=Decimal(existing["shares"]),
                    idempotency_key=str(existing["idempotency_key"]),
                    signed_payload=existing.get("signed_payload"),
                )
            )

    async def _post_prepared(self, prepared: PreparedPolymarketOrder) -> None:
        result = await self.polymarket.post_prepared_hedge(prepared)
        await self.repository.accept_hedge_order(
            idempotency_key=prepared.idempotency_key,
            order_id=result.order_id,
            state=result.status,
            simulated=result.simulated,
        )
        self._order_sent_at[result.order_id] = utc_now()
        await self.repository.audit(
            actor="SYSTEM",
            action="POLYMARKET_HEDGE_SUBMITTED",
            reason="incremental IBKR fill obligation",
            details={
                "order_id": result.order_id,
                "token_id": prepared.token_id,
                "side": "BUY",
                "order_type": "GTC",
                "post_only": False,
                "limit_price": prepared.limit_price,
                "price_source": "CURRENT_LOWEST_ASK",
                "shares": prepared.shares,
                "simulated": result.simulated,
            },
        )
        if result.immediately_matched_shares > 0:
            await self.repository.record_polymarket_execution(
                execution_id=f"{result.order_id}:SIMULATED_FILL",
                order_id=result.order_id,
                quantity=result.immediately_matched_shares,
                price=result.limit_price,
                executed_at=utc_now(),
                details={"simulated": True},
            )

    async def _handle_polymarket_trade(
        self, payload: dict[str, Any], received_at: datetime
    ) -> None:
        status = str(payload.get("status") or "").upper()
        if status == "FAILED":
            trade_id = str(payload.get("id") or "")
            reversed_credit = await self.repository.fail_polymarket_execution(trade_id)
            maker_orders = payload.get("maker_orders")
            if not reversed_credit and isinstance(maker_orders, list | tuple):
                for maker in maker_orders:
                    if isinstance(maker, dict):
                        order_id = str(maker.get("order_id") or "")
                        if order_id and await self.repository.fail_polymarket_execution(
                            f"{trade_id}:{order_id}"
                        ):
                            reversed_credit = True
                            break
            await self.state.add_alert(
                AlertSeverity.CRITICAL,
                "POLYMARKET_TRADE_FAILED",
                f"Polymarket trade {payload.get('id')} failed settlement; its hedge "
                f"credit was {'reopened' if reversed_credit else 'not found in this strategy'}",
                flashing=True,
            )
            return
        execution_id = str(payload.get("id") or "")
        quantity = _decimal(payload.get("size"))
        price = _decimal(payload.get("price"))
        if not execution_id or quantity is None or price is None:
            return
        executed_at = _timestamp(
            payload.get("matched_at") or payload.get("match_time") or payload.get("timestamp"),
            received_at,
        )
        taker_order_id = str(payload.get("taker_order_id") or "")
        credited = False
        if taker_order_id:
            credited = await self.repository.record_polymarket_execution(
                execution_id=execution_id,
                order_id=taker_order_id,
                quantity=quantity,
                price=price,
                executed_at=executed_at,
                details=payload,
            )
        if credited:
            return
        maker_orders = payload.get("maker_orders")
        if not isinstance(maker_orders, list | tuple):
            return
        for maker in maker_orders:
            if not isinstance(maker, dict):
                continue
            maker_order_id = str(maker.get("order_id") or "")
            matched = _decimal(maker.get("matched_amount"))
            maker_price = _decimal(maker.get("price")) or price
            if not maker_order_id or matched is None:
                continue
            if await self.repository.record_polymarket_execution(
                execution_id=f"{execution_id}:{maker_order_id}",
                order_id=maker_order_id,
                quantity=matched,
                price=maker_price,
                executed_at=executed_at,
                details=payload,
            ):
                return

    async def _cancel_unprofitable_residual(self, snapshot: EngineSnapshot, batch: Any) -> None:
        if (
            batch.batch_id is None
            or batch.zq_order_id is None
            or batch.limit_price is None
            or batch.remaining_quantity <= 0
            or batch.cancel_reason is not None
            or batch.state in {BatchState.CANCEL_PENDING, BatchState.COMPLETE, BatchState.IDLE}
        ):
            return
        contracts = int(batch.remaining_quantity)
        if Decimal(contracts) != batch.remaining_quantity or contracts <= 0:
            return
        book25 = snapshot.books.get(self._token_id("INC25"))
        book50 = snapshot.books.get(self._token_id("INC50PLUS"))
        effr = snapshot.effr.rate_percent
        if (
            book25 is None
            or book50 is None
            or book25.best_ask is None
            or book50.best_ask is None
            or effr is None
            or not snapshot.effr.valid
        ):
            await self._request_zq_cancel(
                batch=batch,
                reason="residual hedge market or validated EFFR became unavailable",
                residual=None,
                required=self.settings.min_net_profit_usd
                * batch.remaining_quantity
                / Decimal(batch.original_quantity),
            )
            return
        fee_parameters = snapshot.metadata.get("polymarket_fee_parameters")
        fee_parameters_at = snapshot.metadata.get("polymarket_fee_parameters_at")
        try:
            fee_timestamp = datetime.fromisoformat(
                str(fee_parameters_at).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (TypeError, ValueError):
            fee_timestamp = None
        fee_current = bool(
            fee_timestamp is not None
            and (utc_now() - fee_timestamp).total_seconds()
            <= max(60, self.settings.polymarket_book_snapshot_interval_seconds * 2)
        )
        if (
            not isinstance(fee_parameters, dict)
            or any(code not in fee_parameters for code in ("INC25", "INC50PLUS"))
            or not fee_current
        ):
            await self._request_zq_cancel(
                batch=batch,
                reason="current Polymarket taker-fee parameters became unavailable",
                residual=None,
                required=self.settings.min_net_profit_usd
                * batch.remaining_quantity
                / Decimal(batch.original_quantity),
            )
            return
        scale = batch.remaining_quantity / Decimal(batch.original_quantity)
        margin = snapshot.margin_preview.next_batch_initial_margin
        q25 = round_shares_up(hedge_shares_per_contract(25) * batch.remaining_quantity)
        q50 = round_shares_up(hedge_shares_per_contract(50) * batch.remaining_quantity)
        emergency25 = walk_asks(
            book25.asks, q25, price_cap=self.settings.polymarket_emergency_max_price
        )
        emergency50 = walk_asks(
            book50.asks, q50, price_cap=self.settings.polymarket_emergency_max_price
        )
        fee_prices25 = [value for value in (book25.best_ask, emergency25.vwap) if value]
        fee_prices50 = [value for value in (book50.best_ask, emergency50.vwap) if value]
        polymarket_fees = max(
            self._taker_fee(fee_parameters["INC25"], q25, value) for value in fee_prices25
        ) + max(self._taker_fee(fee_parameters["INC50PLUS"], q50, value) for value in fee_prices50)
        residual = build_three_state_opportunity(
            contracts=contracts,
            zq_price=batch.limit_price,
            pre_meeting_effr=effr,
            inc25_book=book25,
            inc50_book=book50,
            cost_inputs=CostInputs(
                ibkr_commission=conservative_ibkr_round_trip_commission(
                    contracts=batch.remaining_quantity,
                    configured_per_contract=self.settings.ibkr_commission_estimate,
                    entry_preview_commission=(
                        snapshot.margin_preview.commission * scale
                        if snapshot.margin_preview.commission is not None
                        else None
                    ),
                ),
                polymarket_fees=polymarket_fees,
            ),
            incremental_margin=margin * scale if margin is not None else None,
            emergency_cash_reserve=Decimal("0"),
            post_price_cap=self.settings.polymarket_hard_price_cap,
            emergency_price_cap=self.settings.polymarket_emergency_max_price,
        )
        required = self.settings.min_net_profit_usd * scale
        depth_failed = any(not check.passed for check in residual.gate_checks)
        profit_failed = (
            residual.minimum_net_profit is None or residual.minimum_net_profit < required
        )
        margin_refresh_pending = (
            snapshot.margin_preview.status is MarginPreviewStatus.PENDING
            and residual.return_on_capital_bps is None
        )
        return_failed = not margin_refresh_pending and (
            residual.return_on_capital_bps is None
            or residual.return_on_capital_bps < self.settings.min_return_on_capital_bps
        )
        if depth_failed or profit_failed or return_failed:
            await self._request_zq_cancel(
                batch=batch,
                reason=(
                    "unfilled residual no longer passes exact-ask hedge depth, scaled "
                    "minimum profit, and return gates"
                ),
                residual=residual,
                required=required,
            )

    async def _request_zq_cancel(
        self,
        *,
        batch: Any,
        reason: str,
        residual: Opportunity | None,
        required: Decimal,
    ) -> None:
        # Do not persist CANCEL_PENDING until the IBKR client has accepted the
        # cancellation request. A local API error must leave the batch retryable.
        self.ibkr.cancel_order(batch.zq_order_id)
        marked = await self.repository.set_batch_cancel_pending(
            batch_id=batch.batch_id,
            reason=reason,
            residual_minimum_net_profit=(
                residual.minimum_net_profit if residual is not None else None
            ),
            required_profit=required,
            residual_return_bps=(residual.return_on_capital_bps if residual is not None else None),
        )
        if not marked:
            return
        await self.repository.audit(
            actor="SYSTEM",
            action="ZQ_RESIDUAL_CANCEL_REQUESTED",
            reason=reason,
            correlation_id=batch.batch_id,
            details={
                "order_id": batch.zq_order_id,
                "remaining_quantity": batch.remaining_quantity,
                "residual_minimum_net_profit": (
                    residual.minimum_net_profit if residual is not None else None
                ),
                "required_profit": required,
                "residual_return_on_capital_bps": (
                    residual.return_on_capital_bps if residual is not None else None
                ),
                "late_fills_remain_hedgeable": True,
            },
        )

    async def _publish(self) -> None:
        batch = await self.repository.active_batch_view()
        positions = list(await self.repository.strategy_portfolio_positions(self.settings))
        strategy_keys = {(item.venue, item.instrument) for item in positions}
        observed_zq = self._observed_zq_position() if self._ibkr_positions_complete else None
        enriched: list[PortfolioPositionView] = []
        for position in positions:
            venue_quantity: Decimal | None = None
            reconciled: bool | None = None
            if (
                position.venue == "IBKR"
                and position.instrument == self.settings.ibkr_zq_contract_month
            ):
                venue_quantity = observed_zq
            elif position.venue == "POLYMARKET":
                if self.settings.simulate_polymarket_fills:
                    venue_quantity = position.strategy_quantity
                elif self._polymarket_positions_complete:
                    venue_quantity = self._polymarket_positions.get(
                        position.instrument, (Decimal("0"), None)
                    )[0]
            if venue_quantity is not None:
                reconciled = venue_quantity == position.strategy_quantity
            enriched.append(
                position.model_copy(
                    update={"venue_quantity": venue_quantity, "reconciled": reconciled}
                )
            )

        if observed_zq not in {None, Decimal("0")} and (
            "IBKR",
            self.settings.ibkr_zq_contract_month,
        ) not in strategy_keys:
            enriched.append(
                PortfolioPositionView(
                    venue="IBKR",
                    instrument=self.settings.ibkr_zq_contract_month,
                    label=f"ZQ {self.settings.ibkr_zq_contract_month}",
                    venue_quantity=observed_zq,
                    multiplier=Decimal("4167"),
                    reconciled=False,
                )
            )
        if not self.settings.simulate_polymarket_fills and self._polymarket_positions_complete:
            labels = {
                leg.yes_token_id: f"{leg.code} YES" for leg in self.settings.market_legs
            } | {leg.no_token_id: f"{leg.code} NO" for leg in self.settings.market_legs}
            for token_id, (quantity, average_price) in self._polymarket_positions.items():
                if quantity == 0 or ("POLYMARKET", token_id) in strategy_keys:
                    continue
                enriched.append(
                    PortfolioPositionView(
                        venue="POLYMARKET",
                        instrument=token_id,
                        label=labels.get(token_id, f"Token {token_id[:12]}"),
                        venue_quantity=quantity,
                        average_entry_price=average_price,
                        reconciled=False,
                    )
                )
        snapshot = await self.state.get()
        portfolio = value_strategy_portfolio(
            snapshot,
            PortfolioView(
                positions=tuple(enriched),
                valuation_complete=not enriched,
                valuation_reason=(
                    "no open strategy positions" if not enriched else "awaiting executable marks"
                ),
            ),
        )
        unresolved_hedges = await self.repository.unresolved_hedge_obligation_count()
        await self.state.set_execution_state(batch, portfolio, unresolved_hedges)

    def _new_entry_authorized(self, snapshot: EngineSnapshot) -> bool:
        return (
            snapshot.armed
            and not snapshot.paused
            and not snapshot.kill_switch
            and int(snapshot.metadata.get("unresolved_hedge_obligations") or 0) == 0
            and not any(
                obligation.deficit_shares > 0
                for obligation in snapshot.active_batch.obligations
            )
            and self.settings.ibkr_order_submission_enabled
            and self._polymarket_routing_enabled()
            and (
                self.settings.run_mode.value == "PAPER"
                or (self.settings.run_mode.is_live and self.settings.live_trading_enabled)
            )
        )

    def _polymarket_routing_enabled(self) -> bool:
        return self.settings.polymarket_order_submission_enabled and (
            self.settings.run_mode.value == "PAPER"
            or (self.settings.run_mode.is_live and self.settings.live_trading_enabled)
        )

    def _token_id(self, code: str) -> str:
        return next(leg.yes_token_id for leg in self.settings.market_legs if leg.code == code)

    @staticmethod
    def _taker_fee(parameters: Any, shares: Decimal, price: Decimal | None) -> Decimal:
        if price is None or not isinstance(parameters, dict):
            raise ValueError("current taker-fee inputs are unavailable")
        rate = Decimal(str(parameters["rate"]))
        exponent = Decimal(str(parameters["exponent"]))
        return shares * rate * ((price * (Decimal("1") - price)) ** exponent)
