from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic import SecretStr

from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode


class FakeOrder:
    pass


def test_ibkr_entry_order_is_structurally_buy_lmt_day(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "ibkr_trading_mode": "paper",
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
