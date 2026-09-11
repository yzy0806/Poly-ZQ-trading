from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.polymarket import (
    PolymarketAccountSnapshot,
    PolymarketAdapter,
    PolymarketOrderResult,
    PreparedPolymarketOrder,
)
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import BookLevel, OrderBook, utc_now
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import AuditLogRecord, ExecutionRecord, OrderRecord
from zq_arb.persistence.repository import Repository
from zq_arb.services.engine import EngineRuntime
from zq_arb.services.state import StateStore


@pytest.fixture
async def ledger(tmp_path, settings):
    configured = settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'ledger.db').as_posix()}",
            "run_mode": RunMode.PAPER,
            "ibkr_trading_mode": "paper",
            "simulate_polymarket_fills": False,
            "polymarket_order_submission_enabled": True,
            "ibkr_order_submission_enabled": True,
            "shutdown_drain_seconds": 1,
        }
    )
    database = Database(configured)
    await database.initialize()
    repository = Repository(database)
    state = StateStore(configured)
    token = next(leg.yes_token_id for leg in configured.market_legs if leg.code == "INC25")
    await state.set_books(
        (
            OrderBook(
                token_id=token,
                asks=(BookLevel(price=Decimal(".3"), size=Decimal("10000")),),
                bids=(BookLevel(price=Decimal(".2"), size=Decimal("10000")),),
                stream_synchronized=True,
            ),
        )
    )
    await repository.create_zq_batch_intent(
        batch_id="batch",
        order_id=42,
        contract_month=configured.ibkr_zq_contract_month,
        quantity=10,
        limit_price=Decimal("96.3"),
        strategy_version="test",
        snapshot_id=1,
    )
    await repository.mark_zq_submitted("batch")
    obligations = await repository.record_ibkr_execution_and_obligations(
        order_id=42,
        execution_id="zq-1",
        quantity=Decimal("1"),
        price=Decimal("96.3"),
        executed_at=utc_now(),
        token_shares={token: Decimal("100")},
        details={},
    )
    signed = []

    async def prepare(**values):
        result = PreparedPolymarketOrder(
            **values,
            signed_payload={"salt": len(signed) + 1},
            order_id=f"poly-{len(signed) + 1}",
        )
        signed.append(result)
        return result

    async def post(prepared):
        return PolymarketOrderResult(
            order_id=prepared.order_id,
            status="live",
            requested_shares=prepared.shares,
            immediately_matched_shares=Decimal("0"),
            limit_price=prepared.limit_price,
        )

    venue = SimpleNamespace(
        prepare_hedge_limit=AsyncMock(side_effect=prepare),
        post_prepared_hedge=AsyncMock(side_effect=post),
        get_order=AsyncMock(side_effect=LookupError("not found")),
        cancel_order=AsyncMock(return_value=True),
        account_snapshot=AsyncMock(),
        close=AsyncMock(),
    )
    ibkr = MagicMock()
    ibkr.disconnect = AsyncMock()
    ibkr.ingress_diagnostics.return_value = {"pending": 0}
    ibkr.event_queue_overflowed = False
    coordinator = ExecutionCoordinator(
        settings=configured,
        repository=repository,
        state=state,
        ibkr=ibkr,
        polymarket=venue,
    )
    coordinator._recovery_ready = True  # This fixture represents an already reconciled session.
    result = SimpleNamespace(
        settings=configured,
        db=database,
        repo=repository,
        state=state,
        venue=venue,
        ibkr=ibkr,
        execution=coordinator,
        token=token,
        obligation=obligations[0],
        signed=signed,
    )
    try:
        yield result
    finally:
        await coordinator.close()
        await database.close()


async def route(h):
    await h.execution._route_one_hedge(await h.state.get(), h.obligation)


def trade(order="poly-1", shares="100", status="CONFIRMED", trade_id="trade-1"):
    return {
        "id": trade_id,
        "taker_order_id": order,
        "size": shares,
        "price": ".3",
        "status": status,
        "matched_at": utc_now().isoformat(),
    }


async def receive(h, payload):
    await h.execution.handle_polymarket_event(
        VenueEvent(
            venue="POLYMARKET",
            kind="user_trade",
            payload=payload,
        )
    )


