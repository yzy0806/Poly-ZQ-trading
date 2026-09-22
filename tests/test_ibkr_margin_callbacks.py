from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.domain.enums import MarginPreviewStatus, RunMode
from zq_arb.domain.models import utc_now
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.repository import Repository
from zq_arb.services.state import StateStore


@pytest.fixture
def preview_bridge(settings):
    configured = settings.model_copy(
        update={
            "ibkr_account_id": SecretStr("DU123456"),
            "ibkr_zq_contract_month": "202610",
            "ibkr_zq_subscription_months": "202610,202611,202612",
            "fomc_rate_effective_date": date(2026, 10, 29),
            "ibkr_zq_child_order_quantity": 5,
        }
    )

    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper

    adapter = IbkrAdapter(configured, asyncio.Queue())
    adapter._api = SimpleNamespace(
        wrapper=SimpleNamespace(EWrapper=type("Wrapper", (), {})),
        client=SimpleNamespace(EClient=Client),
        order=SimpleNamespace(Order=SimpleNamespace),
        order_cancel=SimpleNamespace(OrderCancel=SimpleNamespace),
    )
    adapter._client = MagicMock()
    adapter._client.isConnected.return_value = True
    adapter._next_order_id = 4361
    contract = SimpleNamespace(
        symbol="ZQ", secType="FUT", lastTradeDateOrContractMonth="20261030", conId=123
    )
    adapter._contracts["202610"] = contract
    events = []
    adapter._emit = lambda kind, payload: events.append(
        VenueEvent(venue="IBKR", kind=kind, payload=payload)
    )
    return SimpleNamespace(
        adapter=adapter,
        bridge=adapter._build_client(),
        contract=contract,
        settings=configured,
        events=events,
    )


def request_preview(h):
    order_id = h.adapter.request_zq_margin_preview(
        month="202610", limit_price=Decimal("96.1"), quantity=5
    )
    submitted = h.adapter._client.placeOrder.call_args.args[2]
    order = SimpleNamespace(
        **vars(submitted), clientId=h.settings.ibkr_client_id, orderId=order_id, permId=0
    )
    return order_id, order


def open_order(h, order_id, order):
    h.bridge.openOrder(
        order_id,
        h.contract,
        order,
        SimpleNamespace(
            status="PreSubmitted", initMarginChange="2961.41", maintMarginChange="2575.14"
        ),
    )


def order_status(h, order_id, client_id=None):
    h.bridge.orderStatus(
        order_id,
        "PreSubmitted",
        Decimal(0),
        Decimal(5),
        0,
        0,
        0,
        0,
        h.settings.ibkr_client_id if client_id is None else client_id,
        "",
        0,
    )


def test_late_open_order_is_not_a_live_order(preview_bridge):
    h = preview_bridge
    order_id, order = request_preview(h)
    open_order(h, order_id, order)
    h.adapter.finish_margin_preview(order_id)
    h.events.clear()
    open_order(h, order_id, order)
    assert not h.events


def test_cleanup_errors_do_not_forget_preview_identity(preview_bridge):
    h = preview_bridge
    order_id, _ = request_preview(h)
    h.adapter.finish_margin_preview(order_id)
    h.events.clear()
    for code in (202, 10148, 202):
        h.bridge.error(order_id, code, "Preview cleanup acknowledgement")
        order_status(h, order_id)
    assert not h.events


def test_preview_identity_survives_more_than_100_requests(preview_bridge):
    h = preview_bridge
    oldest, order = request_preview(h)
    h.adapter.finish_margin_preview(oldest)
    for _ in range(110):
        order_id, _ = request_preview(h)
        h.adapter.finish_margin_preview(order_id)
    h.events.clear()
    open_order(h, oldest, order)
    order_status(h, oldest)
    assert not h.events


def test_another_clients_same_order_id_is_not_hidden(preview_bridge):
    h = preview_bridge
    order_id, _ = request_preview(h)
    h.adapter.finish_margin_preview(order_id)
    h.events.clear()
    order_status(h, order_id, client_id=h.settings.ibkr_client_id + 1)
    assert [e.kind for e in h.events] == ["order_status"]


