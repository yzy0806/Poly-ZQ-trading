from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from test_ibkr_callback_reconciliation import (
    complete_refresh,
    event,
    finish_hedges,
    live_fill,  # noqa: F401
)
from test_opening_inventory import opening  # noqa: F401

from zq_arb.adapters.events import VenueEvent
from zq_arb.domain.enums import AlertSeverity, ConnectionStatus
from zq_arb.domain.models import AlertView
from zq_arb.execution.maintenance import MaintenanceHold
from zq_arb.persistence.models import AuditLogRecord
from zq_arb.services.engine import EngineRuntime


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
async def maintenance(live_fill):  # noqa: F811
    h = live_fill
    h.c.maintenance = MaintenanceHold(
        h.settings.model_copy(update={"ibkr_maintenance_enabled": True})
    )
    clock = SimpleNamespace(now=instant("2026-09-14T20:59:00Z"))
    h.c.maintenance.now = lambda: clock.now
    h.clock = clock
    h.ibkr.connected = True
    await h.state.set_ibkr_health(ConnectionStatus.CONNECTED, "test connected")
    return h


async def drain(h):
    await h.c.maintain()
    h.ibkr.cancel_order.assert_called_with(166)
    assert not h.c.maintenance.drained  # A request is not an acknowledgement.
    await h.c.handle_ibkr_event(
        event(h, "order_status", status="Cancelled", filled="0", remaining="0")
    )
    await h.c.maintain()
    assert h.c.maintenance.drained


async def disconnect(h):
    h.clock.now = instant("2026-09-14T21:10:00Z")
    h.ibkr.connected = False
    await h.c.handle_ibkr_event(event(h, "connection", status="DISCONNECTED"))


async def reconnect(h):
    await h.c.handle_ibkr_event(event(h, "connection", status="CONNECTING"))
    await h.c.handle_ibkr_event(event(h, "connection", status="CONNECTED"))
    h.ibkr.connected = True
    assert await h.c.begin_ibkr_reconciliation(9007)
    await h.c.handle_ibkr_event(event(h, "position", position="23"))
    await complete_refresh(h, 9007)
    assert (await h.state.get()).reconciliation.clean


async def actions(h):
    async with h.repo.database.session() as session:
        return list(await session.scalars(select(AuditLogRecord.action)))


async def test_normal_break_keeps_armed_and_resumes_without_opportunity(maintenance):
    h = maintenance
    before = await h.state.get()
    assert before.armed and h.c._new_entry_authorized(before) is False  # Clock gate, before tick.
    await drain(h)
    await disconnect(h)
    assert await h.c.maintenance_recovery_allowed()
    h.clock.now += timedelta(seconds=90)
    await reconnect(h)
    await h.c.maintain()
    assert not h.c.maintenance.recovering(h.clock.now)
    current = await h.state.get()
    assert current.armed and not current.paused
    assert not h.c._new_entry_authorized(current)
    h.clock.now = instant("2026-09-14T22:00:00Z")
    await h.c.maintain()
    current = await h.state.get()
    assert current.armed and not current.paused and not current.opportunities
    assert h.c._new_entry_authorized(current)
    assert (await actions(h)).count("MAINTENANCE_STARTED") == 1
    assert (await actions(h)).count("MAINTENANCE_COMPLETED") == 1
    assert "ARM" not in await actions(h)
    await h.c.maintain()
    assert (await actions(h)).count("MAINTENANCE_COMPLETED") == 1


@pytest.mark.parametrize("path", ["watchdog", "reconciliation"])
@pytest.mark.parametrize("elapsed", [11, 29, 30, 31])
async def test_independent_refresh_deadline(live_fill, path, elapsed):  # noqa: F811
    h = live_fill
    assert h.settings.execution_request_timeout_seconds == 10
    assert h.settings.ibkr_account_refresh_timeout_seconds == 30
    assert h.settings.ibkr_callback_settle_seconds == 2
    await h.c.begin_ibkr_reconciliation(9007)
    h.c._ibkr_refresh_started = asyncio.get_running_loop().time() - elapsed
    if path == "watchdog" and elapsed >= 30:
        with pytest.raises(TimeoutError):
            await h.c.check_ibkr_refresh_timeout()
    elif path == "watchdog":
        await h.c.check_ibkr_refresh_timeout()
    else:
        await h.c._attempt_automated_reconciliation()
    current = await h.state.get()
    assert current.paused == (elapsed >= 30)
    assert not h.c._new_entry_authorized(current)
    if elapsed < 30:
        await h.c.handle_ibkr_event(event(h, "position", position="23"))
        await h.c.handle_ibkr_event(
            event(h, "open_order", status="Submitted", order_ref="new-1", action="BUY")
        )
        await complete_refresh(h, 9007)
        assert (await h.state.get()).reconciliation.clean


