from __future__ import annotations

from decimal import Decimal

from zq_arb.analytics.payoff import (
    CostInputs,
    build_three_state_opportunity,
    hedge_shares_per_contract,
    round_shares_up,
    walk_asks,
)
from zq_arb.domain.models import BookLevel, OrderBook


def book(token_id: str, prices: tuple[tuple[str, str], ...]) -> OrderBook:
    bids = (
        (
            BookLevel(
                price=Decimal(prices[0][0]) - Decimal("0.01"),
                size=Decimal(prices[0][1]),
            ),
        )
        if prices
        else ()
    )
    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=tuple(BookLevel(price=Decimal(price), size=Decimal(size)) for price, size in prices),
        tick_size=Decimal("0.01"),
    )


def test_approved_hedge_share_ratios() -> None:
    assert round_shares_up(hedge_shares_per_contract(25)) == Decimal("486.15")
    assert round_shares_up(hedge_shares_per_contract(50)) == Decimal("972.30")


def test_depth_walker_respects_price_cap() -> None:
    result = walk_asks(
        [
            BookLevel(price=Decimal("0.40"), size=Decimal("50")),
            BookLevel(price=Decimal("0.50"), size=Decimal("100")),
        ],
        Decimal("100"),
        price_cap=Decimal("0.45"),
    )
    assert result.filled_shares == 50
    assert not result.sufficient
    assert result.worst_price == Decimal("0.40")


def test_three_state_profit_contains_every_approved_state() -> None:
    opportunity = build_three_state_opportunity(
        direction="LONG",
        contracts=10,
        zq_price=Decimal("96.30"),
        pre_meeting_effr=Decimal("3.625"),
        inc25_book=book("25", (("0.30", "6000"),)),
        inc50_book=book("50", (("0.10", "12000"),)),
        cost_inputs=CostInputs(),
        incremental_margin=Decimal("5000"),
        emergency_cash_reserve=Decimal("0"),
        post_price_cap=Decimal("0.95"),
        emergency_price_cap=Decimal("0.99"),
    )
    assert [row.move_bps for row in opportunity.scenarios] == [0, 25, 50]
    assert opportunity.token_requirements == {
        "INC25": Decimal("4861.50"),
        "INC50PLUS": Decimal("9723.00"),
    }
    assert opportunity.minimum_net_profit == min(
        *(row.net_pnl for row in opportunity.scenarios),
        *(row.net_pnl for row in opportunity.emergency_scenarios),
    )
    assert opportunity.committed_capital is not None
    assert opportunity.return_on_capital_bps is not None


def test_empty_book_fails_closed() -> None:
    opportunity = build_three_state_opportunity(
        direction="SHORT",
        contracts=10,
        zq_price=Decimal("96.30"),
        pre_meeting_effr=Decimal("3.625"),
        inc25_book=book("25", ()),
        inc50_book=book("50", ()),
        cost_inputs=CostInputs(),
        incremental_margin=Decimal("0"),
        emergency_cash_reserve=Decimal("0"),
        post_price_cap=Decimal("0.95"),
        emergency_price_cap=Decimal("0.99"),
    )
    assert not opportunity.tradeable
    assert "order book is empty" in opportunity.gate_reasons
