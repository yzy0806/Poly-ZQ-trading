from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic import SecretStr

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode


class FakeOrder:
    pass


class FakeOrderCancel:
    def __init__(self) -> None:
        self.manualOrderCancelTime = ""


def test_ibkr_callback_queue_overflow_sets_a_sticky_trading_halt_signal(
    settings: Settings,
) -> None:
    queue: asyncio.Queue[VenueEvent] = asyncio.Queue(maxsize=1)
    adapter = IbkrAdapter(settings, queue)
    adapter._enqueue(VenueEvent(venue="IBKR", kind="first"))
    adapter._enqueue(VenueEvent(venue="IBKR", kind="lost"))
    assert adapter.event_queue_overflowed


def test_ibkr_entry_order_is_structurally_buy_lmt_day(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "ibkr_trading_mode": "paper",
            "ibkr_account_id": SecretStr("DU123456"),
        }
    )
    adapter = IbkrAdapter(configured, asyncio.Queue())
    adapter._api = SimpleNamespace(order=SimpleNamespace(Order=FakeOrder))  # type: ignore[assignment]
    adapter._client = MagicMock()
    adapter._contracts["202609"] = object()
    adapter._next_order_id = 101

    order_id = adapter.submit_zq_limit_day(
        month="202609",
        limit_price=Decimal("96.330"),
        quantity=10,
        order_ref="LONG-ONLY-TEST",
    )

    assert order_id == 101
    submitted = adapter._client.placeOrder.call_args.args[2]
    assert submitted.action == "BUY"
    assert submitted.orderType == "LMT"
    assert submitted.tif == "DAY"
    assert submitted.totalQuantity == 10
    assert submitted.account == "DU123456"


def test_ibkr_margin_preview_is_non_routing_buy_10_what_if(settings: Settings) -> None:
    configured = settings.model_copy(update={"ibkr_account_id": SecretStr("DU123456")})
    adapter = IbkrAdapter(configured, asyncio.Queue())
    adapter._api = SimpleNamespace(order=SimpleNamespace(Order=FakeOrder))  # type: ignore[assignment]
    adapter._client = MagicMock()
    adapter._client.isConnected.return_value = True
    adapter._contracts["202609"] = object()
    adapter._next_order_id = 501

    order_id = adapter.request_zq_margin_preview(
        month="202609",
        limit_price=Decimal("96.330"),
        quantity=10,
    )

    assert order_id == 501
    submitted = adapter._client.placeOrder.call_args.args[2]
    assert submitted.action == "BUY"
    assert submitted.orderType == "LMT"
    assert submitted.tif == "DAY"
    assert submitted.totalQuantity == 10
    assert submitted.whatIf is True
    assert submitted.transmit is True
    assert submitted.account == "DU123456"


def test_ibkr_recovery_requests_orders_executions_and_positions(settings: Settings) -> None:
    execution_filter = object()
    adapter = IbkrAdapter(settings, asyncio.Queue())
    adapter._api = SimpleNamespace(
        execution=SimpleNamespace(ExecutionFilter=MagicMock(return_value=execution_filter))
    )  # type: ignore[assignment]
    adapter._client = MagicMock()
    adapter._client.isConnected.return_value = True

    adapter.request_open_orders_and_executions()

    adapter._client.reqOpenOrders.assert_called_once_with()
    adapter._client.reqExecutions.assert_called_once_with(9_003, execution_filter)
    adapter._client.reqPositions.assert_called_once_with()


def test_ibkr_cancellations_use_typed_order_cancel_payload(settings: Settings) -> None:
    configured = settings.model_copy(update={"ibkr_order_submission_enabled": True})
    adapter = IbkrAdapter(configured, asyncio.Queue())
    adapter._api = SimpleNamespace(  # type: ignore[assignment]
        order_cancel=SimpleNamespace(OrderCancel=FakeOrderCancel)
    )
    adapter._client = MagicMock()
    adapter._client.isConnected.return_value = True

    adapter.cancel_order(915)
    adapter.cancel_margin_preview(7001)

    calls = adapter._client.cancelOrder.call_args_list
    assert [call.args[0] for call in calls] == [915, 7001]
    assert all(isinstance(call.args[1], FakeOrderCancel) for call in calls)