async def test_restart_timeout_reconnects_without_latching_pause(maintenance):
    h = maintenance
    await drain(h)
    await disconnect(h)
    await h.c.handle_ibkr_event(event(h, "connection", status="CONNECTING"))
    await h.c.handle_ibkr_event(event(h, "connection", status="CONNECTED"))
    await h.c.begin_ibkr_reconciliation(9007)
    h.c._ibkr_refresh_started -= 31
    with pytest.raises(TimeoutError):
        await h.c.check_ibkr_refresh_timeout()
    current = await h.state.get()
    assert not current.paused and current.armed and not current.reconciliation.clean
    assert await h.c.maintenance_recovery_allowed()
    # Late markers cannot certify the timed-out read; reconnect remains mandatory.
    await h.c.handle_ibkr_event(event(h, "position", position="23"))
    await complete_refresh(h, 9007)
    assert not (await h.state.get()).reconciliation.clean
    with pytest.raises(RuntimeError, match="reconnect"):
        await h.c.begin_ibkr_reconciliation(9008)


@pytest.mark.parametrize("at", ["2026-09-14T21:05:00Z", "2026-09-14T21:13:00Z"])
async def test_unrelated_timeout_during_break_still_pauses(maintenance, at):
    h = maintenance
    await drain(h)
    h.clock.now = instant(at)
    await h.c.begin_ibkr_reconciliation(9007)
    h.c._ibkr_refresh_started -= 31
    with pytest.raises(TimeoutError):
        await h.c.check_ibkr_refresh_timeout()
    assert (await h.state.get()).paused


async def test_retries_cannot_reclassify_an_earlier_unrelated_outage(maintenance):
    h = maintenance
    await drain(h)
    h.clock.now = instant("2026-09-14T21:08:00Z")
    await h.c.handle_ibkr_event(event(h, "connection", status="DISCONNECTED"))
    assert not await h.c.maintenance_recovery_allowed()
    h.clock.now = instant("2026-09-14T21:10:00Z")
    assert not await h.c.maintenance_recovery_allowed()
    await h.c.begin_ibkr_reconciliation(9007)
    h.c._ibkr_refresh_started -= 31
    with pytest.raises(TimeoutError):
        await h.c.check_ibkr_refresh_timeout()
    assert (await h.state.get()).paused


async def test_recovered_earlier_outage_does_not_prevent_scheduled_restart(maintenance):
    h = maintenance
    await drain(h)
    h.clock.now = instant("2026-09-14T21:05:00Z")
    await h.c.handle_ibkr_event(event(h, "connection", status="DISCONNECTED"))
    await reconnect(h)
    await h.c.maintain()
    assert h.c.maintenance.interruption_at is None
    await disconnect(h)
    assert await h.c.maintenance_recovery_allowed()


async def test_connectivity_error_records_outage_start_before_later_timeout(maintenance):
    h = maintenance
    await drain(h)
    h.clock.now = instant("2026-09-14T21:11:50Z")
    await h.c.handle_ibkr_event(event(h, "error", code=1100))
    h.clock.now += timedelta(seconds=31)
    await h.c.begin_ibkr_reconciliation(9007)
    h.c._ibkr_refresh_started -= 31
    with pytest.raises(TimeoutError):
        await h.c.check_ibkr_refresh_timeout()
    assert not (await h.state.get()).paused
    assert await h.c.maintenance_recovery_allowed()


async def test_safety_fault_has_priority_over_expected_restart_timeout(maintenance):
    h = maintenance
    await drain(h)
    await disconnect(h)
    await h.c.begin_ibkr_reconciliation(9007)
    h.c._ibkr_unknown_orders.add(999)
    h.c._ibkr_refresh_started -= 31
    with pytest.raises(TimeoutError):
        await h.c.check_ibkr_refresh_timeout()
    current = await h.state.get()
    assert current.paused and not current.armed
    assert "unexplained venue evidence" in current.metadata["pause_reason"]
    assert not await h.c.maintenance_recovery_allowed()


@pytest.mark.parametrize("control", ["disarm", "pause", "halt"])
async def test_operator_action_is_never_overridden(maintenance, control):
    h = maintenance
    await drain(h)
    if control == "halt":
        await h.c.halt("operator test")
    elif control == "pause":
        await h.state.set_operating_state(armed=False, paused=True, pause_reason="Operator pause")
    else:
        await h.state.set_operating_state(armed=False)
    h.clock.now = instant("2026-09-14T22:00:00Z")
    await h.c.maintain()
    current = await h.state.get()
    assert not current.armed and not h.c._new_entry_authorized(current)
    if control == "pause":
        assert current.metadata["pause_reason"] == "Operator pause"
    if control == "halt":
        assert current.kill_switch


