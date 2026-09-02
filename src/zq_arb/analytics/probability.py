from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from zq_arb.domain.models import FedWatchDiagnostic, ProbabilitySnapshot

ONE_HUNDRED = Decimal("100")
TWENTY_FIVE_BPS_PERCENT = Decimal("0.25")
TWENTY_FIVE_BPS = Decimal("25")
MIN_MODELED_MOVE_BPS = Decimal("-50")
MAX_MODELED_MOVE_BPS = Decimal("50")
MOVE_BY_BUCKET = {
    "DEC50PLUS": Decimal("-50"),
    "DEC25": Decimal("-25"),
    "NO_CHANGE": Decimal("0"),
    "INC25": Decimal("25"),
    "INC50PLUS": Decimal("50"),
}


@dataclass(frozen=True, slots=True)
class DiagnosticPrices:
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


def implied_decision_move_bps(
    futures_price: Decimal,
    pre_meeting_effr_percent: Decimal,
    *,
    days_before: int = 16,
    days_after: int = 14,
) -> Decimal:
    if days_before < 0 or days_after <= 0:
        raise ValueError("calendar weights must include positive post-decision days")
    total_days = Decimal(days_before + days_after)
    monthly_average = implied_average_effr(futures_price)
    return (
        (monthly_average - pre_meeting_effr_percent)
        * ONE_HUNDRED
        * total_days
        / Decimal(days_after)
    )


def _bucket_for_move(move_bps: int) -> str:
    if move_bps <= -50:
        return "DEC50PLUS"
    if move_bps == -25:
        return "DEC25"
    if move_bps == 0:
        return "NO_CHANGE"
    if move_bps == 25:
        return "INC25"
    return "INC50PLUS"


def adjacent_outcome_distribution(
    expected_move_bps: Decimal,
) -> tuple[int | None, Decimal | None, int | None, Decimal | None, dict[str, Decimal]]:
    empty = {code: Decimal("0") for code in MOVE_BY_BUCKET}
    if expected_move_bps < MIN_MODELED_MOVE_BPS or expected_move_bps > MAX_MODELED_MOVE_BPS:
        return None, None, None, None, empty

    if expected_move_bps == MAX_MODELED_MOVE_BPS:
        lower = 25
        upper = 50
    else:
        lower = (
            int((expected_move_bps / TWENTY_FIVE_BPS).to_integral_value(rounding=ROUND_FLOOR)) * 25
        )
        upper = lower + 25
    upper_probability = (expected_move_bps - Decimal(lower)) / TWENTY_FIVE_BPS
    lower_probability = Decimal("1") - upper_probability
    empty[_bucket_for_move(lower)] += lower_probability
    empty[_bucket_for_move(upper)] += upper_probability
    return lower, lower_probability, upper, upper_probability, empty


