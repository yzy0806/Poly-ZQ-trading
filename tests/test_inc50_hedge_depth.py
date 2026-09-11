from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from zq_arb.adapters.polymarket import PreparedPolymarketOrder
from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import BookLevel, HedgeObligationView, OrderBook
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.services.state import StateStore


@pytest.mark.parametrize(
    ("leg_code", "shares", "expected_limit", "book_changes"),
    [
        ("INC50PLUS", "1000", "0.010", False),
        ("INC50PLUS", "100", "0.009", False),
        ("INC25", "1000", "0.009", False),
        ("INC50PLUS", "1000", "0.010", True),
    ],
)
async def test_hedge_router_uses_required_depth_and_rechecks_before_persisting(
    settings: Settings,
    leg_code: str,
    shares: str,
    expected_limit: str,
    book_changes: bool,
) -> None:
    configured = settings.model_copy(update={
        "run_mode": RunMode.PAPER,
        "simulate_polymarket_fills": True,
        "polymarket_order_submission_enabled": True,
    })
    token_id = next(leg.yes_token_id for leg in configured.market_legs if leg.code == leg_code)
    book = OrderBook(
        token_id=token_id,
        bids=(BookLevel(price=Decimal("0.008"), size=Decimal("1000")),),
        asks=(
            BookLevel(price=Decimal("0.009"), size=Decimal("100")),
            BookLevel(price=Decimal("0.010"), size=Decimal("1100")),
            BookLevel(price=Decimal("0.011"), size=Decimal("2000")),
        ),
        tick_size=Decimal("0.001"),
    )
    state = StateStore(configured)
    await state.set_books((book,))
    obligation = HedgeObligationView(
        obligation_id="depth-obligation", batch_id="depth-batch", exec_id="depth-execution",
        token_id=token_id, due_shares=Decimal(shares),
    )
    repository = MagicMock()
    repository.ledger_orders = AsyncMock(return_value=())
    repository.hedge_obligation = AsyncMock(return_value=obligation)
    repository.create_hedge_order_intent = AsyncMock(return_value=True)
    prepared = PreparedPolymarketOrder(
        token_id=token_id, limit_price=Decimal(expected_limit), shares=Decimal(shares),
        idempotency_key="depth-obligation:ATTEMPT:1", signed_payload=None,
    )

    async def prepare(**_kwargs: object) -> PreparedPolymarketOrder:
        if book_changes:
            await state.set_books((book.model_copy(update={
                "asks": (book.asks[0], book.asks[2]),
            }),))
        return prepared

    polymarket = MagicMock()
    polymarket.prepare_hedge_limit = AsyncMock(side_effect=prepare)
    coordinator = ExecutionCoordinator(
        settings=configured, repository=repository, state=state,
        ibkr=MagicMock(), polymarket=polymarket,
    )
    coordinator._post_prepared = AsyncMock()

    await coordinator._route_one_hedge(await state.get(), obligation)

    polymarket.prepare_hedge_limit.assert_awaited_once_with(
        token_id=token_id, limit_price=Decimal(expected_limit), shares=Decimal(shares),
        idempotency_key="depth-obligation:ATTEMPT:1",
    )
    if book_changes:
        repository.create_hedge_order_intent.assert_not_awaited()
        coordinator._post_prepared.assert_not_awaited()
    else:
        assert repository.create_hedge_order_intent.await_args.kwargs["limit_price"] == Decimal(
            expected_limit
        )
        coordinator._post_prepared.assert_awaited_once_with(prepared)
