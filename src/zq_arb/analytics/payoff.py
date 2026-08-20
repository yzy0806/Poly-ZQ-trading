from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from zq_arb.analytics.probability import theoretical_settlement
from zq_arb.domain.enums import Side
from zq_arb.domain.models import BookLevel, Opportunity, OrderBook, ScenarioPnl

FUTURES_POINT_VALUE = Decimal("4167")
FULL_MONTH_BP_VALUE = Decimal("41.67")
APPROVED_SCENARIOS = (0, 25, 50)


@dataclass(frozen=True, slots=True)
class DepthCost:
    requested_shares: Decimal
    filled_shares: Decimal
    total_cost: Decimal
    vwap: Decimal | None
    sufficient: bool
    worst_price: Decimal | None


@dataclass(frozen=True, slots=True)
class CostInputs:
    ibkr_commission: Decimal = Decimal("0")
    polymarket_fees: Decimal = Decimal("0")
    zq_slippage_reserve: Decimal = Decimal("0")
    polymarket_slippage_reserve: Decimal = Decimal("0")
    rounding_reserve: Decimal = Decimal("0")
    model_reserve: Decimal = Decimal("0")
    operational_reserve: Decimal = Decimal("0")
    effr_basis_reserve: Decimal = Decimal("0")

    @property
    def explicit_costs(self) -> Decimal:
        return (
            self.ibkr_commission
            + self.polymarket_fees
            + self.zq_slippage_reserve
            + self.polymarket_slippage_reserve
            + self.rounding_reserve
        )

    @property
    def reserves(self) -> Decimal:
        return self.model_reserve + self.operational_reserve + self.effr_basis_reserve


def hedge_shares_per_contract(move_bps: int) -> Decimal:
    # Multiply before division so the approved $41.67 approximation does not
    # create a repeating Decimal that rounds 486.15 up by an unintended cent.
    return Decimal(abs(move_bps)) * Decimal(14) * FULL_MONTH_BP_VALUE / Decimal(30)


def round_shares_up(shares: Decimal, precision: Decimal = Decimal("0.01")) -> Decimal:
    return shares.quantize(precision, rounding=ROUND_CEILING)


def walk_asks(
    levels: tuple[BookLevel, ...] | list[BookLevel],
    shares: Decimal,
    *,
    price_cap: Decimal | None = None,
) -> DepthCost:
    if shares <= 0:
        raise ValueError("requested shares must be positive")
    remaining = shares
    total = Decimal("0")
    worst: Decimal | None = None
    for level in sorted(levels, key=lambda item: item.price):
        if price_cap is not None and level.price > price_cap:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        total += take * level.price
        remaining -= take
        worst = level.price
        if remaining == 0:
            break
    filled = shares - remaining
    return DepthCost(
        requested_shares=shares,
        filled_shares=filled,
        total_cost=total,
        vwap=(total / filled if filled > 0 else None),
        sufficient=remaining == 0,
        worst_price=worst,
    )


def maker_price(book: OrderBook, price_cap: Decimal) -> Decimal | None:
    """Return the highest non-marketable bid, rounded down to the active tick."""

    if book.best_bid is None or book.tick_size is None or book.tick_size <= 0:
        return None
    if book.best_ask is not None and book.best_bid >= book.best_ask:
        return None
    bounded = min(book.best_bid, price_cap)
    tick_count = (bounded / book.tick_size).to_integral_value(rounding=ROUND_FLOOR)
    rounded = tick_count * book.tick_size
    return rounded if rounded > 0 else None


def _payout(outcome_code: str, move_bps: int, *, yes_side: bool) -> Decimal:
    event_occurs = (outcome_code == "INC25" and move_bps == 25) or (
        outcome_code == "INC50PLUS" and move_bps >= 50
    )
    payout = Decimal("1") if event_occurs else Decimal("0")
    return payout if yes_side else Decimal("1") - payout