def direct_zq_probability(
    *,
    target_contract_month: str,
    target_bid: Decimal,
    target_ask: Decimal,
    pre_meeting_effr: Decimal,
    fedwatch: FedWatchDiagnostic | None = None,
    days_before: int = 16,
    days_after: int = 14,
) -> ProbabilitySnapshot:
    if target_bid <= 0 or target_ask <= 0 or target_bid > target_ask:
        raise ValueError("target ZQ bid and ask must form a positive, non-crossed market")
    target_mid = (target_bid + target_ask) / Decimal("2")
    move_mid = implied_decision_move_bps(
        target_mid,
        pre_meeting_effr,
        days_before=days_before,
        days_after=days_after,
    )
    move_buy = implied_decision_move_bps(
        target_bid,
        pre_meeting_effr,
        days_before=days_before,
        days_after=days_after,
    )
    move_bid_reference = implied_decision_move_bps(
        target_ask,
        pre_meeting_effr,
        days_before=days_before,
        days_after=days_after,
    )
    lower, lower_probability, upper, upper_probability, buckets = adjacent_outcome_distribution(
        move_mid
    )
    executable_buy_probability: Decimal | None = None
    bid_reference_probability: Decimal | None = None
    if lower is not None and upper is not None:
        width = Decimal(upper - lower)
        executable_buy_probability = (move_buy - Decimal(lower)) / width
        bid_reference_probability = (move_bid_reference - Decimal(lower)) / width

    probabilities = (
        lower_probability,
        upper_probability,
        executable_buy_probability,
        bid_reference_probability,
    )
    valid = lower is not None and all(
        value is not None and Decimal("0") <= value <= Decimal("1") for value in probabilities
    )
    return ProbabilitySnapshot(
        target_contract_month=target_contract_month,
        target_bid=target_bid,
        target_ask=target_ask,
        target_mid=target_mid,
        pre_meeting_effr=pre_meeting_effr,
        post_decision_weight=Decimal(days_after) / Decimal(days_before + days_after),
        implied_average_effr_bid=implied_average_effr(target_bid),
        implied_average_effr_ask=implied_average_effr(target_ask),
        implied_average_effr_mid=implied_average_effr(target_mid),
        expected_move_bps=move_mid,
        executable_buy_expected_move_bps=move_buy,
        bid_reference_expected_move_bps=move_bid_reference,
        lower_step_bps=lower,
        lower_probability=lower_probability,
        upper_step_bps=upper,
        upper_probability=upper_probability,
        bucket_probabilities=buckets,
        executable_buy_probability=executable_buy_probability,
        bid_reference_probability=bid_reference_probability,
        fedwatch=fedwatch or FedWatchDiagnostic(),
        valid=valid,
        reason=(
            "direct ZQU6 adjacent-outcome model calculated"
            if valid
            else "direct ZQU6 result is outside the modeled adjacent-outcome range"
        ),
    )


def with_polymarket_expectation(
    snapshot: ProbabilitySnapshot,
    mid_probabilities: Mapping[str, Decimal],
) -> ProbabilitySnapshot:
    probability_sum = sum(mid_probabilities.values(), start=Decimal("0"))
    expected_move: Decimal | None = None
    if probability_sum > 0:
        weighted_move = sum(
            (
                mid_probabilities.get(code, Decimal("0")) * move
                for code, move in MOVE_BY_BUCKET.items()
            ),
            start=Decimal("0"),
        )
        expected_move = weighted_move / probability_sum
    gap = (
        snapshot.expected_move_bps - expected_move
        if snapshot.expected_move_bps is not None and expected_move is not None
        else None
    )
    return snapshot.model_copy(
        update={
            "polymarket_probability_sum": probability_sum,
            "polymarket_expected_move_bps": expected_move,
            "expected_move_gap_bps": gap,
        }
    )


def fedwatch_reference(
    prices: DiagnosticPrices,
    *,
    pre_meeting_effr: Decimal,
) -> FedWatchDiagnostic:
    rates = {
        "202609": implied_average_effr(prices.september),
        "202610": implied_average_effr(prices.october),
        "202611": implied_average_effr(prices.november),
    }
    october_start = (Decimal(31) * rates["202610"] - Decimal(3) * rates["202611"]) / Decimal(28)
    september_start = pre_meeting_effr
    september_end = october_start
    expected_move_percent = september_end - september_start
    expected_steps = expected_move_percent / TWENTY_FIVE_BPS_PERCENT
    lower_step = int(expected_steps.to_integral_value(rounding=ROUND_FLOOR))
    fractional = expected_steps - Decimal(lower_step)
    lower_probability = Decimal("1") - fractional
    upper_probability = fractional
    modeled_september = (Decimal(16) * september_start + Decimal(14) * september_end) / Decimal(30)
    residual_bps = (rates["202609"] - modeled_september) * ONE_HUNDRED
    bucket_probabilities = {code: Decimal("0") for code in MOVE_BY_BUCKET}
    for move_bps, probability in (
        (lower_step * 25, lower_probability),
        ((lower_step + 1) * 25, upper_probability),
    ):
        bucket_probabilities[_bucket_for_move(move_bps)] += probability

    return FedWatchDiagnostic(
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
        valid=True,
        reason="manual or official EFFR plus September-November diagnostic calculated",
    )
