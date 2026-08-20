from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode, Side
from zq_arb.domain.models import Opportunity
from zq_arb.risk.engine import GateContext, RiskEngine


def clear_context() -> GateContext:
    return GateContext(
        now=datetime(2026, 8, 20, tzinfo=UTC),
        ibkr_connected=True,
        ibkr_data_live=True,
        polymarket_connected=True,
        mapping_verified=True,
        eligibility_checked=True,
        eligibility_blocked=False,
        eligibility_country="HK",
        books_fresh=True,
        quotes_fresh=True,
        cross_venue_synchronized=True,
        contract_verified=True,
        full_hedge_depth_available=True,
        margin_preview_available=True,
        projected_full_excess_liquidity=Decimal("20000"),
        projected_margin_cushion=Decimal("0.75"),
        next_batch_initial_margin=Decimal("1000"),
        current_zq_position=0,
        active_batches=0,
        unresolved_hedge=False,
        reconciliation_clean=True,
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
    result = RiskEngine(settings).qualify(profitable_opportunity(), clear_context())
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
        ({"ibkr_connected": False}, "IBKR disconnected"),
        ({"ibkr_data_live": False}, "IBKR data is not live"),
        ({"polymarket_connected": False}, "Polymarket disconnected"),
        ({"mapping_verified": False}, "Polymarket mapping or rule hash unverified"),
        ({"eligibility_checked": False}, "geographic eligibility not checked"),
        ({"eligibility_blocked": True}, "geographic eligibility is blocked or indeterminate"),
        ({"books_fresh": False}, "Polymarket books are stale"),
        ({"quotes_fresh": False}, "ZQ quotes are stale"),
        ({"cross_venue_synchronized": False}, "cross-venue timestamps are not synchronized"),
        ({"contract_verified": False}, "ZQ contract details are not verified"),
        ({"full_hedge_depth_available": False}, "full 10-contract hedge depth unavailable"),
        ({"margin_preview_available": False}, "IBKR what-if margin preview unavailable"),
        ({"next_batch_initial_margin": None}, "next-batch initial margin unavailable"),
        ({"projected_full_excess_liquidity": None}, "projected full excess liquidity unavailable"),
        ({"projected_margin_cushion": None}, "projected margin cushion unavailable"),
        ({"current_zq_position": 100}, "aggregate ZQ limit would be exceeded"),
        ({"active_batches": 1}, "another batch is active"),
        ({"reconciliation_clean": False}, "venue reconciliation is not clean"),
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
