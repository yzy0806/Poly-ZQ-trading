from datetime import UTC, datetime
from decimal import Decimal

from zq_arb.analytics.portfolio import value_strategy_portfolio
from zq_arb.domain.enums import DataQuality
from zq_arb.domain.models import (
    BookLevel,
    EngineSnapshot,
    OrderBook,
    PortfolioPositionView,
    PortfolioView,
    Quote,
)


def test_strategy_portfolio_uses_conservative_executable_bids() -> None:
    now = datetime.now(UTC)
    zq_month = "202609"
    inc25 = "inc25-token"
    inc50 = "inc50-token"
    snapshot = EngineSnapshot(
        software_version="test",
        config_version="test",
        strategy_version="test",
        quotes={
            zq_month: Quote(
                instrument="ZQU6",
                bid=Decimal("96.2925"),
                ask=Decimal("96.2950"),
                market_data_type=1,
                quality=DataQuality.LIVE,
                pretrade_qualified=True,
                received_at=now,
            )
        },
        books={
            inc25: OrderBook(
                token_id=inc25,
                bids=(BookLevel(price=Decimal("0.57"), size=Decimal("5000")),),
                asks=(BookLevel(price=Decimal("0.58"), size=Decimal("5000")),),
                stream_synchronized=True,
                received_at=now,
            ),
            inc50: OrderBook(
                token_id=inc50,
                bids=(BookLevel(price=Decimal("0.006"), size=Decimal("5000")),),
                asks=(BookLevel(price=Decimal("0.007"), size=Decimal("5000")),),
                stream_synchronized=True,
                received_at=now,
            ),
        },
        portfolio=PortfolioView(
            positions=(
                PortfolioPositionView(
                    venue="IBKR",
                    instrument=zq_month,
                    label="ZQ 202609",
                    strategy_quantity=Decimal("3"),
                    venue_quantity=Decimal("3"),
                    average_entry_price=Decimal("96.2950"),
                    multiplier=Decimal("4167"),
                    reconciled=True,
                ),
                PortfolioPositionView(
                    venue="POLYMARKET",
                    instrument=inc25,
                    label="INC25 YES",
                    strategy_quantity=Decimal("1458.45"),
                    venue_quantity=Decimal("1458.45"),
                    average_entry_price=Decimal("0.56"),
                    simulated=True,
                    reconciled=True,
                ),
                PortfolioPositionView(
                    venue="POLYMARKET",
                    instrument=inc50,
                    label="INC50PLUS YES",
                    strategy_quantity=Decimal("2916.90"),
                    venue_quantity=Decimal("2916.90"),
                    average_entry_price=Decimal("0.007"),
                    simulated=True,
                    reconciled=True,
                ),
            )
        ),
    )

    result = value_strategy_portfolio(snapshot)
    rows = {item.label: item for item in result.positions}

    assert rows["ZQ 202609"].unrealized_pnl == Decimal("-31.2525")
    assert rows["INC25 YES"].cost_basis == Decimal("816.7320")
    assert rows["INC25 YES"].market_value == Decimal("831.3165")
    assert rows["INC25 YES"].unrealized_pnl == Decimal("14.5845")
    assert rows["INC50PLUS YES"].unrealized_pnl == Decimal("-2.91690")
    assert result.zq_unrealized_pnl == Decimal("-31.2525")
    assert result.polymarket_unrealized_pnl == Decimal("11.66760")
    assert result.combined_unrealized_pnl == Decimal("-19.58490")
    assert result.valuation_complete
    assert result.valuation_reason == "all strategy positions marked to executable best bids"
