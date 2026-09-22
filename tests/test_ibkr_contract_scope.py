from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import utc_now
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.repository import Repository
from zq_arb.services.state import StateStore


@pytest.fixture
async def october(settings, tmp_path):
    configured = settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'october.sqlite3'}",
            "ibkr_zq_contract_month": "202610",
            "ibkr_zq_subscription_months": "202610,202611,202612",
            "fomc_rate_effective_date": date(2026, 10, 29),
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
            "simulate_polymarket_fills": False,
        }
    )
    db = Database(configured)
    await db.initialize()
    c = ExecutionCoordinator(
        settings=configured,
        repository=Repository(db),
        state=StateStore(configured),
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    c.ibkr.event_queue_overflowed = False
    await c.recover()
    c._ibkr_open_orders_complete = True
    c._ibkr_completed_orders_complete = True
    c._ibkr_executions_complete = True
    c._ibkr_positions_complete = True
    c._ibkr_snapshot_at = utc_now()
    c._polymarket_positions_complete = True
    c._polymarket_snapshot_complete = True
    c._polymarket_snapshot_at = utc_now()
    await c._attempt_automated_reconciliation()
    assert (await c.state.get()).reconciliation.clean
    await c.state.set_operating_state(armed=True, paused=False)
    try:
        yield c
    finally:
        await c.close()
        await db.close()


def manual_order(kind="open_order", **changes):
    payload = {"order_id": 0, "client_id": 0, "perm_id": 123, "status": "Submitted"}
    if kind == "open_order":
        payload.update(symbol="ZQ", security_type="FUT", contract_month="20260930")
    payload.update(changes)
    return VenueEvent(venue="IBKR", kind=kind, payload=payload)


@pytest.mark.parametrize("status_first", [False, True])
async def test_september_manual_order_does_not_block_october(october, status_first):
    c = october
    await c.begin_ibkr_reconciliation(request_id=7)
    callbacks = [manual_order(), manual_order("order_status")]
    if status_first:
        callbacks.reverse()
    for callback in callbacks:
        await c.handle_ibkr_event(callback)
        current = await c.state.get()
        assert not current.paused
        assert not c._new_entry_authorized(current)  # The unfinished read still blocks entry.
    for kind in ("open_order_end", "completed_order_end", "execution_end", "position_end"):
        await c.handle_ibkr_event(VenueEvent(venue="IBKR", kind=kind, payload={"request_id": 7}))
    current = await c.state.get()
    assert current.reconciliation.clean and current.armed and not current.paused
    assert c._new_entry_authorized(current)
    assert not c._ibkr_foreign_orders
    c.ibkr.cancel_order.assert_not_called()
    c.ibkr.submit_zq_limit_day.assert_not_called()
    assert not await c.repository.ledger_orders()


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_month": "20261030"},
        {"contract_month": ""},
        {"contract_month": "202613"},
        {"contract_month": "20260931"},
        {"contract_month": "not-a-month"},
        {"security_type": ""},
        {"security_type": "FOP"},
        {"symbol": ""},
        {"perm_id": 0},
    ],
)
async def test_target_or_unidentified_foreign_orders_still_block(october, changes):
    c = october
    await c.handle_ibkr_event(manual_order(**changes))
    current = await c.state.get()
    assert current.reconciliation.status == "MISMATCH"
    assert current.paused and not current.armed
    assert not c._new_entry_authorized(current)
    assert not c._ibkr_other_month_orders


async def test_two_manual_orders_sharing_zero_ids_are_scoped_by_permanent_id(october):
    c = october
    await c.handle_ibkr_event(manual_order())
    await c.handle_ibkr_event(manual_order(perm_id=456, contract_month="20261030"))
    await c.handle_ibkr_event(manual_order("order_status", status="Cancelled"))
    current = await c.state.get()
    assert current.reconciliation.status == "MISMATCH"
    assert c._ibkr_foreign_orders == {"0:0:456"}
    assert not c._new_entry_authorized(current)


@pytest.mark.parametrize("permanent_id", [0, 456])
async def test_unidentified_status_cannot_borrow_september_order_scope(october, permanent_id):
    c = october
    await c.handle_ibkr_event(manual_order())
    await c.handle_ibkr_event(manual_order("order_status", perm_id=permanent_id))
    assert (await c.state.get()).reconciliation.status == "MISMATCH"


async def test_unidentified_order_still_blocks_after_snapshot_completes(october):
    c = october
    await c.begin_ibkr_reconciliation(request_id=7)
    await c.handle_ibkr_event(manual_order("order_status"))
    assert not (await c.state.get()).paused
    assert not c._new_entry_authorized(await c.state.get())
    for kind in ("open_order_end", "completed_order_end", "execution_end", "position_end"):
        await c.handle_ibkr_event(VenueEvent(venue="IBKR", kind=kind, payload={"request_id": 7}))
    current = await c.state.get()
    assert current.reconciliation.status == "MISMATCH"
    assert current.paused and not current.armed
    assert not c._new_entry_authorized(current)


async def test_identified_october_order_pauses_before_snapshot_completes(october):
    c = october
    await c.begin_ibkr_reconciliation(request_id=7)
    await c.handle_ibkr_event(manual_order("order_status"))
    assert not (await c.state.get()).paused
    await c.handle_ibkr_event(manual_order(contract_month="20261030"))
    current = await c.state.get()
    assert current.paused and not current.armed
    assert not c._new_entry_authorized(current)


async def test_reconnect_discards_previous_contract_classification(october):
    c = october
    await c.handle_ibkr_event(manual_order())
    await c.handle_ibkr_event(
        VenueEvent(venue="IBKR", kind="connection", payload={"status": "DISCONNECTED"})
    )
    await c.handle_ibkr_event(manual_order("order_status"))
    assert c._ibkr_foreign_orders == {"0:0:123"}
    assert not c._ibkr_other_month_orders
    assert not c._new_entry_authorized(await c.state.get())


async def test_foreign_callback_matching_strategy_order_is_not_ignored(october):
    c = october
    await c.repository.create_zq_batch_intent(
        batch_id="october-batch",
        order_id=42,
        contract_month="202610",
        quantity=5,
        limit_price=Decimal("96.1"),
        strategy_version="test",
        snapshot_id=1,
    )
    await c.repository.update_zq_order_status(
        order_id=42, status="Submitted", permanent_id="123"
    )
    await c.handle_ibkr_event(manual_order())
    assert c._ibkr_foreign_orders == {"0:0:123"}
    assert not c._ibkr_other_month_orders
    assert not c._new_entry_authorized(await c.state.get())


def test_open_order_bridge_preserves_security_type(settings):
    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper

    adapter = IbkrAdapter(settings, asyncio.Queue())
    adapter._api = SimpleNamespace(
        wrapper=SimpleNamespace(EWrapper=type("Wrapper", (), {})),
        client=SimpleNamespace(EClient=Client),
    )
    adapter._emit = MagicMock()
    bridge = adapter._build_client()
    bridge.openOrder(
        0,
        SimpleNamespace(symbol="ZQ", secType="FUT", lastTradeDateOrContractMonth="20260930"),
        SimpleNamespace(clientId=0, permId=123),
        SimpleNamespace(status="Submitted"),
    )
    kind, payload = adapter._emit.call_args.args
    assert kind == "open_order"
    assert payload["security_type"] == "FUT"
    assert payload["contract_month"] == "20260930"
