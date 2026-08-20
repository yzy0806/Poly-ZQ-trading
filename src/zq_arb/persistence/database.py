from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from zq_arb.config import Settings
from zq_arb.persistence.models import Base


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"timeout": settings.sqlite_busy_timeout_ms / 1_000},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if settings.database_url.startswith("sqlite"):
            self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        busy_timeout = self.settings.sqlite_busy_timeout_ms
        checkpoint = self.settings.sqlite_wal_autocheckpoint_pages

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")
            cursor.execute(f"PRAGMA wal_autocheckpoint={checkpoint}")
            cursor.close()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if self.settings.database_url.startswith("sqlite"):
                await connection.execute(text("PRAGMA integrity_check"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()