async def test_disarmed_before_break_stays_disarmed(maintenance):
    h = maintenance
    await h.state.set_operating_state(armed=False)
    await drain(h)
    await disconnect(h)
    await reconnect(h)
    h.clock.now = instant("2026-09-14T22:00:00Z")
    await h.c.maintain()
    assert not (await h.state.get()).armed


async def test_unconfirmed_cancel_at_close_fails_drain(maintenance):
    h = maintenance
    await h.c.maintain()
    h.clock.now = instant("2026-09-14T21:00:00Z")
    await h.c.maintain()
    current = await h.state.get()
    assert current.paused and not current.armed
    assert "drain" in current.metadata["pause_reason"]
    await disconnect(h)
    assert not await h.c.maintenance_recovery_allowed()


@pytest.mark.parametrize("live_fill", [5], indirect=True)
async def test_partial_fill_during_drain_still_hedges_actual_fill(maintenance):
    h = maintenance
    await h.c.maintain()
    for kind in ("execution", "position", "order_status"):
        extra = (
            {"status": "Cancelled", "filled": "1", "remaining": "0"}
            if kind == "order_status"
            else {}
        )
        await h.c.handle_ibkr_event(event(h, kind, **extra))
    await h.c.cycle(await h.state.get())
    await finish_hedges(h)
    await h.c.maintain()
    assert h.c.maintenance.drained
    assert len(await h.repo.all_hedge_obligations()) == 2
    assert (await h.state.get()).armed
    h.ibkr.submit_zq_limit_day.assert_not_called()


async def test_delayed_recovery_resumes_before_deadline(maintenance):
    h = maintenance
    await drain(h)
    await disconnect(h)
    h.clock.now = instant("2026-09-14T22:00:00Z")
    await h.c.maintain()
    assert h.c.maintenance.blocks_entry(h.clock.now)
    assert (await h.state.get()).armed
    h.clock.now += timedelta(minutes=2)
    await reconnect(h)
    await h.c.maintain()
    assert not h.c.maintenance.blocks_entry(h.clock.now)
    assert (await h.state.get()).armed


async def test_recovery_deadline_latches_and_cannot_extend(maintenance):
    h = maintenance
    await drain(h)
    await disconnect(h)
    h.clock.now = instant("2026-09-14T22:05:00Z")
    assert not await h.c.maintenance_recovery_allowed()
    await h.c.maintain()
    current = await h.state.get()
    assert current.paused and not current.armed
    assert "maintenance recovery deadline" in current.metadata["pause_reason"]
    await reconnect(h)
    await h.c.maintain()
    assert not (await h.state.get()).armed


@pytest.mark.parametrize(
    "now,blocked",
    [
        ("2026-09-14T20:58:59Z", False),
        ("2026-09-14T20:59:00Z", True),
        ("2026-09-14T21:59:59Z", True),
        ("2026-09-14T22:00:00Z", False),
        ("2026-09-18T22:00:00Z", True),  # Friday does not reopen at 17:00.
        ("2026-09-19T12:00:00Z", True),
        ("2026-09-20T21:59:59Z", True),
        ("2026-09-20T22:00:00Z", False),
        ("2026-11-02T21:59:00Z", True),  # US standard time.
        ("2026-11-02T22:59:59Z", True),
        ("2026-11-02T23:00:00Z", False),
    ],
)
def test_regular_schedule_and_dst(settings, now, blocked):
    hold = MaintenanceHold(settings.model_copy(update={"ibkr_maintenance_enabled": True}))
    assert hold.blocks_entry(instant(now)) == blocked


async def test_connection_worker_retries_through_expected_restart(maintenance, monkeypatch):
    h = maintenance
    await drain(h)
    await disconnect(h)
    runtime = EngineRuntime(h.settings)
    runtime.execution = h.c
    runtime.state = h.state
    runtime.ibkr = MagicMock()
    runtime.ibkr.connect = AsyncMock(side_effect=ConnectionError("scheduled restart"))
    runtime.ibkr.disconnect = AsyncMock()
    waits = []

    async def sleep(delay):
        waits.append(delay)
        if len(waits) == h.settings.ibkr_reconnect_max_attempts + 2:
            runtime._stopping.set()

    monkeypatch.setattr("zq_arb.services.engine.asyncio.sleep", sleep)
    try:
        await runtime._ibkr_connection_loop()
        assert runtime.ibkr.connect.await_count > h.settings.ibkr_reconnect_max_attempts
        assert max(waits) <= 15
        assert (await h.state.get()).armed
    finally:
        await runtime.polymarket.close()
        await runtime.database.close()


