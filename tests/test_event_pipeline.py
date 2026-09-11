from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import PolymarketProtocolError, update_books_from_stream_event
from zq_arb.config import Settings
from zq_arb.domain.enums import BatchState, RunMode
from zq_arb.domain.models import (
    BatchView,
    BookLevel,
    Opportunity,
    OpportunityCalculation,
    OpportunityCostBreakdown,
    OrderBook,
    Quote,
)
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import BatchRecord, OrderRecord
from zq_arb.persistence.repository import Repository
from zq_arb.services.engine import EngineRuntime
from zq_arb.services.state import StateStore


def book(asset: str = "token", depth: int = 100) -> OrderBook:
    return OrderBook(
        token_id=asset,
        bids=tuple(
            BookLevel(price=Decimal(".49") - Decimal(".001") * i, size=1000) for i in range(depth)
        ),
        asks=tuple(
            BookLevel(price=Decimal(".51") + Decimal(".001") * i, size=1000) for i in range(depth)
        ),
        stream_synchronized=True,
        source="WEBSOCKET",
    )


def delta(*changes: tuple[str, str, str], generation: int = 0) -> VenueEvent:
    return VenueEvent(
        venue="POLYMARKET",
        kind="price_change",
        stream_generation=generation,
        payload={
            "price_changes": [
                {"asset_id": "token", "side": side, "price": price, "size": size}
                for side, price, size in changes
            ]
        },
    )


