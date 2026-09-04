from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from zq_arb.config import Settings
from zq_arb.domain.enums import ConnectionStatus, GateStatus, RunMode, Side
from zq_arb.domain.models import GateCheck, Opportunity


class GateContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    now: datetime
    ibkr_status: ConnectionStatus
    polymarket_connected: bool
    mapping_verified: bool
    eligibility_checked: bool
    eligibility_blocked: bool | None
    eligibility_country: str | None
    polymarket_books_synchronized: bool
    target_subscription_qualified: bool
    effr_qualified: bool
    cross_venue_snapshot_qualified: bool
    contract_verified: bool
    full_hedge_depth_available: bool
    margin_preview_available: bool | None
    margin_preview_actual: str
    margin_preview_detail: str
    projected_full_excess_liquidity: Decimal | None
    projected_margin_cushion: Decimal | None
    next_batch_initial_margin: Decimal | None
    current_zq_position: int
    active_batches: int
    unresolved_hedge: bool
    reconciliation_clean: bool
    reconciliation_detail: str
    critical_alert_active: bool
    paused: bool
    kill_switch: bool
    cross_venue_checks: tuple[GateCheck, ...] = ()


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tradeable: bool
    reasons: tuple[str, ...]
    checks: tuple[GateCheck, ...] = ()


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def qualify(self, opportunity: Opportunity, context: GateContext) -> Qualification:
        checks: list[GateCheck] = []
        settings = self.settings
        now = context.now.astimezone(UTC)

        def add(
            code: str,
            category: str,
            label: str,
            passed: bool | None,
            actual: Any,
            operator: str,
            required: Any,
            failure_reason: str,
            *,
            unit: str | None = None,
            pass_detail: str | None = None,
            blocking: bool = True,
            applicable: bool = True,
        ) -> None:
            if not applicable:
                status = GateStatus.NOT_APPLICABLE
                detail = pass_detail or f"{label} is not applicable"
            elif passed is None:
                status = GateStatus.UNAVAILABLE
                detail = failure_reason
            elif passed:
                status = GateStatus.PASSED
                detail = pass_detail or f"{label} passed"
            else:
                status = GateStatus.FAILED
                detail = failure_reason
            checks.append(
                GateCheck(
                    code=code,
                    category=category,
                    label=label,
                    status=status,
                    blocking=blocking and applicable,
                    actual_value=_value(actual) if actual is not None else "unavailable",
                    operator=operator,
                    required_value=_value(required),
                    unit=unit,
                    detail=detail,
                    observed_at=now,
                )
            )

        add(
            "IBKR_CONNECTION",
            "VENUE",
            "IBKR connection",
            context.ibkr_status is ConnectionStatus.CONNECTED,
            context.ibkr_status.value,
            "==",
            ConnectionStatus.CONNECTED.value,
            f"IBKR connection is {context.ibkr_status.value}",
        )
        add(
            "POLYMARKET_CONNECTION",
            "VENUE",
            "Polymarket connection",
            context.polymarket_connected,
            "CONNECTED" if context.polymarket_connected else "DISCONNECTED",
            "==",
            "CONNECTED",
            "Polymarket disconnected",
        )
        add(
            "MARKET_MAPPING",
            "MARKET_RULES",
            "Polymarket mapping and rule hash",
            context.mapping_verified,
            "VERIFIED" if context.mapping_verified else "UNVERIFIED",
            "==",
            "VERIFIED",
            "Polymarket mapping or rule hash unverified",
        )
        add(
            "ELIGIBILITY_CHECKED",
            "ELIGIBILITY",
            "Geographic eligibility check",
            context.eligibility_checked,
            "CHECKED" if context.eligibility_checked else "NOT_CHECKED",
            "==",
            "CHECKED",
            "geographic eligibility not checked",
        )
        eligibility_actual = (
            "BLOCKED"
            if context.eligibility_blocked is True
            else "OPEN"
            if context.eligibility_blocked is False
            else "INDETERMINATE"
        )
        add(
            "ELIGIBILITY_OPENING",
            "ELIGIBILITY",
            "Opening-order eligibility",
            context.eligibility_blocked is False if context.eligibility_checked else None,
            eligibility_actual,
            "==",
            "OPEN",
            "geographic eligibility is blocked or indeterminate",
            applicable=context.eligibility_checked,
        )
        add(
            "LIVE_COUNTRY",
            "ELIGIBILITY",
            "Live deployment country",
            context.eligibility_country == "HK",
            context.eligibility_country,
            "==",
            "HK",
            "live deployment country is not HK",
            applicable=settings.run_mode.is_live,
        )

        if context.cross_venue_checks:
            checks.extend(context.cross_venue_checks)
        else:
            add(
                "POLYMARKET_BOOK_SYNC",
                "CROSS_VENUE",
                "Polymarket hedge-book synchronization",
                context.polymarket_books_synchronized,
                context.polymarket_books_synchronized,
                "==",
                True,
                "Polymarket hedge books are WebSocket-unsynchronized",
            )
            add(
                "ZQU6_SUBSCRIPTION_QUALIFIED",
                "CROSS_VENUE",
                "ZQU6 subscription qualification",
                context.target_subscription_qualified,
                context.target_subscription_qualified,
                "==",
                True,
                "ZQU6 current-generation live subscription is not qualified",
            )
            add(
                "PRE_MEETING_EFFR",
                "CROSS_VENUE",
                "Pre-meeting EFFR qualification",
                context.effr_qualified,
                context.effr_qualified,
                "==",
                True,
                "pre-meeting EFFR is not qualified",
            )
            add(
                "CROSS_VENUE_SNAPSHOT",
                "CROSS_VENUE",
                "Cross-venue immutable snapshot",
                context.cross_venue_snapshot_qualified,
                context.cross_venue_snapshot_qualified,
                "==",
                True,
                "cross-venue snapshot is not execution-qualified",
            )

        add(
            "ZQ_CONTRACT_VERIFIED",
            "CONTRACT",
            "ZQ contract details",
            context.contract_verified,
            "VERIFIED" if context.contract_verified else "UNVERIFIED",
            "==",
            "VERIFIED",
            "ZQ contract details are not verified",
        )
        add(
            "LONG_ONLY_DIRECTION",
            "STRATEGY",
            "Version-one opportunity direction",
            opportunity.direction == "LONG",
            opportunity.direction,
            "==",
            "LONG",
            "version one permits long ZQ opportunities only",
        )
        add(
            "ZQ_ENTRY_SIDE",
            "STRATEGY",
            "New ZQ entry side",
            opportunity.zq_side is Side.BUY,
            opportunity.zq_side.value,
            "==",
            Side.BUY.value,
            "new ZQ entries must be BUY orders",
        )

        if opportunity.gate_checks:
            checks.extend(opportunity.gate_checks)
        else:
            add(
                "FULL_HEDGE_DEPTH",
                "HEDGE_LIQUIDITY",
                "Full 10-contract hedge depth",
                context.full_hedge_depth_available,
                context.full_hedge_depth_available,
                "==",
                True,
                "full 10-contract hedge depth unavailable",
            )

        add(
            "IBKR_MARGIN_PREVIEW",
            "MARGIN",
            "IBKR what-if margin preview",
            context.margin_preview_available,
            context.margin_preview_actual,
            "==",
            "CURRENT",
            context.margin_preview_detail,
        )
        add(
            "NEXT_BATCH_INITIAL_MARGIN",
            "MARGIN",
            "Next-batch initial margin",
            context.next_batch_initial_margin >= 0
            if context.next_batch_initial_margin is not None
            else None,
            context.next_batch_initial_margin,
            ">=",
            0,
            "next-batch initial margin unavailable",
            unit="USD",
        )
        if context.next_batch_initial_margin is None:
            liquidity_actual = (
                f"{context.projected_full_excess_liquidity} USD"
                if context.projected_full_excess_liquidity is not None
                else "unavailable"
            )
            add(
                "PROJECTED_EXCESS_LIQUIDITY",
                "MARGIN",
                "Projected full excess liquidity",
                None,
                liquidity_actual,
                ">=",
                (
                    f"max({settings.min_full_excess_liquidity_usd} USD, "
                    f"{settings.min_excess_liquidity_margin_multiplier} x unavailable margin)"
                ),
                "required liquidity cannot be calculated because next-batch margin is unavailable",
            )
        else:
            minimum_liquidity = max(
                settings.min_full_excess_liquidity_usd,
                settings.min_excess_liquidity_margin_multiplier * context.next_batch_initial_margin,
            )
            liquidity_passed = (
                context.projected_full_excess_liquidity >= minimum_liquidity
                if context.projected_full_excess_liquidity is not None
                else None
            )
            add(
                "PROJECTED_EXCESS_LIQUIDITY",
                "MARGIN",
                "Projected full excess liquidity",
                liquidity_passed,
                context.projected_full_excess_liquidity,
                ">=",
                minimum_liquidity,
                "projected full excess liquidity unavailable"
                if context.projected_full_excess_liquidity is None
                else "projected full excess liquidity below limit",
                unit="USD",
            )
        cushion_passed = (
            context.projected_margin_cushion >= settings.min_margin_cushion_ratio
            if context.projected_margin_cushion is not None
            else None
        )
        add(
            "PROJECTED_MARGIN_CUSHION",
            "MARGIN",
            "Projected margin cushion",
            cushion_passed,
            context.projected_margin_cushion,
            ">=",
            settings.min_margin_cushion_ratio,
            "projected margin cushion unavailable"
            if context.projected_margin_cushion is None
            else "projected margin cushion below limit",
            unit="ratio",
        )
        add(
            "NET_SHORT_PROHIBITED",
            "POSITION",
            "Current net ZQ position",
            context.current_zq_position >= 0,
            context.current_zq_position,
            ">=",
            0,
            "strategy ZQ position is net short",
            unit="contracts",
        )
        projected_position = context.current_zq_position + settings.ibkr_zq_child_order_quantity
        add(
            "MAX_ZQ_POSITION",
            "POSITION",
            "Projected ZQ position after BUY 10",
            projected_position <= settings.max_zq_position,
            projected_position,
            "<=",
            settings.max_zq_position,
            "aggregate ZQ limit would be exceeded",
            unit="contracts",
        )
        add(
            "ACTIVE_BATCH_LIMIT",
            "EXECUTION",
            "Active 10-contract batches",
            context.active_batches < settings.max_open_batches,
            context.active_batches,
            "<",
            settings.max_open_batches,
            "another batch is active",
            unit="batches",
        )
        add(
            "HEDGE_RECONCILIATION",
            "EXECUTION",
            "Unresolved hedge deficit",
            not context.unresolved_hedge,
            context.unresolved_hedge,
            "==",
            False,
            "hedge deficit is unresolved",
        )
        add(
            "VENUE_RECONCILIATION",
            "EXECUTION",
            "Venue reconciliation",
            context.reconciliation_clean,
            "CLEAN" if context.reconciliation_clean else "NOT_CLEAN",
            "==",
            "CLEAN",
            context.reconciliation_detail,
        )
        add(
            "CRITICAL_ALERT",
            "OPERATIONS",
            "Unresolved critical alert",
            not context.critical_alert_active,
            context.critical_alert_active,
            "==",
            False,
            "critical alert is active",
        )
        add(
            "PAUSE_STATE",
            "OPERATIONS",
            "New-trade pause",
            not context.paused,
            context.paused,
            "==",
            False,
            "new trades are paused",
        )
        add(
            "KILL_SWITCH",
            "OPERATIONS",
            "Kill switch",
            not context.kill_switch,
            context.kill_switch,
            "==",
            False,
            "kill switch is active",
        )

        add(
            "FOMC_CUTOFF",
            "TIME",
            "FOMC new-batch cutoff",
            now < settings.fomc_trading_cutoff_utc,
            now,
            "<",
            settings.fomc_trading_cutoff_utc,
            "FOMC new-batch cutoff has begun",
        )

        profit_passed = (
            opportunity.minimum_net_profit >= settings.min_net_profit_usd
            if opportunity.minimum_net_profit is not None
            else None
        )
        add(
            "MINIMUM_NET_PROFIT",
            "ECONOMICS",
            "Conservative minimum net profit",
            profit_passed,
            opportunity.minimum_net_profit,
            ">=",
            settings.min_net_profit_usd,
            "minimum scenario profit unavailable"
            if opportunity.minimum_net_profit is None
            else "minimum scenario profit below threshold",
            unit="USD",
        )
        return_passed = (
            opportunity.return_on_capital_bps >= settings.min_return_on_capital_bps
            if opportunity.return_on_capital_bps is not None
            else None
        )
        add(
            "MINIMUM_RETURN_ON_CAPITAL",
            "ECONOMICS",
            "Return on committed capital",
            return_passed,
            opportunity.return_on_capital_bps,
            ">=",
            settings.min_return_on_capital_bps,
            "return on committed capital unavailable"
            if opportunity.return_on_capital_bps is None
            else "return on committed capital below threshold",
            unit="bps",
        )
        add(
            "RUN_MODE",
            "MODE",
            "Order-authorizing run mode",
            settings.run_mode is not RunMode.READ_ONLY,
            settings.run_mode.value,
            "!=",
            RunMode.READ_ONLY.value,
            "READ_ONLY mode prohibits orders",
        )

        failed = tuple(
            check for check in checks if check.blocking and check.status is not GateStatus.PASSED
        )
        reasons = tuple(dict.fromkeys(check.detail for check in failed))
        return Qualification(tradeable=not failed, reasons=reasons, checks=tuple(checks))

    def live_readiness(self) -> Qualification:
        errors = self.settings.live_readiness_errors()
        checks = tuple(
            GateCheck(
                code=f"LIVE_READINESS_{index}",
                category="LIVE_READINESS",
                label="Live-readiness configuration",
                status=GateStatus.FAILED,
                actual_value="not ready",
                operator="==",
                required_value="ready",
                detail=error,
            )
            for index, error in enumerate(errors, start=1)
        )
        return Qualification(tradeable=not errors, reasons=tuple(errors), checks=checks)
