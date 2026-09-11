from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from polymarket.models.clob.orders import SignedOrder

from zq_arb.adapters.polymarket import PolymarketAdapter, PolymarketProtocolError
from zq_arb.domain.enums import RunMode


def sample_order(signature_type=0):
    return SignedOrder(
        builder="0x" + "00" * 32,
        expiration=0,
        maker="0x" + "33" * 20,
        maker_amount=30000000,
        metadata="0x" + "00" * 32,
        order_type="GTC",
        salt=123,
        side="BUY",
        signature="0x" + "00" * 65,
        signature_type=signature_type,
        signer="0x" + "44" * 20,
        taker_amount=100000000,
        timestamp=1700000000000,
        token_id="123456",  # noqa: S106 - synthetic market identifier
    )


def signing_client(order):
    return SimpleNamespace(
        _ctx=SimpleNamespace(
            environment_config=SimpleNamespace(
                chain_id=137,
                standard_exchange="0x" + "11" * 20,
                neg_risk_exchange="0x" + "22" * 20,
            ),
            order_metadata=SimpleNamespace(
                resolve_market=AsyncMock(return_value=SimpleNamespace(neg_risk=False)),
            ),
        ),
        create_limit_order=AsyncMock(return_value=order),
        post_order=AsyncMock(),
    )


@pytest.mark.parametrize(
    "signature_type,expected",
    [
        (0, "0x48178497c213c0cd5d243c8bc8d2ca449ca567015d7ab2cfbcfde0242df07053"),
        (3, "0x4108f41f1b6934758b973c0f19e0bb4735ec5ba2f3360bf8912281931a55d766"),
    ],
)
async def test_sdk_exchange_hash_is_stable_for_eoa_and_deposit_wallet(signature_type, expected):
    order = sample_order(signature_type)
    client = signing_client(order)
    assert await PolymarketAdapter._signed_order_id(client, order) == expected
    assert (
        await PolymarketAdapter._signed_order_id(client, replace(order, signature="0x" + "11" * 65))
        == expected
    )
    assert await PolymarketAdapter._signed_order_id(client, replace(order, salt=124)) != expected
    client._ctx.order_metadata.resolve_market.return_value.neg_risk = True
    assert await PolymarketAdapter._signed_order_id(client, order) != expected


async def test_prepared_order_round_trip_and_acknowledgement_identity(settings):
    configured = settings.model_copy(
        update={
            "run_mode": RunMode.PAPER,
            "polymarket_order_submission_enabled": True,
            "simulate_polymarket_fills": False,
        }
    )
    adapter = PolymarketAdapter(configured)
    order = sample_order()
    client = signing_client(order)
    adapter._authenticated_client = AsyncMock(return_value=client)
    try:
        prepared = await adapter.prepare_hedge_limit(
            token_id=order.token_id,
            limit_price=Decimal(".3"),
            shares=Decimal("100"),
            idempotency_key="order-intent",
        )
        assert prepared.signed_payload == asdict(order)
        client.post_order.return_value = SimpleNamespace(
            ok=True, order_id=prepared.order_id, status="matched"
        )
        result = await adapter.post_prepared_hedge(prepared)
        assert result.immediately_matched_shares == 0
        client.post_order.assert_awaited_once_with(order)
        client.post_order.return_value.order_id = "unexpected-order"
        with pytest.raises(PolymarketProtocolError, match="differs"):
            await adapter.post_prepared_hedge(prepared)
        with pytest.raises(RuntimeError, match="persisted signed order hash"):
            await adapter.post_prepared_hedge(replace(prepared, order_id=None))
        sent = client.post_order.await_count
        with pytest.raises(PolymarketProtocolError, match="Persisted payload"):
            await adapter.post_prepared_hedge(
                replace(prepared, signed_payload=asdict(replace(order, salt=456)))
            )
        assert client.post_order.await_count == sent
    finally:
        await adapter.close()


async def test_account_snapshot_exhausts_pagination_and_labels_truncation(settings):
    class Pager:
        def __init__(self, count):
            self.count = count

        async def iter_items(self):
            for i in range(self.count):
                yield SimpleNamespace(model_dump=lambda mode, i=i: {"id": str(i)})

    adapter = PolymarketAdapter(settings)
    adapter._authenticated_client = AsyncMock(
        return_value=SimpleNamespace(
            list_open_orders=lambda: Pager(3),
            list_account_trades=lambda: Pager(1001),
        )
    )
    adapter.current_event_positions = AsyncMock(return_value=())
    try:
        account = await adapter.account_snapshot()
        assert len(account.open_orders) == 3 and len(account.trades) == 1001
        assert account.trades_complete
        limited = await adapter.account_snapshot(trade_limit=500)
        assert len(limited.trades) == 500 and not limited.trades_complete
    finally:
        await adapter.close()
