from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from zq_arb.domain.enums import RunMode
from zq_arb.execution.coordinator import ExecutionCoordinator
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import (
    AuditLogRecord,
    ExecutionEnvironmentRecord,
    ExecutionRecord,
    OpeningInventoryRecord,
)
from zq_arb.persistence.opening_inventory import (
    adopt_opening_inventory,
    receipt_signature,
)
from zq_arb.persistence.repository import Repository
from zq_arb.services.state import StateStore


@pytest.fixture
async def opening(tmp_path, settings):
    configured = settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'opening.db').as_posix()}",
            "run_mode": RunMode.LIMITED_LIVE,
            "ibkr_trading_mode": "live",
            "simulate_polymarket_fills": False,
            "ibkr_zq_child_order_quantity": 1,
            "max_zq_position": 25,
        }
    )
    db = Database(configured)
    await db.initialize()
    tokens = {leg.code: leg.yes_token_id for leg in configured.market_legs}
    trade = {
        "id": "manual-pm-1",
        "asset_id": tokens["INC25"],
        "side": "BUY",
        "size": "100",
        "price": ".33",
        "status": "CONFIRMED",
        "trader_side": "TAKER",
    }
    snapshot = {
        "identity": db.identity,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": "verified venue inventory test",
        "account_verified": True,
        "reads_complete": True,
        "open_orders": [],
        "positions": [
            {
                "venue": "IBKR",
                "instrument": configured.ibkr_zq_contract_month,
                "quantity": "23",
                "average_price": "96.32",
            },
            {
                "venue": "POLYMARKET",
                "instrument": tokens["INC25"],
                "quantity": "11181.5957",
                "average_price": ".3322",
            },
            {
                "venue": "POLYMARKET",
                "instrument": tokens["INC50PLUS"],
                "quantity": "22362.9",
                "average_price": ".0035",
            },
        ],
        "source_trades": {"IBKR": [], "POLYMARKET": [trade]},
        "receipts": {"IBKR": [], "POLYMARKET": [receipt_signature("POLYMARKET", trade)]},
    }
    try:
        yield Repository(db), snapshot, trade
    finally:
        await db.close()


async def test_opening_inventory_counts_positions_and_preserves_execution_history(opening):
    repo, snapshot, _ = opening
    assert await adopt_opening_inventory(repo, snapshot, reason="verified manual holdings")
    assert not await adopt_opening_inventory(repo, snapshot, reason="idempotent retry")
    month = repo.database.settings.ibkr_zq_contract_month
    assert await repo.strategy_zq_quantity(month) == 23
    positions = await repo.strategy_portfolio_positions(repo.database.settings)
    assert sorted(position.strategy_quantity for position in positions) == [
        Decimal("23"),
        Decimal("11181.5957"),
        Decimal("22362.9"),
    ]
    async with repo.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditLogRecord)) == 1
    await repo.create_zq_batch_intent(
        batch_id="new",
        order_id=123,
        contract_month=month,
        quantity=1,
        limit_price=Decimal("96.3"),
        strategy_version="test",
        snapshot_id=1,
    )
    await repo.record_ibkr_execution_and_obligations(
        order_id=123,
        execution_id="new-1",
        quantity=Decimal(1),
        price=Decimal("96.3"),
        executed_at=datetime.now(UTC),
        token_shares={},
        details={},
    )
    assert await repo.strategy_zq_quantity(month) == 24
    await repo.database.validate_execution_environment()


