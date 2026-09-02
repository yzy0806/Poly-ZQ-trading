from __future__ import annotations

from decimal import Decimal

from zq_arb.domain.models import EngineSnapshot, PortfolioPositionView, PortfolioView, utc_now


def _venue_total(
    positions: tuple[PortfolioPositionView, ...], venue: str
) -> Decimal | None:
    relevant = [
        item for item in positions if item.venue == venue and item.strategy_quantity != 0
    ]
    if not relevant:
        return Decimal("0")
    if any(item.unrealized_pnl is None for item in relevant):
        return None
    return sum(
        (item.unrealized_pnl for item in relevant if item.unrealized_pnl is not None),
        Decimal("0"),
    )


def value_strategy_portfolio(
    snapshot: EngineSnapshot, portfolio: PortfolioView | None = None
) -> PortfolioView:
    """Mark durable strategy positions to conservative executable best bids."""

    source = portfolio or snapshot.portfolio
    valued_at = utc_now()
    valued: list[PortfolioPositionView] = []
    missing_marks: list[str] = []
    for position in source.positions:
        mark: Decimal | None = None
        mark_source = "UNAVAILABLE"
        mark_updated_at = None
        if position.venue == "IBKR":
            quote = snapshot.quotes.get(position.instrument)
            if quote is not None and quote.bid is not None:
                mark = quote.bid
                mark_updated_at = quote.received_at
                mark_source = (
                    "IBKR LIVE BEST BID"
                    if quote.market_data_type == 1 and quote.pretrade_qualified
                    else "IBKR LAST BEST BID — UNQUALIFIED"
                )
        else:
            book = snapshot.books.get(position.instrument)
            if book is not None and book.best_bid is not None:
                mark = book.best_bid
                mark_updated_at = book.received_at
                mark_source = (
                    "POLYMARKET LIVE BEST BID"
                    if book.stream_synchronized
                    else "POLYMARKET LAST BEST BID — UNSYNCHRONIZED"
                )

        cost_basis: Decimal | None = None
        market_value: Decimal | None = None
        unrealized_pnl: Decimal | None = None
        if position.strategy_quantity != 0:
            if position.average_entry_price is None or mark is None:
                missing_marks.append(position.label)
            elif position.venue == "IBKR":
                unrealized_pnl = (
                    mark - position.average_entry_price
                ) * position.multiplier * position.strategy_quantity
            else:
                cost_basis = position.average_entry_price * position.strategy_quantity
                market_value = mark * position.strategy_quantity
                unrealized_pnl = market_value - cost_basis
        valued.append(
            position.model_copy(
                update={
                    "mark_price": mark,
                    "mark_source": mark_source,
                    "cost_basis": cost_basis,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "mark_updated_at": mark_updated_at,
                }
            )
        )

    positions = tuple(valued)
    zq_pnl = _venue_total(positions, "IBKR")
    polymarket_pnl = _venue_total(positions, "POLYMARKET")
    combined = (
        zq_pnl + polymarket_pnl
        if zq_pnl is not None and polymarket_pnl is not None
        else None
    )
    attributed = [item for item in positions if item.strategy_quantity != 0]
    complete = not missing_marks
    if not attributed:
        reason = (
            "no strategy-attributed positions; venue-only differences require reconciliation"
            if positions
            else "no open strategy positions"
        )
    elif missing_marks:
        reason = f"missing executable marks for {', '.join(missing_marks)}"
    else:
        reason = "all strategy positions marked to executable best bids"
    return PortfolioView(
        positions=positions,
        zq_unrealized_pnl=zq_pnl,
        polymarket_unrealized_pnl=polymarket_pnl,
        combined_unrealized_pnl=combined,
        valuation_complete=complete,
        valuation_reason=reason,
        valued_at=valued_at,
    )
