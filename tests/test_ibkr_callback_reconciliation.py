from __future__ import annotations

import asyncio
from decimal import Decimal
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from test_opening_inventory import opening  # noqa: F401

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.ibkr import IbkrAdapter
from zq_arb.adapters.polymarket import PolymarketOrderResult, PreparedPolymarketOrder
from zq_arb.domain.models import BookLevel, OrderBook, utc_now
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.models import AuditLogRecord, ExecutionRecord
from zq_arb.persistence.opening_inventory import adopt_opening_inventory
from zq_arb.services.state import StateStore


@pytest.fixture
async def live_fill(opening, request):  # noqa: F811
    repo, baseline, _ = opening
    await adopt_opening_inventory(repo, baseline, reason="verified test opening holdings")
    settings = repo.database.settings.model_copy(
        update={
            "ibkr_zq_child_order_quantity": getattr(request, "param", 1),
            "live_trading_enabled": True,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
        }
    )
    state = StateStore(settings)
    signed = []

    async def prepare(**values):
        order = PreparedPolymarketOrder(
            **values, signed_payload={"salt": len(signed) + 1}, order_id=f"hedge-{len(signed)}"
        )
        signed.append(order)
        return order

    async def post(order):
        return PolymarketOrderResult(
            order_id=order.order_id,
            status="live",
            requested_shares=order.shares,
            immediately_matched_shares=Decimal(0),
            limit_price=order.limit_price,
        )

    pm = SimpleNamespace(
        prepare_hedge_limit=AsyncMock(side_effect=prepare),
        post_prepared_hedge=AsyncMock(side_effect=post),
    )
    ibkr = MagicMock()
    ibkr.event_queue_overflowed = False
    coordinator = ExecutionCoordinator(
        settings=settings, repository=repo, state=state, ibkr=ibkr, polymarket=pm
    )
    await state.set_books(
        tuple(
            OrderBook(
                token_id=leg.yes_token_id,
                asks=(BookLevel(price=Decimal(".1"), size=Decimal("100000")),),
                bids=(BookLevel(price=Decimal(".09"), size=Decimal("100000")),),
                stream_synchronized=True,
            )
            for leg in settings.market_legs
            if leg.code in {"INC25", "INC50PLUS"}
        )
    )
    await repo.create_zq_batch_intent(
        batch_id="new-1",
        order_id=166,
        contract_month=settings.ibkr_zq_contract_month,
        quantity=settings.ibkr_zq_child_order_quantity,
        limit_price=Decimal("96.3"),
        strategy_version="test",
        snapshot_id=1,
    )
    await repo.mark_zq_submitted("new-1")
    coordinator._ibkr_open_order_ids = {166}
    coordinator._ibkr_positions = {"123": Decimal(23)}
    coordinator._ibkr_open_orders_complete = True
    coordinator._ibkr_completed_orders_complete = True
    coordinator._ibkr_executions_complete = True
    coordinator._ibkr_positions_complete = True
    coordinator._ibkr_snapshot_at = utc_now()
    coordinator._polymarket_positions = {
        p["instrument"]: (Decimal(p["quantity"]), Decimal(p["average_price"]))
        for p in baseline["positions"]
        if p["venue"] == "POLYMARKET"
    }
    coordinator._polymarket_positions_complete = True
    coordinator._polymarket_snapshot_complete = True
    coordinator._polymarket_snapshot_at = utc_now()
    await coordinator._attempt_automated_reconciliation()
    assert (await state.get()).reconciliation.clean
    await state.set_operating_state(armed=True, paused=False)
    h = SimpleNamespace(c=coordinator, state=state, repo=repo, settings=settings, ibkr=ibkr)
    try:
        yield h
    finally:
        await coordinator.close()


def event(h, kind, **changes):
    common = {
        "order_id": 166,
        "client_id": h.settings.ibkr_client_id,
        "perm_id": "700",
        "symbol": "ZQ",
        "contract_month": h.settings.ibkr_zq_contract_month,
    }
    payloads = {
        "position": {
            "contract_id": 123,
            "security_type": "FUT",
            "position": "24",
            "account_fingerprint": h.repo.database.identity["ibkr_account"],
        },
        "execution": {"exec_id": "fill-1", "side": "BOT", "shares": "1", "price": "96.3"},
        "order_status": {"status": "Filled", "filled": "1", "remaining": "0"},
        "open_order": {"status": "Filled", "order_ref": "new-1", "action": "BUY"},
    }
    return VenueEvent(
        venue="IBKR", kind=kind, payload={**common, **payloads.get(kind, {}), **changes}
    )


