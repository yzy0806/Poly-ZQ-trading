from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from zq_arb.domain.models import ProbabilitySnapshot

ONE_HUNDRED = Decimal("100")
TWENTY_FIVE_BPS_PERCENT = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class ReferencePrices:
    august: Decimal
    september: Decimal
    october: Decimal
    november: Decimal


def implied_average_effr(futures_price: Decimal) -> Decimal:
    return ONE_HUNDRED - futures_price


def theoretical_settlement(
    pre_meeting_effr_percent: Decimal,
    move_bps: Decimal,
    *,
    days_before: int = 16,
    days_after: int = 14,
) -> Decimal:
    total_days = Decimal(days_before + days_after)
    post_rate = pre_meeting_effr_percent + move_bps / ONE_HUNDRED
    average_rate = (
        Decimal(days_before) * pre_meeting_effr_percent + Decimal(days_after) * post_rate
    ) / total_days
    return ONE_HUNDRED - average_rate


def executable_probability(
    execution_price: Decimal,
    settlement_zero: Decimal,
    settlement_twenty_five: Decimal,
) -> Decimal:
    denominator = settlement_zero - settlement_twenty_five
    if denominator == 0:
        raise ValueError("settlement states must be distinct")
    return (settlement_zero - execution_price) / denominator


def fedwatch_reference(prices: ReferencePrices) -> ProbabilitySnapshot:
    rates = {
        "202608": implied_average_effr(prices.august),
        "202609": implied_average_effr(prices.september),
        "202610": implied_average_effr(prices.october),
        "202611": implied_average_effr(prices.november),
    }
    october_start = (Decimal(31) * rates["202610"] - Decimal(3) * rates["202611"]) / Decimal(28)
    september_start = rates["202608"]
    september_end = october_start
    expected_move_percent = september_end - september_start
    expected_steps = expected_move_percent / TWENTY_FIVE_BPS_PERCENT
    lower_step = int(expected_steps.to_integral_value(rounding=ROUND_FLOOR))
    fractional = expected_steps - Decimal(lower_step)
    lower_probability = Decimal("1") - fractional
    upper_probability = fractional
    modeled_september = (Decimal(16) * september_start + Decimal(14) * september_end) / Decimal(30)
    residual_bps = (rates["202609"] - modeled_september) * ONE_HUNDRED

    zero = theoretical_settlement(september_start, Decimal("0"))
    plus_25 = theoretical_settlement(september_start, Decimal("25"))
    long_probability = executable_probability(prices.september, zero, plus_25)
    bucket_probabilities = {
        "DEC50PLUS": Decimal("0"),
        "DEC25": Decimal("0"),
        "NO_CHANGE": Decimal("0"),
        "INC25": Decimal("0"),
        "INC50PLUS": Decimal("0"),
    }
    for move_bps, probability in (
        (lower_step * 25, lower_probability),
        ((lower_step + 1) * 25, upper_probability),
    ):
        if move_bps <= -50:
            bucket = "DEC50PLUS"
        elif move_bps == -25:
            bucket = "DEC25"
        elif move_bps == 0:
            bucket = "NO_CHANGE"
        elif move_bps == 25:
            bucket = "INC25"
        else:
            bucket = "INC50PLUS"
        bucket_probabilities[bucket] += probability

    return ProbabilitySnapshot(
        rates=rates,
        september_start_effr=september_start,
        september_end_effr=september_end,
        october_start_effr=october_start,
        expected_move_bps=expected_move_percent * ONE_HUNDRED,
        expected_steps=expected_steps,
        lower_step_bps=lower_step * 25,
        lower_probability=lower_probability,
        upper_step_bps=(lower_step + 1) * 25,
        upper_probability=upper_probability,
        bucket_probabilities=bucket_probabilities,
        september_residual_bps=residual_bps,
        executable_long_probability=long_probability,
        executable_short_probability=long_probability,
        valid=Decimal("0") <= long_probability <= Decimal("1"),
        reason=(
            "reference tree calculated"
            if Decimal("0") <= long_probability <= Decimal("1")
            else "executable binary probability is outside [0,1]"
        ),
    )
