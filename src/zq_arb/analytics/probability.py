from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from zq_arb.domain.calendar import MeetingCalendar, next_contract_month
from zq_arb.domain.models import FedWatchDiagnostic, ProbabilitySnapshot

ONE_HUNDRED = Decimal("100")
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


def implied_average_effr(futures_price: Decimal) -> Decimal:
    return ONE_HUNDRED - futures_price


def theoretical_settlement(
    pre_meeting_effr_percent: Decimal,
    move_bps: Decimal,
    *,
    calendar: MeetingCalendar,
) -> Decimal:
    total_days = Decimal(calendar.total_days)
    post_rate = pre_meeting_effr_percent + move_bps / ONE_HUNDRED
    average_rate = (
        Decimal(calendar.days_before) * pre_meeting_effr_percent
        + Decimal(calendar.days_after) * post_rate
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
    calendar: MeetingCalendar,
) -> Decimal:
    total_days = Decimal(calendar.total_days)
    monthly_average = implied_average_effr(futures_price)
    return (
        (monthly_average - pre_meeting_effr_percent)
        * ONE_HUNDRED
        * total_days
        / Decimal(calendar.days_after)
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
    calendar: MeetingCalendar,
) -> ProbabilitySnapshot:
    if target_contract_month != calendar.contract_month:
        raise ValueError("target contract and meeting calendar differ")
    if target_bid <= 0 or target_ask <= 0 or target_bid > target_ask:
        raise ValueError("target ZQ bid and ask must form a positive, non-crossed market")
    target_mid = (target_bid + target_ask) / Decimal("2")
    move_mid = implied_decision_move_bps(
        target_mid,
        pre_meeting_effr,
        calendar=calendar,
    )
    move_buy = implied_decision_move_bps(
        target_bid,
        pre_meeting_effr,
        calendar=calendar,
    )
    move_bid_reference = implied_decision_move_bps(
        target_ask,
        pre_meeting_effr,
        calendar=calendar,
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
        post_decision_weight=Decimal(calendar.days_after) / Decimal(calendar.total_days),
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
            f"direct {calendar.symbol} adjacent-outcome model calculated"
            if valid
            else f"direct {calendar.symbol} result is outside the modeled adjacent-outcome range"
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
    prices: Mapping[str, Decimal],
    *,
    pre_meeting_effr: Decimal,
    calendar: MeetingCalendar,
    anchor_contract_month: str,
    intervening_effective_dates: tuple[date, ...] = (),
) -> FedWatchDiagnostic:
    """Infer the target end rate from an explicitly selected non-meeting anchor.

    Work backwards over intervening monthly averages. A configured rate change
    splits that month into pre/post days; a month with no change is an anchor.
    This is a diagnostic only, never an execution-authorizing signal.
    """
    rates = {month: implied_average_effr(price) for month, price in prices.items()}
    target = calendar.contract_month
    if anchor_contract_month <= target:
        raise ValueError("FedWatch anchor must follow the target month")
    months: list[str] = []
    month = next_contract_month(target)
    while month <= anchor_contract_month:
        months.append(month)
        month = next_contract_month(month)
    if target not in rates or any(month not in rates for month in months):
        return FedWatchDiagnostic(reason="awaiting qualified target and anchor-path quotes")
    changes = {value.strftime("%Y%m"): value for value in intervening_effective_dates}
    if len(changes) != len(intervening_effective_dates) or any(
        month not in months[:-1] for month in changes
    ):
        raise ValueError("invalid intervening calendar or non-meeting anchor")
    post_rate = rates[anchor_contract_month]
    for month in reversed(months[:-1]):
        if month in changes:
            interval = MeetingCalendar(month, changes[month])
            post_rate = (
                Decimal(interval.total_days) * rates[month]
                - Decimal(interval.days_after) * post_rate
            ) / Decimal(interval.days_before)
        else:
            post_rate = rates[month]
    expected_move = (post_rate - pre_meeting_effr) * ONE_HUNDRED
    lower, lower_probability, upper, upper_probability, buckets = adjacent_outcome_distribution(
        expected_move
    )
    modeled_average = (
        Decimal(calendar.days_before) * pre_meeting_effr + Decimal(calendar.days_after) * post_rate
    ) / Decimal(calendar.total_days)
    return FedWatchDiagnostic(
        rates=rates,
        target_contract_month=target,
        anchor_contract_month=anchor_contract_month,
        start_effr=pre_meeting_effr,
        end_effr=post_rate,
        expected_move_bps=expected_move,
        expected_steps=expected_move / TWENTY_FIVE_BPS,
        lower_step_bps=lower,
        lower_probability=lower_probability,
        upper_step_bps=upper,
        upper_probability=upper_probability,
        bucket_probabilities=buckets,
        target_residual_bps=(rates[target] - modeled_average) * ONE_HUNDRED,
        valid=lower is not None,
        reason=f"{calendar.symbol} diagnostic using non-meeting anchor {anchor_contract_month}",
    )
