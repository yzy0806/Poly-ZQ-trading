from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from zq_arb.config import Settings
from zq_arb.domain.identity import identity_fingerprint
from zq_arb.persistence.models import (
    Base,
    BatchRecord,
    ExecutionEnvironmentRecord,
    ExecutionRecord,
    HedgeObligationRecord,
    OpeningInventoryRecord,
    OrderRecord,
    VenueEventRecord,
)


def execution_identity(settings: Settings) -> dict[str, str]:
    return {
        "ibkr_mode": settings.ibkr_trading_mode.lower(),
        "ibkr_client_id": str(settings.ibkr_client_id),
        "ibkr_account": identity_fingerprint(settings.ibkr_account_id.get_secret_value()),
        "polymarket_mode": "SIMULATED" if settings.simulate_polymarket_fills else "REAL",
        "polymarket_wallet": identity_fingerprint(
            settings.polymarket_funder_address.get_secret_value()
        ),
        "polymarket_api_owner": identity_fingerprint(
            settings.polymarket_api_key.get_secret_value()
        ),
        "polymarket_host": settings.polymarket_clob_host.rstrip("/"),
        "polymarket_chain": str(settings.polymarket_chain_id),
    }


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # This service has one writer process. Keep read/modify/write transactions
        # serialized, including the two hedge legs and urgent cancellation updates.
        self._transaction_lock = asyncio.Lock()
        self._active_session: ContextVar[tuple[asyncio.Task[Any] | None, AsyncSession] | None] = (
            ContextVar("execution_database_session", default=None)
        )
        self.identity = execution_identity(settings)
        self._environment_validated = False
        self.revision = 0
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
                result = await connection.execute(text("PRAGMA integrity_check"))
                if result.scalar() != "ok":
                    raise RuntimeError("SQLite integrity check failed")

    async def validate_execution_environment(self) -> None:
        """Bind a fresh ledger; never silently bless legacy or cross-environment data."""
        if self._environment_validated:
            return
        async with self.session() as session:
            record = await session.get(ExecutionEnvironmentRecord, 1)
            if record is None:
                for model in (BatchRecord, OrderRecord, ExecutionRecord):
                    if await session.scalar(select(func.count()).select_from(model)):
                        raise RuntimeError(
                            "Legacy execution ledger has no environment identity. Use READ_ONLY "
                            "to inspect it; reconstruct and reconcile venue history before trading."
                        )
                session.add(
                    ExecutionEnvironmentRecord(
                        id=1,
                        schema_version=1,
                        identity=self.identity,
                    )
                )
            elif record.schema_version != 1 or record.identity != self.identity:
                raise RuntimeError(
                    "Execution database belongs to a different environment/account/wallet; "
                    "use a separate database or inspect the existing one in READ_ONLY."
                )
            if self.settings.run_mode.is_live and self.settings.ibkr_trading_mode.lower() != "live":
                raise RuntimeError("Live execution requires the IBKR live environment")
            if self.settings.run_mode.is_live and self.settings.simulate_polymarket_fills:
                raise RuntimeError("Live execution refuses simulated Polymarket state")
            opening = await session.get(OpeningInventoryRecord, 1)
            if opening is not None:
                from zq_arb.persistence.opening_inventory import validate_opening_inventory

                validate_opening_inventory(opening, self.settings, self.identity)
            orders = (await session.scalars(select(OrderRecord))).all()
            executions = (await session.scalars(select(ExecutionRecord))).all()
            rows: list[OrderRecord | ExecutionRecord] = [*orders, *executions]
            for item in rows:
                if item.details.get("execution_environment") != self.identity:
                    raise RuntimeError(
                        "Execution ledger contains unidentified or incompatible rows"
                    )
                if not self.settings.simulate_polymarket_fills and (
                    item.details.get("simulated") or item.venue_order_id.startswith("SIM-")
                ):
                    raise RuntimeError("Real execution refuses simulated orders or fills")
                if (
                    isinstance(item, OrderRecord)
                    and item.venue == "POLYMARKET"
                    and (
                        not self.settings.simulate_polymarket_fills
                        and not item.details.get("signed_payload")
                    )
                ):
                    raise RuntimeError("Real execution refuses an unsigned persisted hedge intent")
        self._environment_validated = True

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        active = self._active_session.get()
        if active is not None and active[0] is asyncio.current_task():
            yield active[1]
            return
        async with self._transaction_lock, self.sessions() as session:

            def track_execution_write(sync_session: Any, *_: Any) -> None:
                models = (
                    BatchRecord,
                    OrderRecord,
                    ExecutionRecord,
                    HedgeObligationRecord,
                    VenueEventRecord,
                    ExecutionEnvironmentRecord,
                    OpeningInventoryRecord,
                )
                if any(
                    isinstance(item, models)
                    and (
                        item in sync_session.new
                        or item in sync_session.deleted
                        or sync_session.is_modified(item)
                    )
                    for item in sync_session.new | sync_session.dirty | sync_session.deleted
                ):
                    session.info["execution_mutated"] = True

            event.listen(session.sync_session, "before_flush", track_execution_write)
            token = self._active_session.set((asyncio.current_task(), session))
            try:
                yield session
                await session.commit()
                if session.info.get("execution_mutated"):
                    self.revision += 1
            except BaseException:
                await session.rollback()
                raise
            finally:
                self._active_session.reset(token)

    async def close(self) -> None:
        await self.engine.dispose()