def venue_order(status="FILLED", matched="100", trade_ids=("trade-1",)):
    return {"status": status, "size_matched": matched, "associate_trades": list(trade_ids)}


async def settled(h):
    await route(h)
    await receive(h, trade())
    await h.repo.verify_polymarket_order("poly-1", venue_order())
    fresh_venues(h, shares="100")


def fresh_venues(h, shares="0"):
    h.execution._ibkr_open_orders_complete = True
    h.execution._ibkr_completed_orders_complete = True
    h.execution._ibkr_snapshot_at = utc_now()
    h.execution._ibkr_executions_complete = True
    h.execution._ibkr_positions_complete = True
    h.execution._ibkr_positions = {"contract": Decimal("1")}
    h.execution._ibkr_open_order_ids = {42}
    h.execution._polymarket_snapshot_complete = True
    h.execution._polymarket_positions_complete = True
    h.execution._polymarket_positions = {h.token: (Decimal(shares), Decimal(".3"))}
    h.execution._polymarket_snapshot_at = utc_now()


async def test_lost_response_survives_restart_without_signing_replacement(ledger):
    h = ledger
    h.venue.post_prepared_hedge.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        await route(h)
    assert (await h.repo.pending_polymarket_intents())[0]["state"] == "OUTCOME_UNKNOWN"
    restarted = ExecutionCoordinator(
        settings=h.settings,
        repository=h.repo,
        state=h.state,
        ibkr=h.ibkr,
        polymarket=h.venue,
    )
    await restarted.recover()
    for _ in range(3):
        await restarted._route_one_hedge(await h.state.get(), h.obligation)
    assert len(h.signed) == 1
    assert len(await h.repo.ledger_orders("POLYMARKET")) == 1
    h.venue.post_prepared_hedge.assert_awaited_once()
    # A delayed fill can be associated even though the POST never returned its ID.
    await receive(h, trade())
    h.venue.get_order.side_effect = None
    h.venue.get_order.return_value = venue_order()
    await restarted._route_one_hedge(await h.state.get(), h.obligation)
    assert (await h.repo.hedge_obligation(h.obligation.obligation_id)).confirmed_shares == 100
    assert len(h.signed) == 1
    await restarted.close()


async def test_concurrent_routing_creates_only_one_order(ledger):
    await asyncio.gather(*(route(ledger) for _ in range(4)))
    assert len(ledger.signed) == 1


async def test_restart_waits_for_inventory_and_blocks_a_manual_hedge(ledger):
    h = ledger
    await h.execution.recover()
    await route(h)
    assert not h.signed
    fresh_venues(h, shares="100")  # A hedge placed while the engine was offline.
    await h.execution._attempt_automated_reconciliation()
    await route(h)
    assert not h.signed
    assert (await h.state.get()).reconciliation.status == "MISMATCH"
    h.ibkr.cancel_order.assert_called_with(42)
    # Once venue evidence shows no unrecorded hedge, the existing debt can resume.
    fresh_venues(h)
    await h.execution._attempt_automated_reconciliation()
    await route(h)
    assert len(h.signed) == 1


async def test_unknown_outcome_retransmits_the_identical_signed_order(ledger):
    h = ledger
    h.venue.post_prepared_hedge.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        await route(h)
    async with h.db.session() as session:
        order = await session.scalar(select(OrderRecord).where(OrderRecord.venue == "POLYMARKET"))
        order.details = {
            **order.details,
            "last_submit_at": (utc_now() - timedelta(seconds=10)).isoformat(),
        }
    with pytest.raises(TimeoutError):
        await route(h)
    assert len(h.signed) == 1
    calls = h.venue.post_prepared_hedge.await_args_list
    assert len(calls) == 2 and calls[0].args[0] == calls[1].args[0]