async def finish_hedges(h, expected_orders=2):
    await h.c.wait_for_hedges()
    orders = await h.repo.ledger_orders("POLYMARKET")
    assert len(orders) == expected_orders
    for order in orders:
        trade_id = f"trade-{order.venue_order_id}"
        await h.c.handle_polymarket_event(
            VenueEvent(
                venue="POLYMARKET",
                kind="user_trade",
                payload={
                    "id": trade_id,
                    "taker_order_id": order.venue_order_id,
                    "size": str(order.quantity),
                    "price": str(order.price),
                    "status": "CONFIRMED",
                },
            )
        )
        await h.repo.verify_polymarket_order(
            order.venue_order_id,
            {
                "status": "FILLED",
                "size_matched": str(order.quantity),
                "associate_trades": [trade_id],
            },
        )
    h.c._polymarket_positions = {
        p.instrument: (p.strategy_quantity, p.average_entry_price)
        for p in await h.repo.strategy_portfolio_positions(h.settings)
        if p.venue == "POLYMARKET"
    }
    h.c._polymarket_open_order_ids.clear()
    h.c._polymarket_snapshot_complete = True
    h.c._polymarket_positions_complete = True
    h.c._polymarket_snapshot_at = utc_now()
    await h.c._attempt_automated_reconciliation()


@pytest.mark.parametrize("ordering", list(permutations(["position", "execution", "order_status"])))
async def test_fill_callback_order_does_not_latch_pause(live_fill, ordering):
    h = live_fill
    for kind in ordering:
        await h.c.handle_ibkr_event(event(h, kind))
        current = await h.state.get()
        assert current.armed and not current.paused
        assert not h.c._new_entry_authorized(current)
    await h.c.handle_ibkr_event(event(h, "execution"))  # duplicate must not buy again
    await finish_hedges(h)
    current = await h.state.get()
    assert current.reconciliation.clean
    assert current.armed and not current.paused
    assert await h.repo.strategy_zq_quantity(h.settings.ibkr_zq_contract_month) == 24
    assert len(await h.repo.all_hedge_obligations()) == 2
    async with h.repo.database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExecutionRecord)
                .where(ExecutionRecord.venue == "IBKR")
            )
            == 1
        )
    h.ibkr.cancel_order.assert_not_called()


@pytest.mark.parametrize(
    "callback_account,accepted",
    [("U1234567", True), (" u1234567 ", True), ("U7654567", False)],
)
async def test_real_execution_bridge_uses_ledger_account_identity(
    live_fill, callback_account, accepted
):
    h = live_fill

    class Client:
        def __init__(self, wrapper):
            self.wrapper = wrapper

    adapter = IbkrAdapter(h.settings, asyncio.Queue())
    adapter._api = SimpleNamespace(
        wrapper=SimpleNamespace(EWrapper=type("Wrapper", (), {})),
        client=SimpleNamespace(EClient=Client),
    )
    emitted = []
    adapter._emit = lambda kind, payload: emitted.append(
        VenueEvent(venue="IBKR", kind=kind, payload=payload)
    )
    bridge = adapter._build_client()
    bridge.execDetails(
        9001,
        SimpleNamespace(
            symbol="ZQ", lastTradeDateOrContractMonth=h.settings.ibkr_zq_contract_month, conId=123
        ),
        SimpleNamespace(
            acctNumber=callback_account,
            execId="fill-1",
            orderId=166,
            clientId=h.settings.ibkr_client_id,
            permId=700,
            side="BOT",
            shares=Decimal(1),
            price=96.3,
            time=utc_now().isoformat(),
        ),
    )
    callback = emitted.pop()
    assert callback_account not in str(callback.payload)
    if accepted:
        assert callback.payload["account_fingerprint"] == h.repo.database.identity["ibkr_account"]
    await h.c.handle_ibkr_event(event(h, "position"))
    await h.c.handle_ibkr_event(callback)
    if accepted:
        await h.c.handle_ibkr_event(event(h, "order_status"))
        await h.c.handle_ibkr_event(callback)  # Gateway history replay must remain idempotent.
        await finish_hedges(h)
        current = await h.state.get()
        assert current.reconciliation.clean and current.armed and not current.paused
        assert await h.repo.strategy_zq_quantity(h.settings.ibkr_zq_contract_month) == 24
        assert len(await h.repo.all_hedge_obligations()) == 2
    else:
        # This different account has the same final four digits as the real account.
        current = await h.state.get()
        assert current.paused and not current.armed
        assert await h.repo.strategy_zq_quantity(h.settings.ibkr_zq_contract_month) == 23
        assert not await h.repo.all_hedge_obligations()


