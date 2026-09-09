"""Offline load exercise; no venue connections, credentials, or routed orders.

Run from the repository with PYTHONPATH=src. JSON goes to stdout. The real
consumer, state reducers, SQLite ledger, analytics, and simulated hedge path run.
Only the IBKR cancel transport is replaced; new entries remain disabled/unarmed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import threading
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from zq_arb.adapters.events import VenueEvent
from zq_arb.api.app import dashboard_snapshot
from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import BookLevel, OrderBook, Quote, utc_now
from zq_arb.services.engine import EngineRuntime


async def run(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings(_env_file=Path("deploy/zq-arb.env.example")).model_copy(
        update={
            "database_url": "sqlite+aiosqlite:///:memory:",
            "run_mode": RunMode.PAPER,
            "effr_source": "MANUAL",
            "pre_meeting_effr_percent": Decimal("3.625"),
            "live_trading_enabled": False,
            "ibkr_order_submission_enabled": False,
            "polymarket_order_submission_enabled": True,
            "simulate_polymarket_fills": True,
        }
    )
    runtime = EngineRuntime(settings)
    # Fail loudly if any future code change tries to turn this into a network test.
    runtime.ibkr.connect = AsyncMock(side_effect=AssertionError("offline benchmark"))
    runtime.ibkr.submit_zq_limit_day = MagicMock(side_effect=AssertionError("offline benchmark"))
    runtime.ibkr.cancel_order = MagicMock()
    runtime.polymarket._authenticated_client = AsyncMock(
        side_effect=AssertionError("offline benchmark")
    )
    await runtime.database.initialize()
    assets = [
        asset for leg in settings.market_legs for asset in (leg.yes_token_id, leg.no_token_id)
    ]
    await runtime.state.set_books(
        tuple(
            OrderBook(
                token_id=asset,
                source="WEBSOCKET",
                stream_synchronized=True,
                bids=tuple(
                    BookLevel(price=Decimal(".49") - Decimal(".001") * i, size=100000)
                    for i in range(args.depth)
                ),
                asks=tuple(
                    BookLevel(price=Decimal(".51") + Decimal(".001") * i, size=100000)
                    for i in range(args.depth)
                ),
            )
            for asset in assets
        )
    )
    await runtime.state.update(
        lambda s: s.model_copy(
            update={
                "quotes": {
                    month: Quote(instrument=month, bid=Decimal("96.3"), ask=Decimal("96.305"))
                    for month in settings.subscription_contract_months
                }
            }
        )
    )
    await runtime.repository.create_zq_batch_intent(
        batch_id="OFFLINE",
        order_id=42,
        contract_month=settings.ibkr_zq_contract_month,
        quantity=10,
        limit_price=Decimal("96.3"),
        strategy_version=settings.strategy_version,
        snapshot_id=1,
    )
    await runtime.repository.mark_zq_submitted("OFFLINE")
    runtime.ibkr._loop = asyncio.get_running_loop()
    processed_critical: list[int] = []
    received_critical: list[int] = []
    original = runtime.execution.handle_ibkr_event

    async def observe(event: VenueEvent) -> None:
        await original(event)
        if "benchmark_sequence" in event.payload:
            processed_critical.append(event.payload["benchmark_sequence"])

    runtime.execution.handle_ibkr_event = observe
    stop_thread = threading.Event()
    sent: Counter[str] = Counter()
    duration = args.events / args.rate

    def ibkr_producer() -> None:
        started = time.perf_counter()
        for offset in range(0, args.events, args.burst):
            delay = started + offset / args.rate - time.perf_counter()
            if stop_thread.wait(max(0, delay)):
                return
            for index in range(offset, min(offset + args.burst, args.events)):
                runtime.ibkr._emit(
                    "tick_size",
                    {
                        "month": settings.ibkr_zq_contract_month,
                        "tick_type": 0,
                        "size": str(index + 1),
                    },
                )
                sent["IBKR_quotes"] += 1

    async def polymarket_producer() -> None:
        started = time.perf_counter()
        for offset in range(0, args.events, args.burst):
            await asyncio.sleep(max(0, started + offset / args.rate - time.perf_counter()))
            for index in range(offset, min(offset + args.burst, args.events)):
                await runtime.events.put(
                    VenueEvent(
                        venue="POLYMARKET",
                        kind="price_change",
                        payload={
                            "price_changes": [
                                {
                                    "asset_id": assets[index % len(assets)],
                                    "price": ".49",
                                    "size": str(index + 1),
                                    "side": "BUY",
                                }
                            ]
                        },
                    )
                )
                sent["POLYMARKET_deltas"] += 1

    async def critical_producer() -> None:
        sequence = 0

        def emit(kind: str, payload: dict[str, object]) -> None:
            nonlocal sequence
            sequence += 1
            received_critical.append(sequence)
            runtime.ibkr._emit(kind, {**payload, "benchmark_sequence": sequence})

        fill = {
            "exec_id": "OFFLINE-FIRST",
            "order_id": 42,
            "side": "BOT",
            "shares": "3",
            "price": "96.3",
            "time": utc_now().isoformat(),
        }
        await asyncio.sleep(duration * 0.1)
        emit("execution", fill)
        emit("execution", fill)  # Duplicate must not create duplicate hedge obligations.
        emit("account_summary", {"tag": "NetLiquidation", "value": "1000000"})
        await asyncio.sleep(duration * 0.7)
        emit(
            "order_status", {"order_id": 42, "status": "Cancelled", "filled": "3", "remaining": "0"}
        )
        emit("execution", {**fill, "exec_id": "OFFLINE-LATE", "shares": "2"})
        emit("account_summary", {"tag": "NetLiquidation", "value": "1000001"})

    views = 0

    async def dashboard_reader() -> None:
        nonlocal views
        subscription = runtime.state.subscribe()
        try:
            async for snapshot in subscription:
                dashboard_snapshot(snapshot).model_dump_json()
                views += 1
                await asyncio.sleep(0.25)
        finally:
            await subscription.aclose()

    runtime._tasks = [
        asyncio.create_task(runtime._process_events(), name="venue-event-processor"),
        asyncio.create_task(runtime._analytics_loop()),
        asyncio.create_task(runtime._ibkr_market_data_supervisor_loop()),
        *(asyncio.create_task(dashboard_reader()) for _ in range(2)),
    ]
    producers = [asyncio.create_task(critical_producer())]
    if args.mode in {"ibkr", "mixed"}:
        producers.append(asyncio.create_task(asyncio.to_thread(ibkr_producer)))
    if args.mode in {"polymarket", "mixed"}:
        producers.append(asyncio.create_task(polymarket_producer()))
    started = time.perf_counter()
    try:
        await asyncio.wait_for(asyncio.gather(*producers), timeout=duration + 60)
        async with asyncio.timeout(60):
            while runtime.ibkr.ingress_diagnostics()["pending"]:  # noqa: ASYNC110 - thread-owned buffer
                await asyncio.sleep(0.01)
            await runtime.events.join()
        elapsed = time.perf_counter() - started
        snapshot = await runtime.state.get()
        quantity = await runtime.repository.strategy_zq_quantity(settings.ibkr_zq_contract_month)
        unresolved = await runtime.repository.unresolved_hedge_obligation_count()
        metrics = runtime.event_diagnostics()
        expected_ibkr = sent["IBKR_quotes"] + len(received_critical)
        expected_poly = sent["POLYMARKET_deltas"]
        quotes_correct = args.mode == "polymarket" or (
            snapshot.quotes[settings.ibkr_zq_contract_month].bid_size == args.events
        )
        books_correct = args.mode == "ibkr" or all(
            snapshot.books[assets[index % len(assets)]].best_bid_size == index + 1
            for index in range(max(0, args.events - len(assets)), args.events)
        )
        passed = (
            not runtime.ibkr.event_queue_overflowed
            and not snapshot.kill_switch
            and not metrics["failed"]
            and not metrics["skipped"]
            and metrics["processed"].get("IBKR", 0) == expected_ibkr
            and metrics["processed"].get("POLYMARKET", 0) == expected_poly
            and received_critical == processed_critical
            and quantity == 5
            and unresolved == 0
            and snapshot.account.net_liquidation == Decimal("1000001")
            and quotes_correct
            and books_correct
        )
        return {
            "passed": passed,
            "collected_utc": utc_now().isoformat(),
            "environment": platform.platform() + " Python " + platform.python_version(),
            "mode": args.mode,
            "events_per_venue": args.events,
            "target_rate_per_venue": args.rate,
            "producer_burst": args.burst,
            "books": 10,
            "levels_per_side": args.depth,
            "dashboard_readers": 2,
            "database": "in-memory SQLite",
            "network": "none; simulated hedge transport",
            "elapsed_seconds": round(elapsed, 3),
            "sent": dict(sent),
            "processed_events_per_second": round((expected_ibkr + expected_poly) / elapsed, 1),
            "critical_callbacks_verified_in_order": len(processed_critical),
            "duplicate_and_late_fill_quantity": str(quantity),
            "unresolved_hedges": unresolved,
            "final_quotes_and_books_correct": quotes_correct and books_correct,
            "dashboard_views": views,
            "diagnostics": metrics,
        }
    finally:
        stop_thread.set()
        for task in [*producers, *runtime._tasks]:
            task.cancel()
        await asyncio.gather(*producers, *runtime._tasks, return_exceptions=True)
        await runtime.ibkr.disconnect()
        await runtime.polymarket.close()
        await runtime.database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("ibkr", "polymarket", "mixed"), default="mixed")
    parser.add_argument("--events", type=int, default=30000)
    parser.add_argument("--rate", type=int, default=2500)
    parser.add_argument("--burst", type=int, default=100)
    parser.add_argument("--depth", type=int, default=100)
    arguments = parser.parse_args()
    if (
        min(arguments.events, arguments.rate, arguments.burst, arguments.depth) < 1
        or arguments.depth > 490
    ):
        parser.error("counts must be positive; depth must be at most 490")
    result = asyncio.run(run(arguments))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