async def test_cancellation_ack_waits_for_cumulative_fills_before_replacement(ledger):
    h = ledger
    await route(h)
    h.execution._order_sent_at["poly-1"] = utc_now() - timedelta(minutes=1)
    h.venue.get_order.side_effect = [venue_order("LIVE", "0", ()), venue_order("CANCELLED", "60")]
    await route(h)
    assert len(h.signed) == 1  # The 60-share fill has not arrived yet.
    await receive(h, trade(shares="60"))
    h.venue.get_order.side_effect = None
    h.venue.get_order.return_value = venue_order("CANCELLED", "60")
    await route(h)
    assert [item.shares for item in h.signed] == [100, 40]


async def test_fill_can_be_processed_while_cancel_request_is_waiting(ledger):
    h = ledger
    await route(h)
    h.execution._order_sent_at["poly-1"] = utc_now() - timedelta(minutes=1)
    h.venue.get_order.side_effect = [venue_order("LIVE", "0", ()), venue_order("CANCELLED", "60")]

    async def cancel(_):
        await asyncio.wait_for(receive(h, trade(shares="60")), timeout=1)
        return True

    h.venue.cancel_order.side_effect = cancel
    await route(h)
    assert [item.shares for item in h.signed] == [100, 40]


async def test_every_excess_fill_is_preserved_and_costed(ledger):
    h = ledger
    await route(h)
    await receive(h, trade(shares="60"))
    assert await h.repo.verify_polymarket_order("poly-1", venue_order("CANCELLED", "60"))
    await route(h)
    await receive(h, trade(order="poly-2", shares="60", trade_id="trade-2"))
    async with h.db.session() as session:
        rows = (
            await session.scalars(
                select(ExecutionRecord)
                .where(ExecutionRecord.venue == "POLYMARKET")
                .order_by(ExecutionRecord.id)
            )
        ).all()
    assert [row.quantity for row in rows] == [60, 60]
    assert [Decimal(row.details["allocated_shares"]) for row in rows] == [60, 40]
    obligation = await h.repo.hedge_obligation(h.obligation.obligation_id)
    assert obligation.confirmed_shares == 100
    assert obligation.excess_shares == 20
    positions = await h.repo.strategy_portfolio_positions(h.settings)
    position = next(item for item in positions if item.venue == "POLYMARKET")
    assert position.strategy_quantity == 120
    assert position.average_entry_price * position.strategy_quantity == 36
    assert "excess_hedges" in await h.repo.hedge_safety_differences()


@pytest.mark.parametrize(
    "statuses,confirmed,pending",
    [
        (["MATCHED", "MATCHED", "MINED", "CONFIRMED", "MATCHED"], 100, 0),
        (["FAILED", "MATCHED", "MINED"], 0, 0),
        (["MATCHED", "MINED", "FAILED", "MATCHED"], 0, 0),
        (["MATCHED", "MINED", "RETRYING"], 0, 100),
        (["CONFIRMED", "FAILED", "CONFIRMED"], 0, 100),
        (["FAILED", "CONFIRMED", "FAILED"], 0, 100),
    ],
)
async def test_trade_lifecycle_is_idempotent_and_preserves_quantity(
    ledger, statuses, confirmed, pending
):
    h = ledger
    await route(h)
    for status in statuses:
        await receive(h, trade(status=status))
    obligation = await h.repo.hedge_obligation(h.obligation.obligation_id)
    assert obligation.confirmed_shares == confirmed
    assert obligation.pending_shares == pending
    async with h.db.session() as session:
        rows = (
            await session.scalars(
                select(ExecutionRecord).where(ExecutionRecord.venue == "POLYMARKET")
            )
        ).all()
    assert len(rows) == 1 and rows[0].quantity == 100
    await route(h)
    assert len(h.signed) == 1  # Pending/failed match alone does not prove the order is terminal.


async def test_unknown_trade_is_durable_and_replayed_after_order_is_identified(ledger):
    h = ledger
    await receive(h, trade())
    assert len(await h.repo.pending_venue_receipts("POLYMARKET")) == 1
    await route(h)
    h.venue.get_order.side_effect = None
    h.venue.get_order.return_value = venue_order()
    h.venue.account_snapshot.return_value = PolymarketAccountSnapshot((), (), utc_now(), ())
    await h.execution.reconcile_polymarket_account()
    assert not await h.repo.pending_venue_receipts("POLYMARKET")
    assert (await h.repo.hedge_obligation(h.obligation.obligation_id)).confirmed_shares == 100