@pytest.mark.parametrize(
    "mutation",
    [
        "identity",
        "shortfall",
        "open_order",
        "incomplete",
        "unsettled",
        "receipt",
        "duplicate",
        "nonfinite",
    ],
)
async def test_inventory_import_rejects_unverified_or_incomplete_evidence(opening, mutation):
    repo, original, _ = opening
    snapshot = deepcopy(original)
    if mutation == "identity":
        snapshot["identity"]["ibkr_account"] = "another-account"
    elif mutation == "shortfall":
        snapshot["positions"][1]["quantity"] = "11179.9828"
    elif mutation == "open_order":
        snapshot["open_orders"] = [{"id": "working"}]
    elif mutation == "incomplete":
        snapshot["reads_complete"] = False
    elif mutation == "unsettled":
        snapshot["source_trades"]["POLYMARKET"][0]["status"] = "MATCHED"
    elif mutation == "receipt":
        snapshot["receipts"]["POLYMARKET"].append("unknown-history")
    elif mutation == "duplicate":
        snapshot["positions"].append(snapshot["positions"][0])
    elif mutation == "nonfinite":
        snapshot["positions"][0]["quantity"] = "NaN"
    with pytest.raises(RuntimeError):
        await adopt_opening_inventory(repo, snapshot, reason="must reject")
    assert await repo.strategy_zq_quantity(repo.database.settings.ibkr_zq_contract_month) == 0


async def test_account_correction_requires_explicit_import_and_empty_ledger(opening):
    repo, snapshot, _ = opening
    wrong = {**repo.database.identity, "ibkr_account": "previous-paper-account"}
    async with repo.database.session() as session:
        session.add(ExecutionEnvironmentRecord(id=1, schema_version=1, identity=wrong))
    with pytest.raises(RuntimeError, match="cannot rebind"):
        await adopt_opening_inventory(repo, snapshot, reason="no correction flag")
    assert await adopt_opening_inventory(
        repo, snapshot, reason="operator corrected account", allow_empty_account_correction=True
    )
    db = Database(repo.database.settings)
    try:
        await db.validate_execution_environment()  # A fresh process must validate the import.
        async with db.session() as session:
            record = await session.get(OpeningInventoryRecord, 1)
            record.snapshot = {**record.snapshot, "source": "tampered"}
        db._environment_validated = False
        with pytest.raises(RuntimeError, match="evidence hash"):
            await db.validate_execution_environment()
    finally:
        await db.close()


async def test_historical_replay_does_not_hide_new_or_changed_trades(opening):
    repo, snapshot, trade = opening
    await repo.save_venue_receipt("POLYMARKET", trade)
    await adopt_opening_inventory(repo, snapshot, reason="adopt known history")
    state = StateStore(repo.database.settings)
    coordinator = ExecutionCoordinator(
        settings=repo.database.settings,
        repository=repo,
        state=state,
        ibkr=MagicMock(),
        polymarket=MagicMock(),
    )
    await coordinator._handle_polymarket_trade(trade, datetime.now(UTC))
    assert await repo.pending_venue_receipts("POLYMARKET") == ()
    for changed in (
        {**trade, "id": "new-manual-trade"},
        {**trade, "size": "101"},
        {**trade, "status": "FAILED"},
    ):
        await coordinator._handle_polymarket_trade(changed, datetime.now(UTC))
    assert len(await repo.pending_venue_receipts("POLYMARKET")) == 3
    assert (await repo.hedge_safety_differences())["unprocessed_polymarket_events"]


async def test_unknown_history_rolls_back_account_correction(opening):
    repo, snapshot, trade = opening
    await repo.save_venue_receipt("POLYMARKET", {**trade, "id": "not-in-verified-history"})
    with pytest.raises(RuntimeError, match="Unmatched pending"):
        await adopt_opening_inventory(repo, snapshot, reason="must reject")
    async with repo.database.session() as session:
        assert await session.get(OpeningInventoryRecord, 1) is None
        assert await session.get(ExecutionEnvironmentRecord, 1) is None


def test_receipt_signatures_normalize_decimal_and_aliases():
    trade = {
        "id": "t",
        "asset_id": "token",
        "side": "BUY",
        "size": "100.0",
        "price": "0.3300",
        "status": "CONFIRMED",
    }
    websocket = {**trade, "token_id": "token", "size": "100", "price": ".33", "status": "MINED"}
    del websocket["asset_id"]
    assert receipt_signature("POLYMARKET", trade) == receipt_signature("POLYMARKET", websocket)
