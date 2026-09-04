from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from zq_arb.config import Settings
from zq_arb.domain.enums import (
    ConnectionStatus,
    DataQuality,
    FarmStatus,
    MarginPreviewStatus,
    QuoteRole,
    RunMode,
    SubscriptionStatus,
)
from zq_arb.domain.models import (
    AccountMetrics,
    BookLevel,
    EffrObservation,
    EligibilityStatus,
    MarginPreview,
    MarketMappingStatus,
    OrderBook,
    Quote,
    VenueHealth,
    utc_now,
)
from zq_arb.services.engine import EngineRuntime


def quote(month: str, price: str) -> Quote:
    mid = Decimal(price)
    now = utc_now()
    role = QuoteRole.TARGET if month == "202609" else QuoteRole.DIAGNOSTIC
    return Quote(
        instrument=month,
        bid=mid - Decimal("0.0025"),
        ask=mid + Decimal("0.0025"),
        last=mid,
        quality=DataQuality.LIVE,
        role=role,
        last_price_change_at=now,
        last_market_data_event_at=now,
        market_data_type=1,
        subscription_status=SubscriptionStatus.ACTIVE,
        farm_status=FarmStatus.CONNECTED,
    )


def book(token_id: str, bid: str, ask: str, market: str) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        market=market,
        bids=(BookLevel(price=Decimal(bid), size=Decimal("20000")),),
        asks=(BookLevel(price=Decimal(ask), size=Decimal("20000")),),
        tick_size=Decimal("0.001") if Decimal(ask) < Decimal("0.01") else Decimal("0.01"),
        min_order_size=Decimal("5"),
        source="WEBSOCKET",
        stream_synchronized=True,
    )


