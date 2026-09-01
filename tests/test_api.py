from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from zq_arb.api.app import create_app, dashboard_snapshot
from zq_arb.config import Settings
from zq_arb.domain.enums import ConnectionStatus, DataQuality
from zq_arb.domain.models import (
    BookLevel,
    EligibilityStatus,
    EngineSnapshot,
    MarketMappingStatus,
    OrderBook,
    Quote,
    VenueHealth,
)


def test_dashboard_snapshot_bounds_depth() -> None:
    asset_id = "outcome-asset"
    levels = tuple(BookLevel(price=index / 100, size=10) for index in range(1, 30))
    snapshot = EngineSnapshot(
        software_version="test",
        config_version="test",
        strategy_version="test",
        books={asset_id: OrderBook(token_id=asset_id, bids=levels, asks=levels)},
    )
    view = dashboard_snapshot(snapshot)
    assert len(view.books[asset_id].bids) == 10
    assert len(snapshot.books[asset_id].bids) == 29


@pytest.mark.asyncio
async def test_login_state_and_read_only_control(tmp_path: Path, settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "runtime_data_dir": tmp_path,
            "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
            "log_dir": tmp_path / "logs",
            "audit_export_dir": tmp_path / "audit",
            "dashboard_username": "operator",
            "dashboard_password": SecretStr("password"),
            "session_signing_key": SecretStr("session-signing-key-for-tests"),
            "control_confirmation_secret": SecretStr("control-secret"),
        }
    )
    app = create_app(configured)
    runtime = app.state.runtime
    runtime.repository.audit = AsyncMock(return_value="event")
    runtime.confirm_reconciliation = AsyncMock(return_value=None)
    runtime.reset_strategy_risk = AsyncMock(return_value=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        not_ready = await client.get("/readyz")
        assert not_ready.status_code == 503
        assert "target ZQ quote is unavailable" in not_ready.json()["reasons"]
        unauthorized = await client.get("/api/v1/state")
        assert unauthorized.status_code == 401
        authenticated = await client.post(
            "/api/v1/session/login",
            json={"username": "operator", "password": "password"},
        )
        assert authenticated.status_code == 200
        state = await client.get("/api/v1/state")
        assert state.status_code == 200
        readiness = await client.get("/api/v1/readiness")
        assert readiness.status_code == 200
        csrf = client.cookies.get("zq_arb_csrf")
        assert csrf is not None
        arm = await client.post(
            "/api/v1/control",
            headers={"X-CSRF-Token": csrf},
            json={
                "action": "ARM",
                "reason": "read-only test",
                "confirmation_secret": "control-secret",
            },
        )
        assert arm.status_code == 409
        bad_csrf = await client.post(
            "/api/v1/control",
            headers={"X-CSRF-Token": "wrong"},
            json={
                "action": "PAUSE_NEW_TRADES",
                "reason": "negative security test",
                "confirmation_secret": "control-secret",
            },
        )
        assert bad_csrf.status_code == 403
        reconciled = await client.post(
            "/api/v1/control",
            headers={"X-CSRF-Token": csrf},
            json={
                "action": "CONFIRM_RECONCILED",
                "reason": "venue positions checked manually",
                "confirmation_secret": "control-secret",
            },
        )
        assert reconciled.status_code == 200
        runtime.confirm_reconciliation.assert_awaited_once()
        risk_reset = await client.post(
            "/api/v1/control",
            headers={"X-CSRF-Token": csrf},
            json={
                "action": "RESET_STRATEGY_RISK",
                "reason": "approved post-review high-water reset",
                "confirmation_secret": "control-secret",
            },
        )
        assert risk_reset.status_code == 200
        runtime.reset_strategy_risk.assert_awaited_once()
    await runtime.polymarket.close()
    await runtime.database.close()


@pytest.mark.asyncio
async def test_ready_requires_and_accepts_complete_fresh_data(
    tmp_path: Path,
    settings: Settings,
) -> None:
    configured = settings.model_copy(
        update={
            "runtime_data_dir": tmp_path,
            "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'ready.db').as_posix()}",
            "log_dir": tmp_path / "logs",
            "audit_export_dir": tmp_path / "audit",
        }
    )
    app = create_app(configured)
    runtime = app.state.runtime
    current = await runtime.state.get()
    required_months = {configured.ibkr_zq_contract_month}
    quotes = {
        month: Quote(
            instrument=month,
            bid=Decimal("96.30"),
            ask=Decimal("96.31"),
            quality=DataQuality.LIVE,
            analytics_qualified=True,
        )
        for month in required_months
    }
    books = {
        token_id: OrderBook(token_id=token_id, stream_synchronized=True, source="WEBSOCKET")
        for leg in configured.market_legs
        for token_id in (leg.yes_token_id, leg.no_token_id)
    }
    await runtime.state.replace(
        current.model_copy(
            update={
                "ibkr": VenueHealth(status=ConnectionStatus.CONNECTED),
                "polymarket": VenueHealth(status=ConnectionStatus.CONNECTED),
                "quotes": quotes,
                "books": books,
                "mapping": MarketMappingStatus(verified=True),
                "eligibility": EligibilityStatus(checked=True, blocked=False),
            }
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
    await runtime.polymarket.close()
    await runtime.database.close()
