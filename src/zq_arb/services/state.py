from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.polymarket import update_books_from_stream_event
from zq_arb.config import Settings
from zq_arb.domain.enums import (
    AlertSeverity,
    ConnectionStatus,
    DataQuality,
    FarmStatus,
    MarginPreviewStatus,
    QuoteRole,
    RunMode,
    SubscriptionStatus,
)
from zq_arb.domain.models import (
    AccountMetrics,
    AlertView,
    EffrObservation,
    EligibilityStatus,
    EngineSnapshot,
    IbkrFarmHealth,
    MarginPreview,
    MarketMappingStatus,
    OrderBook,
    Quote,
    ReconciliationStatusView,
    StrategyRiskView,
    VenueHealth,
    utc_now,
)


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


class StateStore:
    """Single-writer state facade with bounded fan-out for dashboard consumers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        if settings.effr_source == "MANUAL":
            manual_rate = settings.pre_meeting_effr_percent
            effr = EffrObservation(
                source="MANUAL",
                rate_percent=manual_rate,
                fetched_at=utc_now(),
                valid=manual_rate is not None,
                reason=(
                    "explicit manual EFFR override from environment"
                    if manual_rate is not None
                    else "EFFR_SOURCE is MANUAL but PRE_MEETING_EFFR_PERCENT is absent"
                ),
            )
        else:
            effr = EffrObservation()
        self._snapshot = EngineSnapshot(
            software_version=settings.software_version,
            config_version=settings.config_version,
            strategy_version=settings.strategy_version,
            run_mode=settings.run_mode,
            effr=effr,
            ibkr_farms={
                "US_FUTURES": IbkrFarmHealth(name="usfuture", service="MARKET_DATA"),
                "HMDS": IbkrFarmHealth(name="hmds", service="HISTORICAL_DATA"),
                "SECDEF": IbkrFarmHealth(name="secdef", service="SECURITY_DEFINITION"),
            },
            metadata={
                "contract_verification": {},
                "ibkr_market_data_type": None,
                "ibkr_subscription_generation": 0,
                "ibkr_generation_preallocated": False,
                "ibkr_resubscribe_required": False,
                "zq_position": 0,
                "active_batches": 0,
                "reconciliation_clean": False,
                "ibkr_connectivity_recovery_pending": False,
                "drawdown_halt_active": False,
            },
            strategy_risk=StrategyRiskView(
                allocated_capital=settings.strategy_allocated_capital_usd,
                equity=settings.strategy_allocated_capital_usd,
                high_water_mark=settings.strategy_allocated_capital_usd,
            ),
        )
        self._subscribers: set[asyncio.Queue[EngineSnapshot]] = set()
        self._account_values: dict[str, Decimal] = {}
        self._month_data_types: dict[str, int] = {}

    async def get(self) -> EngineSnapshot:
        async with self._lock:
            return self._snapshot.model_copy(deep=True)

    async def replace(self, snapshot: EngineSnapshot) -> EngineSnapshot:
        async with self._lock:
            self._snapshot = snapshot.model_copy(
                update={
                    "snapshot_id": self._snapshot.snapshot_id + 1,
                    "generated_at": utc_now(),
                },
                deep=True,
            )
            subscribers = tuple(self._subscribers)
            published = self._snapshot
        self._fan_out(published, subscribers)
        return published

    @staticmethod
    def _fan_out(
        published: EngineSnapshot,
        subscribers: tuple[asyncio.Queue[EngineSnapshot], ...],
    ) -> None:
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(published)
            except asyncio.QueueFull:
                pass

    async def update(self, updater: Callable[[EngineSnapshot], EngineSnapshot]) -> EngineSnapshot:
        async with self._lock:
            current = self._snapshot.model_copy(deep=True)
            updated = updater(current)
            self._snapshot = updated.model_copy(
                update={
                    "snapshot_id": self._snapshot.snapshot_id + 1,
                    "generated_at": utc_now(),
                },
                deep=True,
            )
            subscribers = tuple(self._subscribers)
            published = self._snapshot
        self._fan_out(published, subscribers)
        return published

    async def subscribe(self) -> AsyncGenerator[EngineSnapshot, None]:
        queue: asyncio.Queue[EngineSnapshot] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
            initial = self._snapshot
        await queue.put(initial)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def set_ibkr_health(
        self, status: ConnectionStatus, message: str, *, authenticated: bool = False
    ) -> None:
        now = utc_now()

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            reconnects = snapshot.ibkr.reconnect_count
            if status is ConnectionStatus.CONNECTING and snapshot.ibkr.status not in {
                ConnectionStatus.DISCONNECTED,
                ConnectionStatus.CONNECTING,
            }:
                reconnects += 1
            return snapshot.model_copy(
                update={
                    "ibkr": VenueHealth(
                        status=status,
                        authenticated=authenticated,
                        message=message,
                        reconnect_count=reconnects,
                        last_message_at=now,
                    )
                }
            )

        await self.update(apply)

    async def set_polymarket_health(
        self, status: ConnectionStatus, message: str, *, authenticated: bool = False
    ) -> None:
        now = utc_now()

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            reconnects = snapshot.polymarket.reconnect_count
            if status is ConnectionStatus.CONNECTING and snapshot.polymarket.status not in {
                ConnectionStatus.DISCONNECTED,
                ConnectionStatus.CONNECTING,
            }:
                reconnects += 1
            return snapshot.model_copy(
                update={
                    "polymarket": VenueHealth(
                        status=status,
                        authenticated=authenticated,
                        message=message,
                        reconnect_count=reconnects,
                        last_message_at=now,
                    )
                }
            )

        await self.update(apply)

    async def set_mapping(self, mapping: MarketMappingStatus) -> None:
        await self.update(lambda snapshot: snapshot.model_copy(update={"mapping": mapping}))

    async def set_eligibility(self, eligibility: EligibilityStatus) -> None:
        await self.update(lambda snapshot: snapshot.model_copy(update={"eligibility": eligibility}))

    async def set_effr(self, effr: EffrObservation) -> None:
        await self.update(lambda snapshot: snapshot.model_copy(update={"effr": effr}))

    async def set_books(self, books: tuple[OrderBook, ...]) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            updated = dict(snapshot.books)
            updated.update({book.token_id: book for book in books})
            return snapshot.model_copy(update={"books": updated})

        await self.update(apply)

    async def mark_polymarket_books_unsynchronized(self) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            books = {
                token_id: book.model_copy(update={"stream_synchronized": False})
                for token_id, book in snapshot.books.items()
            }
            return snapshot.model_copy(update={"books": books})

        await self.update(apply)

    async def reconcile_polymarket_books(
        self, rest_books: tuple[OrderBook, ...]
    ) -> tuple[str, ...]:
        mismatches: list[str] = []

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            updated = dict(snapshot.books)
            for rest_book in rest_books:
                current = updated.get(rest_book.token_id)
                if current is None or not current.stream_synchronized:
                    updated[rest_book.token_id] = rest_book
                    continue
                same_hash = bool(
                    current.book_hash
                    and rest_book.book_hash
                    and current.book_hash == rest_book.book_hash
                )
                same_content = current.bids == rest_book.bids and current.asks == rest_book.asks
                rest_is_older = bool(
                    current.source_timestamp
                    and rest_book.source_timestamp
                    and rest_book.source_timestamp < current.source_timestamp
                )
                if same_hash or same_content or rest_is_older:
                    updated[rest_book.token_id] = current.model_copy(
                        update={
                            "market": current.market or rest_book.market,
                            "tick_size": rest_book.tick_size or current.tick_size,
                            "min_order_size": (
                                rest_book.min_order_size or current.min_order_size
                            ),
                            "negative_risk": (
                                rest_book.negative_risk
                                if rest_book.negative_risk is not None
                                else current.negative_risk
                            ),
                            "last_reconciled_at": rest_book.last_reconciled_at,
                        }
                    )
                    continue
                mismatches.append(rest_book.token_id)
                updated[rest_book.token_id] = rest_book
            return snapshot.model_copy(update={"books": updated})

        await self.update(apply)
        return tuple(mismatches)

    async def add_alert(
        self,
        severity: AlertSeverity,
        code: str,
        message: str,
        *,
        flashing: bool = False,
    ) -> AlertView:
        alert = AlertView(
            alert_id=str(uuid4()),
            severity=severity,
            code=code,
            message=message,
            flashing=flashing,
        )

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            existing = tuple(item for item in snapshot.alerts if item.code != code)
            return snapshot.model_copy(update={"alerts": (alert, *existing)[:100]})

        await self.update(apply)
        return alert

    async def resolve_alerts(self, code_prefix: str) -> None:
        now = utc_now()

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            alerts = tuple(
                alert.model_copy(update={"resolved": True, "resolved_at": now, "flashing": False})
                if alert.code.startswith(code_prefix) and not alert.resolved
                else alert
                for alert in snapshot.alerts
            )
            return snapshot.model_copy(update={"alerts": alerts})

        await self.update(apply)

    async def begin_ibkr_subscriptions(self, *, advance_generation: bool = True) -> int:
        """Start a generation that requires a new complete streaming bid/ask."""

        generation = 0
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            nonlocal generation
            self._month_data_types.clear()
            metadata = deepcopy(snapshot.metadata)
            generation = int(metadata.get("ibkr_subscription_generation") or 0)
            if advance_generation:
                generation += 1
            metadata["ibkr_subscription_generation"] = generation
            metadata["ibkr_generation_preallocated"] = False
            quotes = {
                month: quote.model_copy(
                    update={
                        "subscription_status": SubscriptionStatus.PENDING_REVALIDATION,
                        "subscription_generation": generation,
                        "bid": None,
                        "ask": None,
                        "last": None,
                        "source_timestamp": None,
                        "bid_size": None,
                        "ask_size": None,
                        "quality": DataQuality.UNKNOWN,
                        "market_data_type": None,
                        "last_market_data_event_at": None,
                        "last_price_change_at": None,
                        "analytics_qualified": False,
                        "pretrade_qualified": False,
                        "validation_reason": "awaiting complete current-generation live bid/ask",
                    }
                )
                for month, quote in snapshot.quotes.items()
            }
            return snapshot.model_copy(update={"metadata": metadata, "quotes": quotes})

        await self.update(apply)
        return generation

    async def invalidate_ibkr_subscriptions(
        self,
        status: SubscriptionStatus,
        reason: str,
        *,
        increment_generation: bool = False,
    ) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            metadata = deepcopy(snapshot.metadata)
            generation = int(metadata.get("ibkr_subscription_generation") or 0)
            if increment_generation:
                generation += 1
                metadata["ibkr_subscription_generation"] = generation
                metadata["ibkr_generation_preallocated"] = True
            quotes = {
                month: quote.model_copy(
                    update={
                        "subscription_status": status,
                        "subscription_generation": generation,
                        "analytics_qualified": False,
                        "pretrade_qualified": False,
                        "validation_reason": reason,
                    }
                )
                for month, quote in snapshot.quotes.items()
            }
            return snapshot.model_copy(update={"metadata": metadata, "quotes": quotes})

        await self.update(apply)

    async def set_ibkr_resubscribe_required(self, required: bool) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            metadata = deepcopy(snapshot.metadata)
            metadata["ibkr_resubscribe_required"] = required
            return snapshot.model_copy(update={"metadata": metadata})

        await self.update(apply)

    async def acknowledge_alert(self, alert_id: str) -> bool:
        found = False

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            nonlocal found
            alerts: list[AlertView] = []
            for alert in snapshot.alerts:
                if alert.alert_id == alert_id:
                    found = True
                    alerts.append(
                        alert.model_copy(update={"acknowledged": True, "flashing": False})
                    )
                else:
                    alerts.append(alert)
            return snapshot.model_copy(update={"alerts": tuple(alerts)})

        await self.update(apply)
        return found

    async def apply_ibkr_event(self, event: VenueEvent) -> None:
        if event.kind == "connection":
            status_name = str(event.payload.get("status") or "DISCONNECTED")
            status = ConnectionStatus(status_name)
            await self.set_ibkr_health(
                status,
                f"TWS {status.value.lower()}",
                authenticated=status is ConnectionStatus.CONNECTED,
            )
            await self._set_metadata_values(
                ibkr_connectivity_recovery_pending=(status is not ConnectionStatus.CONNECTED)
            )
            if status is ConnectionStatus.DISCONNECTED:
                await self.invalidate_ibkr_subscriptions(
                    SubscriptionStatus.DISCONNECTED,
                    "TWS socket disconnected",
                )
                await self.invalidate_reconciliation("TWS socket disconnected")
            return
        if event.kind == "market_data_type":
            data_type = int(event.payload.get("market_data_type") or 0)
            month = str(event.payload.get("month") or "")
            quality = {
                1: DataQuality.LIVE,
                2: DataQuality.FROZEN,
                3: DataQuality.DELAYED,
                4: DataQuality.FROZEN,
            }.get(data_type, DataQuality.UNKNOWN)
            if month:
                self._month_data_types[month] = data_type

            def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
                metadata = deepcopy(snapshot.metadata)
                metadata["ibkr_market_data_type"] = data_type
                metadata["ibkr_data_quality"] = quality.value
                quotes = dict(snapshot.quotes)
                if month and month in quotes:
                    quote = quotes[month].model_copy(
                        update={"market_data_type": data_type, "quality": quality}
                    )
                    generation = int(metadata.get("ibkr_subscription_generation") or 0)
                    if (
                        data_type == 1
                        and quote.farm_status is FarmStatus.CONNECTED
                        and quote.subscription_generation == generation
                        and quote.has_valid_two_sided_market
                    ):
                        quote = quote.model_copy(
                            update={
                                "subscription_status": SubscriptionStatus.ACTIVE,
                                "validation_reason": (
                                    "current-generation live subscription has a complete "
                                    "uncrossed bid/ask"
                                ),
                            }
                        )
                    quotes[month] = quote
                return snapshot.model_copy(update={"metadata": metadata, "quotes": quotes})

            await self.update(apply)
            return
        if event.kind == "contract_details":
            month = str(event.payload.get("month") or "")

            def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
                metadata = deepcopy(snapshot.metadata)
                verified = dict(metadata.get("contract_verification") or {})
                verified[month] = {
                    "verified": bool(event.payload.get("verified")),
                    "contract_id": event.payload.get("contract_id"),
                    "local_symbol": event.payload.get("local_symbol"),
                    "errors": event.payload.get("errors") or [],
                }
                metadata["contract_verification"] = verified
                return snapshot.model_copy(update={"metadata": metadata})

            await self.update(apply)
            return
        if event.kind in {"tick_price", "tick_size"}:
            await self._apply_quote_event(event)
            return
        if event.kind == "margin_preview_requested":
            await self._set_margin_preview_pending(event)
            return
        if event.kind == "margin_preview":
            await self._apply_margin_preview(event)
            return
        if event.kind == "account_summary":
            await self._apply_account_summary(event)
            return
        if event.kind == "pnl":
            await self._apply_pnl(event)
            return
        if event.kind in {"open_order", "order_status", "execution"}:
            await self.invalidate_reconciliation(
                f"IBKR {event.kind.replace('_', ' ')} event requires operator reconciliation"
            )
            return
        if event.kind == "configuration_warning":
            await self.add_alert(
                AlertSeverity.WARNING,
                str(event.payload.get("code") or "IBKR_CONFIGURATION_WARNING"),
                "IBKR_ACCOUNT_ID must be configured for account-specific P&L",
            )
            return
        if event.kind == "error":
            await self._apply_ibkr_error(event)

    async def _apply_quote_event(self, event: VenueEvent) -> None:
        month = str(event.payload.get("month") or "")
        if not month:
            return
        tick_type_value = event.payload.get("tick_type")
        tick_type = int(tick_type_value) if tick_type_value is not None else -1
        if event.kind == "tick_price":
            field = {1: "bid", 2: "ask", 4: "last", 66: "bid", 67: "ask", 68: "last"}.get(tick_type)
        else:
            field = {0: "bid_size", 3: "ask_size", 69: "bid_size", 70: "ask_size"}.get(tick_type)
        value = _decimal(event.payload.get("price") or event.payload.get("size"))
        if field is None or value is None or value < 0:
            return
        current = await self.get()
        existing = current.quotes.get(month)
        data_type_value = (
            self._month_data_types.get(month)
            or (existing.market_data_type if existing else None)
            or current.metadata.get("ibkr_market_data_type")
        )
        data_type = int(data_type_value) if data_type_value is not None else None
        quality_by_type: dict[int, DataQuality] = {
            1: DataQuality.LIVE,
            2: DataQuality.FROZEN,
            3: DataQuality.DELAYED,
            4: DataQuality.FROZEN,
        }
        quality = quality_by_type.get(data_type or 0, DataQuality.UNKNOWN)
        farm_status = self._zq_farm_status(current)
        generation = int(current.metadata.get("ibkr_subscription_generation") or 0)
        role = self._quote_role(month)

        base = existing or Quote(instrument=month, role=role)
        previous_value = getattr(base, field)
        updates = {
            field: value,
            "received_at": event.received_at,
            "source_timestamp": event.source_timestamp,
            "last_market_data_event_at": event.received_at,
            "market_data_type": data_type,
            "quality": quality,
            "role": role,
            "subscription_generation": generation,
            "farm_status": farm_status,
            "subscription_status": (
                base.subscription_status
                if existing is not None
                else SubscriptionStatus.PENDING_REVALIDATION
            ),
        }
        if field in {"bid", "ask", "last"} and (
            previous_value != value or base.last_price_change_at is None
        ):
            updates["last_price_change_at"] = event.received_at
        quote = base.model_copy(update=updates)
        valid = (
            quality is DataQuality.LIVE
            and farm_status is FarmStatus.CONNECTED
            and quote.has_valid_two_sided_market
        )
        if valid:
            quote = quote.model_copy(
                update={
                    "subscription_status": SubscriptionStatus.ACTIVE,
                    "validation_reason": (
                        "current-generation live subscription has a complete uncrossed bid/ask"
                    ),
                }
            )

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            quotes = dict(snapshot.quotes)
            quotes[month] = quote
            return snapshot.model_copy(update={"quotes": quotes})

        await self.update(apply)

    async def _set_margin_preview_pending(self, event: VenueEvent) -> None:
        preview = MarginPreview(
            status=MarginPreviewStatus.PENDING,
            order_id=int(event.payload["order_id"]),
            contract_month=str(event.payload["contract_month"]),
            quantity=int(event.payload["quantity"]),
            limit_price=_decimal(event.payload.get("limit_price")),
            requested_at=event.received_at,
        )
        await self.update(
            lambda snapshot: snapshot.model_copy(update={"margin_preview": preview})
        )

    async def _apply_margin_preview(self, event: VenueEvent) -> None:
        order_id = int(event.payload["order_id"])
        current = await self.get()
        if current.margin_preview.order_id != order_id:
            return
        fields = {
            name: self._margin_decimal(event.payload.get(name))
            for name in (
                "init_margin_before",
                "init_margin_change",
                "init_margin_after",
                "maintenance_margin_before",
                "maintenance_margin_change",
                "maintenance_margin_after",
                "equity_with_loan_before",
                "equity_with_loan_change",
                "equity_with_loan_after",
                "commission",
            )
        }
        init_change = fields["init_margin_change"]
        error = None if init_change is not None else "IBKR returned no usable initial-margin change"
        preview = MarginPreview(
            status=(
                MarginPreviewStatus.AVAILABLE
                if init_change is not None
                else MarginPreviewStatus.FAILED
            ),
            order_id=order_id,
            contract_month=str(event.payload["contract_month"]),
            quantity=int(event.payload["quantity"]),
            limit_price=_decimal(event.payload.get("limit_price")),
            commission_currency=str(event.payload.get("commission_currency") or "") or None,
            warning_text=str(event.payload.get("warning_text") or "") or None,
            error=error,
            requested_at=current.margin_preview.requested_at,
            received_at=event.received_at,
            **fields,
        )
        await self.update(
            lambda snapshot: snapshot.model_copy(update={"margin_preview": preview})
        )

    async def fail_margin_preview(self, order_id: int | None, error: str) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            current = snapshot.margin_preview
            if order_id is not None and current.order_id not in {None, order_id}:
                return snapshot
            return snapshot.model_copy(
                update={
                    "margin_preview": current.model_copy(
                        update={
                            "status": MarginPreviewStatus.FAILED,
                            "order_id": order_id or current.order_id,
                            "error": error,
                            "received_at": utc_now(),
                        }
                    )
                }
            )

        await self.update(apply)

    @staticmethod
    def _margin_decimal(value: Any) -> Decimal | None:
        number = _decimal(str(value).replace(",", ""))
        if number is None or abs(number) >= Decimal("1e100"):
            return None
        return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _quote_role(self, month: str) -> QuoteRole:
        if month == self.settings.ibkr_zq_contract_month:
            return QuoteRole.TARGET
        return QuoteRole.DIAGNOSTIC

    @staticmethod
    def _zq_farm_status(snapshot: EngineSnapshot) -> FarmStatus:
        farm = snapshot.ibkr_farms.get("US_FUTURES")
        return farm.status if farm else FarmStatus.UNKNOWN

    async def _apply_account_summary(self, event: VenueEvent) -> None:
        tag = str(event.payload.get("tag") or "")
        value = _decimal(event.payload.get("value"))
        if value is not None:
            self._account_values[tag] = value
        current = await self.get()
        fields = {
            "net_liquidation": "NetLiquidation",
            "total_cash_value": "TotalCashValue",
            "init_margin": "InitMarginReq",
            "maintenance_margin": "MaintMarginReq",
            "available_funds": "AvailableFunds",
            "excess_liquidity": "ExcessLiquidity",
            "full_init_margin": "FullInitMarginReq",
            "full_maintenance_margin": "FullMaintMarginReq",
            "full_available_funds": "FullAvailableFunds",
            "full_excess_liquidity": "FullExcessLiquidity",
            "cushion": "Cushion",
        }
        updates: dict[str, Any] = {
            field: self._account_values.get(account_tag) for field, account_tag in fields.items()
        }
        updates.update(
            {
                "account_fingerprint": event.payload.get("account_fingerprint"),
                "daily_pnl": current.account.daily_pnl,
                "unrealized_pnl": current.account.unrealized_pnl,
                "realized_pnl": current.account.realized_pnl,
                "futures_pnl": current.account.futures_pnl,
                "received_at": event.received_at,
            }
        )
        account = AccountMetrics(**updates)
        await self.update(lambda snapshot: snapshot.model_copy(update={"account": account}))

    async def _apply_pnl(self, event: VenueEvent) -> None:
        current = await self.get()
        account = current.account.model_copy(
            update={
                "daily_pnl": _decimal(event.payload.get("daily_pnl")),
                "unrealized_pnl": _decimal(event.payload.get("unrealized_pnl")),
                "realized_pnl": _decimal(event.payload.get("realized_pnl")),
                "received_at": event.received_at,
            }
        )
        await self.update(lambda snapshot: snapshot.model_copy(update={"account": account}))

    async def _set_ibkr_farm(
        self,
        key: str,
        *,
        name: str,
        service: str,
        status: FarmStatus,
        message: str,
    ) -> None:
        now = utc_now()

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            farms = dict(snapshot.ibkr_farms)
            prior = farms.get(key)
            farms[key] = IbkrFarmHealth(
                name=name,
                service=service,
                status=status,
                message=message,
                current=status is FarmStatus.DISCONNECTED,
                last_changed_at=now,
            )
            quotes = dict(snapshot.quotes)
            if key == "US_FUTURES":
                generation = int(snapshot.metadata.get("ibkr_subscription_generation") or 0)
                startup_connection = bool(
                    status is FarmStatus.CONNECTED
                    and (prior is None or prior.status is FarmStatus.UNKNOWN)
                )
                updated_quotes: dict[str, Quote] = {}
                for month, quote in quotes.items():
                    updated = quote.model_copy(update={"farm_status": status})
                    if (
                        startup_connection
                        and updated.market_data_type == 1
                        and updated.subscription_generation == generation
                        and updated.has_valid_two_sided_market
                    ):
                        updated = updated.model_copy(
                            update={
                                "subscription_status": SubscriptionStatus.ACTIVE,
                                "validation_reason": (
                                    "current-generation live subscription has a complete "
                                    "uncrossed bid/ask"
                                ),
                            }
                        )
                    updated_quotes[month] = updated
                quotes = updated_quotes
            return snapshot.model_copy(update={"ibkr_farms": farms, "quotes": quotes})

        await self.update(apply)

    @staticmethod
    def _farm_name(message: str) -> str:
        return message.rsplit(":", 1)[-1].strip().lower() if ":" in message else "unknown"

    async def _apply_ibkr_error(self, event: VenueEvent) -> None:
        code = int(event.payload.get("code") or 0)
        message = str(event.payload.get("message") or "unknown TWS error")
        if bool(event.payload.get("margin_preview")):
            await self.fail_margin_preview(
                int(event.payload.get("request_id") or 0) or None,
                f"IBKR what-if error {code}: {message}",
            )
            return
        farm_name = self._farm_name(message)
        relevant_zq_farm = farm_name.startswith("usfuture")

        if code in {2103, 2104}:
            alert_code = f"IBKR_2103_{farm_name.upper()}"
            if relevant_zq_farm:
                prior = (await self.get()).ibkr_farms.get("US_FUTURES")
                status = FarmStatus.DISCONNECTED if code == 2103 else FarmStatus.CONNECTED
                await self._set_ibkr_farm(
                    "US_FUTURES",
                    name=farm_name,
                    service="MARKET_DATA",
                    status=status,
                    message=message,
                )
                if code == 2103:
                    await self.invalidate_ibkr_subscriptions(
                        SubscriptionStatus.SUSPECT,
                        "US futures market-data farm disconnected",
                    )
                elif prior is not None and prior.status is FarmStatus.DISCONNECTED:
                    await self.invalidate_ibkr_subscriptions(
                        SubscriptionStatus.PENDING_REVALIDATION,
                        "US futures farm recovered; awaiting a new streaming subscription",
                    )
                    await self.set_ibkr_resubscribe_required(True)
            if code == 2103:
                await self.add_alert(AlertSeverity.WARNING, alert_code, message)
            else:
                await self.resolve_alerts(alert_code)
            return
        if code in {2105, 2106}:
            status = FarmStatus.DISCONNECTED if code == 2105 else FarmStatus.CONNECTED
            await self._set_ibkr_farm(
                "HMDS",
                name=farm_name,
                service="HISTORICAL_DATA",
                status=status,
                message=message,
            )
            if code == 2105:
                await self.add_alert(AlertSeverity.WARNING, "IBKR_2105_HMDS", message)
            else:
                await self.resolve_alerts("IBKR_2105_HMDS")
            return
        if code in {2157, 2158}:
            status = FarmStatus.DISCONNECTED if code == 2157 else FarmStatus.CONNECTED
            await self._set_ibkr_farm(
                "SECDEF",
                name=farm_name,
                service="SECURITY_DEFINITION",
                status=status,
                message=message,
            )
            if code == 2157:
                await self.add_alert(AlertSeverity.WARNING, "IBKR_2157_SECDEF", message)
            else:
                await self.resolve_alerts("IBKR_2157_SECDEF")
            return
        if code == 1100:
            await self._set_metadata_values(ibkr_connectivity_recovery_pending=True)
            await self.invalidate_reconciliation("IBKR connectivity was lost")
            await self.invalidate_ibkr_subscriptions(
                SubscriptionStatus.DISCONNECTED,
                "IBKR connectivity lost",
                increment_generation=True,
            )
        elif code in {1101, 1102}:
            await self.set_ibkr_health(
                ConnectionStatus.CONNECTED,
                message,
                authenticated=True,
            )
            await self._set_metadata_values(ibkr_connectivity_recovery_pending=False)
            await self.invalidate_ibkr_subscriptions(
                SubscriptionStatus.PENDING_REVALIDATION,
                "IBKR connectivity restored; awaiting a new streaming subscription",
            )
            await self.set_ibkr_resubscribe_required(True)
            await self.resolve_alerts("IBKR_1100")
            return
        if code in {2107, 2108}:
            return
        severity = AlertSeverity.CRITICAL if code in {1100, 1300} else AlertSeverity.WARNING
        await self.add_alert(
            severity,
            f"IBKR_{code}",
            message,
            flashing=severity is AlertSeverity.CRITICAL,
        )
        if severity is AlertSeverity.CRITICAL:
            await self.set_ibkr_health(ConnectionStatus.DEGRADED, message)

    async def apply_polymarket_event(self, event: VenueEvent) -> None:
        if event.kind.lower() == "stream_connected":
            await self.set_polymarket_health(
                ConnectionStatus.CONNECTED,
                "market WebSocket connected; synchronizing books",
                authenticated=False,
            )
            return
        current = await self.get()
        updated_books = update_books_from_stream_event(current.books, event)
        if updated_books:
            await self.set_books(updated_books)
        await self.set_polymarket_health(
            ConnectionStatus.CONNECTED,
            f"market WebSocket: {event.kind}",
            authenticated=False,
        )

    async def set_operating_state(
        self,
        *,
        paused: bool | None = None,
        kill_switch: bool | None = None,
        armed: bool | None = None,
    ) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            updates: dict[str, Any] = {}
            if paused is not None:
                updates["paused"] = paused
            if kill_switch is not None:
                updates["kill_switch"] = kill_switch
            if armed is not None:
                updates["armed"] = armed and self.settings.run_mode is not RunMode.READ_ONLY
            return snapshot.model_copy(update=updates)

        await self.update(apply)

    async def set_strategy_risk(self, risk: StrategyRiskView) -> None:
        await self.update(lambda snapshot: snapshot.model_copy(update={"strategy_risk": risk}))

    async def set_drawdown_halt(self, active: bool) -> None:
        await self._set_metadata_values(drawdown_halt_active=active)

    async def confirm_reconciliation(
        self,
        *,
        actor: str,
        reason: str,
        snapshot_id: int,
    ) -> None:
        now = utc_now()

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            metadata = deepcopy(snapshot.metadata)
            metadata["reconciliation_clean"] = True
            return snapshot.model_copy(
                update={
                    "metadata": metadata,
                    "reconciliation": ReconciliationStatusView(
                        clean=True,
                        method="MANUAL_OPERATOR_ATTESTATION",
                        confirmed_by=actor,
                        confirmed_at=now,
                        confirmed_snapshot_id=snapshot_id,
                        reason=reason,
                    ),
                }
            )

        await self.update(apply)

    async def invalidate_reconciliation(self, reason: str) -> None:
        now = utc_now()

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            metadata = deepcopy(snapshot.metadata)
            metadata["reconciliation_clean"] = False
            current = snapshot.reconciliation
            return snapshot.model_copy(
                update={
                    "metadata": metadata,
                    "reconciliation": current.model_copy(
                        update={
                            "clean": False,
                            "reason": reason,
                            "invalidated_at": now,
                        }
                    ),
                }
            )

        await self.update(apply)

    async def _set_metadata_values(self, **values: Any) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            metadata = deepcopy(snapshot.metadata)
            metadata.update(values)
            return snapshot.model_copy(update={"metadata": metadata})

        await self.update(apply)
