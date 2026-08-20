from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from zq_arb.config import Settings

SESSION_COOKIE = "zq_arb_session"
CSRF_COOKIE = "zq_arb_csrf"


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    username: str
    csrf: str


class LoginRateLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def permit(self, key: str) -> bool:
        now = time.monotonic()
        history = self._history[key]
        while history and now - history[0] > self.window_seconds:
            history.popleft()
        if len(history) >= self.attempts:
            return False
        history.append(now)
        return True

    def clear(self, key: str) -> None:
        self._history.pop(key, None)


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = settings.session_signing_key.get_secret_value()
        self._serializer = URLSafeTimedSerializer(key, salt="zq-arb-dashboard-v1")

    def authenticate_password(self, username: str, password: str) -> bool:
        return hmac.compare_digest(
            username, self.settings.dashboard_username
        ) and hmac.compare_digest(password, self.settings.dashboard_password.get_secret_value())

    def create(self, username: str) -> tuple[str, SessionIdentity]:
        identity = SessionIdentity(username=username, csrf=secrets.token_urlsafe(32))
        token = self._serializer.dumps({"sub": identity.username, "csrf": identity.csrf})
        return token, identity

    def decode(self, token: str | None) -> SessionIdentity | None:
        if not token:
            return None
        try:
            payload = self._serializer.loads(
                token,
                max_age=self.settings.dashboard_session_max_age_seconds,
            )
        except (BadSignature, SignatureExpired):
            return None
        username = str(payload.get("sub") or "")
        csrf = str(payload.get("csrf") or "")
        if not username or not csrf:
            return None
        return SessionIdentity(username=username, csrf=csrf)

    def require_request(self, request: Request) -> SessionIdentity:
        identity = self.decode(request.cookies.get(SESSION_COOKIE))
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
            )
        return identity

    def require_control_request(self, request: Request) -> SessionIdentity:
        identity = self.require_request(request)
        supplied = request.headers.get("X-CSRF-Token", "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not supplied or not cookie:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token required")
        if not hmac.compare_digest(supplied, identity.csrf) or not hmac.compare_digest(
            cookie, identity.csrf
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")
        return identity

    def require_websocket(self, websocket: WebSocket) -> SessionIdentity | None:
        return self.decode(websocket.cookies.get(SESSION_COOKIE))
