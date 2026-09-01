from __future__ import annotations

import asyncio
import contextlib
import importlib
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import structlog

from zq_arb.adapters.events import VenueEvent
from zq_arb.config import Settings

LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IbApiModules:
    client: ModuleType
    wrapper: ModuleType
    contract: ModuleType
    order: ModuleType
    execution: ModuleType


class IbkrAdapterError(RuntimeError):
    pass


def _load_official_api(path: Path) -> IbApiModules:
    resolved = path.resolve()
    if not resolved.is_dir() or not (resolved / "ibapi").is_dir():
        raise IbkrAdapterError(f"official IBKR Python API not found at configured path: {resolved}")
    text_path = str(resolved)
    if text_path not in sys.path:
        sys.path.insert(0, text_path)
    return IbApiModules(
        client=importlib.import_module("ibapi.client"),
        wrapper=importlib.import_module("ibapi.wrapper"),
        contract=importlib.import_module("ibapi.contract"),
        order=importlib.import_module("ibapi.order"),
        execution=importlib.import_module("ibapi.execution"),
    )


class IbkrAdapter:
    """Thread-safe bridge from the official TWS callback API into asyncio."""

    ACCOUNT_TAGS = (
        "NetLiquidation,TotalCashValue,InitMarginReq,MaintMarginReq,AvailableFunds,"
        "ExcessLiquidity,FullInitMarginReq,FullMaintMarginReq,FullAvailableFunds,"
        "FullExcessLiquidity,Cushion"
    )

    def __init__(self, settings: Settings, queue: asyncio.Queue[VenueEvent]) -> None:
        self.settings = settings
        self.queue = queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._api: IbApiModules | None = None
        self._client: Any = None
        self._network_thread: threading.Thread | None = None
        self._connected = asyncio.Event()
        self._next_order_id: int | None = None
        self._order_id_lock = threading.Lock()
        self._contracts: dict[str, Any] = {}
        self._request_to_month: dict[int, str] = {}
        self._stream_request_ids: set[int] = set()
        self._margin_preview_context: dict[int, dict[str, Any]] = {}
        self._completed_margin_preview_ids: deque[int] = deque(maxlen=100)
        self._stopped = False
        self._pnl_requested = False

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and self._client.isConnected())

    async def connect(self) -> None:
        if self.connected:
            return
        self._connected.clear()
        self._loop = asyncio.get_running_loop()
        self._api = _load_official_api(self.settings.ibkr_python_api_path)
        self._client = self._build_client()
        self._emit("connection", {"status": "CONNECTING"})
        self._client.connect(
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            clientId=self.settings.ibkr_client_id,
        )
        self._network_thread = threading.Thread(
            target=self._client.run,
            name="ibkr-tws-network",
            daemon=True,
        )
        self._network_thread.start()
        try:
            await asyncio.wait_for(
                self._connected.wait(), timeout=self.settings.ibkr_connect_timeout_seconds
            )
        except TimeoutError as exc:
            await self.disconnect()
            raise IbkrAdapterError("TWS did not provide a valid order id before timeout") from exc
        self._client.reqMarketDataType(self.settings.ibkr_market_data_type)
        self._client.reqManagedAccts()
        self._client.reqAccountSummary(9_001, "All", self.ACCOUNT_TAGS)
        if self.settings.ibkr_account_configured:
            self._client.reqPnL(9_002, self.settings.ibkr_account_id.get_secret_value(), "")
            self._pnl_requested = True
        else:
            self._emit("configuration_warning", {"code": "IBKR_ACCOUNT_ID_REQUIRED"})
        self._emit("connection", {"status": "CONNECTED"})

    def _build_client(self) -> Any:
        if self._api is None:
            raise IbkrAdapterError("IB API modules not loaded")
        adapter = self
        api = self._api

        class Bridge(api.wrapper.EWrapper, api.client.EClient):  # type: ignore[misc, name-defined]
            def __init__(self) -> None:
                api.wrapper.EWrapper.__init__(self)
                api.client.EClient.__init__(self, self)

            def nextValidId(self, orderId: int) -> None:
                adapter._next_order_id = orderId
                adapter._signal_connected()
                adapter._emit("next_valid_id", {"order_id": orderId})

            def connectionClosed(self) -> None:
                adapter._connected.clear()
                adapter._emit("connection", {"status": "DISCONNECTED"})

            def managedAccounts(self, accountsList: str) -> None:
                account_count = len([value for value in accountsList.split(",") if value])
                adapter._emit("managed_accounts", {"account_count": account_count})

            def error(self, *args: Any) -> None:
                req_id, code, message, advanced = adapter._normalize_error(args)
                if req_id is not None and req_id in adapter._completed_margin_preview_ids:
                    adapter._completed_margin_preview_ids.remove(req_id)
                    return
                month = adapter._request_to_month.get(req_id) if req_id is not None else None
                preview = adapter._margin_preview_context.get(req_id or -1)
                adapter._emit(
                    "error",
                    {
                        "request_id": req_id,
                        "month": month,
                        "margin_preview": preview is not None,
                        "margin_preview_context": preview,
                        "code": code,
                        "message": message,
                        "advanced_reject": advanced,
                    },
                )

            def contractDetails(self, reqId: int, contractDetails: Any) -> None:
                adapter._handle_contract_details(reqId, contractDetails)

            def contractDetailsEnd(self, reqId: int) -> None:
                adapter._emit("contract_details_end", {"request_id": reqId})

            def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
                adapter._emit(
                    "tick_price",
                    {
                        "request_id": reqId,
                        "month": adapter._request_to_month.get(reqId),
                        "tick_type": tickType,
                        "price": str(price),
                        "can_auto_execute": getattr(attrib, "canAutoExecute", None),
                    },
                )

            def tickSize(self, reqId: int, tickType: int, size: Any) -> None:
                adapter._emit(
                    "tick_size",
                    {
                        "request_id": reqId,
                        "month": adapter._request_to_month.get(reqId),
                        "tick_type": tickType,
                        "size": str(size),
                    },
                )

            def marketDataType(self, reqId: int, marketDataType: int) -> None:
                adapter._emit(
                    "market_data_type",
                    {
                        "request_id": reqId,
                        "month": adapter._request_to_month.get(reqId),
                        "market_data_type": marketDataType,
                    },
                )

            def openOrder(
                self, orderId: int, contract: Any, order: Any, orderState: Any
            ) -> None:
                context = adapter._margin_preview_context.get(orderId)
                if context is None:
                    adapter._emit(
                        "open_order",
                        {
                            "order_id": orderId,
                            "contract_id": getattr(contract, "conId", None),
                            "symbol": str(getattr(contract, "symbol", "")),
                            "contract_month": str(
                                getattr(contract, "lastTradeDateOrContractMonth", "")
                            ),
                            "action": str(getattr(order, "action", "")),
                            "quantity": str(getattr(order, "totalQuantity", "")),
                            "limit_price": str(getattr(order, "lmtPrice", "")),
                            "order_ref": str(getattr(order, "orderRef", "")),
                            "status": str(getattr(orderState, "status", "")),
                        },
                    )
                    return
                adapter._emit(
                    "margin_preview",
                    {
                        **context,
                        "order_id": orderId,
                        "status": str(getattr(orderState, "status", "")),
                        "init_margin_before": str(
                            getattr(orderState, "initMarginBefore", "")
                        ),
                        "init_margin_change": str(
                            getattr(orderState, "initMarginChange", "")
                        ),
                        "init_margin_after": str(
                            getattr(orderState, "initMarginAfter", "")
                        ),
                        "maintenance_margin_before": str(
                            getattr(orderState, "maintMarginBefore", "")
                        ),
                        "maintenance_margin_change": str(
                            getattr(orderState, "maintMarginChange", "")
                        ),
                        "maintenance_margin_after": str(
                            getattr(orderState, "maintMarginAfter", "")
                        ),
                        "equity_with_loan_before": str(
                            getattr(orderState, "equityWithLoanBefore", "")
                        ),
                        "equity_with_loan_change": str(
                            getattr(orderState, "equityWithLoanChange", "")
                        ),
                        "equity_with_loan_after": str(
                            getattr(orderState, "equityWithLoanAfter", "")
                        ),
                        "commission": str(getattr(orderState, "commission", "")),
                        "commission_currency": str(
                            getattr(orderState, "commissionCurrency", "")
                        ),
                        "warning_text": str(getattr(orderState, "warningText", "")),
                    },
                )

            def accountSummary(
                self, reqId: int, account: str, tag: str, value: str, currency: str
            ) -> None:
                adapter._emit(
                    "account_summary",
                    {
                        "request_id": reqId,
                        "account_fingerprint": adapter._fingerprint_account(account),
                        "tag": tag,
                        "value": value,
                        "currency": currency,
                    },
                )

            def pnl(
                self,
                reqId: int,
                dailyPnL: float,
                unrealizedPnL: float,
                realizedPnL: float,
            ) -> None:
                adapter._emit(
                    "pnl",
                    {
                        "request_id": reqId,
                        "daily_pnl": str(dailyPnL),
                        "unrealized_pnl": str(unrealizedPnL),
                        "realized_pnl": str(realizedPnL),
                    },
                )

            def orderStatus(self, *args: Any) -> None:
                order_id = int(args[0]) if args else -1
                if (
                    order_id in adapter._margin_preview_context
                    or order_id in adapter._completed_margin_preview_ids
                ):
                    return
                names = (
                    "order_id",
                    "status",
                    "filled",
                    "remaining",
                    "avg_fill_price",
                    "perm_id",
                    "parent_id",
                    "last_fill_price",
                    "client_id",
                    "why_held",
                    "mkt_cap_price",
                )
                adapter._emit("order_status", dict(zip(names, map(str, args), strict=False)))

            def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
                adapter._emit(
                    "execution",
                    {
                        "request_id": reqId,
                        "exec_id": str(execution.execId),
                        "order_id": int(execution.orderId),
                        "contract_id": int(contract.conId),
                        "side": str(execution.side),
                        "shares": str(execution.shares),
                        "price": str(execution.price),
                        "time": str(execution.time),
                    },
                )

        return Bridge()

    @staticmethod
    def _normalize_error(args: tuple[Any, ...]) -> tuple[int | None, int | None, str, str]:
        if len(args) >= 5:
            req_id, _, code, message, advanced = args[:5]
        elif len(args) >= 4:
            req_id, code, message, advanced = args[:4]
        elif len(args) >= 3:
            req_id, code, message = args[:3]
            advanced = ""
        else:
            return None, None, " ".join(str(value) for value in args), ""
        return int(req_id), int(code), str(message), str(advanced or "")

    @staticmethod
    def _fingerprint_account(account: str) -> str:
        if len(account) <= 4:
            return "****"
        return f"***{account[-4:]}"

    def _signal_connected(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.set)

    def _emit(
        self,
        kind: str,
        payload: dict[str, Any],
        source_timestamp: datetime | None = None,
    ) -> None:
        if self._loop is None or self._stopped:
            return
        event = VenueEvent(
            venue="IBKR",
            kind=kind,
            payload=payload,
            source_timestamp=source_timestamp,
        )
        self._loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: VenueEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            LOGGER.critical("venue_event_queue_overflow", venue="IBKR", kind=event.kind)

    def _new_zq_contract(self, month: str) -> Any:
        if self._api is None:
            raise IbkrAdapterError("IB API modules not loaded")
        contract = self._api.contract.Contract()
        contract.symbol = "ZQ"
        contract.secType = "FUT"
        contract.exchange = self.settings.ibkr_zq_exchange
        contract.currency = self.settings.ibkr_zq_currency
        contract.tradingClass = self.settings.ibkr_zq_trading_class
        contract.lastTradeDateOrContractMonth = month
        contract.multiplier = "4167"
        return contract

    def _handle_contract_details(self, request_id: int, details: Any) -> None:
        month = self._request_to_month.get(request_id)
        contract = details.contract
        errors: list[str] = []
        expected = {
            "symbol": "ZQ",
            "secType": "FUT",
            "exchange": self.settings.ibkr_zq_exchange,
            "currency": self.settings.ibkr_zq_currency,
            "tradingClass": self.settings.ibkr_zq_trading_class,
            "multiplier": "4167",
        }
        for field, value in expected.items():
            if str(getattr(contract, field, "")) != value:
                errors.append(f"{field} mismatch")
        if month and not str(contract.lastTradeDateOrContractMonth).startswith(month):
            errors.append("contract month mismatch")
        if month and not errors:
            self._contracts[month] = contract
        self._emit(
            "contract_details",
            {
                "request_id": request_id,
                "month": month,
                "contract_id": int(contract.conId),
                "local_symbol": str(contract.localSymbol),
                "expiry": str(contract.lastTradeDateOrContractMonth),
                "min_tick": str(details.minTick),
                "time_zone": str(details.timeZoneId),
                "verified": not errors,
                "errors": errors,
            },
        )

    def request_contracts_and_market_data(self) -> None:
        if not self.connected:
            raise IbkrAdapterError("TWS is not connected")
        for index, month in enumerate(self.settings.reference_contract_months, start=1):
            detail_request_id = 1_000 + index
            market_request_id = 2_000 + index
            self._request_to_month[detail_request_id] = month
            self._request_to_month[market_request_id] = month
            self._stream_request_ids.add(market_request_id)
            contract = self._new_zq_contract(month)
            self._client.reqContractDetails(detail_request_id, contract)
            self._client.reqMktData(market_request_id, contract, "", False, False, [])

    def resubscribe_market_data(self) -> None:
        """Replace every streaming subscription after an IBKR connectivity restore."""

        if not self.connected:
            raise IbkrAdapterError("TWS is not connected")
        for request_id in tuple(self._stream_request_ids):
            self._client.cancelMktData(request_id)
            self._request_to_month.pop(request_id, None)
        self._stream_request_ids.clear()
        self.request_contracts_and_market_data()

    def request_open_orders_and_executions(self) -> None:
        if not self.connected:
            raise IbkrAdapterError("TWS is not connected")
        if self._api is None:
            raise IbkrAdapterError("IB API modules not loaded")
        self._client.reqOpenOrders()
        self._client.reqExecutions(9_003, self._api.execution.ExecutionFilter())

    def _allocate_order_id(self) -> int:
        with self._order_id_lock:
            if self._next_order_id is None:
                raise IbkrAdapterError("no valid IBKR order id")
            value = self._next_order_id
            self._next_order_id += 1
            return value

    def submit_zq_limit_day(
        self,
        *,
        month: str,
        limit_price: Decimal,
        quantity: int,
        order_ref: str,
    ) -> int:
        """Submit a version-one long-ZQ entry; the strategy cannot create SELL orders."""

        if self.settings.run_mode.value != "PAPER":
            raise PermissionError("IBKR orders require RUN_MODE=PAPER in this build")
        if not self.settings.ibkr_order_submission_enabled:
            raise PermissionError("IBKR_ORDER_SUBMISSION_ENABLED is false")
        if self.settings.ibkr_trading_mode.lower() != "paper":
            raise PermissionError("configured IBKR endpoint is not marked as paper")
        if quantity != self.settings.ibkr_zq_child_order_quantity:
            raise ValueError("ZQ child order quantity must be exactly 10")
        contract = self._contracts.get(month)
        if contract is None:
            raise IbkrAdapterError("qualified ZQ contract is unavailable")
        if self._api is None:
            raise IbkrAdapterError("IB API modules not loaded")
        order = self._api.order.Order()
        order.action = "BUY"
        order.orderType = "LMT"
        order.tif = "DAY"
        order.totalQuantity = quantity
        order.lmtPrice = float(limit_price)
        order.orderRef = order_ref
        order.transmit = True
        order_id = self._allocate_order_id()
        self._client.placeOrder(order_id, contract, order)
        return order_id

    def request_zq_margin_preview(
        self,
        *,
        month: str,
        limit_price: Decimal,
        quantity: int,
    ) -> int:
        """Request a non-routing BUY ZQ what-if and return its IBKR order id."""

        if not self.connected:
            raise IbkrAdapterError("TWS is not connected")
        if not self.settings.ibkr_account_configured:
            raise IbkrAdapterError("IBKR_ACCOUNT_ID is not configured")
        if quantity != self.settings.ibkr_zq_child_order_quantity:
            raise ValueError("margin preview quantity must match the 10-contract child order")
        contract = self._contracts.get(month)
        if contract is None:
            raise IbkrAdapterError("verified ZQ contract is unavailable for margin preview")
        if self._api is None:
            raise IbkrAdapterError("IB API modules not loaded")
        order_id = self._allocate_order_id()
        context = {
            "contract_month": month,
            "side": "BUY",
            "quantity": quantity,
            "limit_price": str(limit_price),
        }
        self._margin_preview_context[order_id] = context
        order = self._api.order.Order()
        order.action = "BUY"
        order.orderType = "LMT"
        order.tif = "DAY"
        order.totalQuantity = quantity
        order.lmtPrice = float(limit_price)
        order.account = self.settings.ibkr_account_id.get_secret_value()
        order.orderRef = f"ZQ-MARGIN-PREVIEW-{order_id}"
        order.whatIf = True
        order.transmit = True
        self._emit("margin_preview_requested", {**context, "order_id": order_id})
        self._client.placeOrder(order_id, contract, order)
        return order_id

    def cancel_margin_preview(self, order_id: int) -> None:
        self._completed_margin_preview_ids.append(order_id)
        if self._client is not None and self.connected:
            with contextlib.suppress(Exception):
                self._client.cancelOrder(order_id, "margin-preview-complete")
        self._margin_preview_context.pop(order_id, None)

    def cancel_order(self, order_id: int) -> None:
        if not self.settings.ibkr_order_submission_enabled:
            raise PermissionError("IBKR_ORDER_SUBMISSION_ENABLED is false")
        self._client.cancelOrder(order_id, "profitability-or-operator-cancel")

    async def events(self) -> AsyncIterator[VenueEvent]:
        while not self._stopped:
            yield await self.queue.get()

    async def disconnect(self) -> None:
        self._stopped = True
        if self._client is not None:
            with contextlib.suppress(Exception):
                if self.connected:
                    self._client.cancelAccountSummary(9_001)
                    if self._pnl_requested:
                        self._client.cancelPnL(9_002)
                    for request_id in tuple(self._request_to_month):
                        if request_id >= 2_000:
                            self._client.cancelMktData(request_id)
                    self._margin_preview_context.clear()
                    self._completed_margin_preview_ids.clear()
                    self._stream_request_ids.clear()
                    self._client.disconnect()
        if self._network_thread is not None:
            await asyncio.to_thread(self._network_thread.join, 2)
