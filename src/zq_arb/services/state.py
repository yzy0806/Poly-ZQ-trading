from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from zq_arb.adapters.events import VenueEvent
from zq_arb.config import Settings
from zq_arb.domain.enums import (
    AlertSeverity,
    ConnectionStatus,
    DataQuality,
    RunMode,
)
from zq_arb.domain.models import (
    AccountMetrics,
    AlertView,
    EligibilityStatus,
    EngineSnapshot,
    MarketMappingStatus,
    OrderBook,
    Quote,
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
        self._snapshot = EngineSnapshot(
            software_version=settings.software_version,
            config_version=settings.config_version,
            strategy_version=settings.strategy_version,
            run_mode=settings.run_mode,
            metadata={
                "contract_verification": {},
                "ibkr_market_data_type": None,
                "zq_position": 0,
                "active_batches": 0,
                "margin_preview_available": False,
                "next_batch_initial_margin": None,
                "reconciliation_clean": False,
            },
        )
        self._subscribers: set[asyncio.Queue[EngineSnapshot]] = set()
        self._account_values: dict[str, Decimal] = {}
        self._quote_parts: dict[str, dict[str, Any]] = {}

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
        return published

    async def update(self, updater: Callable[[EngineSnapshot], EngineSnapshot]) -> EngineSnapshot:
        current = await self.get()
        return await self.replace(updater(current))

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

    async def set_books(self, books: tuple[OrderBook, ...]) -> None:
        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            updated = dict(snapshot.books)
            updated.update({book.token_id: book for book in books})
            return snapshot.model_copy(update={"books": updated})

        await self.update(apply)

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
            return
        if event.kind == "market_data_type":
            data_type = int(event.payload.get("market_data_type") or 0)
            quality = {
                1: DataQuality.LIVE,
                2: DataQuality.FROZEN,
                3: DataQuality.DELAYED,
                4: DataQuality.FROZEN,
            }.get(data_type, DataQuality.UNKNOWN)

            def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
                metadata = deepcopy(snapshot.metadata)
                metadata["ibkr_market_data_type"] = data_type
                metadata["ibkr_data_quality"] = quality.value
                return snapshot.model_copy(update={"metadata": metadata})

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
        if event.kind == "account_summary":
            await self._apply_account_summary(event)
            return
        if event.kind == "pnl":
            await self._apply_pnl(event)
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
        part = self._quote_parts.setdefault(month, {})
        tick_type = int(event.payload.get("tick_type") or -1)
        if event.kind == "tick_price":
            field = {1: "bid", 2: "ask", 4: "last", 66: "bid", 67: "ask", 68: "last"}.get(tick_type)
        else:
            field = {0: "bid_size", 3: "ask_size", 69: "bid_size", 70: "ask_size"}.get(tick_type)
        value = _decimal(event.payload.get("price") or event.payload.get("size"))
        if field is None or value is None or value < 0:
            return
        part[field] = value
        part["received_at"] = event.received_at
        current = await self.get()
        data_type = current.metadata.get("ibkr_market_data_type")
        quality = DataQuality.LIVE if data_type == 1 else DataQuality.DELAYED
        quote = Quote(instrument=month, quality=quality, **part)

        def apply(snapshot: EngineSnapshot) -> EngineSnapshot:
            quotes = dict(snapshot.quotes)
            quotes[month] = quote
            return snapshot.model_copy(update={"quotes": quotes})

        await self.update(apply)

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

    async def _apply_ibkr_error(self, event: VenueEvent) -> None:
        code = int(event.payload.get("code") or 0)
        message = str(event.payload.get("message") or "unknown TWS error")
        informational = {2104, 2106, 2107, 2108, 2158}
        if code in informational:
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
        await self.set_polymarket_health(
            ConnectionStatus.CONNECTED,
            f"market stream: {event.kind}",
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