async def test_all_owned_maker_fills_are_credited_and_unknown_legs_stay_durable(ledger):
    h = ledger
    await route(h)
    payload = {
        **trade(order="other-taker"),
        "trader_side": "MAKER",
        "maker_orders": [
            {"order_id": "poly-1", "matched_amount": "60", "price": ".3"},
            {
                "order_id": "poly-2",
                "matched_amount": "40",
                "price": ".3",
                "maker_address": h.settings.polymarket_funder_address.get_secret_value(),
            },
        ],
    }
    await receive(h, payload)
    assert len(await h.repo.pending_venue_receipts("POLYMARKET")) == 1
    second = await h.repo.record_ibkr_execution_and_obligations(
        order_id=42,
        execution_id="zq-2",
        quantity=Decimal("1"),
        price=Decimal("96.3"),
        executed_at=utc_now(),
        token_shares={h.token: Decimal("100")},
        details={},
    )
    await h.execution._route_one_hedge(await h.state.get(), second[0])
    await receive(h, payload)
    await receive(h, payload)
    assert not await h.repo.pending_venue_receipts("POLYMARKET")
    obligations = await h.repo.all_hedge_obligations()
    assert [item.confirmed_shares for item in obligations] == [60, 40]
    positions = await h.repo.strategy_portfolio_positions(h.settings)
    assert next(item.strategy_quantity for item in positions if item.venue == "POLYMARKET") == 100


@pytest.mark.parametrize("fault", ["position", "ibkr_order", "poly_order", "stale", "incomplete"])
async def test_reconciliation_cannot_hide_inventory_orders_or_stale_data(ledger, fault):
    h = ledger
    await settled(h)
    await h.execution._attempt_automated_reconciliation()
    assert (await h.state.get()).reconciliation.clean
    if fault == "position":
        h.execution._polymarket_positions = {}
    elif fault == "ibkr_order":
        h.execution._ibkr_open_order_ids.add(999)
    elif fault == "poly_order":
        h.execution._polymarket_open_order_ids.add("unexpected")
    elif fault == "stale":
        h.execution._polymarket_snapshot_at = utc_now() - timedelta(minutes=5)
    else:
        h.execution._polymarket_snapshot_complete = False
    await h.execution._attempt_automated_reconciliation()
    current = await h.state.get()
    assert not current.reconciliation.clean
    assert current.reconciliation.status == (
        "UNKNOWN" if fault in {"stale", "incomplete"} else "MISMATCH"
    )
    assert not h.execution._new_entry_authorized(current.model_copy(update={"armed": True}))


async def test_event_during_account_read_invalidates_that_snapshot(ledger):
    h = ledger
    await settled(h)
    started, finish = asyncio.Event(), asyncio.Event()

    async def account():
        started.set()
        await finish.wait()
        return PolymarketAccountSnapshot((), (), utc_now(), ({"asset": h.token, "size": "100"},))

    h.venue.account_snapshot.side_effect = account
    task = asyncio.create_task(h.execution.reconcile_polymarket_account())
    await started.wait()
    await receive(h, trade())
    finish.set()
    await task
    assert (await h.state.get()).reconciliation.status == "UNKNOWN"


async def test_clean_authorization_cannot_outlive_a_ledger_change(ledger):
    h = ledger
    await settled(h)
    await h.execution._attempt_automated_reconciliation()
    await h.state.set_operating_state(armed=True, paused=False)
    old = await h.state.get()
    assert h.execution._new_entry_authorized(old)
    receipt = await h.repo.save_venue_receipt("IBKR", {"exec_id": "unprocessed"})
    assert not h.execution._new_entry_authorized(old)
    # Same inventory after a processed receipt still needs a fresh ledger authorization.
    await h.repo.finish_venue_receipt("IBKR", receipt)
    await h.execution._attempt_automated_reconciliation()
    assert h.execution._new_entry_authorized(await h.state.get())


