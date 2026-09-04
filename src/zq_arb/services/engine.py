from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.nyfed import NewYorkFedEffrAdapter
from zq_arb.adapters.polymarket import PolymarketAdapter, PolymarketProtocolError
from zq_arb.analytics.payoff import (
    CostInputs,
    build_three_state_opportunity,
    conservative_ibkr_round_trip_commission,
    hedge_shares_per_contract,
    round_shares_up,
    walk_asks,
)
from zq_arb.analytics.portfolio import value_strategy_portfolio
from zq_arb.analytics.probability import (
    DiagnosticPrices,
    direct_zq_probability,
    fedwatch_reference,
    with_polymarket_expectation,
)
from zq_arb.config import Settings
from zq_arb.domain.enums import (
    AlertSeverity,
    BatchState,
    ConnectionStatus,
    DataQuality,
    FarmStatus,
    GateStatus,
    MarginPreviewStatus,
    MarginQualificationStatus,
    QuoteRole,
    SubscriptionStatus,
)
from zq_arb.domain.models import (
    EffrObservation,
    EngineSnapshot,
    FedWatchDiagnostic,
    GateCheck,
    MarginPreview,
    MarketProbabilityComparison,
    Opportunity,
    OrderBook,
    ProbabilitySnapshot,
    Quote,
    utc_now,
)
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.repository import Repository
from zq_arb.risk.engine import GateContext, RiskEngine
from zq_arb.services.state import StateStore

LOGGER = structlog.get_logger(__name__)


def _mid(quote: Quote) -> Decimal | None:
    if quote.bid is not None and quote.ask is not None:
        return (quote.bid + quote.ask) / Decimal("2")
    return quote.last


