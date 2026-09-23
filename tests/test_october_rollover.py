from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from zq_arb.analytics.payoff import (
    CostInputs,
    build_three_state_opportunity,
    hedge_shares_per_contract,
    round_shares_up,
)
from zq_arb.analytics.probability import (
    direct_zq_probability,
    fedwatch_reference,
    theoretical_settlement,
)
from zq_arb.config import Settings, get_settings
from zq_arb.domain.calendar import MeetingCalendar, next_contract_month
from zq_arb.domain.models import BookLevel, OrderBook
from zq_arb.persistence.database import Database

OCTOBER = MeetingCalendar("202610", date(2026, 10, 29))


@pytest.mark.parametrize(
    "month,effective,before,after,symbol",
    [
        ("202609", date(2026, 9, 17), 16, 14, "ZQU6"),
        ("202610", date(2026, 10, 29), 28, 3, "ZQV6"),
        ("202402", date(2024, 2, 22), 21, 8, "ZQG4"),
        ("202612", date(2026, 12, 10), 9, 22, "ZQZ6"),
    ],
)
def test_calendar_weights_include_weekends_and_leap_days(month, effective, before, after, symbol):
    calendar = MeetingCalendar(month, effective)
    assert (calendar.days_before, calendar.days_after, calendar.symbol) == (before, after, symbol)
    assert calendar.post_decision_weight == Decimal(after) / (before + after)
    assert next_contract_month("202612") == "202701"


@pytest.mark.parametrize(
    "month,effective",
    [("202613", date(2026, 10, 29)), ("202610", date(2026, 11, 1)), ("202610", date(2026, 10, 1))],
)
def test_calendar_rejects_invalid_or_zero_weight_periods(month, effective):
    with pytest.raises(ValueError):
        MeetingCalendar(month, effective)


def test_october_weights_drive_probabilities_and_scenario_hedges():
    rate = Decimal("3.88")
    mid = Decimal("100") - (rate + Decimal("0.125") * 3 / 31)
    direct = direct_zq_probability(
        target_contract_month="202610",
        target_bid=mid,
        target_ask=mid,
        pre_meeting_effr=rate,
        calendar=OCTOBER,
    )
    assert direct.post_decision_weight == Decimal(3) / 31
    assert direct.expected_move_bps.quantize(Decimal("0.000001")) == Decimal("12.500000")
    assert direct.upper_probability.quantize(Decimal("0.000001")) == Decimal("0.500000")
    assert "ZQV6" in direct.reason
    assert round_shares_up(hedge_shares_per_contract(25, calendar=OCTOBER)) == Decimal("100.82")
    assert round_shares_up(hedge_shares_per_contract(50, calendar=OCTOBER)) == Decimal("201.63")
    asset_id = "test-market-asset"
    book = OrderBook(
        token_id=asset_id,
        bids=(BookLevel(price=Decimal(".01"), size=10000),),
        asks=(BookLevel(price=Decimal(".02"), size=10000),),
    )
    result = build_three_state_opportunity(
        calendar=OCTOBER,
        contracts=5,
        zq_price=mid,
        pre_meeting_effr=rate,
        inc25_book=book,
        inc50_book=book,
        cost_inputs=CostInputs(),
        incremental_margin=100,
        emergency_cash_reserve=Decimal(0),
        post_price_cap=Decimal(1),
        emergency_price_cap=Decimal(1),
    )
    # Round the entire quantity once, rather than multiplying a rounded per-contract number.
    assert result.token_requirements == {
        "INC25": Decimal("504.08"),
        "INC50PLUS": Decimal("1008.15"),
    }
    assert result.calculation.inc25_shares_per_contract == Decimal("100.82")
    gross = [s.gross_pnl for s in result.scenarios]
    # Linear hedge quantities leave the CME settlement-rounding residual in P&L.
    assert gross[1] - gross[0] == Decimal("4.040")
    assert gross[2] - gross[0] == Decimal("8.070")
    assert result.scenarios[1].settlement_price == theoretical_settlement(
        rate, Decimal(25), calendar=OCTOBER
    )
    with pytest.raises(ValueError, match="calendar differ"):
        direct_zq_probability(
            target_contract_month="202609",
            target_bid=mid,
            target_ask=mid,
            pre_meeting_effr=rate,
            calendar=OCTOBER,
        )


