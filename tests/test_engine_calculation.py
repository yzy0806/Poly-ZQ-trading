from __future__ import annotations

from decimal import Decimal

import pytest

from zq_arb.config import Settings
from zq_arb.domain.enums import ConnectionStatus, DataQuality, RunMode
from zq_arb.domain.models import (
    AccountMetrics,
    BookLevel,
    EligibilityStatus,
    MarketMappingStatus,
    OrderBook,
    Quote,
    VenueHealth,
)
from zq_arb.services.engine import EngineRuntime


def quote(month: str, price: str) -> Quote:
    mid = Decimal(price)
    return Quote(
        instrument=month,
        bid=mid - Decimal("0.0025"),
        ask=mid + Decimal("0.0025"),
        last=mid,
        quality=DataQuality.LIVE,
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
async def test_engine_builds_comparisons_and_both_profit_paths(settings: Settings) -> None:
    paper = settings.model_copy(update={"run_mode": RunMode.PAPER})
    runtime = EngineRuntime(paper)
    snapshot = await runtime.state.get()
    quotes = {
        month: quote(month, price)
        for month, price in zip(
            paper.reference_contract_months,
            ("96.375", "96.325", "96.250", "96.200"),
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
    calculated = runtime._calculate(
        snapshot.model_copy(
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
                    full_excess_liquidity=Decimal("50000"),
                    cushion=Decimal("0.8"),
                ),
                "metadata": {
                    "contract_verification": {paper.ibkr_zq_contract_month: {"verified": True}},
                    "ibkr_market_data_type": 1,
                    "zq_position": 0,
                    "active_batches": 0,
                    "margin_preview_available": True,
                    "next_batch_initial_margin": "1000",
                    "reconciliation_clean": True,
                },
            }
        )
    )
    await runtime.polymarket.close()
    await runtime.database.close()
    assert calculated.probabilities.valid
    assert len(calculated.probability_comparisons) == 5
    assert {opportunity.direction for opportunity in calculated.opportunities} == {
        "LONG",
        "SHORT",
    }
    long = next(item for item in calculated.opportunities if item.direction == "LONG")
    assert long.token_prices["INC25"] == Decimal("0.27")
    assert long.emergency_token_prices["INC25"] == Decimal("0.28")
    assert len(long.scenarios) == len(long.emergency_scenarios) == 3


@pytest.mark.asyncio
async def test_engine_waits_for_target_and_anchor_quotes(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    snapshot = await runtime.state.get()
    calculated = runtime._calculate(snapshot)
    await runtime.polymarket.close()
    await runtime.database.close()
    assert "pre-meeting anchor" in calculated.health_messages[0]


@pytest.mark.asyncio
async def test_direct_signal_does_not_require_october_or_november(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    snapshot = await runtime.state.get()
    anchor_month = settings.reference_contract_months[0]
    calculated = runtime._calculate(
        snapshot.model_copy(
            update={
                "quotes": {
                    anchor_month: quote(anchor_month, "96.375"),
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