class EngineRuntime:
    """Owns background tasks, venue lifecycle, calculations, and fail-closed shutdown."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = StateStore(settings)
        self.database = Database(settings)
        self.repository = Repository(self.database)
        self.events: asyncio.Queue[VenueEvent] = asyncio.Queue(maxsize=settings.event_queue_maxsize)
        self.ibkr = IbkrAdapter(settings, self.events)
        self.nyfed = (
            NewYorkFedEffrAdapter(settings) if settings.effr_source == "NYFED_API" else None
        )
        self.polymarket = PolymarketAdapter(settings)
        self.execution = ExecutionCoordinator(
            settings=settings,
            repository=self.repository,
            state=self.state,
            ibkr=self.ibkr,
            polymarket=self.polymarket,
        )
        self.risk = RiskEngine(settings)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._polymarket_resync_requested = asyncio.Event()
        self._margin_preview_refresh_requested = asyncio.Event()
        self._polymarket_fee_parameters: dict[str, dict[str, Decimal]] = {}
        self._polymarket_fee_parameters_at: datetime | None = None
        self._event_overflow_halted = False

    async def start(self) -> None:
        await self.database.initialize()
        await self.repository.ensure_config_version(self.settings)
        await self.execution.recover()
        await self.repository.audit(
            actor="SYSTEM",
            action="ENGINE_START",
            reason="application startup",
            details={
                "run_mode": self.settings.run_mode.value,
                "effr_source": self.settings.effr_source,
                "manual_effr_percent": (
                    str(self.settings.pre_meeting_effr_percent)
                    if self.settings.pre_meeting_effr_percent is not None
                    else None
                ),
            },
        )
        self._tasks.extend(
            (
                asyncio.create_task(self._process_events(), name="venue-event-processor"),
                asyncio.create_task(self._analytics_loop(), name="analytics-loop"),
                asyncio.create_task(self._polymarket_reference_loop(), name="polymarket-reference"),
                asyncio.create_task(
                    self._polymarket_fee_parameter_loop(),
                    name="polymarket-fee-parameters",
                ),
                asyncio.create_task(self._polymarket_stream_loop(), name="polymarket-stream"),
                asyncio.create_task(self._ibkr_connection_loop(), name="ibkr-connection"),
                asyncio.create_task(
                    self._ibkr_market_data_supervisor_loop(),
                    name="ibkr-market-data-supervisor",
                ),
                asyncio.create_task(self._ibkr_margin_preview_loop(), name="ibkr-margin-preview"),
            )
        )
        if (
            self.settings.polymarket_order_submission_enabled
            and not self.settings.simulate_polymarket_fills
            and (
                self.settings.run_mode.value == "PAPER"
                or (self.settings.run_mode.is_live and self.settings.live_trading_enabled)
            )
        ):
            self._tasks.extend(
                (
                    asyncio.create_task(
                        self._polymarket_user_stream_loop(),
                        name="polymarket-user-stream",
                    ),
                    asyncio.create_task(
                        self._polymarket_account_reconciliation_loop(),
                        name="polymarket-account-reconciliation",
                    ),
                    asyncio.create_task(
                        self._polymarket_order_heartbeat_loop(),
                        name="polymarket-order-heartbeat",
                    ),
                )
            )
        if self.settings.effr_source == "NYFED_API":
            self._tasks.append(
                asyncio.create_task(self._effr_reference_loop(), name="nyfed-effr-reference")
            )

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        await self.state.set_operating_state(paused=True, armed=False)
        await self.repository.audit(
            actor="SYSTEM",
            action="ENGINE_STOP",
            reason="application shutdown",
        )
        for background_task in self._tasks:
            background_task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.ibkr.disconnect()
        if self.nyfed is not None:
            await self.nyfed.close()
        await self.polymarket.close()
        await self.database.close()

    async def _process_events(self) -> None:
        while not self._stopping.is_set():
            event = await self.events.get()
            try:
                if event.venue == "IBKR":
                    await self.execution.handle_ibkr_event(event)
                    await self.state.apply_ibkr_event(event)
                    if event.kind in {"connection", "account_summary", "pnl"} or (
                        event.kind in {"tick_price", "tick_size"}
                        and str(event.payload.get("month") or "")
                        == self.settings.ibkr_zq_contract_month
                    ):
                        self._margin_preview_refresh_requested.set()
                else:
                    await self.execution.handle_polymarket_event(event)
                    await self.state.apply_polymarket_event(event)
            except PolymarketProtocolError as exc:
                LOGGER.warning("polymarket_book_resync_required", kind=event.kind, error=str(exc))
                await self.state.mark_polymarket_books_unsynchronized()
                await self.state.set_polymarket_health(
                    ConnectionStatus.DEGRADED,
                    "market WebSocket book integrity failed; REST recovery requested",
                )
                self._polymarket_resync_requested.set()
            except Exception:
                LOGGER.exception(
                    "venue_event_processing_failed", venue=event.venue, kind=event.kind
                )
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "EVENT_PROCESSING_FAILED",
                    f"Failed to process {event.venue} {event.kind}",
                    flashing=True,
                )
            finally:
                self.events.task_done()

    async def _ibkr_connection_loop(self) -> None:
        attempts = 0
        while not self._stopping.is_set():
            try:
                await self.state.set_ibkr_health(ConnectionStatus.CONNECTING, "connecting to TWS")
                await self.ibkr.connect()
                await self.state.begin_ibkr_subscriptions()
                self.ibkr.request_contracts_and_market_data()
                await self.state.set_ibkr_resubscribe_required(False)
                await self.execution.begin_ibkr_reconciliation()
                self.ibkr.request_open_orders_and_executions()
                self._margin_preview_refresh_requested.set()
                attempts = 0
                while self.ibkr.connected and not self._stopping.is_set():
                    try:
                        await asyncio.wait_for(
                            self._stopping.wait(),
                            timeout=self.settings.ibkr_heartbeat_seconds,
                        )
                    except TimeoutError:
                        continue
                if not self._stopping.is_set():
                    raise ConnectionError("TWS network loop stopped")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                LOGGER.warning("ibkr_connection_failed", attempt=attempts, error=str(exc))
                await self.state.set_ibkr_health(
                    ConnectionStatus.FAILED, f"TWS: {type(exc).__name__}"
                )
                if attempts >= self.settings.ibkr_reconnect_max_attempts:
                    await self.state.add_alert(
                        AlertSeverity.CRITICAL,
                        "IBKR_RECONNECT_EXHAUSTED",
                        "IBKR reconnect attempts exhausted",
                        flashing=True,
                    )
                    return
                await asyncio.sleep(self.settings.ibkr_reconnect_backoff_seconds)

    async def _ibkr_market_data_supervisor_loop(self) -> None:
        """Recreate current-generation streams after a socket or farm recovery."""

        while not self._stopping.is_set():
            snapshot = await self.state.get()
            if self.ibkr.event_queue_overflowed and not self._event_overflow_halted:
                self._event_overflow_halted = True
                await self.state.set_operating_state(
                    kill_switch=True,
                    paused=True,
                    armed=False,
                )
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "VENUE_EVENT_QUEUE_OVERFLOW",
                    "An IBKR callback was lost because the venue event queue overflowed; "
                    "trading is halted pending restart and reconciliation",
                    flashing=True,
                )
            if self.ibkr.connected and snapshot.metadata.get("ibkr_resubscribe_required"):
                try:
                    await self.state.begin_ibkr_subscriptions(
                        advance_generation=not bool(
                            snapshot.metadata.get("ibkr_generation_preallocated")
                        )
                    )
                    self.ibkr.resubscribe_market_data()
                    await self.state.set_ibkr_resubscribe_required(False)
                    self._margin_preview_refresh_requested.set()
                except Exception as exc:
                    LOGGER.warning("ibkr_market_data_resubscribe_failed", error=str(exc))
            await asyncio.sleep(0.25)

    async def _ibkr_margin_preview_loop(self) -> None:
        """Maintain a paced, non-routing BUY-10 ZQ margin preview."""

        loop = asyncio.get_running_loop()
        last_requested = float("-inf")
        while not self._stopping.is_set():
            snapshot = await self.state.get()
            target = snapshot.quotes.get(self.settings.ibkr_zq_contract_month)
            farm = snapshot.ibkr_farms.get("US_FUTURES")
            verified = bool(
                (snapshot.metadata.get("contract_verification") or {})
                .get(self.settings.ibkr_zq_contract_month, {})
                .get("verified")
            )
            due = loop.time() - last_requested >= self.settings.ibkr_margin_preview_interval_seconds
            preview = snapshot.margin_preview
            preview_age = preview.age_seconds()
            preview_matches = bool(
                preview.contract_month == self.settings.ibkr_zq_contract_month
                and preview.quantity == self.settings.ibkr_zq_child_order_quantity
                and target is not None
                and target.bid is not None
                and preview.limit_price == target.bid
            )
            refresh_needed = bool(
                self._margin_preview_refresh_requested.is_set()
                or not preview.available
                or not preview_matches
                or preview_age is None
                or preview_age >= self.settings.ibkr_margin_preview_interval_seconds
            )
            eligible = bool(
                due
                and refresh_needed
                and self.ibkr.connected
                and self.settings.ibkr_account_configured
                and snapshot.ibkr.status is ConnectionStatus.CONNECTED
                and farm is not None
                and farm.status is FarmStatus.CONNECTED
                and verified
                and target is not None
                and target.bid is not None
                and target.subscription_status is SubscriptionStatus.ACTIVE
                and target.market_data_type == 1
                and target.has_valid_two_sided_market
            )
            if not eligible:
                await asyncio.sleep(0.25)
                continue
            order_id: int | None = None
            last_requested = loop.time()
            self._margin_preview_refresh_requested.clear()
            try:
                assert target is not None and target.bid is not None
                order_id = self.ibkr.request_zq_margin_preview(
                    month=self.settings.ibkr_zq_contract_month,
                    limit_price=target.bid,
                    quantity=self.settings.ibkr_zq_child_order_quantity,
                )
                deadline = loop.time() + self.settings.ibkr_margin_preview_timeout_seconds
                while loop.time() < deadline:
                    preview = (await self.state.get()).margin_preview
                    if preview.order_id == order_id and preview.status in {
                        MarginPreviewStatus.AVAILABLE,
                        MarginPreviewStatus.FAILED,
                    }:
                        break
                    await asyncio.sleep(0.05)
                else:
                    await self.state.fail_margin_preview(
                        order_id,
                        f"IBKR what-if response exceeded "
                        f"{self.settings.ibkr_margin_preview_timeout_seconds}s timeout",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("ibkr_margin_preview_failed", error=str(exc))
                await self.state.fail_margin_preview(
                    order_id, f"IBKR what-if request failed: {type(exc).__name__}: {exc}"
                )
            finally:
                if order_id is not None:
                    self.ibkr.cancel_margin_preview(order_id)
            await asyncio.sleep(0.25)

    async def _polymarket_reference_loop(self) -> None:
        mapping_due = True
        eligibility_due = True
        last_mapping: datetime | None = None
        last_eligibility: datetime | None = None
        while not self._stopping.is_set():
            self._polymarket_resync_requested.clear()
            now = utc_now()
            try:
                if (
                    mapping_due
                    or last_mapping is None
                    or (now - last_mapping).total_seconds() >= 3600
                ):
                    mapping = await self.polymarket.verify_market_mapping()
                    await self.state.set_mapping(mapping)
                    last_mapping = now
                    mapping_due = False
                if (
                    eligibility_due
                    or last_eligibility is None
                    or (now - last_eligibility).total_seconds()
                    >= self.settings.geoblock_refresh_seconds
                ):
                    eligibility = await self.polymarket.check_eligibility()
                    await self.state.set_eligibility(eligibility)
                    last_eligibility = now
                    eligibility_due = False
                books = await self.polymarket.snapshot_all_books()
                mismatches = await self.state.reconcile_polymarket_books(books)
                if mismatches:
                    LOGGER.warning(
                        "polymarket_book_reconciliation_mismatch",
                        mismatch_count=len(mismatches),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("polymarket_rest_reconciliation_failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._polymarket_resync_requested.wait(),
                    timeout=self.settings.polymarket_book_snapshot_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _refresh_polymarket_fee_parameters(self) -> None:
        parameters = await self.polymarket.fetch_hedge_fee_parameters()
        required_codes = {"INC25", "INC50PLUS"}
        missing_codes = required_codes.difference(parameters)
        if missing_codes:
            raise PolymarketProtocolError(
                "CLOB fee response omitted required hedge markets: "
                + ", ".join(sorted(missing_codes))
            )
        self._polymarket_fee_parameters = parameters
        self._polymarket_fee_parameters_at = utc_now()

    async def _polymarket_fee_parameter_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._refresh_polymarket_fee_parameters()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("polymarket_fee_parameter_refresh_failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.settings.polymarket_book_snapshot_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _effr_reference_loop(self) -> None:
        assert self.nyfed is not None
        while not self._stopping.is_set():
            try:
                previous = (await self.state.get()).effr
                observation = await self.nyfed.fetch_latest()
                await self.state.set_effr(observation)
                await self.state.resolve_alerts("NYFED_EFFR_FETCH")
                if (
                    previous.source,
                    previous.rate_percent,
                    previous.effective_date,
                    previous.target_rate_from,
                    previous.target_rate_to,
                    previous.revision_indicator,
                ) != (
                    observation.source,
                    observation.rate_percent,
                    observation.effective_date,
                    observation.target_rate_from,
                    observation.target_rate_to,
                    observation.revision_indicator,
                ):
                    await self.repository.audit(
                        actor="SYSTEM",
                        action="EFFR_REFERENCE_UPDATED",
                        reason=observation.reason,
                        details={
                            "observation": observation.model_dump(mode="json"),
                            "endpoint": self.settings.nyfed_effr_api_url,
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("nyfed_effr_fetch_failed", error=str(exc))
                snapshot = await self.state.get()
                current = snapshot.effr
                expired = bool(
                    current.effective_date is None
                    or (utc_now().date() - current.effective_date).days
                    > self.settings.nyfed_effr_max_age_days
                )
                if expired:
                    await self.state.set_effr(
                        current.model_copy(
                            update={
                                "valid": False,
                                "reason": f"New York Fed EFFR unavailable: {type(exc).__name__}",
                            }
                        )
                    )
                await self.state.add_alert(
                    AlertSeverity.WARNING,
                    "NYFED_EFFR_FETCH",
                    f"New York Fed EFFR refresh failed: {type(exc).__name__}",
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.settings.nyfed_effr_refresh_seconds,
                )
            except TimeoutError:
                pass

    async def _polymarket_stream_loop(self) -> None:
        token_ids = [
            token_id
            for leg in self.settings.market_legs
            for token_id in (leg.yes_token_id, leg.no_token_id)
        ]
        attempts = 0
        while not self._stopping.is_set():
            try:
                await self.state.mark_polymarket_books_unsynchronized()
                await self.state.set_polymarket_health(
                    ConnectionStatus.CONNECTING,
                    "connecting to Polymarket market WebSocket",
                )
                async for event in self.polymarket.public_market_stream(token_ids):
                    if event.kind.lower() != "stream_connected":
                        attempts = 0
                    await self.events.put(event)
                raise ConnectionError("Polymarket market stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                LOGGER.warning("polymarket_stream_failed", attempt=attempts, error=str(exc))
                await self.state.mark_polymarket_books_unsynchronized()
                await self.state.set_polymarket_health(
                    ConnectionStatus.DEGRADED,
                    "market WebSocket unavailable; REST display remains fail-closed",
                )
                self._polymarket_resync_requested.set()
                if attempts == self.settings.polymarket_reconnect_max_attempts:
                    await self.state.add_alert(
                        AlertSeverity.WARNING,
                        "POLYMARKET_STREAM_RECONNECTING",
                        "Polymarket market WebSocket reconnect threshold reached; retrying",
                    )
                await asyncio.sleep(self.settings.polymarket_reconnect_backoff_seconds)

    async def _polymarket_user_stream_loop(self) -> None:
        attempts = 0
        while not self._stopping.is_set():
            try:
                async for event in self.polymarket.authenticated_user_stream():
                    attempts = 0
                    await self.events.put(event)
                raise ConnectionError("Polymarket authenticated user stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                LOGGER.warning("polymarket_user_stream_failed", attempt=attempts, error=str(exc))
                await self.state.set_polymarket_health(
                    ConnectionStatus.DEGRADED,
                    "authenticated user stream unavailable; REST reconciliation active",
                    authenticated=False,
                )
                if attempts >= self.settings.polymarket_reconnect_max_attempts:
                    await self.state.add_alert(
                        AlertSeverity.CRITICAL,
                        "POLYMARKET_USER_STREAM_RECONNECTING",
                        "Authenticated Polymarket user stream reconnect threshold reached",
                        flashing=True,
                    )
                    attempts = 0
                await asyncio.sleep(self.settings.polymarket_reconnect_backoff_seconds)

    async def _polymarket_account_reconciliation_loop(self) -> None:
        interval = max(5, self.settings.polymarket_book_snapshot_interval_seconds)
        while not self._stopping.is_set():
            try:
                await self.execution.reconcile_polymarket_account()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("polymarket_account_reconciliation_failed", error=str(exc))
                await self.state.add_alert(
                    AlertSeverity.WARNING,
                    "POLYMARKET_ACCOUNT_RECONCILIATION_FAILED",
                    f"Authenticated account reconciliation failed: {type(exc).__name__}",
                )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _polymarket_order_heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.polymarket.send_order_heartbeat()
                await self.state.resolve_alerts("POLYMARKET_ORDER_HEARTBEAT")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("polymarket_order_heartbeat_failed", error=str(exc))
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "POLYMARKET_ORDER_HEARTBEAT_FAILED",
                    "Polymarket cancel-on-disconnect heartbeat failed; open hedge orders "
                    "may be auto-cancelled and will be reconciled",
                    flashing=True,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.settings.polymarket_user_ws_ping_seconds,
                )
            except TimeoutError:
                pass

    async def _analytics_loop(self) -> None:
        interval = self.settings.engine_state_publish_interval_ms / 1_000
        while not self._stopping.is_set():
            try:
                updated = await self.state.update(self._calculate)
                await self.execution.cycle(updated)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("analytics_cycle_failed", error=str(exc))
                await self.state.add_alert(
                    AlertSeverity.CRITICAL,
                    "ANALYTICS_FAILED",
                    f"Analytics cycle failed: {type(exc).__name__}",
                    flashing=True,
                )
            await asyncio.sleep(interval)

    def _calculate(self, snapshot: EngineSnapshot) -> EngineSnapshot:
        now = utc_now()
        months = self.settings.subscription_contract_months
        target_month = self.settings.ibkr_zq_contract_month
        generation = int(snapshot.metadata.get("ibkr_subscription_generation") or 0)
        qualified_quotes = {
            month: self._qualify_quote(quote, generation=generation)
            for month, quote in snapshot.quotes.items()
        }
        snapshot = snapshot.model_copy(update={"quotes": qualified_quotes})
        snapshot = snapshot.model_copy(
            update={"portfolio": value_strategy_portfolio(snapshot)}
        )
        snapshot = snapshot.model_copy(
            update={"margin_preview": self._margin_preview_view(snapshot, now)}
        )
        target_quote = snapshot.quotes.get(target_month)
        effr = snapshot.effr
        missing_inputs: list[str] = []
        if target_quote is None or target_quote.bid is None or target_quote.ask is None:
            missing_inputs.append(f"{target_month} bid/ask")
        if not effr.valid or effr.rate_percent is None:
            missing_inputs.append("validated pre-meeting EFFR")
        if (
            target_quote is None
            or target_quote.bid is None
            or target_quote.ask is None
            or not effr.valid
            or effr.rate_percent is None
        ):
            probabilities = ProbabilitySnapshot(
                target_contract_month=target_month,
                pre_meeting_effr=effr.rate_percent,
                reason=f"awaiting {', '.join(missing_inputs)}",
            )
            return snapshot.model_copy(
                update={
                    "probabilities": probabilities,
                    "probability_comparisons": self._probability_comparisons(
                        snapshot,
                        {},
                        now,
                    ),
                    "health_messages": tuple(
                        dict.fromkeys(
                            (
                                *(f"Awaiting {item}" for item in missing_inputs),
                                *(() if effr.valid else (f"EFFR not qualified: {effr.reason}",)),
                            )
                        )
                    ),
                    "quotes": qualified_quotes,
                }
            )
        diagnostic = FedWatchDiagnostic()
        reference_quotes = [snapshot.quotes.get(month) for month in months]
        reference_mids = [_mid(quote) if quote is not None else None for quote in reference_quotes]
        if all(
            quote is not None and quote.analytics_qualified for quote in reference_quotes
        ) and all(value is not None for value in reference_mids):
            diagnostic_values = [value for value in reference_mids if value is not None]
            diagnostic = fedwatch_reference(
                DiagnosticPrices(*diagnostic_values),
                pre_meeting_effr=effr.rate_percent,
            )
        probabilities = direct_zq_probability(
            target_contract_month=target_month,
            target_bid=target_quote.bid,
            target_ask=target_quote.ask,
            pre_meeting_effr=effr.rate_percent,
            fedwatch=diagnostic,
        )
        polymarket_midpoints = {
            leg.code: book.midpoint
            for leg in self.settings.market_legs
            if (book := snapshot.books.get(leg.yes_token_id)) is not None
            and book.midpoint is not None
        }
        probabilities = with_polymarket_expectation(probabilities, polymarket_midpoints)
        analytics_qualified = effr.valid and target_quote.analytics_qualified
        long_book_25 = self._book(snapshot, "INC25", yes=True)
        long_book_50 = self._book(snapshot, "INC50PLUS", yes=True)
        relevant_books = (long_book_25, long_book_50)
        books_available = all(book is not None for book in relevant_books)
        books_fresh = (
            books_available
            and all(book is not None and book.stream_synchronized for book in relevant_books)
            and snapshot.polymarket.status is ConnectionStatus.CONNECTED
        )
        cross_venue_checks = self._cross_venue_checks(
            snapshot,
            target_quote=target_quote,
            effr=effr,
            probabilities=probabilities,
            long_book_25=long_book_25,
            long_book_50=long_book_50,
            now=now,
            generation=generation,
        )
        blocking_cross_venue_checks = tuple(check for check in cross_venue_checks if check.blocking)
        failed_cross_venue_checks = tuple(
            check for check in blocking_cross_venue_checks if not check.passed
        )
        execution_qualified = not failed_cross_venue_checks
        qualification_reason = (
            "EXECUTION-QUALIFIED immutable snapshot"
            if execution_qualified
            else (
                f"NOT EXECUTION-QUALIFIED — {len(failed_cross_venue_checks)} of "
                f"{len(blocking_cross_venue_checks)} execution checks failed"
            )
        )
        probabilities = probabilities.model_copy(
            update={
                "analytics_qualified": analytics_qualified,
                "execution_qualified": execution_qualified,
                "qualification_reason": qualification_reason,
                "qualification_checks": cross_venue_checks,
            }
        )
        opportunities: list[Opportunity] = []
        _, _, _, incremental_margin = self._margin_preview_qualification(snapshot, now)
        emergency_reserve = Decimal("0")
        q25 = round_shares_up(
            hedge_shares_per_contract(25) * Decimal(self.settings.ibkr_zq_child_order_quantity)
        )
        q50 = round_shares_up(
            hedge_shares_per_contract(50) * Decimal(self.settings.ibkr_zq_child_order_quantity)
        )
        fee_parameters_current, _ = self._polymarket_fee_parameter_status(now)
        polymarket_fees: Decimal | None = (
            Decimal("0") if fee_parameters_current else None
        )
        if (
            polymarket_fees is not None
            and long_book_25 is not None
            and long_book_25.best_ask is not None
        ):
            emergency25 = walk_asks(
                long_book_25.asks,
                q25,
                price_cap=self.settings.polymarket_emergency_max_price,
            )
            fee_prices25 = [long_book_25.best_ask]
            if emergency25.vwap is not None:
                fee_prices25.append(emergency25.vwap)
            polymarket_fees += max(
                self._polymarket_taker_fee("INC25", q25, value) for value in fee_prices25
            )
        if (
            polymarket_fees is not None
            and long_book_50 is not None
            and long_book_50.best_ask is not None
        ):
            emergency50 = walk_asks(
                long_book_50.asks,
                q50,
                price_cap=self.settings.polymarket_emergency_max_price,
            )
            fee_prices50 = [long_book_50.best_ask]
            if emergency50.vwap is not None:
                fee_prices50.append(emergency50.vwap)
            polymarket_fees += max(
                self._polymarket_taker_fee("INC50PLUS", q50, value) for value in fee_prices50
            )
        costs = CostInputs(
            ibkr_commission=conservative_ibkr_round_trip_commission(
                contracts=self.settings.ibkr_zq_child_order_quantity,
                configured_per_contract=self.settings.ibkr_commission_estimate,
                entry_preview_commission=(
                    snapshot.margin_preview.commission if incremental_margin is not None else None
                ),
            ),
            polymarket_fees=polymarket_fees,
        )
        if probabilities.pre_meeting_effr is not None and books_available:
            price = target_quote.bid
            book25 = long_book_25
            book50 = long_book_50
            if price is not None and book25 is not None and book50 is not None:
                raw = build_three_state_opportunity(
                    contracts=self.settings.ibkr_zq_child_order_quantity,
                    zq_price=price,
                    pre_meeting_effr=probabilities.pre_meeting_effr,
                    inc25_book=book25,
                    inc50_book=book50,
                    cost_inputs=costs,
                    incremental_margin=incremental_margin,
                    emergency_cash_reserve=emergency_reserve,
                    post_price_cap=self.settings.polymarket_hard_price_cap,
                    emergency_price_cap=self.settings.polymarket_emergency_max_price,
                )
                context = self._gate_context(
                    snapshot,
                    raw,
                    now=now,
                    books_fresh=books_fresh,
                    target_subscription_qualified=target_quote.pretrade_qualified,
                    effr_qualified=effr.valid,
                    cross_venue_checks=cross_venue_checks,
                )
                qualification = self.risk.qualify(raw, context)
                opportunities.append(
                    raw.model_copy(
                        update={
                            "tradeable": qualification.tradeable,
                            "gate_reasons": qualification.reasons,
                            "gate_checks": qualification.checks,
                        }
                    )
                )
        health: list[str] = []
        zq_farm = snapshot.ibkr_farms.get("US_FUTURES")
        if zq_farm is None or zq_farm.status is not FarmStatus.CONNECTED:
            health.append("US futures market-data farm disconnected")
        if target_quote.subscription_status is not SubscriptionStatus.ACTIVE:
            health.append("ZQU6 current-generation live subscription is not qualified")
        if not target_quote.analytics_qualified:
            health.append(f"ZQU6 not qualified: {target_quote.validation_reason}")
        if not effr.valid:
            health.append(f"EFFR not qualified: {effr.reason}")
        if not books_fresh:
            health.append("Polymarket hedge books are unavailable or WebSocket-unsynchronized")
        if not fee_parameters_current:
            health.append("Current Polymarket taker-fee parameters are unavailable")
        if not probabilities.valid:
            health.append(probabilities.reason)
        if snapshot.mapping.errors:
            health.extend(snapshot.mapping.errors)
        metadata = deepcopy(snapshot.metadata)
        market_event_ages = tuple(
            max(0, int((now - quote.last_market_data_event_at).total_seconds() * 1_000))
            for quote in (target_quote,)
            if quote.last_market_data_event_at is not None
        )
        metadata.update(
            {
                "maximum_market_event_age_ms": max(market_event_ages, default=None),
                "cross_venue_snapshot_qualified": execution_qualified,
                "cross_venue_snapshot_at": now.isoformat(),
                "polymarket_fee_parameters": {
                    code: {name: str(value) for name, value in values.items()}
                    for code, values in self._polymarket_fee_parameters.items()
                },
                "polymarket_fee_parameters_at": (
                    self._polymarket_fee_parameters_at.isoformat()
                    if self._polymarket_fee_parameters_at is not None
                    else None
                ),
            }
        )
        return snapshot.model_copy(
            update={
                "quotes": qualified_quotes,
                "probabilities": probabilities,
                "probability_comparisons": self._probability_comparisons(
                    snapshot,
                    probabilities.bucket_probabilities,
                    now,
                ),
                "opportunities": tuple(opportunities),
                "health_messages": tuple(dict.fromkeys(health)),
                "metadata": metadata,
            }
        )

    def _cross_venue_checks(
        self,
        snapshot: EngineSnapshot,
        *,
        target_quote: Quote,
        effr: EffrObservation,
        probabilities: ProbabilitySnapshot,
        long_book_25: OrderBook | None,
        long_book_50: OrderBook | None,
        now: datetime,
        generation: int,
    ) -> tuple[GateCheck, ...]:
        checks: list[GateCheck] = []

        def add(
            code: str,
            label: str,
            passed: bool | None,
            actual: str,
            operator: str,
            required: str,
            detail: str,
            *,
            blocking: bool = True,
            unit: str | None = None,
        ) -> None:
            status = (
                GateStatus.UNAVAILABLE
                if passed is None
                else GateStatus.PASSED
                if passed
                else GateStatus.FAILED
            )
            resolved_detail = (
                f"{label} passed: {actual} {operator} {required}" if passed else detail
            )
            checks.append(
                GateCheck(
                    code=code,
                    category="CROSS_VENUE",
                    label=label,
                    status=status,
                    blocking=blocking,
                    actual_value=actual,
                    operator=operator,
                    required_value=required,
                    unit=unit,
                    detail=resolved_detail,
                    observed_at=now,
                )
            )

        def quote_checks(prefix: str, label: str, quote: Quote) -> None:
            add(
                f"{prefix}_LIVE_DATA",
                f"{label} live market data",
                quote.market_data_type == 1 and quote.quality is DataQuality.LIVE,
                f"{quote.quality.value} ({quote.market_data_type or 'unknown'})",
                "==",
                "LIVE (1)",
                f"{label} market data is not live",
            )
            add(
                f"{prefix}_SUBSCRIPTION",
                f"{label} subscription",
                quote.subscription_status is SubscriptionStatus.ACTIVE,
                quote.subscription_status.value,
                "==",
                SubscriptionStatus.ACTIVE.value,
                f"{label} subscription is not active",
            )
            add(
                f"{prefix}_GENERATION",
                f"{label} subscription generation",
                quote.subscription_generation == generation,
                str(quote.subscription_generation),
                "==",
                str(generation),
                f"{label} subscription generation is not current",
            )
            add(
                f"{prefix}_FARM",
                f"{label} US futures farm",
                quote.farm_status is FarmStatus.CONNECTED,
                quote.farm_status.value,
                "==",
                FarmStatus.CONNECTED.value,
                f"{label} US futures farm is not connected",
            )
            add(
                f"{prefix}_TWO_SIDED",
                f"{label} two-sided quote",
                quote.has_valid_two_sided_market,
                f"bid={quote.bid}; ask={quote.ask}",
                "==",
                "positive and uncrossed",
                f"{label} bid/ask is incomplete or crossed",
            )

        quote_checks("ZQU6", "ZQU6", target_quote)
        add(
            "PRE_MEETING_EFFR",
            "Pre-meeting EFFR input",
            effr.valid and effr.rate_percent is not None,
            (
                f"source={effr.source}; rate={effr.rate_percent}; "
                f"effective_date={effr.effective_date or 'manual'}"
            ),
            "==",
            "validated NYFED_API observation or explicit MANUAL override",
            f"pre-meeting EFFR is not qualified: {effr.reason}",
            unit="percent",
        )
        add(
            "DIRECT_ZQ_MODEL",
            "Direct ZQU6 adjacent-state probability model",
            probabilities.valid,
            (
                f"move={probabilities.expected_move_bps}; "
                f"buy_probability={probabilities.executable_buy_probability}; "
                f"bid_reference_probability={probabilities.bid_reference_probability}"
            ),
            "within",
            "move [-50, 50] bp and probabilities [0, 1]",
            "direct ZQU6 move or adjacent-state probability is outside the approved range",
        )
        polymarket_connected = snapshot.polymarket.status is ConnectionStatus.CONNECTED
        add(
            "POLYMARKET_WEBSOCKET",
            "Polymarket market WebSocket",
            polymarket_connected,
            snapshot.polymarket.status.value,
            "==",
            ConnectionStatus.CONNECTED.value,
            "Polymarket market WebSocket is not connected",
        )
        fee_parameters_current, fee_age_seconds = self._polymarket_fee_parameter_status(now)
        add(
            "POLYMARKET_TAKER_FEES",
            "Current Polymarket taker-fee parameters",
            fee_parameters_current,
            ", ".join(
                f"{code}={self._polymarket_fee_parameters.get(code)}"
                for code in ("INC25", "INC50PLUS")
            )
            + f"; age={fee_age_seconds}",
            "==",
            "current parameters loaded for both hedge markets",
            "current Polymarket taker-fee parameters are unavailable",
        )
        entry_commission = snapshot.margin_preview.commission
        commission = conservative_ibkr_round_trip_commission(
            contracts=self.settings.ibkr_zq_child_order_quantity,
            configured_per_contract=self.settings.ibkr_commission_estimate,
            entry_preview_commission=entry_commission,
        )
        configured_commission = self.settings.ibkr_commission_estimate * Decimal(
            self.settings.ibkr_zq_child_order_quantity
        )
        add(
            "IBKR_COMMISSION_ESTIMATE",
            "Conservative IBKR BUY-10 round-trip commission",
            commission >= configured_commission,
            f"{commission} (entry what-if={entry_commission})",
            ">=",
            f"configured floor {configured_commission} USD",
            "IBKR round-trip commission is below the configured floor",
            unit="USD",
        )
        for code, label, book in (
            ("INC25_YES_BOOK", "INC25 YES hedge book", long_book_25),
            ("INC50PLUS_YES_BOOK", "INC50PLUS YES hedge book", long_book_50),
        ):
            synchronized = book is not None and book.stream_synchronized
            actual = (
                "SYNCHRONIZED"
                if synchronized
                else "UNAVAILABLE"
                if book is None
                else "UNSYNCHRONIZED"
            )
            add(
                code,
                label,
                synchronized if book is not None else None,
                actual,
                "==",
                "SYNCHRONIZED",
                f"{label} is unavailable or WebSocket-unsynchronized",
            )
        return tuple(checks)

    def _polymarket_taker_fee(self, code: str, shares: Decimal, price: Decimal) -> Decimal:
        parameters = self._polymarket_fee_parameters.get(code)
        if parameters is None:
            raise RuntimeError(f"Polymarket taker-fee parameters are unavailable for {code}")
        rate = parameters["rate"]
        exponent = parameters["exponent"]
        return shares * rate * ((price * (Decimal("1") - price)) ** exponent)

    def _polymarket_fee_parameter_status(
        self, now: datetime
    ) -> tuple[bool, float | None]:
        available = all(
            code in self._polymarket_fee_parameters for code in ("INC25", "INC50PLUS")
        )
        age_seconds = (
            (now - self._polymarket_fee_parameters_at).total_seconds()
            if self._polymarket_fee_parameters_at is not None
            else None
        )
        current = available and (
            age_seconds is not None
            and age_seconds
            <= max(60, self.settings.polymarket_book_snapshot_interval_seconds * 2)
        )
        return current, age_seconds

    def _qualify_quote(self, quote: Quote, *, generation: int) -> Quote:
        role = quote.role
        base_reasons: list[str] = []
        if quote.market_data_type != 1 or quote.quality is not DataQuality.LIVE:
            base_reasons.append("market data is not live")
        if quote.subscription_status is not SubscriptionStatus.ACTIVE:
            base_reasons.append("subscription is not active")
        if quote.subscription_generation != generation:
            base_reasons.append("subscription generation mismatch")
        if quote.farm_status is not FarmStatus.CONNECTED:
            base_reasons.append("US futures farm is not connected")
        if not quote.has_valid_two_sided_market:
            base_reasons.append("two-sided quote is incomplete or crossed")
        analytics_qualified = not base_reasons
        pretrade_qualified = role is QuoteRole.TARGET and not base_reasons
        if base_reasons:
            reason = "; ".join(base_reasons)
        elif role is QuoteRole.DIAGNOSTIC:
            reason = "diagnostic live subscription qualified; never execution-authorizing"
        else:
            reason = "current-generation live subscription qualified"
        return quote.model_copy(
            update={
                "analytics_qualified": analytics_qualified,
                "pretrade_qualified": pretrade_qualified,
                "validation_reason": reason,
            }
        )

    def _probability_comparisons(
        self,
        snapshot: EngineSnapshot,
        zq_probabilities: dict[str, Decimal],
        now: datetime,
    ) -> tuple[MarketProbabilityComparison, ...]:
        comparisons: list[MarketProbabilityComparison] = []
        for leg in self.settings.market_legs:
            book = snapshot.books.get(leg.yes_token_id)
            midpoint = book.midpoint if book is not None else None
            zq_probability = zq_probabilities.get(leg.code)
            comparisons.append(
                MarketProbabilityComparison(
                    code=leg.code,
                    label=leg.label,
                    zq_probability=zq_probability,
                    polymarket_bid=book.best_bid if book else None,
                    polymarket_bid_size=book.best_bid_size if book else None,
                    polymarket_ask=book.best_ask if book else None,
                    polymarket_ask_size=book.best_ask_size if book else None,
                    polymarket_mid=midpoint,
                    midpoint_gap=(
                        zq_probability - midpoint
                        if zq_probability is not None and midpoint is not None
                        else None
                    ),
                    book_age_ms=book.age_ms(now) if book else None,
                    stream_synchronized=book.stream_synchronized if book else False,
                    mapping_verified=snapshot.mapping.verified,
                )
            )
        return tuple(comparisons)

    def _gate_context(
        self,
        snapshot: EngineSnapshot,
        opportunity: Opportunity,
        *,
        now: datetime,
        books_fresh: bool,
        target_subscription_qualified: bool,
        effr_qualified: bool,
        cross_venue_checks: tuple[GateCheck, ...],
    ) -> GateContext:
        verification = snapshot.metadata.get("contract_verification") or {}
        contract_verified = bool(
            verification.get(self.settings.ibkr_zq_contract_month, {}).get("verified")
        )
        (
            margin_available,
            margin_actual,
            margin_detail,
            next_batch_margin,
        ) = self._margin_preview_qualification(snapshot, now)
        projected_excess = None
        if margin_available and next_batch_margin is not None:
            preview = snapshot.margin_preview
            if preview.projected_excess_liquidity is not None:
                projected_excess = preview.projected_excess_liquidity
            elif snapshot.account.full_excess_liquidity is not None:
                projected_excess = snapshot.account.full_excess_liquidity - next_batch_margin
        projected_cushion = None
        if (
            projected_excess is not None
            and snapshot.account.net_liquidation is not None
            and snapshot.account.net_liquidation > 0
        ):
            projected_cushion = projected_excess / snapshot.account.net_liquidation
        return GateContext(
            now=now,
            ibkr_status=snapshot.ibkr.status,
            polymarket_connected=snapshot.polymarket.status is ConnectionStatus.CONNECTED,
            mapping_verified=snapshot.mapping.verified,
            eligibility_checked=snapshot.eligibility.checked,
            eligibility_blocked=snapshot.eligibility.blocked,
            eligibility_country=snapshot.eligibility.country,
            polymarket_books_synchronized=books_fresh,
            target_subscription_qualified=target_subscription_qualified,
            effr_qualified=effr_qualified,
            cross_venue_snapshot_qualified=(
                target_subscription_qualified and effr_qualified and books_fresh
            ),
            contract_verified=contract_verified,
            full_hedge_depth_available=bool(opportunity.hedge_depth)
            and all(
                depth.sufficient and depth.marketable_limit_price is not None
                for depth in opportunity.hedge_depth
            ),
            margin_preview_available=margin_available,
            margin_preview_actual=margin_actual,
            margin_preview_detail=margin_detail,
            projected_full_excess_liquidity=projected_excess,
            projected_margin_cushion=projected_cushion,
            next_batch_initial_margin=next_batch_margin,
            current_zq_position=int(snapshot.metadata.get("zq_position") or 0),
            active_batches=int(snapshot.metadata.get("active_batches") or 0),
            unresolved_hedge=(
                int(snapshot.metadata.get("unresolved_hedge_obligations") or 0) > 0
                or any(
                    obligation.deficit_shares > 0
                    for obligation in snapshot.active_batch.obligations
                )
            ),
            reconciliation_clean=snapshot.reconciliation.clean,
            reconciliation_detail=snapshot.reconciliation.reason,
            critical_alert_active=any(
                alert.severity is AlertSeverity.CRITICAL
                and not alert.acknowledged
                and not alert.resolved
                for alert in snapshot.alerts
            ),
            paused=snapshot.paused,
            kill_switch=snapshot.kill_switch,
            cross_venue_checks=cross_venue_checks,
        )

    def _margin_preview_qualification(
        self, snapshot: EngineSnapshot, now: datetime
    ) -> tuple[bool | None, str, str, Decimal | None]:
        preview = snapshot.margin_preview
        age = preview.age_seconds(now)
        target = snapshot.quotes.get(self.settings.ibkr_zq_contract_month)
        matches = bool(
            preview.contract_month == self.settings.ibkr_zq_contract_month
            and preview.quantity == self.settings.ibkr_zq_child_order_quantity
            and preview.side.value == "BUY"
            and target is not None
            and target.bid is not None
            and preview.limit_price == target.bid
        )
        fresh = bool(age is not None and age <= self.settings.ibkr_margin_preview_max_age_seconds)
        available = preview.available and matches and fresh and not preview.warning_text
        actual = (
            f"raw-status={preview.status.value}; order={preview.order_id or 'none'}; "
            f"month={preview.contract_month or 'none'}; qty={preview.quantity or 'none'}; "
            f"side={preview.side.value}; age={age if age is not None else 'unavailable'}s; "
            f"limit={preview.limit_price}; target-limit={target.bid if target else None}; "
            f"initial-margin-change={preview.init_margin_change}"
        )
        if available:
            detail = (
                "matching BUY-10 ZQ what-if preview is available and current; "
                f"limit={preview.limit_price}, age={age}s"
            )
            return True, "CURRENT; " + actual, detail, preview.next_batch_initial_margin
        if preview.status in {
            MarginPreviewStatus.NOT_REQUESTED,
            MarginPreviewStatus.PENDING,
        }:
            return (
                None,
                (
                    "REFRESHING; " + actual
                    if preview.status is MarginPreviewStatus.PENDING
                    else "REFRESH_REQUIRED; " + actual
                ),
                preview.error or "IBKR BUY-10 ZQ what-if preview has not completed",
                None,
            )
        reasons: list[str] = []
        if not preview.available:
            reasons.append(preview.error or "IBKR preview did not return usable margin")
        if not matches:
            reasons.append(
                "preview does not match the configured BUY-10 September batch and current limit"
            )
        if not fresh:
            reasons.append(
                f"preview age exceeds {self.settings.ibkr_margin_preview_max_age_seconds}s"
            )
        if preview.warning_text:
            reasons.append(f"IBKR warning: {preview.warning_text}")
        status = "FAILED" if preview.status is MarginPreviewStatus.FAILED else "REFRESH_REQUIRED"
        return False, f"{status}; {actual}", "; ".join(reasons), None

    def _margin_preview_view(
        self,
        snapshot: EngineSnapshot,
        now: datetime,
    ) -> MarginPreview:
        available, _actual, detail, _margin = self._margin_preview_qualification(snapshot, now)
        preview = snapshot.margin_preview
        if available:
            status = MarginQualificationStatus.CURRENT
        elif preview.status is MarginPreviewStatus.PENDING:
            status = MarginQualificationStatus.REFRESHING
        elif preview.status is MarginPreviewStatus.FAILED:
            status = MarginQualificationStatus.FAILED
        elif preview.status is MarginPreviewStatus.NOT_REQUESTED:
            status = MarginQualificationStatus.NOT_REQUESTED
        else:
            status = MarginQualificationStatus.REFRESH_REQUIRED
        return preview.model_copy(
            update={
                "qualification_status": status,
                "qualified_for_next_batch": available is True,
                "qualification_detail": detail,
                "qualification_age_seconds": preview.age_seconds(now),
            }
        )

    def _book(self, snapshot: EngineSnapshot, leg_code: str, *, yes: bool) -> OrderBook | None:
        for leg in self.settings.market_legs:
            if leg.code == leg_code:
                return snapshot.books.get(leg.yes_token_id if yes else leg.no_token_id)
        return None

    @staticmethod
    def _metadata_decimal(snapshot: EngineSnapshot, key: str) -> Decimal | None:
        value: Any = snapshot.metadata.get(key)
        return Decimal(str(value)) if value is not None else None

    def request_margin_preview_refresh(self) -> None:
        self._margin_preview_refresh_requested.set()

    async def confirm_reconciliation(self, actor: str, reason: str) -> None:
        if self.settings.run_mode.is_live:
            raise ValueError("manual reconciliation cannot authorize a live run mode")
        snapshot = await self.state.get()
        if snapshot.ibkr.status is not ConnectionStatus.CONNECTED:
            raise ValueError("IBKR must be connected before reconciliation can be confirmed")
        if snapshot.polymarket.status is not ConnectionStatus.CONNECTED:
            raise ValueError("Polymarket must be connected before reconciliation can be confirmed")
        if snapshot.active_batch.state not in {BatchState.IDLE, BatchState.COMPLETE}:
            raise ValueError("the active batch must be terminal before reconciliation")
        unresolved_hedges = await self.repository.unresolved_hedge_obligation_count()
        if unresolved_hedges > 0:
            raise ValueError("hedge obligations remain unresolved")
        observed = {
            "snapshot_id": snapshot.snapshot_id,
            "ibkr_status": snapshot.ibkr.status.value,
            "polymarket_status": snapshot.polymarket.status.value,
            "zq_position": int(snapshot.metadata.get("zq_position") or 0),
            "active_batches": int(snapshot.metadata.get("active_batches") or 0),
            "unresolved_hedge_obligations": unresolved_hedges,
            "active_batch": snapshot.active_batch.model_dump(mode="json"),
            "synchronized_polymarket_books": sum(
                1 for book in snapshot.books.values() if book.stream_synchronized
            ),
        }
        await self.repository.record_reconciliation(
            actor=actor,
            reason=reason,
            expected={"unresolved_hedge_obligations": 0},
            observed=observed,
        )
        await self.state.confirm_reconciliation(
            actor=actor,
            reason=reason,
            snapshot_id=snapshot.snapshot_id,
        )

    async def audit_control(self, actor: str, action: str, reason: str) -> None:
        await self.repository.audit(actor=actor, action=action, reason=reason)
