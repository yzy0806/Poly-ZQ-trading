from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from zq_arb.analytics.probability import (
    ReferencePrices,
    executable_probability,
    fedwatch_reference,
    implied_average_effr,
    theoretical_settlement,
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
        ReferencePrices(
            august=Decimal("96.375"),
            september=Decimal("96.32"),
            october=Decimal("96.25"),
            november=Decimal("96.20"),
        )
    )
    assert set(snapshot.rates) == {"202608", "202609", "202610", "202611"}
    assert snapshot.expected_steps is not None
    assert snapshot.lower_probability is not None
    assert snapshot.upper_probability is not None
    assert snapshot.lower_probability + snapshot.upper_probability == 1


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
