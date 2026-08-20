from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from zq_arb.config import Settings
from zq_arb.persistence.database import Database
from zq_arb.persistence.models import AuditLogRecord, ConfigVersion
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