async def test_terminal_open_callback_never_reopens_observed_order(live_fill):
    h = live_fill
    for kind in ("position", "execution", "order_status", "open_order"):
        await h.c.handle_ibkr_event(event(h, kind))
    assert 166 not in h.c._ibkr_open_order_ids
    assert not (await h.state.get()).paused


async def test_callback_deadline_expires_without_more_callbacks(live_fill):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    assert not (await h.state.get()).paused
    # The watchdog runs independently of incoming callbacks and analytics.
    await asyncio.sleep(h.settings.ibkr_callback_settle_seconds + 0.2)
    current = await h.state.get()
    assert current.paused and not current.armed
    assert "deadline" in current.metadata["pause_reason"].lower()
    h.ibkr.cancel_order.assert_called_once_with(166)
    async with h.repo.database.session() as session:
        actions = (await session.scalars(select(AuditLogRecord.action))).all()
    assert actions.count("IBKR_CALLBACK_GAP_STARTED") == 1
    assert actions.count("IBKR_CALLBACK_GAP_EXPIRED") == 1


@pytest.mark.parametrize("position", ["22", "25"])
async def test_unexplained_position_does_not_get_timing_allowance(live_fill, position):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position", position=position))
    assert (await h.state.get()).paused


async def test_manual_pause_survives_clean_reconciliation(live_fill):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    await h.state.set_operating_state(paused=True, armed=False)
    await h.c.handle_ibkr_event(event(h, "execution"))
    await h.c.handle_ibkr_event(event(h, "order_status"))
    await finish_hedges(h)
    current = await h.state.get()
    assert current.reconciliation.clean and current.paused and not current.armed


async def complete_refresh(h, request_id):
    for kind in ("open_order_end", "completed_order_end", "position_end", "execution_end"):
        await h.c.handle_ibkr_event(event(h, kind, request_id=request_id))


async def test_immediate_refresh_is_single_flight_and_ignores_old_execution_end(live_fill):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    assert h.c.ibkr_refresh_requested.is_set()
    assert await h.c.begin_ibkr_reconciliation(9004)
    assert not await h.c.begin_ibkr_reconciliation(9005)
    for kind in ("position", "execution", "order_status"):
        await h.c.handle_ibkr_event(event(h, kind))
    await complete_refresh(h, 9003)
    assert not h.c._ibkr_executions_complete
    assert not (await h.state.get()).reconciliation.clean
    await h.c.handle_ibkr_event(event(h, "execution_end", request_id=9004))
    await finish_hedges(h)
    assert (await h.state.get()).reconciliation.clean
    assert not (await h.state.get()).paused


@pytest.mark.parametrize("still_open", [False, True])
async def test_delayed_working_callback_requires_fresh_evidence(live_fill, still_open):
    h = live_fill
    for kind in ("position", "execution", "order_status"):
        await h.c.handle_ibkr_event(event(h, kind))
    await finish_hedges(h)
    await h.c.handle_ibkr_event(event(h, "open_order", status="Submitted"))
    assert not (await h.state.get()).paused
    assert not (await h.state.get()).reconciliation.clean
    assert 166 not in h.c._ibkr_open_order_ids
    assert await h.c.begin_ibkr_reconciliation(9999)
    await h.c.handle_ibkr_event(event(h, "position"))
    if still_open:
        await h.c.handle_ibkr_event(event(h, "open_order", status="Submitted"))
    await complete_refresh(h, 9999)
    current = await h.state.get()
    assert current.paused is still_open
    assert current.reconciliation.clean is not still_open


async def test_fresh_snapshot_with_unrecorded_fill_escalates_before_deadline(live_fill):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    assert await h.c.begin_ibkr_reconciliation(9004)
    await h.c.handle_ibkr_event(event(h, "position"))
    await h.c.handle_ibkr_event(event(h, "order_status"))
    await complete_refresh(h, 9004)
    current = await h.state.get()
    assert current.paused and not current.reconciliation.clean