@pytest.mark.asyncio
async def test_quotes_keep_coordinator_barrier_without_ledger_work(settings: Settings) -> None:
    repository = MagicMock()
    coordinator = ExecutionCoordinator(
        settings=settings,
        repository=repository,
        state=StateStore(settings),
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    coordinator._attempt_automated_reconciliation = AsyncMock()
    coordinator._publish = AsyncMock()
    await coordinator._lock.acquire()
    pending = asyncio.create_task(
        coordinator.handle_ibkr_event(VenueEvent(venue="IBKR", kind="tick_price"))
    )
    await asyncio.sleep(0)
    assert not pending.done()
    coordinator._lock.release()
    await asyncio.wait_for(pending, timeout=1)
    await coordinator.handle_ibkr_event(VenueEvent(venue="IBKR", kind="tick_size"))
    assert repository.mock_calls == []
    coordinator._attempt_automated_reconciliation.assert_not_awaited()
    coordinator._publish.assert_not_awaited()

    await coordinator.handle_ibkr_event(VenueEvent(venue="IBKR", kind="execution_end"))
    coordinator._attempt_automated_reconciliation.assert_awaited_once()
    coordinator._publish.assert_awaited_once()
    assert coordinator._ibkr_executions_complete


@pytest.mark.asyncio
async def test_reconciliation_has_periodic_backstop_without_quotes(settings: Settings) -> None:
    repository = MagicMock()
    repository.active_batch_view = AsyncMock(return_value=BatchView())
    repository.pending_obligations = AsyncMock(return_value=())
    state = StateStore(settings)
    coordinator = ExecutionCoordinator(
        settings=settings,
        repository=repository,
        state=state,
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    coordinator._attempt_automated_reconciliation = AsyncMock()
    coordinator._publish = AsyncMock()
    snapshot = await state.get()
    await coordinator.cycle(snapshot)
    await coordinator.cycle(snapshot)
    coordinator._attempt_automated_reconciliation.assert_awaited_once()
    coordinator._last_reconciliation_check -= 2
    await coordinator.cycle(snapshot)
    assert coordinator._attempt_automated_reconciliation.await_count == 2


@pytest.mark.asyncio
async def test_snapshot_sharing_cannot_mutate_state_or_prior_books(settings: Settings) -> None:
    state = StateStore(settings)
    original = book()
    await state.set_books((original,))
    published = await state.update(lambda s: s.model_copy(update={"metadata": {"nested": [1]}}))
    subscription = state.subscribe()
    subscriber_view = await anext(subscription)
    first = await state.get()
    assert first.books["token"] is not original
    assert first.books["token"].asks is original.asks
    changed = original.model_copy(deep=True, update={"asks": ()})
    assert not changed.asks and len(original.asks) == 100
    published.metadata["nested"].append(2)
    subscriber_view.metadata["nested"].append(3)
    first.metadata["nested"].append(4)
    first.books.clear()
    await state.apply_polymarket_event(delta(("SELL", ".51", "0")))
    latest = await state.get()
    assert latest.metadata["nested"] == [1]
    assert latest.books["token"].best_ask == Decimal(".511")
    assert original.best_ask == Decimal(".51")
    assert subscriber_view.books["token"].best_ask == Decimal(".51")
    await subscription.aclose()


def test_full_depth_promotes_level_six_and_batch_is_atomic() -> None:
    original = book(depth=6)
    (updated,) = update_books_from_stream_event(
        {"token": original},
        delta(*(("SELL", str(level.price), "0") for level in original.asks[:5])),
    )
    assert updated.best_ask == original.asks[5].price
    assert updated.stream_synchronized
    assert len(original.asks) == 6
    # The first delta crosses the old ask; the same message replaces that ask.
    (updated,) = update_books_from_stream_event(
        {"token": book(depth=1)},
        delta(("BUY", ".52", "20"), ("SELL", ".51", "0"), ("SELL", ".53", "30")),
    )
    assert updated.best_bid == Decimal(".52")
    assert updated.best_ask == Decimal(".53")
    with pytest.raises(PolymarketProtocolError, match="crossed"):
        update_books_from_stream_event({"token": original}, delta(("BUY", ".6", "1")))


def test_unsynchronized_book_requires_new_snapshot() -> None:
    unsynchronized = book().model_copy(update={"stream_synchronized": False})
    with pytest.raises(PolymarketProtocolError, match="cannot heal"):
        update_books_from_stream_event({"token": unsynchronized}, delta(("BUY", ".49", "2")))
    (updated,) = update_books_from_stream_event(
        {"token": unsynchronized},
        VenueEvent(
            venue="POLYMARKET",
            kind="tick_size_change",
            payload={"asset_id": "token", "new_tick_size": ".001"},
        ),
    )
    assert not updated.stream_synchronized


@pytest.mark.asyncio
async def test_ibkr_thread_burst_preserves_every_callback_under_backpressure(
    settings: Settings,
) -> None:
    queue: asyncio.Queue[VenueEvent] = asyncio.Queue(maxsize=2)
    adapter = IbkrAdapter(settings.model_copy(update={"event_queue_maxsize": 256}), queue)
    adapter._loop = asyncio.get_running_loop()
    kinds = ["tick_price", "order_status", "execution", "commission", "position", "pnl"]

    def producer() -> None:
        for i in range(240):
            adapter._emit(kinds[i % len(kinds)], {"tick_type": 1, "sequence": i})

    await asyncio.to_thread(producer)
    observed = []
    for _ in range(240):
        event = await asyncio.wait_for(queue.get(), timeout=2)
        observed.append(event.payload["sequence"])
        queue.task_done()
        await asyncio.sleep(0)
    assert observed == list(range(240))
    metrics = adapter.ingress_diagnostics()
    assert metrics["received"] == metrics["enqueued"] == 240
    assert metrics.get("lost", 0) == 0
    assert metrics["pending"] == 0
    assert not adapter.event_queue_overflowed
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_real_ingress_overflow_is_sticky_and_blocks_submission(settings: Settings) -> None:
    adapter = IbkrAdapter(settings.model_copy(update={"event_queue_maxsize": 1}), asyncio.Queue())
    adapter._loop = asyncio.get_running_loop()
    # No yield: deterministically exhaust the thread-side capacity.
    adapter._emit("execution", {"sequence": 1})
    adapter._emit("execution", {"sequence": 2})
    assert adapter.event_queue_overflowed
    assert adapter.ingress_diagnostics()["lost"] == 1
    with pytest.raises(PermissionError, match="callback loss"):
        adapter.submit_zq_limit_day(
            month="202609", limit_price=Decimal("96.3"), quantity=10, order_ref="must-not-send"
        )
    await adapter.disconnect()
    assert adapter.event_queue_overflowed


@pytest.mark.asyncio
async def test_retired_requests_are_filtered_at_admission_and_drain(settings: Settings) -> None:
    queue: asyncio.Queue[VenueEvent] = asyncio.Queue()
    adapter = IbkrAdapter(settings, queue)
    adapter._loop = asyncio.get_running_loop()
    adapter._request_to_month[100] = "202609"
    adapter._emit("tick_size", {"request_id": 100, "tick_type": 5})
    adapter._emit("tick_price", {"request_id": 99, "tick_type": 1})
    adapter._emit("tick_price", {"request_id": 100, "tick_type": 1})
    adapter._request_to_month.clear()
    await asyncio.sleep(0)
    assert queue.empty()
    assert adapter.ingress_diagnostics()["filtered_quote_ticks"] == 1
    assert adapter.ingress_diagnostics()["retired_subscription_events"] == 2
    assert not adapter.event_queue_overflowed
    await adapter.disconnect()


def test_resubscription_never_reuses_request_ids(settings: Settings) -> None:
    adapter = IbkrAdapter(settings, asyncio.Queue())
    adapter._client = MagicMock()
    adapter._client.isConnected.return_value = True
    with patch.object(adapter, "_new_zq_contract", return_value=object()):
        adapter.request_contracts_and_market_data()
        old = set(adapter._request_to_month)
        old_streams = set(adapter._stream_request_ids)
        adapter.resubscribe_market_data()
    assert old.isdisjoint(adapter._request_to_month)
    assert len(adapter._stream_request_ids) == len(settings.subscription_contract_months)
    assert {call.args[0] for call in adapter._client.cancelMktData.call_args_list} == old_streams


@pytest.mark.asyncio
async def test_engine_rejects_old_generations_and_halts_on_processing_failure(
    settings: Settings,
) -> None:
    runtime = EngineRuntime(
        settings.model_copy(update={"database_url": "sqlite+aiosqlite:///:memory:"})
    )
    runtime.execution.handle_ibkr_event = AsyncMock(side_effect=ValueError("injected failure"))
    await runtime.state.set_books((book(),))
    runtime._polymarket_stream_generation = 2
    task = asyncio.create_task(runtime._process_events())
    try:
        await runtime.events.put(delta(("SELL", ".51", "0"), generation=1))
        await runtime.events.put(
            VenueEvent(venue="IBKR", kind="tick_price", payload={"request_id": -1})
        )
        await runtime.events.put(VenueEvent(venue="IBKR", kind="execution"))
        await asyncio.wait_for(runtime.events.join(), timeout=2)
        snapshot = await runtime.state.get()
        assert snapshot.books["token"].best_ask == Decimal(".51")
        assert snapshot.kill_switch and snapshot.paused and not snapshot.armed
        assert runtime.event_diagnostics()["skipped"] == {"POLYMARKET": 1, "IBKR": 1}
        assert runtime.event_diagnostics()["failed"] == {"IBKR": 1}
        runtime.execution.handle_ibkr_event.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.polymarket.close()
        await runtime.database.close()


@pytest.mark.asyncio
async def test_integrity_failure_quarantines_stream_until_fresh_snapshot(
    settings: Settings,
) -> None:
    runtime = EngineRuntime(settings)
    await runtime.state.set_books((book(),))
    task = asyncio.create_task(runtime._process_events())
    try:
        await runtime.events.put(delta(("BUY", ".9", "1")))
        await runtime.events.put(delta(("SELL", ".51", "0")))
        await asyncio.wait_for(runtime.events.join(), timeout=2)
        snapshot = await runtime.state.get()
        assert runtime._polymarket_stream_reset.is_set()
        assert not snapshot.books["token"].stream_synchronized
        assert snapshot.books["token"].best_ask == Decimal(".51")
        assert runtime.event_diagnostics()["skipped"] == {"POLYMARKET": 1}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.polymarket.close()
        await runtime.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["none", "kill", "quote", "overflow"])
async def test_order_authorization_is_rechecked_after_durable_intent(
    settings: Settings,
    interruption: str,
) -> None:
    configured = settings.model_copy(
        update={
            "database_url": "sqlite+aiosqlite:///:memory:",
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
        }
    )
    database = Database(configured)
    await database.initialize()
    repository = Repository(database)
    state = StateStore(configured)
    await state.confirm_reconciliation(actor="test", reason="verified test ledger", snapshot_id=0)
    snapshot = await state.get()
    await state.replace(
        snapshot.model_copy(
            update={
                "armed": True,
                "quotes": {
                    configured.ibkr_zq_contract_month: Quote(
                        instrument="202609", bid=Decimal("96.3")
                    )
                },
            }
        )
    )
    ibkr = SimpleNamespace(
        reserve_order_id=MagicMock(return_value=42),
        submit_zq_limit_day=MagicMock(),
        event_queue_overflowed=False,
    )
    coordinator = ExecutionCoordinator(
        settings=configured,
        repository=repository,
        state=state,
        ibkr=ibkr,
        polymarket=SimpleNamespace(trading_preflight=AsyncMock()),
    )
    opportunity = Opportunity(
        contracts=10,
        zq_price=Decimal("96.3"),
        calculation=OpportunityCalculation(
            inc25_shares_per_contract=1,
            inc50plus_shares_per_contract=1,
            inc25_emergency_hedge_cash=1,
            inc50plus_emergency_hedge_cash=1,
            emergency_hedge_cash=2,
            incremental_initial_margin=1,
            emergency_cash_reserve=2,
            committed_capital=3,
            costs=OpportunityCostBreakdown(ibkr_commission=0, polymarket_fees=0, explicit_costs=0),
        ),
    )
    create = repository.create_zq_batch_intent

    async def persist_then_interrupt(**kwargs: object) -> None:
        await create(**kwargs)
        if interruption == "kill":
            await state.set_operating_state(kill_switch=True, armed=False)
        elif interruption == "quote":
            await state.apply_ibkr_event(
                VenueEvent(
                    venue="IBKR",
                    kind="tick_price",
                    payload={"month": "202609", "tick_type": 1, "price": "96.2"},
                )
            )
        elif interruption == "overflow":
            ibkr.event_queue_overflowed = True

    try:
        with patch.object(repository, "create_zq_batch_intent", side_effect=persist_then_interrupt):
            await coordinator._submit_new_batch(await state.get(), opportunity)
        async with database.session() as session:
            order = await session.scalar(select(OrderRecord))
            batch = await session.scalar(select(BatchRecord))
            assert order is not None and batch is not None
            if interruption == "none":
                ibkr.submit_zq_limit_day.assert_called_once()
                assert order.state == "SUBMITTED"
                with pytest.raises(RuntimeError, match="not an unsent intent"):
                    await repository.abandon_zq_intent(batch.batch_id, "must reject")
            else:
                ibkr.submit_zq_limit_day.assert_not_called()
                assert order.state == "ABORTED"
                assert batch.state == BatchState.COMPLETE.value
                assert (await repository.active_batch_view()).batch_id is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stream_reset_closes_old_stream_and_requires_fresh_generation(
    settings: Settings,
) -> None:
    runtime = EngineRuntime(
        settings.model_copy(
            update={
                "polymarket_reconnect_backoff_seconds": 0.001,
            }
        )
    )
    started: asyncio.Queue[int] = asyncio.Queue()
    closed: list[int] = []

    async def stream(assets: list[str]):
        generation = runtime._polymarket_stream_generation
        try:
            yield VenueEvent(
                venue="POLYMARKET",
                kind="book",
                payload={
                    "asset_id": "token",
                    "bids": [{"price": ".49", "size": "1000"}],
                    "asks": [{"price": ".51", "size": "1000"}],
                },
            )
            await started.put(generation)
            await asyncio.Event().wait()
        finally:
            closed.append(generation)

    runtime.polymarket.public_market_stream = stream
    tasks = [
        asyncio.create_task(runtime._process_events()),
        asyncio.create_task(runtime._polymarket_stream_loop()),
    ]
    try:
        assert await asyncio.wait_for(started.get(), timeout=2) == 1
        await asyncio.wait_for(runtime.events.join(), timeout=2)
        assert (await runtime.state.get()).books["token"].stream_synchronized
        runtime._polymarket_stream_reset.set()
        assert await asyncio.wait_for(started.get(), timeout=2) == 2
        await asyncio.wait_for(runtime.events.join(), timeout=2)
        assert closed == [1]
        await runtime.events.put(delta(("SELL", ".51", "0"), generation=1))
        await runtime.events.put(delta(("BUY", ".49", "22"), generation=2))
        await asyncio.wait_for(runtime.events.join(), timeout=2)
        current = (await runtime.state.get()).books["token"]
        assert current.stream_synchronized
        assert current.best_ask == Decimal(".51")
        assert current.best_bid_size == 22
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.polymarket.close()
        await runtime.database.close()
    assert closed == [1, 2]
