from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from zq_arb.analytics.payoff import (
    CostInputs,
    build_three_state_opportunity,
    conservative_ibkr_round_trip_commission,
    hedge_shares_per_contract,
    marketable_limit_price,
    plan_hedge_entry,
    round_shares_up,
    walk_asks,
)
from zq_arb.domain.calendar import MeetingCalendar
from zq_arb.domain.models import BookLevel, OrderBook

TEST_ASSET_ID = "test-market-asset"


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
    assert round_shares_up(
        hedge_shares_per_contract(25, pre_meeting_effr=Decimal("3.625"),
                                  calendar=MeetingCalendar("202609", date(2026, 9, 17)))
    ) == Decimal("487.54")
    assert round_shares_up(
        hedge_shares_per_contract(50, pre_meeting_effr=Decimal("3.625"),
                                  calendar=MeetingCalendar("202609", date(2026, 9, 17)))
    ) == Decimal("970.92")


def test_commission_uses_configured_round_trip_floor_or_higher_live_preview() -> None:
    assert conservative_ibkr_round_trip_commission(
        contracts=10,
        configured_per_contract=Decimal("3.64"),
        entry_preview_commission=None,
    ) == Decimal("36.40")
    assert conservative_ibkr_round_trip_commission(
        contracts=10,
        configured_per_contract=Decimal("3.64"),
        entry_preview_commission=Decimal("20"),
    ) == Decimal("40")


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