@pytest.mark.asyncio
async def test_engine_builds_comparisons_and_long_only_profit_path(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    runtime = EngineRuntime(paper)
    snapshot = await runtime.state.get()
    quotes = {
        month: quote(month, price)
        for month, price in zip(
            paper.subscription_contract_months,
            ("96.325", "96.250", "96.200"),
            strict=True,
        )
    }
    legs = {leg.code: leg for leg in paper.market_legs}
    books = {
        legs["INC25"].yes_token_id: book(legs["INC25"].yes_token_id, "0.27", "0.28", "INC25_YES"),
        legs["INC25"].no_token_id: book(legs["INC25"].no_token_id, "0.72", "0.73", "INC25_NO"),
        legs["INC50PLUS"].yes_token_id: book(
            legs["INC50PLUS"].yes_token_id, "0.003", "0.004", "INC50PLUS_YES"
        ),
        legs["INC50PLUS"].no_token_id: book(
            legs["INC50PLUS"].no_token_id, "0.996", "0.997", "INC50PLUS_NO"
        ),
    }
    qualified_input = snapshot.model_copy(
        update={
            "ibkr": VenueHealth(status=ConnectionStatus.CONNECTED),
            "polymarket": VenueHealth(status=ConnectionStatus.CONNECTED),
            "eligibility": EligibilityStatus(
                checked=True,
                blocked=False,
                country="HK",
                permitted_for_live=True,
            ),
            "mapping": MarketMappingStatus(
                verified=True,
                rule_hash_match=True,
                market_count_match=True,
            ),
            "quotes": quotes,
            "books": books,
            "account": AccountMetrics(
                net_liquidation=Decimal("100000"),
                full_excess_liquidity=Decimal("50000"),
                cushion=Decimal("0.8"),
            ),
            "margin_preview": MarginPreview(
                status=MarginPreviewStatus.AVAILABLE,
                order_id=7001,
                contract_month=paper.ibkr_zq_contract_month,
                quantity=paper.ibkr_zq_child_order_quantity,
                limit_price=Decimal("96.3225"),
                init_margin_change=Decimal("1000"),
                received_at=utc_now(),
            ),
            "metadata": {
                "contract_verification": {paper.ibkr_zq_contract_month: {"verified": True}},
                "ibkr_market_data_type": 1,
                "ibkr_subscription_generation": 0,
                "zq_position": 0,
                "active_batches": 0,
                "reconciliation_clean": True,
            },
        }
    )
    without_fees = runtime._calculate(qualified_input)
    missing_fee_opportunity = without_fees.opportunities[0]
    assert missing_fee_opportunity.calculation is not None
    assert missing_fee_opportunity.calculation.costs.polymarket_fees is None
    assert missing_fee_opportunity.calculation.costs.explicit_costs is None
    assert missing_fee_opportunity.minimum_net_profit is None
    assert all(
        row.costs is None and row.net_pnl is None
        for row in missing_fee_opportunity.scenarios
    )
    fee_check = next(
        check
        for check in without_fees.probabilities.qualification_checks
        if check.code == "POLYMARKET_TAKER_FEES"
    )
    assert not fee_check.passed

    runtime._polymarket_fee_parameters = {
        "INC25": {"rate": Decimal("0.05"), "exponent": Decimal("1")},
        "INC50PLUS": {"rate": Decimal("0.05"), "exponent": Decimal("1")},
    }
    runtime._polymarket_fee_parameters_at = utc_now()
    calculated = runtime._calculate(qualified_input)
    await runtime.polymarket.close()
    await runtime.database.close()
    assert calculated.probabilities.valid
    assert len(calculated.probability_comparisons) == 5
    inc25_comparison = next(
        row for row in calculated.probability_comparisons if row.code == "INC25"
    )
    assert inc25_comparison.polymarket_bid_size == Decimal("20000")
    assert inc25_comparison.polymarket_ask_size == Decimal("20000")
    assert [opportunity.direction for opportunity in calculated.opportunities] == ["LONG"]
    long = calculated.opportunities[0]
    assert long.zq_side.value == "BUY"
    assert long.zq_price == Decimal("96.3225")
    assert long.token_prices["INC25"] == Decimal("0.28")
    assert long.emergency_token_prices["INC25"] == Decimal("0.28")
    assert len(long.scenarios) == len(long.emergency_scenarios) == 3
    model_check = next(
        check
        for check in calculated.probabilities.qualification_checks
        if check.code == "DIRECT_ZQ_MODEL"
    )
    assert model_check.passed
    assert model_check.required_value == "move [-50, 50] bp and probabilities [0, 1]"


@pytest.mark.asyncio
async def test_fee_parameter_refresh_is_independent_and_atomic(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    expected = {
        "INC25": {"rate": Decimal("0.05"), "exponent": Decimal("1")},
        "INC50PLUS": {"rate": Decimal("0.05"), "exponent": Decimal("1")},
    }
    runtime.polymarket.fetch_hedge_fee_parameters = AsyncMock(return_value=expected)
    runtime.polymarket.snapshot_all_books = AsyncMock(side_effect=RuntimeError("book failure"))

    await runtime._refresh_polymarket_fee_parameters()

    assert runtime._polymarket_fee_parameters == expected
    assert runtime._polymarket_fee_parameters_at is not None
    runtime.polymarket.snapshot_all_books.assert_not_awaited()

    previous_timestamp = runtime._polymarket_fee_parameters_at
    runtime.polymarket.fetch_hedge_fee_parameters = AsyncMock(
        return_value={"INC25": expected["INC25"]}
    )
    with pytest.raises(Exception, match="omitted required hedge markets"):
        await runtime._refresh_polymarket_fee_parameters()
    assert runtime._polymarket_fee_parameters == expected
    assert runtime._polymarket_fee_parameters_at == previous_timestamp

    await runtime.polymarket.close()
    await runtime.database.close()


@pytest.mark.asyncio
async def test_engine_waits_for_target_quote(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    snapshot = await runtime.state.get()
    calculated = runtime._calculate(snapshot)
    await runtime.polymarket.close()
    await runtime.database.close()
    assert "202609 bid/ask" in calculated.health_messages[0]


@pytest.mark.asyncio
async def test_direct_signal_does_not_require_october_or_november(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    snapshot = await runtime.state.get()
    calculated = runtime._calculate(
        snapshot.model_copy(
            update={
                "quotes": {
                    settings.ibkr_zq_contract_month: quote(
                        settings.ibkr_zq_contract_month, "96.325"
                    ),
                }
            }
        )
    )
    await runtime.polymarket.close()
    await runtime.database.close()
    assert calculated.probabilities.valid
    assert not calculated.probabilities.fedwatch.valid
    assert calculated.probabilities.expected_move_bps is not None


@pytest.mark.asyncio
async def test_direct_signal_fails_closed_without_effr(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    snapshot = await runtime.state.get()
    target_month = settings.ibkr_zq_contract_month
    calculated = runtime._calculate(
        snapshot.model_copy(
            update={
                "effr": EffrObservation(reason="official EFFR unavailable"),
                "quotes": {target_month: quote(target_month, "96.325")},
            }
        )
    )
    await runtime.polymarket.close()
    await runtime.database.close()
    assert not calculated.probabilities.valid
    assert "validated pre-meeting EFFR" in calculated.probabilities.reason


@pytest.mark.asyncio
async def test_margin_preview_warning_fails_closed(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    snapshot = await runtime.state.get()
    warned = snapshot.model_copy(
        update={
            "quotes": {
                settings.ibkr_zq_contract_month: quote(settings.ibkr_zq_contract_month, "96.3275")
            },
            "margin_preview": MarginPreview(
                status=MarginPreviewStatus.AVAILABLE,
                order_id=7002,
                contract_month=settings.ibkr_zq_contract_month,
                quantity=settings.ibkr_zq_child_order_quantity,
                limit_price=Decimal("96.3250"),
                init_margin_change=Decimal("1000"),
                warning_text="IBKR margin warning",
                received_at=utc_now(),
            ),
        }
    )
    available, actual, detail, margin = runtime._margin_preview_qualification(warned, utc_now())
    await runtime.polymarket.close()
    await runtime.database.close()
    assert available is False
    assert "AVAILABLE" in actual
    assert detail == "IBKR warning: IBKR margin warning"
    assert margin is None
