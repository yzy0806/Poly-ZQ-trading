from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from zq_arb.config import Settings
from zq_arb.domain.enums import ConnectionStatus, RunMode, Side
from zq_arb.domain.models import Opportunity
from zq_arb.risk.engine import GateContext, RiskEngine


def clear_context() -> GateContext:
    return GateContext(
        now=datetime(2026, 8, 20, tzinfo=UTC),
        ibkr_status=ConnectionStatus.CONNECTED,
        polymarket_connected=True,
        mapping_verified=True,
        eligibility_checked=True,
        eligibility_blocked=False,
        eligibility_country="HK",
        polymarket_books_synchronized=True,
        target_subscription_qualified=True,
        effr_qualified=True,
        cross_venue_snapshot_qualified=True,
        contract_verified=True,
        full_hedge_depth_available=True,
        margin_preview_available=True,
        margin_preview_actual="AVAILABLE",
        margin_preview_detail="matching BUY-10 preview is current",
        projected_full_excess_liquidity=Decimal("20000"),
        projected_margin_cushion=Decimal("0.75"),
        next_batch_initial_margin=Decimal("1000"),
        current_zq_position=0,
        active_batches=0,
        unresolved_hedge=False,
        reconciliation_clean=True,
        reconciliation_detail="manual reconciliation confirmed",
        critical_alert_active=False,
        paused=False,
        kill_switch=False,
        strategy_daily_pnl=Decimal("0"),
        strategy_drawdown=Decimal("0"),
    )


def profitable_opportunity() -> Opportunity:
    return Opportunity(
        direction="LONG",
        zq_side=Side.BUY,
        zq_price=Decimal("96.30"),
        contracts=10,
        minimum_net_profit=Decimal("300"),
        committed_capital=Decimal("5000"),
        return_on_capital_bps=Decimal("600"),
    )


def test_every_clear_gate_allows_paper_qualification(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    result = RiskEngine(paper).qualify(profitable_opportunity(), clear_context())
    assert result.tradeable
    assert result.reasons == ()


def test_read_only_always_prohibits_orders(settings: Settings) -> None:
    read_only = settings.model_copy(update={"run_mode": RunMode.READ_ONLY})
    result = RiskEngine(read_only).qualify(profitable_opportunity(), clear_context())
    assert not result.tradeable
    assert "READ_ONLY mode prohibits orders" in result.reasons


def test_any_single_hard_gate_blocks(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    blocked = clear_context().model_copy(update={"unresolved_hedge": True})
    result = RiskEngine(paper).qualify(profitable_opportunity(), blocked)
    assert not result.tradeable
    assert "hedge deficit is unresolved" in result.reasons


def test_daily_loss_limit_is_inclusive(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    blocked = clear_context().model_copy(update={"strategy_daily_pnl": Decimal("-500")})
    result = RiskEngine(paper).qualify(profitable_opportunity(), blocked)
    assert "daily strategy loss limit reached" in result.reasons


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"ibkr_status": ConnectionStatus.DEGRADED}, "IBKR connection is DEGRADED"),
        ({"polymarket_connected": False}, "Polymarket disconnected"),
        ({"mapping_verified": False}, "Polymarket mapping or rule hash unverified"),
        ({"eligibility_checked": False}, "geographic eligibility not checked"),
        ({"eligibility_blocked": True}, "geographic eligibility is blocked or indeterminate"),
        (
            {"polymarket_books_synchronized": False},
            "Polymarket hedge books are WebSocket-unsynchronized",
        ),
        (
            {"target_subscription_qualified": False},
            "ZQU6 current-generation live subscription is not qualified",
        ),
        (
            {"effr_qualified": False},
            "pre-meeting EFFR is not qualified",
        ),
        (
            {"cross_venue_snapshot_qualified": False},
            "cross-venue snapshot is not execution-qualified",
        ),
        ({"contract_verified": False}, "ZQ contract details are not verified"),
        ({"full_hedge_depth_available": False}, "full 10-contract hedge depth unavailable"),
        (
            {
                "margin_preview_available": False,
                "margin_preview_actual": "FAILED",
                "margin_preview_detail": "IBKR what-if rejected",
            },
            "IBKR what-if rejected",
        ),
        ({"next_batch_initial_margin": None}, "next-batch initial margin unavailable"),
        ({"projected_full_excess_liquidity": None}, "projected full excess liquidity unavailable"),
        ({"projected_margin_cushion": None}, "projected margin cushion unavailable"),
        ({"current_zq_position": 100}, "aggregate ZQ limit would be exceeded"),
        ({"active_batches": 1}, "another batch is active"),
        (
            {
                "reconciliation_clean": False,
                "reconciliation_detail": "manual reconciliation has not been confirmed",
            },
            "manual reconciliation has not been confirmed",
        ),
        ({"critical_alert_active": True}, "critical alert is active"),
        ({"paused": True}, "new trades are paused"),
        ({"kill_switch": True}, "kill switch is active"),
        ({"strategy_drawdown": Decimal("2000")}, "strategy drawdown limit reached"),
    ],
)
def test_each_hard_gate_fails_independently(
    settings: Settings,
    updates: dict[str, object],
    reason: str,
) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    result = RiskEngine(paper).qualify(
        profitable_opportunity(),
        clear_context().model_copy(update=updates),
    )
    assert reason in result.reasons