def test_marketable_limit_is_exactly_the_lowest_ask() -> None:
    market = OrderBook(
        token_id=TEST_ASSET_ID,
        bids=(BookLevel(price=Decimal("0.520"), size=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.530"), size=Decimal("100")),),
    )
    assert marketable_limit_price(market, Decimal("0.95")) == Decimal("0.530")
    assert marketable_limit_price(market, Decimal("0.525")) is None


def test_three_state_profit_contains_every_approved_state() -> None:
    opportunity = build_three_state_opportunity(
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
        calendar=MeetingCalendar("202609", date(2026, 9, 17)),
    )
    assert [row.move_bps for row in opportunity.scenarios] == [0, 25, 50]
    assert opportunity.token_requirements == {
        "INC25": Decimal("4875.39"),
        "INC50PLUS": Decimal("9709.11"),
    }
    assert opportunity.minimum_net_profit == min(
        *(row.net_pnl for row in opportunity.scenarios),
        *(row.net_pnl for row in opportunity.emergency_scenarios),
    )
    assert opportunity.committed_capital is not None
    assert opportunity.return_on_capital_bps is not None
    assert opportunity.calculation is not None
    assert opportunity.calculation.inc25_shares_per_contract == Decimal("487.539")
    assert opportunity.calculation.inc50plus_shares_per_contract == Decimal("970.911")
    assert opportunity.calculation.emergency_hedge_cash == (
        opportunity.calculation.inc25_emergency_hedge_cash
        + opportunity.calculation.inc50plus_emergency_hedge_cash
    )
    first = opportunity.scenarios[0]
    assert first.futures_price_change == first.settlement_price - first.zq_entry_price
    assert first.futures_pnl == (
        Decimal(first.contracts) * first.futures_point_value * first.futures_price_change
    )
    assert first.inc25_pnl == first.inc25_shares * (first.inc25_payout - first.inc25_entry_price)
    assert first.inc50plus_pnl == first.inc50plus_shares * (
        first.inc50plus_payout - first.inc50plus_entry_price
    )
    assert first.polymarket_pnl == first.inc25_pnl + first.inc50plus_pnl
    assert first.gross_pnl == first.futures_pnl + first.polymarket_pnl
    assert first.net_pnl == first.gross_pnl - first.costs


def test_order_book_reports_aggregated_best_level_sizes() -> None:
    market = OrderBook(
        token_id=TEST_ASSET_ID,
        bids=(
            BookLevel(price=Decimal("0.51"), size=Decimal("100")),
            BookLevel(price=Decimal("0.51"), size=Decimal("25")),
            BookLevel(price=Decimal("0.50"), size=Decimal("900")),
        ),
        asks=(
            BookLevel(price=Decimal("0.52"), size=Decimal("80")),
            BookLevel(price=Decimal("0.52"), size=Decimal("20")),
            BookLevel(price=Decimal("0.53"), size=Decimal("700")),
        ),
    )
    assert market.best_bid == Decimal("0.51")
    assert market.best_bid_size == Decimal("125")
    assert market.best_ask == Decimal("0.52")
    assert market.best_ask_size == Decimal("100")


def test_october_cash_settlement_pnl_and_return_match_excel_inputs() -> None:
    opportunity = build_three_state_opportunity(
        contracts=5,
        calendar=MeetingCalendar("202610", date(2026, 10, 29)),
        zq_price=Decimal("96.1"),
        pre_meeting_effr=Decimal("3.88"),
        inc25_book=book("25", (("0.54", "10000"),)),
        inc50_book=book("50", (("0.009", "10000"),)),
        cost_inputs=CostInputs(
            ibkr_commission=Decimal("18.20"), polymarket_fees=Decimal("6.656482476"),
        ),
        incremental_margin=Decimal("2993.63"),
        emergency_cash_reserve=Decimal("0"),
        post_price_cap=Decimal("0.95"),
        emergency_price_cap=Decimal("0.99"),
    )
    assert [row.settlement_price for row in opportunity.scenarios] == [
        Decimal("96.120"), Decimal("96.096"), Decimal("96.072"),
    ]
    assert [row.futures_pnl for row in opportunity.scenarios] == [
        Decimal("416.700"), Decimal("-83.340"), Decimal("-583.380"),
    ]
    assert [row.net_pnl for row in opportunity.scenarios] == [
        Decimal("112.821197524"), Decimal("112.821197524"), Decimal("112.821197524"),
    ]
    assert opportunity.emergency_scenarios == opportunity.scenarios
    assert opportunity.minimum_net_profit == Decimal("112.821197524")
    assert opportunity.committed_capital == Decimal("3272.65232")
    assert opportunity.return_on_capital_bps == (
        Decimal("112.821197524") / Decimal("3272.65232") * Decimal("10000")
    )


def test_share_rounding_residual_is_less_than_one_cent() -> None:
    opportunity = build_three_state_opportunity(
        contracts=1,
        calendar=MeetingCalendar("202609", date(2026, 9, 17)),
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
    zero, hike25, hike50 = opportunity.scenarios
    assert hike25.net_pnl - zero.net_pnl == Decimal("0.001")
    assert hike50.net_pnl - zero.net_pnl == Decimal("0.009")
    assert opportunity.minimum_net_profit == zero.net_pnl
    assert opportunity.return_on_capital_bps == (
        zero.net_pnl / opportunity.committed_capital * Decimal("10000")
    )


def test_residual_uses_frozen_ratios_and_cumulative_share_rounding() -> None:
    opportunity = build_three_state_opportunity(
        contracts=3, calendar=MeetingCalendar("202610", date(2026, 10, 29)),
        zq_price=Decimal("96.1"), pre_meeting_effr=Decimal("3.8805"),
        hedge_ratios={25: Decimal("100.008"), 50: Decimal("200.016")},
        previously_filled_contracts=Decimal(2),
        inc25_book=book("25", (("0.54", "10000"),)),
        inc50_book=book("50", (("0.009", "10000"),)),
        cost_inputs=CostInputs(), incremental_margin=Decimal("1800"),
        emergency_cash_reserve=Decimal(0), post_price_cap=Decimal(".95"),
        emergency_price_cap=Decimal(".99"),
    )
    assert opportunity.token_requirements == {
        "INC25": Decimal("300.02"), "INC50PLUS": Decimal("600.04"),
    }
    assert opportunity.calculation.inc25_shares_per_contract == Decimal("100.008")
    assert opportunity.scenarios[0].settlement_price == Decimal("96.119")


def test_empty_book_fails_closed() -> None:
    opportunity = build_three_state_opportunity(
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
        calendar=MeetingCalendar("202609", date(2026, 9, 17)),
    )
    assert not opportunity.tradeable
    assert any(check.code == "INC25_YES_BEST_ASK_SIZE" for check in opportunity.gate_checks)
    assert opportunity.direction == "LONG"
    assert opportunity.zq_side.value == "BUY"
    assert len(opportunity.hedge_depth) == 2


def test_entry_requires_full_hedge_size_at_the_exact_lowest_ask() -> None:
    opportunity = build_three_state_opportunity(
        contracts=10,
        zq_price=Decimal("96.30"),
        pre_meeting_effr=Decimal("3.625"),
        inc25_book=book("25", (("0.30", "100"), ("0.31", "10000"))),
        inc50_book=book("50", (("0.10", "12000"),)),
        cost_inputs=CostInputs(),
        incremental_margin=Decimal("0"),
        emergency_cash_reserve=Decimal("0"),
        post_price_cap=Decimal("0.95"),
        emergency_price_cap=Decimal("0.99"),
        calendar=MeetingCalendar("202609", date(2026, 9, 17)),
    )
    inc25 = next(item for item in opportunity.hedge_depth if item.leg_code == "INC25 YES")
    assert inc25.available_shares == Decimal("100")
    assert not inc25.sufficient
    assert any(
        check.code == "INC25_YES_BEST_ASK_SIZE" and not check.passed
        for check in opportunity.gate_checks
    )


def test_missing_margin_preserves_profit_but_withholds_capital_and_return() -> None:
    opportunity = build_three_state_opportunity(
        contracts=10,
        zq_price=Decimal("96.30"),
        pre_meeting_effr=Decimal("3.625"),
        inc25_book=book("25", (("0.30", "6000"),)),
        inc50_book=book("50", (("0.10", "12000"),)),
        cost_inputs=CostInputs(),
        incremental_margin=None,
        emergency_cash_reserve=Decimal("0"),
        post_price_cap=Decimal("0.95"),
        emergency_price_cap=Decimal("0.99"),
        calendar=MeetingCalendar("202609", date(2026, 9, 17)),
    )

    assert opportunity.minimum_net_profit is not None
    assert opportunity.committed_capital is None
    assert opportunity.return_on_capital_bps is None
    assert opportunity.calculation is not None
    assert opportunity.calculation.incremental_initial_margin is None
    assert opportunity.calculation.committed_capital is None


def test_one_tick_plan_consumes_only_required_size_and_preserves_true_cost() -> None:
    market = book("50", (("0.009", "100"), ("0.010", "1100"))).model_copy(
        update={"tick_size": Decimal("0.001")}
    )
    plan = plan_hedge_entry(market, Decimal("1000"), Decimal("0.95"), allow_one_tick=True)
    assert plan.available_shares == Decimal("1200")
    assert plan.limit_price == Decimal("0.010")
    assert plan.depth.sufficient
    assert plan.depth.total_cost == Decimal("9.900")
    assert plan.depth.vwap == Decimal("0.0099")
    assert plan.depth.fills == (
        BookLevel(price=Decimal("0.009"), size=Decimal("100")),
        BookLevel(price=Decimal("0.010"), size=Decimal("900")),
    )
    best_only = plan_hedge_entry(market, Decimal("100"), Decimal("0.95"), allow_one_tick=True)
    assert best_only.limit_price == Decimal("0.009")
    assert best_only.depth.total_cost == Decimal("0.900")


@pytest.mark.parametrize(
    ("next_ask", "tick", "cap"),
    [
        ("0.011", "0.001", "0.95"),
        ("0.010", "0.001", "0.0095"),
        ("0.010", None, "0.95"),
        ("0.010", "0", "0.95"),
    ],
)
def test_one_tick_plan_never_skips_a_tick_or_exceeds_the_cap(
    next_ask: str, tick: str | None, cap: str
) -> None:
    market = book("50", (("0.009", "100"), (next_ask, "1100"))).model_copy(
        update={"tick_size": Decimal(tick) if tick is not None else None}
    )
    plan = plan_hedge_entry(market, Decimal("1000"), Decimal(cap), allow_one_tick=True)
    assert plan.available_shares == Decimal("100")
    assert plan.depth.filled_shares == Decimal("100")
    assert not plan.depth.sufficient
    assert plan.limit_price == Decimal("0.009")


def test_depth_cost_aggregates_duplicate_price_levels() -> None:
    market = book("50", (("0.009", "80"), ("0.009", "20"), ("0.01", "1100")))
    plan = plan_hedge_entry(
        market.model_copy(update={"tick_size": Decimal("0.001")}),
        Decimal("1000"),
        Decimal("0.95"),
        allow_one_tick=True,
    )
    assert plan.available_shares == Decimal("1200")
    assert plan.depth.total_cost == Decimal("9.90")
    assert [fill.size for fill in plan.depth.fills] == [Decimal("100"), Decimal("900")]


@pytest.mark.parametrize("next_ask", ["0.010", "0.011"])
def test_inc50_scenarios_use_full_two_price_cost_only_within_one_tick(next_ask: str) -> None:
    market = book("50", (("0.009", "90.18"), (next_ask, "12000"))).model_copy(
        update={"tick_size": Decimal("0.001")}
    )
    opportunity = build_three_state_opportunity(
        contracts=10,
        zq_price=Decimal("96.30"),
        pre_meeting_effr=Decimal("3.625"),
        inc25_book=book("25", (("0.30", "6000"),)),
        inc50_book=market,
        cost_inputs=CostInputs(),
        incremental_margin=Decimal("5000"),
        emergency_cash_reserve=Decimal("0"),
        post_price_cap=Decimal("0.95"),
        emergency_price_cap=Decimal("0.99"),
        calendar=MeetingCalendar("202609", date(2026, 9, 17)),
    )
    depth = opportunity.hedge_depth[1]
    assert depth.best_ask_shares == Decimal("90.18")
    if next_ask == "0.011":
        assert not depth.sufficient
        assert depth.shortfall_shares == Decimal("9618.93")
        assert opportunity.scenarios == ()
        assert any(
            check.code == "INC50PLUS_YES_ENTRY_DEPTH" and not check.passed
            for check in opportunity.gate_checks
        )
        return
    assert depth.sufficient
    assert depth.shortfall_shares == 0
    assert depth.entry_cash_cost == Decimal("97.00092")
    assert depth.entry_vwap == Decimal("97.00092") / Decimal("9709.11")
    assert opportunity.token_prices["INC50PLUS"] == depth.entry_vwap
    assert not opportunity.gate_reasons
    assert [row.move_bps for row in opportunity.scenarios] == [0, 25, 50]
    assert opportunity.scenarios[0].inc50plus_pnl == Decimal("-97.00092")
    assert opportunity.scenarios[2].inc50plus_pnl == Decimal("9612.10908")
