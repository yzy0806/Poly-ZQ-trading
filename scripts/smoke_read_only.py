from __future__ import annotations

import asyncio
import json
from collections import Counter

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import PolymarketAdapter, update_books_from_stream_event
from zq_arb.config import get_settings
from zq_arb.domain.enums import RunMode, SubscriptionStatus
from zq_arb.domain.models import OrderBook
from zq_arb.services.state import StateStore


async def capture_market_websocket(
    polymarket: PolymarketAdapter,
    token_ids: list[str],
    seed_books: tuple[OrderBook, ...],
) -> tuple[dict[str, OrderBook], Counter[str]]:
    books = {book.token_id: book for book in seed_books}
    counts: Counter[str] = Counter()
    try:
        async with asyncio.timeout(10):
            async for event in polymarket.public_market_stream(token_ids):
                counts[event.kind] += 1
                for updated in update_books_from_stream_event(books, event):
                    books[updated.token_id] = updated
                if all(books[token_id].stream_synchronized for token_id in token_ids):
                    break
    except TimeoutError:
        pass
    return books, counts


async def smoke() -> int:
    settings = get_settings()
    if settings.run_mode is not RunMode.READ_ONLY:
        raise RuntimeError("smoke check requires RUN_MODE=READ_ONLY")
    if settings.ibkr_order_submission_enabled or settings.polymarket_order_submission_enabled:
        raise RuntimeError("all order-submission flags must be false")

    queue: asyncio.Queue[VenueEvent] = asyncio.Queue(maxsize=settings.event_queue_maxsize)
    ibkr = IbkrAdapter(settings, queue)
    polymarket = PolymarketAdapter(settings)
    state = StateStore(settings)
    counts: Counter[str] = Counter()
    error_codes: set[int] = set()
    stream_parts: dict[str, set[str]] = {}
    stream_data_types: dict[str, int] = {}
    margin_order_id: int | None = None
    try:
        mapping, eligibility, books = await asyncio.gather(
            polymarket.verify_market_mapping(),
            polymarket.check_eligibility(),
            polymarket.snapshot_all_books(),
        )
        token_ids = [
            token_id
            for leg in settings.market_legs
            for token_id in (leg.yes_token_id, leg.no_token_id)
        ]
        market_stream_task = asyncio.create_task(
            capture_market_websocket(polymarket, token_ids, books)
        )
        await ibkr.connect()
        await state.begin_ibkr_subscriptions()
        ibkr.request_contracts_and_market_data()
        ibkr.request_open_orders_and_executions()
        required_months = (
            settings.reference_contract_months[0],
            settings.ibkr_zq_contract_month,
        )
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            timeout = max(0.01, deadline - asyncio.get_running_loop().time())
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                break
            counts[event.kind] += 1
            await state.apply_ibkr_event(event)
            month = str(event.payload.get("month") or "")
            tick_type = event.payload.get("tick_type")
            if event.kind == "tick_price" and tick_type in {1, 2}:
                stream_parts.setdefault(month, set()).add("bid" if tick_type == 1 else "ask")
            if event.kind == "tick_size" and tick_type in {0, 3}:
                stream_parts.setdefault(month, set()).add(
                    "bid_size" if tick_type == 0 else "ask_size"
                )
            if event.kind == "market_data_type" and month:
                stream_data_types[month] = int(event.payload.get("market_data_type") or 0)
            if event.kind == "error" and event.payload.get("code") is not None:
                error_codes.add(int(event.payload["code"]))
            current = await state.get()
            target_quote = current.quotes.get(settings.ibkr_zq_contract_month)
            target_verified = bool(
                (current.metadata.get("contract_verification") or {})
                .get(settings.ibkr_zq_contract_month, {})
                .get("verified")
            )
            if (
                margin_order_id is None
                and settings.ibkr_account_configured
                and target_verified
                and target_quote is not None
                and target_quote.subscription_status is SubscriptionStatus.ACTIVE
                and target_quote.ask is not None
            ):
                margin_order_id = ibkr.request_zq_margin_preview(
                    month=settings.ibkr_zq_contract_month,
                    limit_price=target_quote.ask,
                    quantity=settings.ibkr_zq_child_order_quantity,
                )
        streamed_books, stream_counts = await market_stream_task
        synchronized_books = sum(
            streamed_books[token_id].stream_synchronized for token_id in token_ids
        )
        complete_streams = {
            month: {"bid", "ask"}.issubset(stream_parts.get(month, set()))
            and stream_data_types.get(month, settings.ibkr_market_data_type) == 1
            for month in required_months
        }
        margin_preview = (await state.get()).margin_preview
        summary = {
            "run_mode": settings.run_mode.value,
            "order_submission_enabled": False,
            "tws_connected": ibkr.connected,
            "ibkr_event_counts": dict(sorted(counts.items())),
            "ibkr_live_zq_quotes_received": counts["tick_price"] > 0,
            "ibkr_current_live_streams": complete_streams,
            "ibkr_error_codes": sorted(error_codes),
            "ibkr_margin_preview": {
                "status": margin_preview.status.value,
                "available": margin_preview.available,
                "order_id": margin_preview.order_id,
                "next_batch_initial_margin": (
                    str(margin_preview.next_batch_initial_margin)
                    if margin_preview.next_batch_initial_margin is not None
                    else None
                ),
                "error": margin_preview.error,
            },
            "polymarket_mapping_verified": mapping.verified,
            "polymarket_rule_hash_match": mapping.rule_hash_match,
            "polymarket_book_count": len(books),
            "polymarket_market_ws_event_counts": dict(sorted(stream_counts.items())),
            "polymarket_stream_synchronized_book_count": synchronized_books,
            "eligibility_checked": eligibility.checked,
            "eligibility_blocked": eligibility.blocked,
            "eligibility_country": eligibility.country,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        required = (
            mapping.verified
            and len(books) == 10
            and synchronized_books == 10
            and ibkr.connected
            and counts["tick_price"] > 0
            and all(complete_streams.values())
            and (margin_preview.available if settings.ibkr_account_configured else True)
        )
        return 0 if required else 1
    finally:
        if margin_order_id is not None:
            ibkr.cancel_margin_preview(margin_order_id)
        await ibkr.disconnect()
        await polymarket.close()


def main() -> None:
    raise SystemExit(asyncio.run(smoke()))


if __name__ == "__main__":
    main()