async def test_duplicates_do_not_extend_deadline(live_fill):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    deadline = h.c._ibkr_gap_deadline
    await h.c.handle_ibkr_event(event(h, "position"))
    assert h.c._ibkr_gap_deadline == deadline
    assert await h.c.begin_ibkr_reconciliation(9004)
    assert h.c._ibkr_gap_deadline == deadline


@pytest.mark.parametrize("fault", ["foreign_client", "unknown_order", "unknown_fill", "identity"])
async def test_independent_fault_interrupts_pending_gap_immediately(live_fill, fault):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    if fault == "foreign_client":
        callback = event(h, "order_status", client_id=999, status="Submitted")
    elif fault == "unknown_order":
        callback = event(h, "open_order", order_id=999, status="Submitted")
    elif fault == "unknown_fill":
        callback = event(h, "execution", order_id=999)
    else:
        callback = event(h, "execution", account_fingerprint="wrong-account")
    await h.c.handle_ibkr_event(callback)
    current = await h.state.get()
    assert current.paused and not current.armed
    assert not h.c._hedge_submission_ready()


async def test_refresh_timeout_cannot_be_cleared_by_late_end_markers(live_fill):
    h = live_fill
    assert await h.c.begin_ibkr_reconciliation(9004)
    h.c._ibkr_refresh_started -= h.settings.execution_request_timeout_seconds + 1
    with pytest.raises(TimeoutError):
        await h.c.check_ibkr_refresh_timeout()
    await h.c.handle_ibkr_event(event(h, "position", position="23"))
    await complete_refresh(h, 9004)
    with pytest.raises(RuntimeError, match="reconnect"):
        await h.c.begin_ibkr_reconciliation(9005)
    assert not (await h.state.get()).reconciliation.clean


async def test_existing_pause_reason_survives_clean_and_clears_only_on_operator_resume(live_fill):
    h = live_fill
    await h.state.set_operating_state(
        paused=True, armed=False, pause_reason="Operator pause: review"
    )
    await h.c._attempt_automated_reconciliation()
    assert (await h.state.get()).metadata["pause_reason"] == "Operator pause: review"
    await h.state.set_operating_state(paused=False, armed=True)
    assert (await h.state.get()).metadata["pause_reason"] is None


async def test_cached_clean_snapshot_cannot_authorize_during_refresh(live_fill):
    h = live_fill
    old = await h.state.get()
    assert h.c._new_entry_authorized(old)
    assert await h.c.begin_ibkr_reconciliation(12345)
    assert not h.c._new_entry_authorized(old)


async def test_restart_does_not_inherit_live_fill_grace_or_armed_state(live_fill):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    restarted = ExecutionCoordinator(
        settings=h.settings,
        repository=h.repo,
        state=StateStore(h.settings),
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    await restarted.recover()
    current = await restarted.state.get()
    assert not current.armed and not current.reconciliation.clean
    assert restarted._ibkr_matched_zq is None and restarted._ibkr_gap is None
    assert not restarted._hedge_submission_ready()
    await restarted.close()


@pytest.mark.parametrize("live_fill", [2], indirect=True)
@pytest.mark.parametrize("cancel_between", [False, True])
async def test_partial_and_late_fills_keep_actual_quantities(live_fill, cancel_between):
    h = live_fill
    await h.c.handle_ibkr_event(event(h, "position"))
    await h.c.handle_ibkr_event(event(h, "execution"))
    await h.c.handle_ibkr_event(event(h, "order_status", status="Submitted", remaining="1"))
    await finish_hedges(h)
    assert not (await h.state.get()).paused
    if cancel_between:
        await h.c.handle_ibkr_event(event(h, "order_status", status="Cancelled"))
    late = event(h, "execution", exec_id="fill-2")
    await h.c.handle_ibkr_event(late)
    await h.c.handle_ibkr_event(event(h, "position", position="25"))
    await h.c.handle_ibkr_event(event(h, "order_status", filled="2"))
    await h.c.handle_ibkr_event(late)
    await finish_hedges(h, expected_orders=4)
    current = await h.state.get()
    assert current.reconciliation.clean and not current.paused
    assert await h.repo.strategy_zq_quantity(h.settings.ibkr_zq_contract_month) == 25
    assert len(await h.repo.all_hedge_obligations()) == 4