async def test_queued_margin_response_cannot_revive_a_timed_out_preview(preview_bridge):
    h = preview_bridge
    store = StateStore(h.settings)
    order_id, order = request_preview(h)
    await store.apply_ibkr_event(h.events.pop())
    open_order(h, order_id, order)
    queued_response = h.events.pop()
    await store.fail_margin_preview(order_id, "IBKR what-if response exceeded timeout")
    await store.apply_ibkr_event(queued_response)
    preview = await store.get_margin_preview()
    assert preview.status is MarginPreviewStatus.FAILED
    assert preview.error == "IBKR what-if response exceeded timeout"


def test_finishing_a_preview_never_sends_a_broker_cancel(preview_bridge):
    h = preview_bridge
    order_id, _ = request_preview(h)
    h.adapter.finish_margin_preview(order_id)
    h.adapter.finish_margin_preview(order_id)
    h.adapter.finish_margin_preview(4354)  # A live/unknown ID cannot acquire preview scope.
    h.adapter._client.cancelOrder.assert_not_called()
    h.events.clear()
    order_status(h, 4354)
    assert [e.kind for e in h.events] == ["order_status"]


def test_failed_send_releases_context_but_retains_identity_for_late_callbacks(preview_bridge):
    h = preview_bridge
    h.adapter._client.placeOrder.side_effect = RuntimeError("Socket send failed")
    with pytest.raises(RuntimeError, match="Socket send failed"):
        request_preview(h)
    order_id, _, submitted = h.adapter._client.placeOrder.call_args.args
    assert not h.adapter._margin_preview_context
    order = SimpleNamespace(**vars(submitted), clientId=h.settings.ibkr_client_id)
    h.events.clear()
    open_order(h, order_id, order)
    order_status(h, order_id)
    assert not h.events
    h.adapter._client.cancelOrder.assert_not_called()


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize("conflict", ["live_order", "wrong_reference", "foreign_client"])
def test_conflicting_order_is_not_treated_as_a_margin_result(preview_bridge, finished, conflict):
    h = preview_bridge
    order_id, order = request_preview(h)
    if finished:
        h.adapter.finish_margin_preview(order_id)
    if conflict == "live_order":
        order.whatIf = False
    elif conflict == "wrong_reference":
        order.orderRef = "real-strategy-order"
    else:
        order.clientId += 1
    h.events.clear()
    open_order(h, order_id, order)
    order_status(h, order_id, order.clientId)
    assert [e.kind for e in h.events] == ["open_order", "order_status"]


def test_completed_preview_and_actual_execution_have_separate_paths(preview_bridge):
    h = preview_bridge
    order_id, order = request_preview(h)
    h.adapter.finish_margin_preview(order_id)
    h.events.clear()
    h.bridge.completedOrder(h.contract, order, SimpleNamespace(status="Cancelled"))
    assert not h.events
    h.bridge.execDetails(
        7,
        h.contract,
        SimpleNamespace(
            acctNumber="DU123456",
            execId="unexpected-fill",
            orderId=order_id,
            clientId=h.settings.ibkr_client_id,
            permId=9,
            side="BOT",
            shares=1,
            price=96.1,
            time="20260923 00:00:00",
        ),
    )
    assert [e.kind for e in h.events] == ["execution"]


async def test_reconnect_retains_old_preview_ids_without_reusing_them(preview_bridge):
    h = preview_bridge
    old_id, order = request_preview(h)
    await h.adapter.disconnect()
    h.bridge = h.adapter._build_client()
    h.bridge.nextValidId(1)
    new_id, _ = request_preview(h)
    assert new_id > old_id
    h.events.clear()
    open_order(h, old_id, order)
    order_status(h, old_id)
    assert not h.events


@pytest.mark.parametrize("finished", [False, True])
async def test_preview_rejection_still_fails_the_margin_check(preview_bridge, finished):
    h = preview_bridge
    store = StateStore(h.settings)
    order_id, order = request_preview(h)
    await store.apply_ibkr_event(h.events.pop())
    if finished:
        open_order(h, order_id, order)
        await store.apply_ibkr_event(h.events.pop())
        assert (await store.get_margin_preview()).status is MarginPreviewStatus.AVAILABLE
        h.adapter.finish_margin_preview(order_id)
    h.bridge.error(order_id, 201, "What-if credit check rejected")
    await store.apply_ibkr_event(h.events.pop())
    preview = await store.get_margin_preview()
    assert preview.status is MarginPreviewStatus.FAILED
    assert "201" in preview.error


