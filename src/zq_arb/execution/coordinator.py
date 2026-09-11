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
    plan_hedge_entry,
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
from zq_arb.persistence.hedge_ledger import normalize_status, order_resolved
from zq_arb.persistence.repository import Repository
from zq_arb.services.state import StateStore

LOGGER = structlog.get_logger(__name__)
IBKR_TERMINAL = frozenset(
    {"FILLED", "CANCELLED", "CANCELED", "APICANCELLED", "INACTIVE", "ABORTED"}
)


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
        self._account_reconciliation_lock = asyncio.Lock()
        self._obligation_locks: dict[str, asyncio.Lock] = {}
        self._hedge_tasks: dict[str, asyncio.Task[None]] = {}
        self._halt_requested = False
        self._recovery_ready = False
        self._cancel_requested_at: dict[int, float] = {}
        self._venue_revision = 0
        self._last_ledger_revision: int | None = None
        self._polymarket_snapshot_at: datetime | None = None
        self._last_reconciliation_check = float("-inf")
        self._order_sent_at: dict[str, datetime] = {}
        self._ibkr_open_order_ids: set[int] = set()
        self._ibkr_open_orders_complete = False
        self._ibkr_completed_orders_complete = False
        self._ibkr_snapshot_at: datetime | None = None
        self._ibkr_reconciliation_in_progress = False
        self._ibkr_foreign_orders: set[str] = set()
        self._ibkr_executions_complete = False
        self._ibkr_positions: dict[str, Decimal] = {}
        self._ibkr_positions_complete = False
        self._polymarket_open_order_ids: set[str] = set()
        self._polymarket_positions: dict[str, tuple[Decimal, Decimal | None]] = {}
        self._polymarket_positions_complete = False
        self._polymarket_snapshot_complete = False
        self._last_reconciliation_signature: tuple[Any, ...] | None = None

    async def recover(self) -> None:
        """Validate provenance before any replay; startup stays blocked pending venue reads."""
        if self.settings.run_mode.value == "PAPER" or self.settings.run_mode.is_live:
            await self.repository.database.validate_execution_environment()
        self._recovery_ready = False
        await self.state.invalidate_reconciliation("startup requires fresh venue reconciliation")
        await self.repository.normalize_active_batch_state()
        await self._publish()

    async def begin_ibkr_reconciliation(self) -> None:
        async with self._lock:
            await self.state.invalidate_reconciliation("IBKR reconciliation in progress")
            self._ibkr_open_order_ids.clear()
            self._ibkr_foreign_orders.clear()
            self._ibkr_open_orders_complete = False
            self._ibkr_completed_orders_complete = False
            self._ibkr_reconciliation_in_progress = True
            self._ibkr_snapshot_at = None
            self._ibkr_executions_complete = False
            self._ibkr_positions.clear()
            self._ibkr_positions_complete = False
            self._last_reconciliation_signature = None

    async def handle_ibkr_event(self, event: VenueEvent) -> None:
        async with self._lock:
            # Retain the serialization barrier with order preparation, but do
            # not query/rebuild the execution ledger for market-data ticks.
            if event.kind in {"tick_price", "tick_size"}:
                return
            if event.kind == "connection" and event.payload.get("status") != "CONNECTED":
                self._recovery_ready = False
                self._ibkr_open_orders_complete = False
                self._ibkr_executions_complete = False
                self._ibkr_positions_complete = False
                self._ibkr_completed_orders_complete = False
            if event.kind in {"execution", "order_status", "open_order", "position"}:
                await self.state.invalidate_reconciliation("IBKR execution state changed")
            if event.kind == "execution":
                await self._handle_ibkr_execution(event)
            elif event.kind == "order_status":
                await self._handle_ibkr_order_status(event)
            elif event.kind == "open_order":
                await self._handle_ibkr_open_order(event)
            elif event.kind == "completed_order":
                await self._handle_ibkr_completed_order(event)
            elif event.kind == "completed_order_end":
                self._ibkr_completed_orders_complete = True
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
            if (
                self._ibkr_reconciliation_in_progress
                and self._ibkr_open_orders_complete
                and self._ibkr_completed_orders_complete
                and self._ibkr_executions_complete
                and self._ibkr_positions_complete
            ):
                self._ibkr_snapshot_at = utc_now()
                self._ibkr_reconciliation_in_progress = False
            await self._attempt_automated_reconciliation()
            await self._publish()

    async def handle_polymarket_event(self, event: VenueEvent) -> None:
        if not event.kind.startswith("user_"):
            return
        async with self._lock:
            await self.invalidate_polymarket_reconciliation(
                "Polymarket user event changed account state"
            )
            if event.kind in {"user_stream_connected", "user_stream_disconnected"}:
                self._recovery_ready = False
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
            await self._attempt_automated_reconciliation()
            await self._publish()

    async def cycle(self, snapshot: EngineSnapshot) -> None:
        """Advance independent hedges without blocking fills or urgent cancellation."""
        if snapshot.kill_switch or self._halt_requested:
            await self.cancel_working_zq("emergency halt")
        now = asyncio.get_running_loop().time()
        if now - self._last_reconciliation_check >= 1:
            async with self._lock:
                await self._attempt_automated_reconciliation()
            self._last_reconciliation_check = now
        batch = await self.repository.active_batch_view()
        if batch.batch_id is not None:
            await self._cancel_unprofitable_residual(await self.state.get(), batch)
        await self._route_pending_hedges(snapshot, await self.repository.pending_obligations())
        if batch.batch_id is None and self._new_entry_authorized(await self.state.get()):
            opportunity = next((item for item in snapshot.opportunities if item.tradeable), None)
            if opportunity is not None:
                await self._submit_new_batch(snapshot, opportunity)
        await self._publish()

    async def invalidate_polymarket_reconciliation(self, reason: str) -> None:
        self._venue_revision += 1
        self._polymarket_snapshot_complete = False
        self._polymarket_positions_complete = False
        self._last_reconciliation_signature = None
        await self.state.invalidate_reconciliation(reason)

    async def reconcile_polymarket_account(self) -> None:
        async with self._account_reconciliation_lock:
            try:
                await self._reconcile_polymarket_account_unlocked()
            except BaseException:
                await self.invalidate_polymarket_reconciliation(
                    "Polymarket account read incomplete"
                )
                raise

    async def _reconcile_polymarket_account_unlocked(self) -> None:
        revision = self._venue_revision
        async with asyncio.timeout(self.settings.execution_request_timeout_seconds):
            account = await self.polymarket.account_snapshot()
            order_snapshots: dict[str, dict[str, Any]] = {}
            for order in await self.repository.ledger_orders("POLYMARKET"):
                if order_resolved(order) or order.venue_order_id.startswith(("SIM-", "INTENT:")):
                    continue
                try:
                    order_snapshots[order.venue_order_id] = await self.polymarket.get_order(
                        order.venue_order_id
                    )
                except Exception as exc:
                    # Absence/404 never proves rejection, cancellation, or zero fills.
                    LOGGER.warning(
                        "order_lookup_unresolved",
                        order_id=order.venue_order_id,
                        error_type=type(exc).__name__,
                    )
                    continue
        async with self._lock:
            tokens = {
                token
                for leg in self.settings.market_legs
                for token in (leg.yes_token_id, leg.no_token_id)
            }
            for trade in account.trades:
                token = str(trade.get("token_id") or trade.get("asset_id") or "")
                if token and token not in tokens:
                    continue
                await self._handle_polymarket_trade(trade, account.captured_at)
            for receipt in await self.repository.pending_venue_receipts("POLYMARKET"):
                await self._handle_polymarket_trade(receipt, account.captured_at)
            for order_id, order_snapshot in order_snapshots.items():
                await self.repository.verify_polymarket_order(order_id, order_snapshot)
            positions: dict[str, tuple[Decimal, Decimal | None]] = {}
            for item in account.positions:
                token_id = str(item.get("asset") or "")
                quantity = _decimal(item.get("size"))
                if not token_id or quantity is None:
                    raise ValueError("Invalid Polymarket position snapshot")
                positions[token_id] = (quantity, _decimal(item.get("avgPrice")))
            self._polymarket_positions = positions
            self._polymarket_open_order_ids = {
                str(item["id"]) for item in account.open_orders if item.get("id")
            }
            complete = account.trades_complete and revision == self._venue_revision
            self._polymarket_positions_complete = complete
            self._polymarket_snapshot_complete = complete
            self._polymarket_snapshot_at = account.captured_at
            await self._attempt_automated_reconciliation()
            await self._publish()

    async def halt(self, reason: str) -> None:
        self._halt_requested = True
        await self.state.set_operating_state(kill_switch=True, paused=True, armed=False)
        await self.repository.audit(actor="SYSTEM", action="EXECUTION_HALTED", reason=reason)
        await self.cancel_working_zq(reason)

    async def cancel_working_zq(self, reason: str) -> None:
        # Intentionally independent of the hedge and reconciliation locks.
        if not (self.settings.run_mode.value == "PAPER" or self.settings.run_mode.is_live):
            return
        for order in await self.repository.ledger_orders("IBKR"):
            if order.state in IBKR_TERMINAL:
                continue
            batch = await self.repository.active_batch_view(order.batch_id)
            now = asyncio.get_running_loop().time()
            order_id = int(order.venue_order_id)
            if now - self._cancel_requested_at.get(order_id, float("-inf")) < 2:
                continue
            try:
                await self._request_zq_cancel(
                    batch=batch,
                    reason=reason,
                    residual=None,
                    required=self.settings.min_net_profit_usd
                    * batch.remaining_quantity
                    / Decimal(batch.original_quantity),
                )
                self._cancel_requested_at[order_id] = now
            except Exception as exc:
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "ZQ_CANCEL_FAILED",
                    f"ZQ cancellation {order_id} remains unconfirmed: {type(exc).__name__}",
                    flashing=True,
                )

    async def shutdown_ready(self) -> bool:
        orders = await self.repository.ledger_orders()
        if any(item.venue == "IBKR" and item.state not in IBKR_TERMINAL for item in orders):
            return False
        if any(item.venue == "POLYMARKET" and not order_resolved(item) for item in orders):
            return False
        if (await self.repository.active_batch_view()).batch_id is not None:
            return False
        if await self.repository.hedge_safety_differences():
            return False
        if await self.repository.unresolved_hedge_obligation_count():
            return False
        return (await self.state.get()).reconciliation.clean and (
            self._last_ledger_revision == self.repository.database.revision
        )

    async def wait_for_hedges(self) -> None:
        if self._hedge_tasks:
            await asyncio.gather(*tuple(self._hedge_tasks.values()))

    async def close(self) -> None:
        for task in tuple(self._hedge_tasks.values()):
            task.cancel()
        await asyncio.gather(*tuple(self._hedge_tasks.values()), return_exceptions=True)

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
        entry_revision = self.repository.database.revision + 1
        current = await self.state.get()
        target = current.quotes.get(self.settings.ibkr_zq_contract_month)
        if (
            current.snapshot_id != snapshot.snapshot_id
            or target is None
            or target.bid is None
            or target.bid != opportunity.zq_price
        ):
            return
        if opportunity.calculation is None or opportunity.calculation.costs.polymarket_fees is None:
            return
        required_cash = (
            opportunity.calculation.emergency_hedge_cash
            + opportunity.calculation.costs.polymarket_fees
        )
        async with asyncio.timeout(self.settings.execution_request_timeout_seconds):
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
        # No awaited operation may separate this final validation and sending.
        final = await self.state.get()
        if (
            final.snapshot_id != rechecked.snapshot_id
            or not self._new_entry_authorized(final, ledger_revision=entry_revision)
            or self.ibkr.event_queue_overflowed is True
        ):
            await self.repository.abandon_zq_intent(batch_id, "authorization changed before send")
            await self._publish()
            return
        self.ibkr.submit_zq_limit_day(
            month=self.settings.ibkr_zq_contract_month,
            limit_price=opportunity.zq_price,
            quantity=self.settings.ibkr_zq_child_order_quantity,
            order_ref=batch_id,
            order_id=order_id,
        )
        self._ibkr_open_order_ids.add(order_id)
        await self.state.invalidate_reconciliation("ZQ order submitted; venue confirmation pending")
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
        if payload.get("symbol") and (
            payload["symbol"] != "ZQ"
            or not str(payload.get("contract_month", "")).startswith(
                self.settings.ibkr_zq_contract_month
            )
        ):
            return
        receipt = await self.repository.save_venue_receipt("IBKR", payload)
        side = str(payload.get("side") or "").upper()
        if side not in {"BOT", "BUY"}:
            return
        execution_id = str(payload.get("exec_id") or "")
        quantity = _decimal(payload.get("shares"))
        price = _decimal(payload.get("price"))
        order_id_value = payload.get("order_id")
        if not execution_id or quantity is None or price is None or order_id_value is None:
            raise ValueError("IBKR execution is missing its authoritative identity or quantity")
        known = self._ibkr_own_client(payload) and any(
            item.venue_order_id == str(order_id_value)
            for item in await self.repository.ledger_orders("IBKR")
        )
        if not known:
            return
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
        await self.repository.finish_venue_receipt("IBKR", receipt)
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
        await self._route_pending_hedges(await self.state.get(), obligations)

    async def _handle_ibkr_order_status(self, event: VenueEvent) -> None:
        payload = event.payload
        order_id = _decimal(payload.get("order_id"))
        if order_id is None:
            return
        status = str(payload.get("status") or "UNKNOWN")
        if not self._ibkr_own_client(payload):
            identity = f"{payload.get('client_id')}:{int(order_id)}"
            if status.upper() in IBKR_TERMINAL:
                self._ibkr_foreign_orders.discard(identity)
            else:
                self._ibkr_foreign_orders.add(identity)
            return
        if status.upper() in IBKR_TERMINAL:
            self._ibkr_open_order_ids.discard(int(order_id))
        else:
            self._ibkr_open_order_ids.add(int(order_id))
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
        if not self._ibkr_own_client(payload):
            self._ibkr_foreign_orders.add(f"{payload.get('client_id')}:{int(order_id)}")
            return
        self._ibkr_open_order_ids.add(int(order_id))
        await self.repository.update_zq_order_status(
            order_id=int(order_id),
            status=str(payload.get("status") or "OPEN"),
            permanent_id=str(payload.get("perm_id") or "") or None,
        )

    def _ibkr_own_client(self, payload: dict[str, Any]) -> bool:
        client_id = payload.get("client_id")
        return client_id is None or str(client_id) == str(self.settings.ibkr_client_id)

    async def _handle_ibkr_completed_order(self, event: VenueEvent) -> None:
        payload = event.payload
        permanent_id = str(payload.get("perm_id") or "")
        match = next(
            (
                order
                for order in await self.repository.ledger_orders("IBKR")
                if (
                    (permanent_id and order.permanent_id == permanent_id)
                    or (
                        self._ibkr_own_client(payload)
                        and order.venue_order_id == str(payload.get("order_id"))
                        and payload.get("order_ref") == order.batch_id
                    )
                )
            ),
            None,
        )
        if match is None or str(payload.get("status", "")).upper() not in IBKR_TERMINAL:
            return
        await self.repository.update_zq_order_status(
            order_id=int(match.venue_order_id),
            status=str(payload["status"]),
            filled=_decimal(payload.get("filled")),
            remaining=Decimal("0"),
            permanent_id=permanent_id or None,
        )
        self._ibkr_open_order_ids.discard(int(match.venue_order_id))

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
        async with self.repository.database.session():
            await self._reconcile_ledger_snapshot()

    async def _reconcile_ledger_snapshot(self) -> None:
        venue_revision = self._venue_revision
        simulated = self.settings.simulate_polymarket_fills
        fresh = simulated or (
            self._polymarket_snapshot_at is not None
            and (utc_now() - self._polymarket_snapshot_at).total_seconds()
            <= self.settings.reconciliation_max_age_seconds
        )
        if not (
            self._ibkr_open_orders_complete
            and self._ibkr_completed_orders_complete
            and self._ibkr_executions_complete
            and self._ibkr_positions_complete
            and (simulated or self._polymarket_snapshot_complete)
            and fresh
            and self._ibkr_snapshot_at is not None
            and (utc_now() - self._ibkr_snapshot_at).total_seconds()
            <= self.settings.reconciliation_max_age_seconds
        ):
            await self.state.invalidate_reconciliation(
                "venue reconciliation is incomplete or stale"
            )
            return
        differences = await self.repository.hedge_safety_differences()
        if self._ibkr_foreign_orders:
            differences["unexpected_ibkr_clients"] = sorted(self._ibkr_foreign_orders)
        expected_zq = await self.repository.strategy_zq_quantity(
            self.settings.ibkr_zq_contract_month
        )
        observed_zq = self._observed_zq_position()
        if observed_zq != expected_zq:
            differences["zq_position_mismatch"] = {
                "expected": str(expected_zq),
                "observed": str(observed_zq),
            }
        orders = await self.repository.ledger_orders()
        expected_ibkr = {
            int(item.venue_order_id)
            for item in orders
            if item.venue == "IBKR" and item.state not in IBKR_TERMINAL
        }
        expected_poly = {
            item.venue_order_id
            for item in orders
            if item.venue == "POLYMARKET" and not order_resolved(item)
        }
        for venue, expected_ids, observed_ids in (
            ("ibkr", expected_ibkr, self._ibkr_open_order_ids),
            ("polymarket", expected_poly, self._polymarket_open_order_ids),
        ):
            if venue == "polymarket" and simulated:
                continue
            if expected_ids - observed_ids:
                differences[f"missing_{venue}_orders"] = sorted(expected_ids - observed_ids)
            if observed_ids - expected_ids:
                differences[f"unexpected_{venue}_orders"] = sorted(observed_ids - expected_ids)
        expected_poly_positions = {
            item.instrument: item.strategy_quantity
            for item in await self.repository.strategy_portfolio_positions(self.settings)
            if item.venue == "POLYMARKET"
        }
        if not simulated:
            mismatches = {}
            for token in expected_poly_positions.keys() | self._polymarket_positions.keys():
                expected = expected_poly_positions.get(token, Decimal("0"))
                observed = self._polymarket_positions.get(token, (Decimal("0"), None))[0]
                if expected != observed:
                    mismatches[token] = {"expected": str(expected), "observed": str(observed)}
            if mismatches:
                differences["polymarket_position_mismatch"] = mismatches
        unresolved = await self.repository.unresolved_hedge_obligation_count()
        if unresolved:
            differences["unresolved_hedge_obligations"] = unresolved
        clean = not differences
        unsafe_differences = {
            "zq_position_mismatch",
            "polymarket_position_mismatch",
            "excess_hedges",
            "unexpected_ibkr_orders",
            "unexpected_ibkr_clients",
            "unexpected_polymarket_orders",
            "unprocessed_ibkr_events",
            "unprocessed_polymarket_events",
        } & differences.keys()
        signature = (
            repr(differences),
            str(expected_zq),
            repr(expected_poly_positions),
            repr(sorted(self._ibkr_open_order_ids)),
            repr(sorted(self._polymarket_open_order_ids)),
        )
        current = await self.state.get()
        if venue_revision != self._venue_revision:
            await self.state.invalidate_reconciliation("venue changed during reconciliation")
            return
        self._recovery_ready = not unsafe_differences
        if (
            signature == self._last_reconciliation_signature
            and current.reconciliation.clean == clean
            and (not clean or self._last_ledger_revision == self.repository.database.revision)
        ):
            return
        self._last_reconciliation_signature = signature
        expected_snapshot = {
            "zq_position": str(expected_zq),
            "polymarket_positions": expected_poly_positions,
            "ibkr_open_order_ids": sorted(expected_ibkr),
            "polymarket_open_order_ids": sorted(expected_poly),
        }
        observed_snapshot = {
            "zq_position": str(observed_zq),
            "polymarket_positions": self._polymarket_positions,
            "ibkr_open_order_ids": sorted(self._ibkr_open_order_ids),
            "polymarket_open_order_ids": sorted(self._polymarket_open_order_ids),
        }
        reconciliation_id = await self.repository.record_automated_reconciliation(
            clean=clean,
            expected=expected_snapshot,
            observed=observed_snapshot,
            differences=differences,
        )
        await self.state.set_automated_reconciliation(
            clean=clean,
            unknown=bool(differences)
            and set(differences)
            <= {
                "unresolved_polymarket_orders",
                "pending_settlement",
                "unresolved_hedge_obligations",
                "missing_polymarket_orders",
                "missing_ibkr_orders",
                "unprocessed_polymarket_events",
            },
            snapshot_id=current.snapshot_id,
            reason=(
                f"venue ledger reconciliation {reconciliation_id} is clean"
                if clean
                else f"venue ledger differences: {differences}"
            ),
        )
        self._last_ledger_revision = self.repository.database.revision if clean else None
        if differences:
            await self.state.add_alert(
                AlertSeverity.CRITICAL,
                "EXECUTION_RECONCILIATION_MISMATCH",
                f"Execution reconciliation blocks new entries: {differences}",
            )
            if unsafe_differences:
                await self.state.set_operating_state(paused=True, armed=False)
                await self.cancel_working_zq("unexplained venue orders, fills, or inventory")
        else:
            await self.state.resolve_alerts("EXECUTION_RECONCILIATION")
            await self.state.resolve_alerts("HEDGE_ROUTING_FAILED")

    async def _route_pending_hedges(
        self,
        snapshot: EngineSnapshot,
        obligations: tuple[HedgeObligationView, ...],
    ) -> None:
        if not self._polymarket_routing_enabled():
            return
        for obligation in obligations:
            task = self._hedge_tasks.get(obligation.obligation_id)
            if task is None or task.done():
                self._hedge_tasks[obligation.obligation_id] = asyncio.create_task(
                    self._run_hedge(snapshot, obligation),
                    name=f"hedge-{obligation.obligation_id}",
                )

    async def _run_hedge(self, snapshot: EngineSnapshot, obligation: HedgeObligationView) -> None:
        try:
            async with asyncio.timeout(self.settings.execution_request_timeout_seconds):
                await self._route_one_hedge(snapshot, obligation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.state.add_alert(
                AlertSeverity.CRITICAL,
                "HEDGE_ROUTING_FAILED",
                f"Hedge {obligation.obligation_id} requires recovery: {type(exc).__name__}",
                flashing=True,
            )
            await self.cancel_working_zq("hedge outcome requires recovery")

    async def _route_one_hedge(
        self, snapshot: EngineSnapshot, obligation: HedgeObligationView
    ) -> None:
        async with self._obligation_locks.setdefault(obligation.obligation_id, asyncio.Lock()):
            orders = [
                item
                for item in await self.repository.ledger_orders("POLYMARKET")
                if item.details.get("obligation_id") == obligation.obligation_id
            ]
            for order in orders:
                if order_resolved(order):
                    continue
                if order.state == "INTENT":
                    if not self._hedge_submission_ready():
                        return
                    if order.price is None:
                        raise RuntimeError("Persisted hedge intent has no limit price")
                    await self._post_prepared(
                        PreparedPolymarketOrder(
                            token_id=str(order.details["token_id"]),
                            limit_price=order.price,
                            shares=order.quantity,
                            idempotency_key=order.idempotency_key,
                            signed_payload=order.details.get("signed_payload"),
                            order_id=(
                                None
                                if order.venue_order_id.startswith("INTENT:")
                                else order.venue_order_id
                            ),
                        )
                    )
                    return
                if order.venue_order_id.startswith("INTENT:"):
                    return
                try:
                    venue_order = await self.polymarket.get_order(order.venue_order_id)
                except Exception:
                    # Unknown remains reserved. A 404 or empty open-order page is not zero exposure.
                    submitted_at = _timestamp(order.details.get("last_submit_at"), utc_now())
                    if (
                        order.state in {"OUTCOME_UNKNOWN", "SUBMITTING"}
                        and self._hedge_submission_ready()
                        and (utc_now() - submitted_at).total_seconds() >= 5
                        and order.price is not None
                        and order.details.get("signed_payload")
                    ):
                        # Replay the identical signed order, never re-sign a replacement.
                        await self._post_prepared(
                            PreparedPolymarketOrder(
                                token_id=str(order.details["token_id"]),
                                limit_price=order.price,
                                shares=order.quantity,
                                idempotency_key=order.idempotency_key,
                                signed_payload=order.details["signed_payload"],
                                order_id=order.venue_order_id,
                            )
                        )
                    return
                resolved = await self.repository.verify_polymarket_order(
                    order.venue_order_id, venue_order
                )
                if not resolved:
                    age = (
                        utc_now() - self._order_sent_at.setdefault(order.venue_order_id, utc_now())
                    ).total_seconds()
                    if (
                        normalize_status(str(venue_order.get("status"))) in {"LIVE", "PARTIAL"}
                        and age >= self.settings.polymarket_hedge_reprice_seconds
                    ):
                        await self.invalidate_polymarket_reconciliation(
                            "hedge cancellation pending"
                        )
                        await self.repository.mark_polymarket_order_cancelled(order.venue_order_id)
                        await self.polymarket.cancel_order(order.venue_order_id)
                        final_order = await self.polymarket.get_order(order.venue_order_id)
                        resolved = await self.repository.verify_polymarket_order(
                            order.venue_order_id, final_order
                        )
                    if not resolved:
                        return
            obligation = await self.repository.hedge_obligation(obligation.obligation_id)
            if not self._hedge_submission_ready():
                return
            if obligation.excess_shares:
                return
            shares = max(Decimal("0"), obligation.deficit_shares - obligation.pending_shares)
            if shares <= 0:
                return
            snapshot = await self.state.get()
            book = snapshot.books.get(obligation.token_id)
            if book is None or book.best_ask is None:
                return
            allow_one_tick = obligation.token_id == self._token_id("INC50PLUS")
            plan = plan_hedge_entry(
                book, shares, self.settings.polymarket_emergency_max_price,
                allow_one_tick=allow_one_tick,
            )
            price = plan.limit_price
            if price is None:
                return
            lowest_ask = book.best_ask
            attempt = obligation.reprice_count + 1
            if attempt > self.settings.polymarket_hedge_max_reprices:
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "HEDGE_REPRICE_LIMIT",
                    "Hedge retry threshold exceeded; remaining hedge stays capped",
                )
            key = f"{obligation.obligation_id}:ATTEMPT:{attempt}"
            prepared = await self.polymarket.prepare_hedge_limit(
                token_id=obligation.token_id,
                limit_price=price,
                shares=shares,
                idempotency_key=key,
            )
            book = (await self.state.get()).books.get(obligation.token_id)
            if book is None or book.best_ask != lowest_ask:
                return
            rechecked_plan = plan_hedge_entry(
                book, shares, self.settings.polymarket_emergency_max_price,
                allow_one_tick=allow_one_tick,
            )
            if rechecked_plan.limit_price != price or rechecked_plan.price_cap != plan.price_cap:
                return
            created = await self.repository.create_hedge_order_intent(
                obligation_id=obligation.obligation_id,
                idempotency_key=key,
                shares=shares,
                limit_price=price,
                attempt=attempt,
                signed_payload=prepared.signed_payload,
                order_id=prepared.order_id,
            )
            if created:
                await self._post_prepared(prepared)

    async def _post_prepared(self, prepared: PreparedPolymarketOrder) -> None:
        await self.invalidate_polymarket_reconciliation("hedge submission requires confirmation")
        await self.repository.set_hedge_submission_state(prepared.idempotency_key, "SUBMITTING")
        try:
            result = await self.polymarket.post_prepared_hedge(prepared)
        except BaseException:
            await self.repository.set_hedge_submission_state(
                prepared.idempotency_key, "OUTCOME_UNKNOWN"
            )
            raise
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
                "price_source": "PERSISTED_HEDGE_LIMIT",
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
        receipt = await self.repository.save_venue_receipt("POLYMARKET", payload)
        trade_id = str(payload.get("id") or "")
        quantity = _decimal(payload.get("size"))
        price = _decimal(payload.get("price"))
        if not trade_id or quantity is None or price is None:
            return
        owned = {item.venue_order_id for item in await self.repository.ledger_orders("POLYMARKET")}
        legs: list[tuple[str, Decimal, Decimal]] = []
        taker = str(payload.get("taker_order_id") or "")
        unmapped = str(payload.get("trader_side", "")).upper() == "TAKER" and taker not in owned
        wallet = self.settings.polymarket_funder_address.get_secret_value().lower()
        if taker in owned:
            legs.append((taker, quantity, price))
        for maker in payload.get("maker_orders") or ():
            if not isinstance(maker, dict):
                continue
            order_id = str(maker.get("order_id") or "")
            matched = _decimal(maker.get("matched_amount"))
            if (
                wallet
                and str(maker.get("maker_address", "")).lower() == wallet
                and order_id not in owned
            ):
                unmapped = True
            if order_id in owned and matched is not None:
                legs.append((order_id, matched, _decimal(maker.get("price")) or price))
        if not legs:
            return
        for order_id, size, fill_price in legs:
            await self.repository.record_polymarket_execution(
                execution_id=f"{trade_id}:{order_id}",
                order_id=order_id,
                quantity=size,
                price=fill_price,
                executed_at=_timestamp(
                    payload.get("matched_at")
                    or payload.get("match_time")
                    or payload.get("timestamp"),
                    received_at,
                ),
                details={**payload, "trade_id": trade_id},
            )
        if not unmapped:
            await self.repository.finish_venue_receipt("POLYMARKET", receipt)
        if normalize_status(str(payload.get("status"))) == "FAILED":
            await self.state.add_alert(
                AlertSeverity.CRITICAL,
                "POLYMARKET_TRADE_FAILED",
                f"Polymarket trade {trade_id} failed settlement",
                flashing=True,
            )

    async def _cancel_unprofitable_residual(self, snapshot: EngineSnapshot, batch: Any) -> None:
        if snapshot.kill_switch or self._halt_requested:
            await self.cancel_working_zq("emergency halt")
            return
        if batch.cancel_reason is not None:
            await self.cancel_working_zq(batch.cancel_reason)
            return
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
        entry25 = plan_hedge_entry(book25, q25, self.settings.polymarket_hard_price_cap)
        entry50 = plan_hedge_entry(
            book50, q50, self.settings.polymarket_hard_price_cap, allow_one_tick=True
        )
        polymarket_fees = sum(
            (
                max(
                    sum(
                        (self._taker_fee(fee_parameters[code], fill.size, fill.price)
                         for fill in depth.fills),
                        Decimal("0"),
                    )
                    for depth in (entry.depth, emergency)
                )
                for code, entry, emergency in (
                    ("INC25", entry25, emergency25),
                    ("INC50PLUS", entry50, emergency50),
                )
            ),
            Decimal("0"),
        )
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

        if (
            observed_zq not in {None, Decimal("0")}
            and (
                "IBKR",
                self.settings.ibkr_zq_contract_month,
            )
            not in strategy_keys
        ):
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
            labels = {leg.yes_token_id: f"{leg.code} YES" for leg in self.settings.market_legs} | {
                leg.no_token_id: f"{leg.code} NO" for leg in self.settings.market_legs
            }
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

    def _new_entry_authorized(
        self,
        snapshot: EngineSnapshot,
        *,
        ledger_revision: int | None = None,
    ) -> bool:
        expected_revision = (
            self._last_ledger_revision if ledger_revision is None else ledger_revision
        )
        manual_simulation = (
            self.settings.run_mode.value == "PAPER"
            and self.settings.simulate_polymarket_fills
            and snapshot.reconciliation.method == "MANUAL_OPERATOR_ATTESTATION"
        )
        return (
            snapshot.armed
            and snapshot.reconciliation.clean
            and (manual_simulation or expected_revision == self.repository.database.revision)
            and not self._halt_requested
            and not snapshot.paused
            and not snapshot.kill_switch
            and int(snapshot.metadata.get("unresolved_hedge_obligations") or 0) == 0
            and not any(
                obligation.deficit_shares > 0 for obligation in snapshot.active_batch.obligations
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

    def _hedge_submission_ready(self) -> bool:
        return self._recovery_ready or (
            self.settings.run_mode.value == "PAPER" and self.settings.simulate_polymarket_fills
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