async def test_missing_zq_requires_completed_order_evidence(ledger):
    h = ledger
    await settled(h)
    h.execution._ibkr_open_order_ids.clear()
    await h.execution._attempt_automated_reconciliation()
    assert (await h.repo.active_batch_view()).batch_id == "batch"
    assert not (await h.state.get()).reconciliation.clean
    await h.execution.handle_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="completed_order",
            payload={
                "order_id": 42,
                "client_id": h.settings.ibkr_client_id,
                "order_ref": "batch",
                "perm_id": "77",
                "status": "Cancelled",
                "filled": "1",
            },
        )
    )
    assert (await h.repo.active_batch_view()).batch_id is None
    assert (await h.state.get()).reconciliation.clean


async def test_another_clients_order_number_cannot_terminalize_our_order(ledger):
    h = ledger
    await h.execution.handle_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="order_status",
            payload={"order_id": 42, "client_id": 999, "status": "Cancelled", "filled": "0"},
        )
    )
    assert (await h.repo.ledger_orders("IBKR"))[0].state == "SUBMITTED"
    assert (await h.repo.active_batch_view()).batch_id == "batch"


async def test_late_settlement_conflict_in_an_older_batch_remains_blocking(ledger):
    h = ledger
    await settled(h)
    await h.repo.update_zq_order_status(order_id=42, status="Cancelled", filled=Decimal("1"))
    assert (await h.repo.active_batch_view()).batch_id is None
    await h.repo.create_zq_batch_intent(
        batch_id="newer-batch",
        order_id=43,
        contract_month=h.settings.ibkr_zq_contract_month,
        quantity=10,
        limit_price=Decimal("96.3"),
        strategy_version="test",
        snapshot_id=2,
    )
    await receive(h, trade(status="FAILED"))
    await h.repo.normalize_active_batch_state()
    assert (await h.repo.active_batch_view()).batch_id == "newer-batch"
    assert [item.batch_id for item in await h.repo.pending_obligations()] == ["batch"]
    assert await h.repo.unresolved_hedge_obligation_count() == 1
    assert not await h.execution.shutdown_ready()


async def test_halt_cancels_zq_without_waiting_for_hedge_network(ledger):
    h = ledger
    started, release = asyncio.Event(), asyncio.Event()

    async def post(_):
        started.set()
        await release.wait()
        raise TimeoutError

    h.venue.post_prepared_hedge.side_effect = post
    await h.execution._route_pending_hedges(await h.state.get(), (h.obligation,))
    await started.wait()
    await asyncio.wait_for(h.execution.halt("operator halt"), timeout=1)
    h.ibkr.cancel_order.assert_called_once_with(42)
    assert (await h.state.get()).kill_switch
    await h.execution.handle_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="execution",
            payload={
                "exec_id": "late-zq",
                "order_id": 42,
                "side": "BOT",
                "shares": "1",
                "price": "96.3",
            },
        )
    )
    assert await h.repo.strategy_zq_quantity(h.settings.ibkr_zq_contract_month) == 2
    h.ibkr.submit_zq_limit_day.assert_not_called()
    release.set()
    await h.execution.wait_for_hedges()


async def test_shutdown_timeout_persists_recovery_requirement(ledger):
    h = ledger
    runtime = EngineRuntime(h.settings)
    await runtime.polymarket.close()
    await runtime.database.close()
    runtime.database, runtime.repository, runtime.state = h.db, h.repo, h.state
    runtime.execution, runtime.ibkr, runtime.polymarket = h.execution, h.ibkr, h.venue
    await runtime.stop()
    h.ibkr.cancel_order.assert_called()
    h.ibkr.disconnect.assert_awaited_once()
    async with h.db.session() as session:
        audit = await session.scalar(
            select(AuditLogRecord).where(AuditLogRecord.action == "ENGINE_STOP")
        )
        assert audit.details["requires_recovery"] is True
        assert audit.details["reconciled_shutdown"] is False


