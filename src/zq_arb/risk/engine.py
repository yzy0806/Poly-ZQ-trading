from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import Opportunity


class GateContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    now: datetime
    ibkr_connected: bool
    ibkr_data_live: bool
    polymarket_connected: bool
    mapping_verified: bool
    eligibility_checked: bool
    eligibility_blocked: bool | None
    eligibility_country: str | None
    books_fresh: bool
    quotes_fresh: bool
    cross_venue_synchronized: bool
    contract_verified: bool
    full_hedge_depth_available: bool
    margin_preview_available: bool
    projected_full_excess_liquidity: Decimal | None
    projected_margin_cushion: Decimal | None
    next_batch_initial_margin: Decimal | None
    current_zq_position: int
    active_batches: int
    unresolved_hedge: bool
    reconciliation_clean: bool
    critical_alert_active: bool
    paused: bool
    kill_switch: bool
    strategy_daily_pnl: Decimal | None
    strategy_drawdown: Decimal | None


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tradeable: bool
    reasons: tuple[str, ...]


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def qualify(self, opportunity: Opportunity, context: GateContext) -> Qualification:
        reasons: list[str] = []
        settings = self.settings
        now = context.now.astimezone(UTC)

        if not context.ibkr_connected:
            reasons.append("IBKR disconnected")
        if not context.ibkr_data_live:
            reasons.append("IBKR data is not live")
        if not context.polymarket_connected:
            reasons.append("Polymarket disconnected")
        if not context.mapping_verified:
            reasons.append("Polymarket mapping or rule hash unverified")
        if not context.eligibility_checked:
            reasons.append("geographic eligibility not checked")
        elif context.eligibility_blocked is not False:
            reasons.append("geographic eligibility is blocked or indeterminate")
        if settings.run_mode.is_live and context.eligibility_country != "HK":
            reasons.append("live deployment country is not HK")
        if not context.books_fresh:
            reasons.append("Polymarket books are stale")
        if not context.quotes_fresh:
            reasons.append("ZQ quotes are stale")
        if not context.cross_venue_synchronized:
            reasons.append("cross-venue timestamps are not synchronized")
        if not context.contract_verified:
            reasons.append("ZQ contract details are not verified")
        if not context.full_hedge_depth_available:
            reasons.append("full 10-contract hedge depth unavailable")
        if not context.margin_preview_available:
            reasons.append("IBKR what-if margin preview unavailable")
        if context.next_batch_initial_margin is None:
            reasons.append("next-batch initial margin unavailable")
        if context.projected_full_excess_liquidity is None:
            reasons.append("projected full excess liquidity unavailable")
        elif context.next_batch_initial_margin is not None:
            minimum = max(
                settings.min_full_excess_liquidity_usd,
                settings.min_excess_liquidity_margin_multiplier * context.next_batch_initial_margin,
            )
            if context.projected_full_excess_liquidity < minimum:
                reasons.append("projected full excess liquidity below limit")
        if context.projected_margin_cushion is None:
            reasons.append("projected margin cushion unavailable")
        elif context.projected_margin_cushion < settings.min_margin_cushion_ratio:
            reasons.append("projected margin cushion below limit")
        if (
            context.current_zq_position + settings.ibkr_zq_child_order_quantity
            > settings.max_zq_position
        ):
            reasons.append("aggregate ZQ limit would be exceeded")
        if context.active_batches >= settings.max_open_batches:
            reasons.append("another batch is active")
        if context.unresolved_hedge:
            reasons.append("hedge deficit is unresolved")
        if not context.reconciliation_clean:
            reasons.append("venue reconciliation is not clean")
        if context.critical_alert_active:
            reasons.append("critical alert is active")
        if context.paused:
            reasons.append("new trades are paused")
        if context.kill_switch:
            reasons.append("kill switch is active")
        if (
            context.strategy_daily_pnl is not None
            and context.strategy_daily_pnl <= -settings.max_daily_loss_usd
        ):
            reasons.append("daily strategy loss limit reached")
        if (
            context.strategy_drawdown is not None
            and context.strategy_drawdown >= settings.max_strategy_drawdown_usd
        ):
            reasons.append("strategy drawdown limit reached")
        if now >= settings.fomc_trading_cutoff_utc:
            reasons.append("FOMC new-batch cutoff has begun")
        if opportunity.minimum_net_profit is None:
            reasons.append("minimum scenario profit unavailable")
        elif opportunity.minimum_net_profit < settings.min_net_profit_usd:
            reasons.append("minimum scenario profit below threshold")
        if opportunity.return_on_capital_bps is None:
            reasons.append("return on committed capital unavailable")
        elif opportunity.return_on_capital_bps < settings.min_return_on_capital_bps:
            reasons.append("return on committed capital below threshold")
        reasons.extend(opportunity.gate_reasons)
        if settings.run_mode is RunMode.READ_ONLY:
            reasons.append("READ_ONLY mode prohibits orders")

        return Qualification(tradeable=not reasons, reasons=tuple(dict.fromkeys(reasons)))

    def live_readiness(self) -> Qualification:
        errors = self.settings.live_readiness_errors()
        return Qualification(tradeable=not errors, reasons=tuple(errors))