def test_connection_errors_are_not_hidden_by_a_preview_id(preview_bridge):
    h = preview_bridge
    order_id, _ = request_preview(h)
    h.adapter.finish_margin_preview(order_id)
    h.events.clear()
    h.bridge.error(order_id, 1100, "Connection lost")
    assert len(h.events) == 1
    assert not h.events[0].payload["margin_preview"]


async def test_old_response_cannot_replace_the_next_preview(preview_bridge):
    h = preview_bridge
    store = StateStore(h.settings)
    old_id, order = request_preview(h)
    await store.apply_ibkr_event(h.events.pop())
    open_order(h, old_id, order)
    old_response = h.events.pop()
    h.adapter.finish_margin_preview(old_id)
    new_id, _ = request_preview(h)
    await store.apply_ibkr_event(h.events.pop())
    await store.apply_ibkr_event(old_response)
    preview = await store.get_margin_preview()
    assert preview.order_id == new_id
    assert preview.status is MarginPreviewStatus.PENDING


async def test_timeout_winning_the_update_lock_cannot_be_overwritten(preview_bridge, monkeypatch):
    h = preview_bridge
    store = StateStore(h.settings)
    order_id, order = request_preview(h)
    await store.apply_ibkr_event(h.events.pop())
    open_order(h, order_id, order)
    response = h.events.pop()
    update = store.update

    async def timeout_before_update(transform):
        monkeypatch.setattr(store, "update", update)
        await store.fail_margin_preview(order_id, "Timeout won the race")
        return await update(transform)

    monkeypatch.setattr(store, "update", timeout_before_update)
    await store.apply_ibkr_event(response)
    assert (await store.get_margin_preview()).status is MarginPreviewStatus.FAILED


@pytest.mark.parametrize("error_first", [False, True])
async def test_late_preview_does_not_pause_reconciliation_but_a_live_order_does(
    preview_bridge, tmp_path, error_first
):
    h = preview_bridge
    configured = h.settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'preview.sqlite3'}",
            "run_mode": RunMode.PAPER,
            "simulate_polymarket_fills": True,
        }
    )
    db = Database(configured)
    await db.initialize()
    coordinator = ExecutionCoordinator(
        settings=configured,
        repository=Repository(db),
        state=StateStore(configured),
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    coordinator.ibkr.event_queue_overflowed = False
    try:
        await coordinator.recover()
        coordinator._ibkr_open_orders_complete = True
        coordinator._ibkr_completed_orders_complete = True
        coordinator._ibkr_executions_complete = True
        coordinator._ibkr_positions_complete = True
        coordinator._ibkr_snapshot_at = utc_now()
        await coordinator._attempt_automated_reconciliation()
        await coordinator.state.set_operating_state(armed=True, paused=False)

        async def drain():
            while h.events:
                event = h.events.pop(0)
                await coordinator.handle_ibkr_event(event)
                await coordinator.state.apply_ibkr_event(event)

        order_id, order = request_preview(h)
        open_order(h, order_id, order)
        await drain()
        assert (
            await coordinator.state.get_margin_preview()
        ).status is MarginPreviewStatus.AVAILABLE
        h.adapter.finish_margin_preview(order_id)
        callbacks = [
            lambda: open_order(h, order_id, order),
            lambda: h.bridge.error(order_id, 202, "Preview cleanup acknowledgement"),
        ]
        if error_first:
            callbacks.reverse()
        for callback in callbacks:
            callback()
            order_status(h, order_id)
            await drain()
        current = await coordinator.state.get()
        assert current.reconciliation.clean and current.armed and not current.paused
        assert not await coordinator.repository.ledger_orders()
        coordinator.ibkr.cancel_order.assert_not_called()

        order.whatIf = False
        open_order(h, order_id, order)
        await drain()
        current = await coordinator.state.get()
        assert current.paused and not current.armed
        assert current.reconciliation.status == "MISMATCH"
    finally:
        await coordinator.close()
        await db.close()
