from __future__ import annotations

import asyncio
import json
from collections import Counter

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import PolymarketAdapter, update_books_from_stream_event
from zq_arb.config import get_settings
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import OrderBook


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
    counts: Counter[str] = Counter()
    error_codes: set[int] = set()
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
        ibkr.request_contracts_and_market_data()
        ibkr.request_open_orders_and_executions()
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            timeout = max(0.01, deadline - asyncio.get_running_loop().time())
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                break
            counts[event.kind] += 1
            if event.kind == "error" and event.payload.get("code") is not None:
                error_codes.add(int(event.payload["code"]))
        streamed_books, stream_counts = await market_stream_task
        synchronized_books = sum(
            streamed_books[token_id].stream_synchronized for token_id in token_ids
        )
        summary = {
            "run_mode": settings.run_mode.value,
            "order_submission_enabled": False,
            "tws_connected": ibkr.connected,
            "ibkr_event_counts": dict(sorted(counts.items())),
            "ibkr_live_zq_quotes_received": counts["tick_price"] > 0,
            "ibkr_error_codes": sorted(error_codes),
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
        )
        return 0 if required else 1
    finally:
        await ibkr.disconnect()
        await polymarket.close()


def main() -> None:
    raise SystemExit(asyncio.run(smoke()))


if __name__ == "__main__":
    main()