async def test_connection_barrier_precedes_refresh(settings):
    runtime = EngineRuntime(settings)
    order = []
    callbacks = []
    runtime.ibkr = MagicMock()
    runtime.ibkr.connected = False

    async def connect():
        order.append("socket")

        async def callback():
            await asyncio.sleep(0)
            await runtime.execution.handle_ibkr_event(
                VenueEvent(venue="IBKR", kind="connection", payload={"status": "CONNECTED"})
            )
            order.append("callback")

        callbacks.append(asyncio.create_task(callback()))

    async def refresh():
        assert runtime.execution.ibkr_connection_ready.is_set()
        assert order == ["socket", "callback"]
        runtime._stopping.set()
        return True

    runtime.ibkr.connect = AsyncMock(side_effect=connect)
    runtime.execution._attempt_automated_reconciliation = AsyncMock()
    runtime.execution._publish = AsyncMock()
    runtime._refresh_ibkr_account = AsyncMock(side_effect=refresh)
    try:
        await runtime._ibkr_connection_loop()
        runtime._refresh_ibkr_account.assert_awaited_once()
    finally:
        await asyncio.gather(*callbacks)
        await runtime.polymarket.close()
        await runtime.database.close()


@pytest.mark.parametrize(
    "code,old,allowed",
    [
        ("IBKR_1100", False, True),
        ("IBKR_1100", True, False),
        ("HEDGE_ROUTING_FAILED", False, False),
    ],
)
async def test_only_current_restart_connectivity_alert_is_recoverable(
    maintenance, code, old, allowed
):
    h = maintenance
    await drain(h)
    await disconnect(h)
    alert = AlertView(
        alert_id="restart-alert",
        code=code,
        message="test",
        severity=AlertSeverity.CRITICAL,
        created_at=h.clock.now - timedelta(hours=1 if old else 0),
    )
    await h.state.update(lambda s: s.model_copy(update={"alerts": (alert,)}))
    assert await h.c.maintenance_recovery_allowed() == allowed
    if allowed:
        await reconnect(h)
        await h.c.maintain()
        assert (await h.state.get()).alerts[0].resolved


async def test_last_end_marker_after_deadline_cannot_certify_late_read(live_fill):  # noqa: F811
    h = live_fill
    await h.c.begin_ibkr_reconciliation(9007)
    await h.c.handle_ibkr_event(event(h, "position", position="23"))
    for kind in ("open_order_end", "completed_order_end", "position_end"):
        await h.c.handle_ibkr_event(event(h, kind))
    h.c._ibkr_refresh_started -= 31
    await h.c.handle_ibkr_event(event(h, "execution_end", request_id=9007))
    current = await h.state.get()
    assert current.paused and not current.reconciliation.clean


async def test_recovery_after_deadline_does_not_resume_even_if_clean(maintenance):
    h = maintenance
    await drain(h)
    await disconnect(h)
    h.clock.now = instant("2026-09-14T22:06:00Z")
    await reconnect(h)
    await h.c.maintain()
    assert (await h.state.get()).paused
    assert not (await h.state.get()).armed


async def test_disarm_during_completion_audit_wins(maintenance):
    h = maintenance
    await drain(h)
    h.clock.now = instant("2026-09-14T22:00:00Z")
    entered = asyncio.Event()
    release = asyncio.Event()
    original = h.repo.audit

    async def audit(**kwargs):
        if kwargs["action"] == "MAINTENANCE_COMPLETED":
            entered.set()
            await release.wait()
        return await original(**kwargs)

    h.repo.audit = audit
    task = asyncio.create_task(h.c.maintain())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await h.state.set_operating_state(armed=False)
    finally:
        release.set()
        await task
    assert not (await h.state.get()).armed


async def test_stale_clean_snapshot_cannot_release_hold(maintenance):
    h = maintenance
    await drain(h)
    h.clock.now = instant("2026-09-14T22:00:00Z")
    h.c._ibkr_snapshot_at -= timedelta(seconds=h.settings.reconciliation_max_age_seconds + 1)
    await h.c.maintain()
    assert h.c.maintenance.blocks_entry(h.clock.now)
    assert (await h.state.get()).armed


async def test_new_engine_during_break_does_not_restore_arming(settings):
    runtime = EngineRuntime(settings.model_copy(update={"ibkr_maintenance_enabled": True}))
    try:
        runtime.execution.maintenance.now = lambda: instant("2026-09-14T21:30:00Z")
        assert runtime.execution.maintenance.blocks_entry(runtime.execution.maintenance.now())
        assert not runtime.execution.maintenance.drained
        assert not (await runtime.state.get()).armed
        assert not runtime.execution.maintenance.recovering(runtime.execution.maintenance.now())
    finally:
        await runtime.polymarket.close()
        await runtime.database.close()
