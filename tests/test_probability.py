from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from zq_arb.analytics.probability import (
    DiagnosticPrices,
    adjacent_outcome_distribution,
    direct_zq_probability,
    executable_probability,
    fedwatch_reference,
    implied_average_effr,
    implied_decision_move_bps,
    theoretical_settlement,
    with_polymarket_expectation,
)


def test_implied_rate_is_100_minus_price() -> None:
    assert implied_average_effr(Decimal("96.375")) == Decimal("3.625")


def test_settlement_uses_all_calendar_days() -> None:
    result = theoretical_settlement(Decimal("3.625"), Decimal("25"))
    assert result == Decimal("96.25833333333333333333333333")


def test_executable_binary_probability_endpoints() -> None:
    zero = theoretical_settlement(Decimal("3.625"), Decimal("0"))
    higher = theoretical_settlement(Decimal("3.625"), Decimal("25"))
    assert executable_probability(zero, zero, higher) == 0
    assert executable_probability(higher, zero, higher) == 1


def test_reference_tree_exposes_all_intermediates() -> None:
    snapshot = fedwatch_reference(
        DiagnosticPrices(
            september=Decimal("96.32"),
            october=Decimal("96.25"),
            november=Decimal("96.20"),
        ),
        pre_meeting_effr=Decimal("3.625"),
    )
    assert set(snapshot.rates) == {"202609", "202610", "202611"}
    assert snapshot.expected_steps is not None
    assert snapshot.lower_probability is not None
    assert snapshot.upper_probability is not None
    assert snapshot.lower_probability + snapshot.upper_probability == 1


def test_direct_september_model_uses_calendar_weight_and_adjacent_states() -> None:
    pre_meeting_effr = Decimal("3.625")
    midpoint = theoretical_settlement(pre_meeting_effr, Decimal("12.5"))
    snapshot = direct_zq_probability(
        target_contract_month="202609",
        target_bid=midpoint,
        target_ask=midpoint,
        pre_meeting_effr=pre_meeting_effr,
    )
    assert snapshot.expected_move_bps is not None
    assert snapshot.expected_move_bps.quantize(Decimal("0.001")) == Decimal("12.500")
    assert snapshot.lower_step_bps == 0
    assert snapshot.upper_step_bps == 25
    assert snapshot.lower_probability is not None
    assert snapshot.upper_probability is not None
    assert snapshot.lower_probability.quantize(Decimal("0.001")) == Decimal("0.500")
    assert snapshot.upper_probability.quantize(Decimal("0.001")) == Decimal("0.500")
    assert snapshot.bucket_probabilities["NO_CHANGE"].quantize(Decimal("0.001")) == Decimal("0.500")
    assert snapshot.bucket_probabilities["INC25"].quantize(Decimal("0.001")) == Decimal("0.500")
    assert snapshot.valid


def test_direct_model_normalizes_polymarket_expected_move() -> None:
    pre_meeting_effr = Decimal("3.625")
    midpoint = theoretical_settlement(pre_meeting_effr, Decimal("12.5"))
    direct = direct_zq_probability(
        target_contract_month="202609",
        target_bid=midpoint,
        target_ask=midpoint,
        pre_meeting_effr=pre_meeting_effr,
    )
    enriched = with_polymarket_expectation(
        direct,
        {
            "DEC50PLUS": Decimal("0.10"),
            "DEC25": Decimal("0.20"),
            "NO_CHANGE": Decimal("0.40"),
            "INC25": Decimal("0.20"),
            "INC50PLUS": Decimal("0.10"),
        },
    )
    assert enriched.polymarket_probability_sum == Decimal("1.00")
    assert enriched.polymarket_expected_move_bps == 0
    assert enriched.expected_move_gap_bps is not None
    assert enriched.expected_move_gap_bps.quantize(Decimal("0.001")) == Decimal("12.500")


def test_direct_model_rejects_a_move_outside_version_one_scenarios() -> None:
    pre_meeting_effr = Decimal("3.625")
    price = theoretical_settlement(pre_meeting_effr, Decimal("75"))
    snapshot = direct_zq_probability(
        target_contract_month="202609",
        target_bid=price,
        target_ask=price,
        pre_meeting_effr=pre_meeting_effr,
    )
    assert not snapshot.valid
    assert not any(snapshot.bucket_probabilities.values())


def test_direct_helpers_cover_boundaries_and_invalid_calendar() -> None:
    lower, lower_probability, upper, upper_probability, _ = adjacent_outcome_distribution(
        Decimal("50")
    )
    assert (lower, upper) == (25, 50)
    assert (lower_probability, upper_probability) == (Decimal("0"), Decimal("1"))
    try:
        implied_decision_move_bps(Decimal("96"), Decimal("4"), days_after=0)
    except ValueError as exc:
        assert "calendar weights" in str(exc)
    else:
        raise AssertionError("zero post-decision weight must be rejected")


@given(
    start=st.decimals(min_value="0", max_value="20", places=4),
    first=st.integers(min_value=-100, max_value=100),
    second=st.integers(min_value=-100, max_value=100),
)
def test_settlement_is_monotonic_in_rate_move(start: Decimal, first: int, second: int) -> None:
    low, high = sorted((first, second))
    low_settlement = theoretical_settlement(start, Decimal(low))
    high_settlement = theoretical_settlement(start, Decimal(high))
    assert low_settlement >= high_settlement
