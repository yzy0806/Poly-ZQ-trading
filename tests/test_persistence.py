from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from zq_arb.config import Settings
from zq_arb.domain.enums import BatchState
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import (
    AuditLogRecord,
    BatchRecord,
    ConfigVersion,
    HedgeObligationRecord,
)
from zq_arb.persistence.repository import Repository


@pytest.mark.asyncio
async def test_config_and_audit_are_durable(tmp_path: Path, settings: Settings) -> None:
    database_path = tmp_path / "engine.sqlite3"
    isolated = settings.model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}"}
    )
    database = Database(isolated)
    await database.initialize()
    repository = Repository(database)
    await repository.ensure_config_version(isolated)
    await repository.audit(actor="TEST", action="VERIFY", reason="unit test")
    async with database.session() as session:
        config_count = await session.scalar(select(func.count()).select_from(ConfigVersion))
        audit_count = await session.scalar(select(func.count()).select_from(AuditLogRecord))
    await database.close()
    assert config_count == 1
    assert audit_count == 1


@pytest.mark.asyncio
async def test_unresolved_hedge_count_covers_the_entire_ledger(
    tmp_path: Path,
    settings: Settings,
) -> None:
    database_path = tmp_path / "hedges.sqlite3"
    isolated = settings.model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}"}
    )
    database = Database(isolated)
    await database.initialize()
    repository = Repository(database)
    async with database.session() as session:
        session.add(
            HedgeObligationRecord(
                obligation_id="old-batch:exec:token",
                batch_id="old-batch",
                exec_id="exec",
                token_id="asset-id",  # noqa: S106 - non-secret synthetic market identifier
                due_shares=10,
                confirmed_shares=9,
                state="PARTIAL",
            )
        )

    assert await repository.unresolved_hedge_obligation_count() == 1
    await database.close()


@pytest.mark.asyncio
async def test_filled_status_and_recovery_complete_a_fully_hedged_batch(
    tmp_path: Path,
    settings: Settings,
) -> None:
    database_path = tmp_path / "terminal-batch.sqlite3"
    isolated = settings.model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}"}
    )
    database = Database(isolated)
    await database.initialize()
    repository = Repository(database)
    await repository.create_zq_batch_intent(
        batch_id="batch-filled-after-hedges",
        order_id=42,
        contract_month=isolated.ibkr_zq_contract_month,
        quantity=10,
        limit_price=Decimal("96.30"),
        strategy_version=isolated.strategy_version,
        snapshot_id=1,
    )
    await repository.mark_zq_submitted("batch-filled-after-hedges")
    await repository.update_zq_order_status(
        order_id=42,
        status="Submitted",
        filled=Decimal("3"),
        remaining=Decimal("7"),
    )
    obligations = await repository.record_ibkr_execution_and_obligations(
        order_id=42,
        execution_id="exec-3",
        quantity=Decimal("3"),
        price=Decimal("96.30"),
        executed_at=datetime.now(UTC),
        token_shares={"asset-25": Decimal("1"), "asset-50": Decimal("2")},
        details={},
    )
    for index, obligation in enumerate(obligations, start=1):
        key = f"{obligation.obligation_id}:attempt-1"
        order_id = f"poly-{index}"
        assert await repository.create_hedge_order_intent(
            obligation_id=obligation.obligation_id,
            idempotency_key=key,
            shares=obligation.due_shares,
            limit_price=Decimal("0.25"),
            attempt=1,
            signed_payload=None,
        )
        await repository.accept_hedge_order(
            idempotency_key=key,
            order_id=order_id,
            state="matched",
            simulated=True,
        )
        assert await repository.record_polymarket_execution(
            execution_id=f"{order_id}:fill",
            order_id=order_id,
            quantity=obligation.due_shares,
            price=Decimal("0.25"),
            executed_at=datetime.now(UTC),
            details={"simulated": True},
        )

    assert (await repository.active_batch_view()).state is BatchState.HEDGED
    await repository.update_zq_order_status(
        order_id=42,
        status="Filled",
        filled=Decimal("3"),
        remaining=Decimal("0"),
    )
    assert (await repository.active_batch_view()).state is BatchState.IDLE

    async with database.session() as session:
        batch = await session.scalar(
            select(BatchRecord).where(BatchRecord.batch_id == "batch-filled-after-hedges")
        )
        assert batch is not None
        batch.state = BatchState.HEDGED.value
    await repository.normalize_active_batch_state()
    assert (await repository.active_batch_view()).state is BatchState.IDLE
    await database.close()
