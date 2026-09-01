from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from zq_arb.analytics.probability import theoretical_settlement
from zq_arb.domain.enums import GateStatus
from zq_arb.domain.models import (
    BookLevel,
    GateCheck,
    HedgeDepthView,
    Opportunity,
    OpportunityCalculation,
    OpportunityCostBreakdown,
    OrderBook,
    ScenarioPnl,
)

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
    """Return the highest valid post-only BUY price under the hard cap."""

    if book.best_ask is None or book.tick_size is None or book.tick_size <= 0:
        return None
    if book.best_bid is not None and book.best_bid >= book.best_ask:
        return None
    bounded = min(book.best_ask - book.tick_size, price_cap)
    tick_count = (bounded / book.tick_size).to_integral_value(rounding=ROUND_FLOOR)
    rounded = tick_count * book.tick_size
    return rounded if rounded > 0 else None


def _payout(outcome_code: str, move_bps: int) -> Decimal:
    event_occurs = (outcome_code == "INC25" and move_bps == 25) or (
        outcome_code == "INC50PLUS" and move_bps >= 50
    )
    return Decimal("1") if event_occurs else Decimal("0")


def build_three_state_opportunity(
    *,
    contracts: int,
    zq_price: Decimal,
    pre_meeting_effr: Decimal,
    inc25_book: OrderBook,
    inc50_book: OrderBook,
    cost_inputs: CostInputs,
    incremental_margin: Decimal | None,
    emergency_cash_reserve: Decimal,
    post_price_cap: Decimal,
    emergency_price_cap: Decimal,
) -> Opportunity:
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    q25 = round_shares_up(hedge_shares_per_contract(25) * Decimal(contracts))
    q50 = round_shares_up(hedge_shares_per_contract(50) * Decimal(contracts))
    post25 = maker_price(inc25_book, post_price_cap)
    post50 = maker_price(inc50_book, post_price_cap)
    depth25 = walk_asks(inc25_book.asks, q25, price_cap=emergency_price_cap)
    depth50 = walk_asks(inc50_book.asks, q50, price_cap=emergency_price_cap)
    depth_views = (
        HedgeDepthView(
            leg_code="INC25 YES",
            required_shares=q25,
            available_shares=depth25.filled_shares,
            shortfall_shares=max(Decimal("0"), q25 - depth25.filled_shares),
            price_cap=emergency_price_cap,
            maker_price=post25,
            emergency_vwap=depth25.vwap,
            worst_price=depth25.worst_price,
            sufficient=depth25.sufficient,
        ),
        HedgeDepthView(
            leg_code="INC50PLUS YES",
            required_shares=q50,
            available_shares=depth50.filled_shares,
            shortfall_shares=max(Decimal("0"), q50 - depth50.filled_shares),
            price_cap=emergency_price_cap,
            maker_price=post50,
            emergency_vwap=depth50.vwap,
            worst_price=depth50.worst_price,
            sufficient=depth50.sufficient,
        ),
    )
    checks: list[GateCheck] = []
    for detail, book in zip(depth_views, (inc25_book, inc50_book), strict=True):
        maker_available = detail.maker_price is not None
        maker_inputs = (
            f"bid={book.best_bid}; ask={book.best_ask}; tick={book.tick_size}; "
            f"cap={post_price_cap}"
        )
        checks.append(
            GateCheck(
                code=f"{detail.leg_code.replace(' ', '_')}_MAKER_PRICE",
                category="HEDGE_LIQUIDITY",
                label=f"{detail.leg_code} post-only maker price",
                status=GateStatus.PASSED if maker_available else GateStatus.UNAVAILABLE,
                actual_value=(
                    f"{detail.maker_price} ({maker_inputs})" if maker_available else maker_inputs
                ),
                operator="<=",
                required_value=str(post_price_cap),
                unit="USD/share",
                detail=(
                    f"{detail.leg_code} post-only price {detail.maker_price} is inside cap "
                    f"{post_price_cap}"
                    if maker_available
                    else (
                        f"{detail.leg_code} cannot derive a post-only BUY price: "
                        f"{maker_inputs}; a valid ask and positive tick are required, and "
                        "the book must be uncrossed"
                    )
                ),
            )
        )
        checks.append(
            GateCheck(
                code=f"{detail.leg_code.replace(' ', '_')}_EMERGENCY_DEPTH",
                category="HEDGE_LIQUIDITY",
                label=f"{detail.leg_code} emergency depth inside cap",
                status=GateStatus.PASSED if detail.sufficient else GateStatus.FAILED,
                actual_value=str(detail.available_shares),
                operator=">=",
                required_value=str(detail.required_shares),
                unit="shares",
                detail=(
                    f"{detail.leg_code} has {detail.available_shares} shares inside the "
                    f"{detail.price_cap} cap; {detail.required_shares} required"
                ),
            )
        )
    reasons = [check.detail for check in checks if not check.passed]
    if any(not detail.sufficient or detail.maker_price is None for detail in depth_views):
        return Opportunity(
            zq_price=zq_price,
            contracts=contracts,
            token_requirements={"INC25": q25, "INC50PLUS": q50},
            hedge_depth=depth_views,
            gate_reasons=tuple(dict.fromkeys(reasons)),
            gate_checks=tuple(checks),
        )
    assert post25 is not None
    assert post50 is not None
    assert depth25.vwap is not None
    assert depth50.vwap is not None

    def scenario_matrix(price25: Decimal, price50: Decimal) -> tuple[ScenarioPnl, ...]:
        scenarios: list[ScenarioPnl] = []
        for move in APPROVED_SCENARIOS:
            settlement = theoretical_settlement(pre_meeting_effr, Decimal(move))
            futures_price_change = settlement - zq_price
            futures_pnl = (
                Decimal(contracts) * FUTURES_POINT_VALUE * futures_price_change
            )
            inc25_payout = _payout("INC25", move)
            inc50plus_payout = _payout("INC50PLUS", move)
            inc25_pnl = q25 * (inc25_payout - price25)
            inc50plus_pnl = q50 * (inc50plus_payout - price50)
            polymarket_pnl = inc25_pnl + inc50plus_pnl
            gross_pnl = futures_pnl + polymarket_pnl
            scenarios.append(
                ScenarioPnl(
                    move_bps=move,
                    settlement_price=settlement,
                    zq_entry_price=zq_price,
                    contracts=contracts,
                    futures_point_value=FUTURES_POINT_VALUE,
                    futures_price_change=futures_price_change,
                    futures_pnl=futures_pnl,
                    inc25_shares=q25,
                    inc25_entry_price=price25,
                    inc25_payout=inc25_payout,
                    inc25_pnl=inc25_pnl,
                    inc50plus_shares=q50,
                    inc50plus_entry_price=price50,
                    inc50plus_payout=inc50plus_payout,
                    inc50plus_pnl=inc50plus_pnl,
                    polymarket_pnl=polymarket_pnl,
                    gross_pnl=gross_pnl,
                    costs=cost_inputs.explicit_costs,
                    reserves=cost_inputs.reserves,
                    net_pnl=(
                        gross_pnl
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
    emergency_hedge_cash = depth25.total_cost + depth50.total_cost
    committed_capital = (
        emergency_hedge_cash + incremental_margin + emergency_cash_reserve
        if incremental_margin is not None
        else None
    )
    return_bps = (
        minimum / committed_capital * Decimal("10000")
        if committed_capital is not None and committed_capital > 0
        else None
    )
    calculation = OpportunityCalculation(
        inc25_shares_per_contract=round_shares_up(hedge_shares_per_contract(25)),
        inc50plus_shares_per_contract=round_shares_up(hedge_shares_per_contract(50)),
        inc25_emergency_hedge_cash=depth25.total_cost,
        inc50plus_emergency_hedge_cash=depth50.total_cost,
        emergency_hedge_cash=emergency_hedge_cash,
        incremental_initial_margin=incremental_margin,
        emergency_cash_reserve=emergency_cash_reserve,
        committed_capital=committed_capital,
        costs=OpportunityCostBreakdown(
            ibkr_commission=cost_inputs.ibkr_commission,
            polymarket_fees=cost_inputs.polymarket_fees,
            zq_slippage_reserve=cost_inputs.zq_slippage_reserve,
            polymarket_slippage_reserve=cost_inputs.polymarket_slippage_reserve,
            rounding_reserve=cost_inputs.rounding_reserve,
            explicit_costs=cost_inputs.explicit_costs,
            model_reserve=cost_inputs.model_reserve,
            operational_reserve=cost_inputs.operational_reserve,
            effr_basis_reserve=cost_inputs.effr_basis_reserve,
            reserves=cost_inputs.reserves,
        ),
    )
    return Opportunity(
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
        calculation=calculation,
        hedge_depth=depth_views,
        tradeable=False,
        gate_reasons=tuple(reasons),
        gate_checks=tuple(checks),
    )
