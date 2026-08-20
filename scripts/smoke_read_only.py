from __future__ import annotations

import asyncio
import json
from collections import Counter

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import PolymarketAdapter
from zq_arb.config import get_settings
from zq_arb.domain.enums import RunMode


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
            "eligibility_checked": eligibility.checked,
            "eligibility_blocked": eligibility.blocked,
            "eligibility_country": eligibility.country,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        required = (
            mapping.verified and len(books) == 10 and ibkr.connected and counts["tick_price"] > 0
        )
        return 0 if required else 1
    finally:
        await ibkr.disconnect()
        await polymarket.close()


def main() -> None:
    raise SystemExit(asyncio.run(smoke()))


if __name__ == "__main__":
    main()