def test_profit_and_return_thresholds_are_independent(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    low = profitable_opportunity().model_copy(
        update={"minimum_net_profit": Decimal("249"), "return_on_capital_bps": Decimal("299")}
    )
    result = RiskEngine(paper).qualify(low, clear_context())
    assert "minimum scenario profit below threshold" in result.reasons
    assert "return on committed capital below threshold" in result.reasons


def test_failed_gate_reports_actual_operator_and_required_value(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    opportunity = profitable_opportunity().model_copy(update={"minimum_net_profit": Decimal("249")})
    result = RiskEngine(paper).qualify(opportunity, clear_context())

    check = next(item for item in result.checks if item.code == "MINIMUM_NET_PROFIT")
    assert check.actual_value == "249"
    assert check.operator == ">="
    assert check.required_value == "250"
    assert check.unit == "USD"
    assert not check.passed


def test_ibkr_gate_preserves_degraded_status(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    context = clear_context().model_copy(update={"ibkr_status": ConnectionStatus.DEGRADED})
    result = RiskEngine(paper).qualify(profitable_opportunity(), context)

    check = next(item for item in result.checks if item.code == "IBKR_CONNECTION")
    assert check.actual_value == "DEGRADED"
    assert check.detail == "IBKR connection is DEGRADED"


def test_opportunity_schema_rejects_every_short_zq_candidate() -> None:
    with pytest.raises(ValidationError):
        Opportunity(
            direction="SHORT",
            zq_side=Side.SELL,
            contracts=10,
        )


def test_indeterminate_eligibility_is_reported_honestly(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    context = clear_context().model_copy(update={"eligibility_blocked": None})
    result = RiskEngine(paper).qualify(profitable_opportunity(), context)

    check = next(item for item in result.checks if item.code == "ELIGIBILITY_OPENING")
    assert check.actual_value == "INDETERMINATE"
    assert check.required_value == "OPEN"
    assert not check.passed


def test_liquidity_gate_does_not_claim_below_limit_when_margin_is_unknown(
    settings: Settings,
) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    context = clear_context().model_copy(update={"next_batch_initial_margin": None})
    result = RiskEngine(paper).qualify(profitable_opportunity(), context)

    check = next(item for item in result.checks if item.code == "PROJECTED_EXCESS_LIQUIDITY")
    assert check.actual_value == "20000 USD"
    assert "unavailable margin" in (check.required_value or "")
    assert check.detail == (
        "required liquidity cannot be calculated because next-batch margin is unavailable"
    )