async def test_shutdown_processes_late_fill_and_cancellation_before_disconnect(ledger):
    h = ledger
    await route(h)
    # Allow instrumented CI runs to process real SQLite transactions; the timeout
    # behavior has its own separate one-second test above.
    runtime = EngineRuntime(h.settings.model_copy(update={"shutdown_drain_seconds": 5}))
    await runtime.polymarket.close()
    await runtime.database.close()
    runtime.database, runtime.repository, runtime.state = h.db, h.repo, h.state
    runtime.execution, runtime.ibkr, runtime.polymarket = h.execution, h.ibkr, h.venue
    h.venue.get_order.side_effect = None
    h.venue.get_order.return_value = venue_order()
    h.venue.account_snapshot.return_value = PolymarketAccountSnapshot(
        (), (), utc_now(), ({"asset": h.token, "size": "100"},)
    )

    cancellation_requested = asyncio.Event()

    def cancel(_):
        h.ibkr.cancel_order.side_effect = None
        runtime.events.put_nowait(
            VenueEvent(venue="POLYMARKET", kind="user_trade", payload=trade())
        )
        cancellation_requested.set()
        runtime.events.put_nowait(
            VenueEvent(
                venue="IBKR",
                kind="order_status",
                payload={"order_id": 42, "status": "Cancelled", "filled": "1", "remaining": "0"},
            )
        )

    h.ibkr.cancel_order.side_effect = cancel
    fresh_venues(h)

    async def reconcile_after_callbacks():
        await cancellation_requested.wait()
        await runtime.events.join()
        h.execution._ibkr_open_order_ids.clear()
        await h.execution.reconcile_polymarket_account()

    async def disconnect():
        assert (await h.repo.hedge_obligation(h.obligation.obligation_id)).confirmed_shares == 100
        assert (await h.repo.active_batch_view()).batch_id is None
        assert not runtime._processing_event

    h.ibkr.disconnect.side_effect = disconnect
    runtime._tasks = [
        asyncio.create_task(runtime._process_events()),
        asyncio.create_task(reconcile_after_callbacks()),
    ]
    await runtime.stop()
    h.ibkr.disconnect.assert_awaited_once()
    async with h.db.session() as session:
        audit = await session.scalar(
            select(AuditLogRecord).where(AuditLogRecord.action == "ENGINE_STOP")
        )
        assert audit.details["reconciled_shutdown"] is True
        assert audit.details["requires_recovery"] is False


async def test_real_adapter_rejects_unsigned_saved_intent(settings):
    configured = settings.model_copy(
        update={
            "run_mode": RunMode.LIMITED_LIVE,
            "live_trading_enabled": True,
            "simulate_polymarket_fills": False,
            "polymarket_order_submission_enabled": True,
        }
    )
    adapter = PolymarketAdapter(configured)
    try:
        with pytest.raises(RuntimeError, match="outside simulation"):
            await adapter.post_prepared_hedge(
                PreparedPolymarketOrder(
                    token_id="asset",  # noqa: S106 - synthetic market identifier
                    limit_price=Decimal(".3"),
                    shares=Decimal("100"),
                    idempotency_key="old-paper-intent",
                    signed_payload=None,
                )
            )
    finally:
        await adapter.close()


@pytest.mark.parametrize("change", ["simulation", "account", "wallet", "ibkr_mode"])
async def test_database_identity_refuses_cross_environment_replay(ledger, change):
    from pydantic import SecretStr

    h = ledger
    changes = {
        "simulation": {"simulate_polymarket_fills": True},
        "account": {"ibkr_account_id": SecretStr("different-account")},
        "wallet": {"polymarket_funder_address": SecretStr("different-wallet")},
        "ibkr_mode": {"ibkr_trading_mode": "live"},
    }
    other = Database(h.settings.model_copy(update=changes[change]))
    try:
        await other.initialize()
        with pytest.raises(RuntimeError, match="different environment"):
            await other.validate_execution_environment()
    finally:
        await other.close()


async def test_legacy_database_is_preserved_but_cannot_replay(ledger):
    from zq_arb.persistence.models import ExecutionEnvironmentRecord

    h = ledger
    async with h.db.session() as session:
        identity = await session.get(ExecutionEnvironmentRecord, 1)
        await session.delete(identity)
    reopened = Database(h.settings)
    try:
        await reopened.initialize()
        with pytest.raises(RuntimeError, match="Legacy execution ledger"):
            await reopened.validate_execution_environment()
        assert len(await Repository(reopened).ledger_orders("IBKR")) == 1
    finally:
        await reopened.close()
