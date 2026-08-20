from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import PolymarketAdapter
from zq_arb.analytics.payoff import CostInputs, build_three_state_opportunity
from zq_arb.analytics.probability import ReferencePrices, fedwatch_reference
from zq_arb.config import Settings
from zq_arb.domain.enums import AlertSeverity, ConnectionStatus, DataQuality
from zq_arb.domain.models import (
    EngineSnapshot,
    MarketProbabilityComparison,
    Opportunity,
    Quote,
    utc_now,
)
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
        self.polymarket = PolymarketAdapter(settings)
        self.risk = RiskEngine(settings)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        await self.database.initialize()
        await self.repository.ensure_config_version(self.settings)
        await self.repository.audit(
            actor="SYSTEM",
            action="ENGINE_START",
            reason="application startup",
            details={"run_mode": self.settings.run_mode.value},
        )
        self._tasks.extend(
            (
                asyncio.create_task(self._process_events(), name="venue-event-processor"),
                asyncio.create_task(self._analytics_loop(), name="analytics-loop"),
                asyncio.create_task(self._polymarket_reference_loop(), name="polymarket-reference"),
                asyncio.create_task(self._polymarket_stream_loop(), name="polymarket-stream"),
                asyncio.create_task(self._ibkr_connection_loop(), name="ibkr-connection"),
            )
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
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.ibkr.disconnect()
        await self.polymarket.close()
        await self.database.close()

    async def _process_events(self) -> None:
        while not self._stopping.is_set():
            event = await self.events.get()
            try:
                if event.venue == "IBKR":
                    await self.state.apply_ibkr_event(event)
                else:
                    await self.state.apply_polymarket_event(event)
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
                self.ibkr.request_contracts_and_market_data()
                self.ibkr.request_open_orders_and_executions()
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

    async def _polymarket_reference_loop(self) -> None:
        mapping_due = True
        eligibility_due = True
        last_mapping: datetime | None = None
        last_eligibility: datetime | None = None
        while not self._stopping.is_set():
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
                await self.state.set_books(books)
                await self.state.set_polymarket_health(
                    ConnectionStatus.CONNECTED,
                    "public CLOB snapshots current",
                    authenticated=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("polymarket_reference_failed", error=str(exc))
                await self.state.set_polymarket_health(
                    ConnectionStatus.DEGRADED,
                    f"public CLOB snapshot failed: {type(exc).__name__}",
                )
            await asyncio.sleep(self.settings.polymarket_book_snapshot_interval_seconds)

    async def _polymarket_stream_loop(self) -> None:
        token_ids = [
            token_id
            for leg in self.settings.market_legs
            for token_id in (leg.yes_token_id, leg.no_token_id)
        ]
        attempts = 0
        while not self._stopping.is_set():
            try:
                async for event in self.polymarket.public_market_stream(token_ids):
                    attempts = 0
                    await self.events.put(event)
                    token_id = str(
                        event.payload.get("asset_id")
                        or event.payload.get("token_id")
                        or event.payload.get("assetId")
                        or ""
                    )
                    if token_id in token_ids and event.kind.lower() in {
                        "book",
                        "price_change",
                        "tick_size_change",
                    }:
                        book = await self.polymarket.fetch_book(token_id)
                        await self.state.set_books((book,))
                raise ConnectionError("Polymarket market stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                LOGGER.warning("polymarket_stream_failed", attempt=attempts, error=str(exc))
                if attempts >= self.settings.polymarket_reconnect_max_attempts:
                    await self.state.set_polymarket_health(
                        ConnectionStatus.DEGRADED,
                        "market stream unavailable; REST snapshots remain active",
                    )
                    return
                await asyncio.sleep(self.settings.polymarket_reconnect_backoff_seconds)

    async def _analytics_loop(self) -> None:
        interval = self.settings.engine_state_publish_interval_ms / 1_000
        while not self._stopping.is_set():
            try:
                current = await self.state.get()
                calculated = self._calculate(current)
                if calculated != current:
                    await self.state.replace(calculated)
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
        months = self.settings.reference_contract_months
        quotes = [snapshot.quotes.get(month) for month in months]
        if any(quote is None or _mid(quote) is None for quote in quotes):
            return snapshot.model_copy(
                update={
                    "probability_comparisons": self._probability_comparisons(
                        snapshot,
                        {},
                        now,
                    ),
                    "health_messages": ("Awaiting all four ZQ reference quotes",),
                }
            )
        complete_quotes = [quote for quote in quotes if quote is not None]
        mids = [_mid(quote) for quote in complete_quotes]
        if any(value is None for value in mids):
            return snapshot
        prices = [value for value in mids if value is not None]
        probabilities = fedwatch_reference(ReferencePrices(*prices))

        quotes_fresh = all(
            quote.age_ms(now) <= self.settings.max_quote_age_ms for quote in complete_quotes
        )
        target_quote = snapshot.quotes[self.settings.ibkr_zq_contract_month]
        long_book_25 = self._book(snapshot, "INC25", yes=True)
        long_book_50 = self._book(snapshot, "INC50PLUS", yes=True)
        short_book_25 = self._book(snapshot, "INC25", yes=False)
        short_book_50 = self._book(snapshot, "INC50PLUS", yes=False)
        relevant_books = (long_book_25, long_book_50, short_book_25, short_book_50)
        books_available = all(book is not None for book in relevant_books)
        books_fresh = books_available and all(
            book is not None and book.age_ms(now) <= self.settings.max_quote_age_ms
            for book in relevant_books
        )
        opportunities: list[Opportunity] = []
        incremental_margin = self._metadata_decimal(
            snapshot, "next_batch_initial_margin"
        ) or Decimal("0")
        emergency_reserve = Decimal("0")
        costs = CostInputs(
            model_reserve=self.settings.model_risk_reserve_usd,
            operational_reserve=self.settings.operational_risk_reserve_usd,
            effr_basis_reserve=self.settings.effr_basis_reserve_usd,
        )
        if probabilities.september_start_effr is not None and books_available:
            candidates = (
                ("LONG", target_quote.ask, long_book_25, long_book_50),
                ("SHORT", target_quote.bid, short_book_25, short_book_50),
            )
            for direction, price, book25, book50 in candidates:
                if price is None or book25 is None or book50 is None:
                    continue
                raw = build_three_state_opportunity(
                    direction=direction,
                    contracts=self.settings.ibkr_zq_child_order_quantity,
                    zq_price=price,
                    pre_meeting_effr=probabilities.september_start_effr,
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
                    quotes_fresh=quotes_fresh,
                    books_fresh=books_fresh,
                )
                qualification = self.risk.qualify(raw, context)
                opportunities.append(
                    raw.model_copy(
                        update={
                            "tradeable": qualification.tradeable,
                            "gate_reasons": qualification.reasons,
                        }
                    )
                )
        health: list[str] = []
        if not quotes_fresh:
            health.append("ZQ quotes are incomplete or stale")
        if not books_fresh:
            health.append("Polymarket hedge books are incomplete or stale")
        if snapshot.mapping.errors:
            health.extend(snapshot.mapping.errors)
        return snapshot.model_copy(
            update={
                "probabilities": probabilities,
                "probability_comparisons": self._probability_comparisons(
                    snapshot,
                    probabilities.bucket_probabilities,
                    now,
                ),
                "opportunities": tuple(opportunities),
                "health_messages": tuple(dict.fromkeys(health)),
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
                    polymarket_ask=book.best_ask if book else None,
                    polymarket_mid=midpoint,
                    midpoint_gap=(
                        zq_probability - midpoint
                        if zq_probability is not None and midpoint is not None
                        else None
                    ),
                    book_age_ms=book.age_ms(now) if book else None,
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
        quotes_fresh: bool,
        books_fresh: bool,
    ) -> GateContext:
        verification = snapshot.metadata.get("contract_verification") or {}
        contract_verified = bool(
            verification.get(self.settings.ibkr_zq_contract_month, {}).get("verified")
        )
        timestamps = [quote.received_at for quote in snapshot.quotes.values()]
        timestamps.extend(book.received_at for book in snapshot.books.values())
        skew_ms = (
            int((max(timestamps) - min(timestamps)).total_seconds() * 1_000)
            if timestamps
            else self.settings.max_cross_venue_timestamp_skew_ms + 1
        )
        return GateContext(
            now=now,
            ibkr_connected=snapshot.ibkr.status is ConnectionStatus.CONNECTED,
            ibkr_data_live=all(
                quote.quality is DataQuality.LIVE for quote in snapshot.quotes.values()
            ),
            polymarket_connected=snapshot.polymarket.status is ConnectionStatus.CONNECTED,
            mapping_verified=snapshot.mapping.verified,
            eligibility_checked=snapshot.eligibility.checked,
            eligibility_blocked=snapshot.eligibility.blocked,
            eligibility_country=snapshot.eligibility.country,
            books_fresh=books_fresh,
            quotes_fresh=quotes_fresh,
            cross_venue_synchronized=skew_ms <= self.settings.max_cross_venue_timestamp_skew_ms,
            contract_verified=contract_verified,
            full_hedge_depth_available=not opportunity.gate_reasons,
            margin_preview_available=bool(snapshot.metadata.get("margin_preview_available")),
            projected_full_excess_liquidity=snapshot.account.full_excess_liquidity,
            projected_margin_cushion=snapshot.account.cushion,
            next_batch_initial_margin=self._metadata_decimal(snapshot, "next_batch_initial_margin"),
            current_zq_position=int(snapshot.metadata.get("zq_position") or 0),
            active_batches=int(snapshot.metadata.get("active_batches") or 0),
            unresolved_hedge=any(
                obligation.deficit_shares > 0 for obligation in snapshot.active_batch.obligations
            ),
            reconciliation_clean=bool(snapshot.metadata.get("reconciliation_clean")),
            critical_alert_active=any(
                alert.severity is AlertSeverity.CRITICAL and not alert.acknowledged
                for alert in snapshot.alerts
            ),
            paused=snapshot.paused,
            kill_switch=snapshot.kill_switch,
            strategy_daily_pnl=snapshot.account.daily_pnl,
            strategy_drawdown=None,
        )

    def _book(self, snapshot: EngineSnapshot, leg_code: str, *, yes: bool) -> Any:
        for leg in self.settings.market_legs:
            if leg.code == leg_code:
                return snapshot.books.get(leg.yes_token_id if yes else leg.no_token_id)
        return None

    @staticmethod
    def _metadata_decimal(snapshot: EngineSnapshot, key: str) -> Decimal | None:
        value: Any = snapshot.metadata.get(key)
        return Decimal(str(value)) if value is not None else None

    async def audit_control(self, actor: str, action: str, reason: str) -> None:
        await self.repository.audit(actor=actor, action=action, reason=reason)