def build_three_state_opportunity(
    *,
    direction: str,
    contracts: int,
    zq_price: Decimal,
    pre_meeting_effr: Decimal,
    inc25_book: OrderBook,
    inc50_book: OrderBook,
    cost_inputs: CostInputs,
    incremental_margin: Decimal,
    emergency_cash_reserve: Decimal,
    post_price_cap: Decimal,
    emergency_price_cap: Decimal,
) -> Opportunity:
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    normalized_direction = direction.upper()
    if normalized_direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")

    yes_side = normalized_direction == "LONG"
    q25 = round_shares_up(hedge_shares_per_contract(25) * Decimal(contracts))
    q50 = round_shares_up(hedge_shares_per_contract(50) * Decimal(contracts))
    post25 = maker_price(inc25_book, post_price_cap)
    post50 = maker_price(inc50_book, post_price_cap)
    depth25 = walk_asks(inc25_book.asks, q25, price_cap=emergency_price_cap)
    depth50 = walk_asks(inc50_book.asks, q50, price_cap=emergency_price_cap)
    reasons: list[str] = []
    if post25 is None:
        reasons.append("INC25 has no valid post-only maker price")
    if post50 is None:
        reasons.append("INC50PLUS has no valid post-only maker price")
    if not depth25.sufficient:
        reasons.append("insufficient INC25 depth inside cap")
    if not depth50.sufficient:
        reasons.append("insufficient INC50PLUS depth inside cap")
    if post25 is None or post50 is None or depth25.vwap is None or depth50.vwap is None:
        reasons.append("order book is empty")
        return Opportunity(
            direction=normalized_direction,
            zq_side=Side.BUY if yes_side else Side.SELL,
            zq_price=zq_price,
            contracts=contracts,
            token_requirements={"INC25": q25, "INC50PLUS": q50},
            gate_reasons=tuple(dict.fromkeys(reasons)),
        )

    def scenario_matrix(price25: Decimal, price50: Decimal) -> tuple[ScenarioPnl, ...]:
        scenarios: list[ScenarioPnl] = []
        for move in APPROVED_SCENARIOS:
            settlement = theoretical_settlement(pre_meeting_effr, Decimal(move))
            futures_pnl = (
                Decimal(contracts)
                * FUTURES_POINT_VALUE
                * (settlement - zq_price if yes_side else zq_price - settlement)
            )
            polymarket_pnl = q25 * (_payout("INC25", move, yes_side=yes_side) - price25)
            polymarket_pnl += q50 * (_payout("INC50PLUS", move, yes_side=yes_side) - price50)
            scenarios.append(
                ScenarioPnl(
                    move_bps=move,
                    futures_pnl=futures_pnl,
                    polymarket_pnl=polymarket_pnl,
                    costs=cost_inputs.explicit_costs,
                    reserves=cost_inputs.reserves,
                    net_pnl=(
                        futures_pnl
                        + polymarket_pnl
                        - cost_inputs.explicit_costs
                        - cost_inputs.reserves
                    ),
                )
            )
        return tuple(scenarios)

    passive_scenarios = scenario_matrix(post25, post50)
    emergency_scenarios = scenario_matrix(depth25.vwap, depth50.vwap)
    passive_minimum = min(scenario.net_pnl for scenario in passive_scenarios)
    emergency_minimum = min(scenario.net_pnl for scenario in emergency_scenarios)
    minimum = min(passive_minimum, emergency_minimum)
    emergency_token_cost = depth25.total_cost + depth50.total_cost
    committed_capital = emergency_token_cost + incremental_margin + emergency_cash_reserve
    return_bps = minimum / committed_capital * Decimal("10000") if committed_capital > 0 else None
    return Opportunity(
        direction=normalized_direction,
        zq_side=Side.BUY if yes_side else Side.SELL,
        zq_price=zq_price,
        contracts=contracts,
        token_requirements={"INC25": q25, "INC50PLUS": q50},
        token_prices={"INC25": post25, "INC50PLUS": post50},
        emergency_token_prices={"INC25": depth25.vwap, "INC50PLUS": depth50.vwap},
        scenarios=passive_scenarios,
        emergency_scenarios=emergency_scenarios,
        passive_minimum_net_profit=passive_minimum,
        emergency_minimum_net_profit=emergency_minimum,
        minimum_net_profit=minimum,
        committed_capital=committed_capital,
        return_on_capital_bps=return_bps,
        tradeable=False,
        gate_reasons=tuple(reasons),
    )
