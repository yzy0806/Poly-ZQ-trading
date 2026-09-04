from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.polymarket import PolymarketAdapter
from zq_arb.config import Settings
from zq_arb.domain.enums import BatchState, MarginPreviewStatus, RunMode
from zq_arb.domain.models import (
    BatchView,
    BookLevel,
    EffrObservation,
    MarginPreview,
    Opportunity,
    OpportunityCalculation,
    OpportunityCostBreakdown,
    OrderBook,
    Quote,
)
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import ExecutionRecord, HedgeObligationRecord, OrderRecord
from zq_arb.persistence.repository import Repository
from zq_arb.services.state import StateStore


def hedge_book(token_id: str, ask: str) -> OrderBook:
    ask_price = Decimal(ask)
    return OrderBook(
        token_id=token_id,
        bids=(
            BookLevel(
                price=max(Decimal("0.001"), ask_price - Decimal("0.01")),
                size=Decimal("50000"),
            ),
        ),
        asks=(BookLevel(price=ask_price, size=Decimal("50000")),),
        tick_size=Decimal("0.001"),
        stream_synchronized=True,
        source="WEBSOCKET",
    )


@pytest.mark.asyncio
async def test_new_entry_waits_for_every_persisted_hedge_deficit(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
        }
    )
    state = StateStore(configured)
    coordinator = ExecutionCoordinator(
        settings=configured,
        repository=MagicMock(),
        state=state,
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    snapshot = (await state.get()).model_copy(
        update={
            "armed": True,
            "metadata": {
                **(await state.get()).metadata,
                "unresolved_hedge_obligations": 1,
            },
        }
    )

    assert not coordinator._new_entry_authorized(snapshot)
    assert coordinator._new_entry_authorized(
        snapshot.model_copy(
            update={
                "metadata": {
                    **snapshot.metadata,
                    "unresolved_hedge_obligations": 0,
                }
            }
        )
    )


@pytest.mark.asyncio
async def test_armed_waiting_does_not_submit_without_a_tradeable_opportunity(
    settings: Settings,
) -> None:
    configured = settings.model_copy(
        update={
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
        }
    )
    repository = MagicMock()
    repository.active_batch_view = AsyncMock(return_value=BatchView())
    coordinator = ExecutionCoordinator(
        settings=configured,
        repository=repository,
        state=StateStore(configured),
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    coordinator._publish = AsyncMock()
    snapshot = (await coordinator.state.get()).model_copy(update={"armed": True})

    await coordinator.cycle(snapshot)

    coordinator.ibkr.submit_zq_limit_day.assert_not_called()


@pytest.mark.asyncio
async def test_ibkr_fill_durably_triggers_incremental_lowest_ask_hedges_and_late_fills(
    tmp_path: Path,
    settings: Settings,
) -> None:
    database_path = tmp_path / "coordinator.sqlite3"
    configured = settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}",
            "run_mode": RunMode.PAPER,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
            "simulate_polymarket_fills": True,
        }
    )
    database = Database(configured)
    await database.initialize()
    repository = Repository(database)
    state = StateStore(configured)
    polymarket = PolymarketAdapter(configured)
    ibkr = MagicMock()
    coordinator = ExecutionCoordinator(
        settings=configured,
        repository=repository,
        state=state,
        ibkr=ibkr,
        polymarket=polymarket,
    )
    inc25 = next(leg for leg in configured.market_legs if leg.code == "INC25")
    inc50 = next(leg for leg in configured.market_legs if leg.code == "INC50PLUS")
    await state.set_books(
        (
            hedge_book(inc25.yes_token_id, "0.28"),
            hedge_book(inc50.yes_token_id, "0.10"),
        )
    )
    await state.confirm_reconciliation(
        actor="operator",
        reason="initial account reconciliation",
        snapshot_id=1,
    )
    await repository.create_zq_batch_intent(
        batch_id="batch-1",
        order_id=42,
        contract_month=configured.ibkr_zq_contract_month,
        quantity=10,
        limit_price=Decimal("96.32"),
        strategy_version=configured.strategy_version,
        snapshot_id=2,
    )
    await repository.mark_zq_submitted("batch-1")

    fill = VenueEvent(
        venue="IBKR",
        kind="execution",
        payload={
            "exec_id": "ibkr-exec-1",
            "order_id": 42,
            "side": "BOT",
            "shares": "3",
            "price": "96.32",
            "time": datetime.now(UTC).isoformat(),
        },
    )
    await coordinator.handle_ibkr_event(fill)
    await state.apply_ibkr_event(fill)
    await coordinator.handle_ibkr_event(fill)

    batch = await repository.active_batch_view()
    assert batch.filled_quantity == Decimal("3")
    assert await repository.strategy_zq_quantity(configured.ibkr_zq_contract_month) == Decimal("3")
    assert batch.remaining_quantity == Decimal("7")
    assert len(batch.obligations) == 2
    assert [item.due_shares for item in batch.obligations] == [
        Decimal("1458.45000000"),
        Decimal("2916.90000000"),
    ]
    assert all(item.deficit_shares == 0 for item in batch.obligations)
    current = await state.get()
    assert current.reconciliation.clean
    positions = {(item.venue, item.label): item for item in current.portfolio.positions}
    zq_position = positions[("IBKR", f"ZQ {configured.ibkr_zq_contract_month}")]
    assert zq_position.strategy_quantity == Decimal("3")
    assert positions[("POLYMARKET", "INC25 YES")].strategy_quantity == Decimal(
        "1458.45000000"
    )
    assert positions[("POLYMARKET", "INC50PLUS YES")].strategy_quantity == Decimal(
        "2916.90000000"
    )
    assert positions[("POLYMARKET", "INC25 YES")].simulated
    assert positions[("POLYMARKET", "INC25 YES")].reconciled is True

    coordinator._polymarket_snapshot_complete = True
    await coordinator.begin_ibkr_reconciliation()
    for venue_event in (
        VenueEvent(
            venue="IBKR",
            kind="open_order",
            payload={"order_id": 42, "status": "Submitted"},
        ),
        VenueEvent(
            venue="IBKR",
            kind="position",
            payload={
                "account_fingerprint": "account",
                "symbol": "ZQ",
                "security_type": "FUT",
                "contract_month": configured.ibkr_zq_contract_month,
                "contract_id": 123,
                "position": "3",
            },
        ),
        VenueEvent(venue="IBKR", kind="open_order_end", payload={}),
        VenueEvent(venue="IBKR", kind="execution_end", payload={}),
        VenueEvent(venue="IBKR", kind="position_end", payload={}),
    ):
        await coordinator.handle_ibkr_event(venue_event)
    reconciled = await state.get()
    assert reconciled.metadata["zq_position"] == 3
    assert reconciled.reconciliation.clean
    assert reconciled.reconciliation.method == "AUTHENTICATED_VENUE_LEDGER"

    await coordinator.handle_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="position",
            payload={
                "account_fingerprint": "account",
                "symbol": "ZQ",
                "security_type": "FUT",
                "contract_month": configured.ibkr_zq_contract_month,
                "contract_id": 123,
                "position": "4",
            },
        )
    )
    assert (await state.get()).metadata["zq_position"] == 4

    await coordinator.begin_ibkr_reconciliation()
    for venue_event in (
        VenueEvent(
            venue="IBKR",
            kind="open_order",
            payload={"order_id": 42, "status": "Submitted"},
        ),
        VenueEvent(
            venue="IBKR",
            kind="position",
            payload={
                "account_fingerprint": "account",
                "symbol": "ZQ",
                "security_type": "FUT",
                "contract_month": configured.ibkr_zq_contract_month,
                "contract_id": 123,
                "position": "2",
            },
        ),
        VenueEvent(venue="IBKR", kind="open_order_end", payload={}),
        VenueEvent(venue="IBKR", kind="execution_end", payload={}),
        VenueEvent(venue="IBKR", kind="position_end", payload={}),
    ):
        await coordinator.handle_ibkr_event(venue_event)
    mismatched = await state.get()
    assert mismatched.metadata["zq_position"] == 2
    assert not mismatched.reconciliation.clean
    assert "zq_position_mismatch" in mismatched.reconciliation.reason

    async with database.session() as session:
        ibkr_execs = await session.scalar(
            select(func.count()).select_from(ExecutionRecord).where(ExecutionRecord.venue == "IBKR")
        )
        poly_orders = tuple(
            (
                await session.scalars(
                    select(OrderRecord)
                    .where(OrderRecord.venue == "POLYMARKET")
                    .order_by(OrderRecord.price)
                )
            ).all()
        )
        obligation_count = await session.scalar(
            select(func.count()).select_from(HedgeObligationRecord)
        )
    assert ibkr_execs == 1
    assert obligation_count == 2
    assert [item.price for item in poly_orders] == [
        Decimal("0.1000000000"),
        Decimal("0.2800000000"),
    ]
    assert all(item.details["simulated"] for item in poly_orders)

    await state.set_books(
        (
            hedge_book(inc25.yes_token_id, "0.90"),
            hedge_book(inc50.yes_token_id, "0.90"),
        )
    )
    await coordinator.cycle(await state.get())
    cancelled = await repository.active_batch_view()
    assert cancelled.state is BatchState.CANCEL_PENDING
    assert cancelled.cancel_reason is not None
    ibkr.cancel_order.assert_called_once_with(42)

    late_fill = fill.model_copy(
        update={
            "payload": {
                **fill.payload,
                "exec_id": "ibkr-exec-late",
                "shares": "2",
            }
        }
    )
    await coordinator.handle_ibkr_event(late_fill)
    assert await repository.strategy_zq_quantity(configured.ibkr_zq_contract_month) == Decimal("5")
    await coordinator.handle_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="order_status",
            payload={
                "order_id": "42",
                "status": "Cancelled",
                "filled": "5",
                "remaining": "5",
            },
        )
    )
    assert (await repository.active_batch_view()).state is BatchState.IDLE

    await coordinator.begin_ibkr_reconciliation()
    for venue_event in (
        VenueEvent(
            venue="IBKR",
            kind="position",
            payload={
                "account_fingerprint": "account",
                "symbol": "ZQ",
                "security_type": "FUT",
                "contract_month": configured.ibkr_zq_contract_month,
                "contract_id": 123,
                "position": "5",
            },
        ),
        VenueEvent(venue="IBKR", kind="open_order_end", payload={}),
        VenueEvent(venue="IBKR", kind="execution_end", payload={}),
        VenueEvent(venue="IBKR", kind="position_end", payload={}),
    ):
        await coordinator.handle_ibkr_event(venue_event)
    assert (await state.get()).reconciliation.clean

    ibkr.reserve_order_id.return_value = 43
    await state.set_operating_state(armed=True, paused=False)
    opportunity = Opportunity(
        contracts=10,
        zq_price=Decimal("96.31"),
        tradeable=True,
        calculation=OpportunityCalculation(
            inc25_shares_per_contract=Decimal("486.15"),
            inc50plus_shares_per_contract=Decimal("972.30"),
            inc25_emergency_hedge_cash=Decimal("2041.83"),
            inc50plus_emergency_hedge_cash=Decimal("58.34"),
            emergency_hedge_cash=Decimal("2100.17"),
            incremental_initial_margin=Decimal("2587.52"),
            emergency_cash_reserve=Decimal("0"),
            committed_capital=Decimal("4687.69"),
            costs=OpportunityCostBreakdown(
                ibkr_commission=Decimal("36.40"),
                polymarket_fees=Decimal("62.11"),
                explicit_costs=Decimal("98.51"),
            ),
        ),
    )
    waiting = await state.get()
    await state.replace(
        waiting.model_copy(
            update={
                "opportunities": (opportunity,),
                "quotes": {
                    configured.ibkr_zq_contract_month: Quote(
                        instrument=configured.ibkr_zq_contract_month,
                        bid=Decimal("96.31"),
                    )
                },
            }
        )
    )

    await coordinator.cycle(await state.get())

    replacement = await repository.active_batch_view()
    assert replacement.batch_id is not None
    assert replacement.original_quantity == 10
    assert replacement.limit_price == Decimal("96.31")
    ibkr.submit_zq_limit_day.assert_called_once()

    await polymarket.close()
    await database.close()


