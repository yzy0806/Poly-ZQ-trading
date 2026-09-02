import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

import structlog
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from zq_arb.api.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    LoginRateLimiter,
    SessionIdentity,
    SessionManager,
)
from zq_arb.config import Settings, get_settings
from zq_arb.domain.enums import ControlAction, RunMode
from zq_arb.domain.models import EngineSnapshot
from zq_arb.observability import configure_logging, ensure_runtime_directories
from zq_arb.services.engine import EngineRuntime

LOGGER = structlog.get_logger(__name__)


def dashboard_snapshot(snapshot: EngineSnapshot) -> EngineSnapshot:
    """Bound the browser payload without changing the authoritative engine state."""

    books = {
        token_id: book.model_copy(update={"bids": book.bids[:10], "asks": book.asks[:10]})
        for token_id, book in snapshot.books.items()
    }
    return snapshot.model_copy(update={"books": books})


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ControlAction
    reason: str = Field(min_length=5, max_length=500)
    confirmation_secret: str = Field(default="", max_length=512)
    alert_id: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    ensure_runtime_directories(configured)
    configure_logging(configured)
    runtime = EngineRuntime(configured)
    sessions = SessionManager(configured)
    limiter = LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="ZQ Polymarket Arbitrage Engine",
        version=configured.software_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.settings = configured
    app.state.sessions = sessions
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; img-src 'self' data:; frame-ancestors 'none'"
        )
        return cast(Response, response)

    def require_auth(request: Request) -> SessionIdentity:
        return sessions.require_request(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "run_mode": configured.run_mode.value}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        snapshot = await runtime.state.get()
        reasons: list[str] = []
        if snapshot.ibkr.status.value != "CONNECTED":
            reasons.append("IBKR is not connected")
        if snapshot.polymarket.status.value != "CONNECTED":
            reasons.append("Polymarket is not connected")
        target_month = configured.ibkr_zq_contract_month
        if target_month not in snapshot.quotes:
            reasons.append("target ZQ quote is unavailable")
        else:
            target_quote = snapshot.quotes[target_month]
            if not target_quote.analytics_qualified:
                reasons.append(f"ZQU6 subscription not qualified: {target_quote.validation_reason}")
        if not snapshot.effr.valid:
            reasons.append(f"pre-meeting EFFR not qualified: {snapshot.effr.reason}")
        expected_tokens = {
            token_id
            for leg in configured.market_legs
            for token_id in (leg.yes_token_id, leg.no_token_id)
        }
        if not expected_tokens.issubset(snapshot.books):
            reasons.append("all ten Polymarket books are unavailable")
        elif any(not snapshot.books[token_id].stream_synchronized for token_id in expected_tokens):
            reasons.append("Polymarket books are not synchronized to the market WebSocket")
        if not snapshot.mapping.verified:
            reasons.append("Polymarket market mapping is unverified")
        if snapshot.eligibility.blocked is not False:
            reasons.append("geographic eligibility is blocked or indeterminate")
        ready = not reasons
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "ready": ready,
                "snapshot_id": snapshot.snapshot_id,
                "reasons": reasons,
            },
        )

    @app.post("/api/v1/session/login")
    async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        if not configured.dashboard_auth_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dashboard credentials are not configured",
            )
        client_key = request.client.host if request.client else "unknown"
        if not limiter.permit(client_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="try again later"
            )
        if not sessions.authenticate_password(payload.username, payload.password):
            await runtime.repository.audit(
                actor="ANONYMOUS",
                action="LOGIN_FAILED",
                reason="invalid dashboard credentials",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
            )
        limiter.clear(client_key)
        token, identity = sessions.create(payload.username)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=configured.cookie_secure,
            samesite="strict",
            max_age=configured.dashboard_session_max_age_seconds,
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            identity.csrf,
            httponly=False,
            secure=configured.cookie_secure,
            samesite="strict",
            max_age=configured.dashboard_session_max_age_seconds,
            path="/",
        )
        await runtime.repository.audit(
            actor=identity.username,
            action="LOGIN_SUCCEEDED",
            reason="dashboard session established",
        )
        return {"authenticated": True, "username": identity.username}

    @app.post("/api/v1/session/logout")
    async def logout(
        request: Request,
        response: Response,
        identity: Annotated[SessionIdentity, Depends(require_auth)],
    ) -> dict[str, bool]:
        del request
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        await runtime.repository.audit(
            actor=identity.username,
            action="LOGOUT",
            reason="operator ended dashboard session",
        )
        return {"authenticated": False}

    @app.get("/api/v1/state")
    async def state(_identity: Annotated[SessionIdentity, Depends(require_auth)]) -> Any:
        return dashboard_snapshot(await runtime.state.get())

    @app.get("/api/v1/readiness")
    async def readiness(
        _identity: Annotated[SessionIdentity, Depends(require_auth)],
    ) -> dict[str, Any]:
        snapshot = await runtime.state.get()
        return {
            "run_mode": configured.run_mode.value,
            "live_authorized": False,
            "live_readiness": runtime.risk.live_readiness(),
            "effr": snapshot.effr,
            "mapping": snapshot.mapping,
            "eligibility": snapshot.eligibility,
        }

    @app.post("/api/v1/control")
    async def control(payload: ControlRequest, request: Request) -> dict[str, Any]:
        identity = sessions.require_control_request(request)
        expected = configured.control_confirmation_secret.get_secret_value()
        if not expected or not hmac.compare_digest(payload.confirmation_secret, expected):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="confirmation failed")
        snapshot = await runtime.state.get()
        if payload.action is ControlAction.ARM:
            if configured.run_mode is RunMode.READ_ONLY:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="READ_ONLY cannot arm"
                )
            if not any(opportunity.tradeable for opportunity in snapshot.opportunities):
                runtime.request_margin_preview_refresh()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "no opportunity passes every hard gate; a current IBKR margin "
                        "preview has been requested"
                    ),
                )
            await runtime.state.set_operating_state(armed=True)
        elif payload.action is ControlAction.DISARM:
            await runtime.state.set_operating_state(armed=False)
        elif payload.action is ControlAction.PAUSE_NEW_TRADES:
            await runtime.state.set_operating_state(paused=True, armed=False)
        elif payload.action is ControlAction.EMERGENCY_HALT:
            await runtime.state.set_operating_state(kill_switch=True, paused=True, armed=False)
        elif payload.action is ControlAction.ACKNOWLEDGE_ALERT:
            if not payload.alert_id or not await runtime.state.acknowledge_alert(payload.alert_id):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
        elif payload.action is ControlAction.CANCEL_UNFILLED:
            if snapshot.active_batch.zq_order_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="no active ZQ order"
                )
            try:
                await runtime.execution.cancel_unfilled(payload.reason)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        elif payload.action is ControlAction.CONFIRM_RECONCILED:
            try:
                await runtime.confirm_reconciliation(identity.username, payload.reason)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
        elif payload.action is ControlAction.RESET_STRATEGY_RISK:
            try:
                await runtime.reset_strategy_risk(identity.username, payload.reason)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
        await runtime.audit_control(identity.username, payload.action.value, payload.reason)
        return {"accepted": True, "action": payload.action.value}

    @app.websocket("/api/v1/ws/state")
    async def state_websocket(websocket: WebSocket) -> None:
        identity = sessions.require_websocket(websocket)
        if identity is None:
            await websocket.close(code=4401, reason="authentication required")
            return
        await websocket.accept()
        try:
            async for snapshot in runtime.state.subscribe():
                await websocket.send_text(dashboard_snapshot(snapshot).model_dump_json())
        except WebSocketDisconnect:
            return

    web_dist = Path(__file__).resolve().parents[3] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="dashboard")
    return app