def test_october_diagnostic_uses_non_meeting_november_not_december_average():
    def diagnostic(december):
        return fedwatch_reference(
            {"202610": Decimal("96.0958064516"), "202611": Decimal("95.87"), "202612": december},
            pre_meeting_effr=Decimal("3.88"),
            calendar=OCTOBER,
            anchor_contract_month="202611",
        )

    first, second = diagnostic(Decimal("95.7")), diagnostic(Decimal("94"))
    assert first.expected_move_bps == second.expected_move_bps == Decimal("25")
    assert first.end_effr == Decimal("4.13")
    assert abs(first.target_residual_bps) < Decimal("0.000001")
    assert not fedwatch_reference(
        {"202610": Decimal("96.1")},
        pre_meeting_effr=Decimal("3.88"),
        calendar=OCTOBER,
        anchor_contract_month="202611",
    ).valid
    with pytest.raises(ValueError, match="non-meeting anchor"):
        fedwatch_reference(
            {"202610": Decimal("96.1"), "202611": Decimal("95.87")},
            pre_meeting_effr=Decimal("3.88"),
            calendar=OCTOBER,
            anchor_contract_month="202611",
            intervening_effective_dates=(date(2026, 11, 5),),
        )


def test_october_example_and_explicit_external_env(monkeypatch, tmp_path):
    example = Path("deploy/zq-arb.env.example")
    private = tmp_path / "development.env"
    private.write_text(example.read_text())
    monkeypatch.setenv("ZQ_ENV_FILE", str(private))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.meeting_calendar == OCTOBER
        assert settings.subscription_contract_months == ("202610", "202611", "202612")
        assert settings.polymarket_event_id == "606422"
        assert settings.fomc_trading_cutoff_utc.isoformat() == "2026-10-28T17:00:00+00:00"
        assert not settings.live_trading_enabled
        assert not settings.ibkr_order_submission_enabled
        assert not settings.polymarket_order_submission_enabled
        get_settings.cache_clear()
        monkeypatch.setenv("ZQ_ENV_FILE", str(tmp_path / "missing.env"))
        with pytest.raises(ValueError, match="does not exist"):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "changes",
    [
        {"fomc_rate_effective_date": "2026-10-28"},
        {"fomc_rate_effective_date": "2026-09-17"},
        {
            "fomc_statement_utc": "2026-09-16T18:00:00Z",
            "fomc_trading_cutoff_utc": "2026-09-16T17:00:00Z",
        },
        {"fedwatch_anchor_contract_month": "202610"},
        {"fedwatch_intervening_rate_effective_dates": "2026-11-05"},
        {"fomc_statement_utc": "2026-10-28T18:00:00"},
        {"fomc_trading_cutoff_utc": "2026-10-28T17:00:00"},
        {"fomc_timezone": "Invalid/Zone"},
        {"ibkr_zq_subscription_months": "202610,202612,202611"},
        {"ibkr_zq_subscription_months": "202610,202611,202613"},
    ],
)
def test_october_configuration_rejects_inconsistent_event_calendar(changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=Path("deploy/zq-arb.env.example"), **changes)


@pytest.mark.asyncio
async def test_database_cannot_be_reused_across_events_or_calendars(tmp_path, settings):
    url = f"sqlite+aiosqlite:///{tmp_path / 'strategy.sqlite3'}"
    september = settings.model_copy(update={"database_url": url})
    old = Database(september)
    await old.initialize()
    await old.validate_execution_environment()
    await old.close()
    for changes in (
        {"ibkr_zq_contract_month": "202610", "fomc_rate_effective_date": date(2026, 10, 29)},
        {"polymarket_event_id": "606422"},
        {"fomc_rate_effective_date": date(2026, 9, 18)},
    ):
        new = Database(september.model_copy(update=changes))
        try:
            with pytest.raises(RuntimeError, match="different environment"):
                await new.validate_execution_environment()
        finally:
            await new.close()