@pytest.mark.asyncio
async def test_pending_margin_refresh_defers_only_residual_return_gate(settings: Settings) -> None:
    state = StateStore(settings)
    ibkr = MagicMock()
    coordinator = ExecutionCoordinator(
        settings=settings,
        repository=MagicMock(),
        state=state,
        ibkr=ibkr,
        polymarket=MagicMock(),
    )
    inc25 = next(leg for leg in settings.market_legs if leg.code == "INC25")
    inc50 = next(leg for leg in settings.market_legs if leg.code == "INC50PLUS")
    now = datetime.now(UTC)
    snapshot = (await state.get()).model_copy(
        update={
            "books": {
                inc25.yes_token_id: hedge_book(inc25.yes_token_id, "0.60"),
                inc50.yes_token_id: hedge_book(inc50.yes_token_id, "0.01"),
            },
            "effr": EffrObservation(
                source="MANUAL",
                rate_percent=Decimal("3.63"),
                effective_date=now.date(),
                fetched_at=now,
                valid=True,
                reason="test EFFR",
            ),
            "margin_preview": MarginPreview(
                status=MarginPreviewStatus.PENDING,
                order_id=7002,
                contract_month=settings.ibkr_zq_contract_month,
                quantity=settings.ibkr_zq_child_order_quantity,
                limit_price=Decimal("96.2875"),
                requested_at=now,
            ),
            "metadata": {
                "polymarket_fee_parameters": {
                    "INC25": {"rate": "0", "exponent": "1"},
                    "INC50PLUS": {"rate": "0", "exponent": "1"},
                },
                "polymarket_fee_parameters_at": now.isoformat(),
            },
        }
    )
    batch = BatchView(
        batch_id="batch-pending-preview",
        state=BatchState.ZQ_SUBMITTED,
        zq_order_id=915,
        original_quantity=10,
        remaining_quantity=Decimal("10"),
        limit_price=Decimal("96.2875"),
        zq_order_status="Submitted",
    )
    profitable_without_return = SimpleNamespace(
        gate_checks=(),
        minimum_net_profit=Decimal("324.09"),
        return_on_capital_bps=None,
    )
    below_profit_floor = SimpleNamespace(
        gate_checks=(),
        minimum_net_profit=Decimal("200"),
        return_on_capital_bps=None,
    )

    with (
        patch(
            "zq_arb.execution.coordinator.build_three_state_opportunity",
            return_value=profitable_without_return,
        ),
        patch.object(coordinator, "_request_zq_cancel", new=AsyncMock()) as request_cancel,
    ):
        await coordinator._cancel_unprofitable_residual(snapshot, batch)
        request_cancel.assert_not_awaited()

    with (
        patch(
            "zq_arb.execution.coordinator.build_three_state_opportunity",
            return_value=below_profit_floor,
        ),
        patch.object(coordinator, "_request_zq_cancel", new=AsyncMock()) as request_cancel,
    ):
        await coordinator._cancel_unprofitable_residual(snapshot, batch)
        request_cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_cancel_transmission_does_not_persist_cancel_pending(
    settings: Settings,
) -> None:
    repository = MagicMock()
    repository.set_batch_cancel_pending = AsyncMock(return_value=True)
    ibkr = MagicMock()
    ibkr.cancel_order.side_effect = RuntimeError("cancel transport failed")
    coordinator = ExecutionCoordinator(
        settings=settings,
        repository=repository,
        state=StateStore(settings),
        ibkr=ibkr,
        polymarket=MagicMock(),
    )
    batch = BatchView(
        batch_id="batch-cancel-error",
        state=BatchState.ZQ_SUBMITTED,
        zq_order_id=915,
        original_quantity=10,
        remaining_quantity=Decimal("10"),
        limit_price=Decimal("96.2875"),
    )

    with pytest.raises(RuntimeError, match="cancel transport failed"):
        await coordinator._request_zq_cancel(
            batch=batch,
            reason="test cancellation",
            residual=None,
            required=Decimal("250"),
        )

    repository.set_batch_cancel_pending.assert_not_awaited()
