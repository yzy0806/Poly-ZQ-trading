# ZQ–Polymarket Arbitrage Engine

Production-oriented Python and TypeScript implementation of the approved September 2026 ZQ/Polymarket design. The repository is deliberately fail-closed: source checkout, dependency installation, process restart, missing credentials, missing reserves, or stale data cannot enable live orders.

## Authorized Stage

Implementation is authorized through `READ_ONLY` and `PAPER`. The checked-in configuration template disables both venue order paths. Live trading is not authorized.

## Repository Layout

```text
src/zq_arb/              Python modular-monolith backend
  adapters/              IBKR and Polymarket boundaries
  analytics/             Decimal-only probability and payoff calculations
  api/                   FastAPI authentication, controls, REST, WebSocket
  domain/                Typed venue-neutral models and events
  execution/             Batch state machine and idempotent fill obligations
  persistence/           Async SQLAlchemy schema and repositories
  risk/                  Fail-closed qualification and live-readiness gates
  services/              Runtime orchestration and authoritative state reducer
web/                     React + TypeScript operator dashboard
tests/                   Deterministic unit, integration, and safety tests
scripts/                 Controlled operational and connectivity checks
```

## Local Setup

1. Keep the real `.env` untracked and local. Every runtime parameter is loaded from that file or an operating-system secret injected under the same variable name.

2. Confirm that `IBKR_PYTHON_API_PATH` points to the official TWS Python API installation. The current machine uses TWS API 10.39.1 at `C:/TWS API/source/pythonclient`.

3. Create and synchronize the Python environment:

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv sync --extra dev
```

4. Install the dashboard dependencies:

```powershell
Set-Location web
npm ci
```

5. Run backend validation and tests:

```powershell
uv run ruff check src tests
uv run mypy src tests
uv run pytest --cov
```

6. Run the public-data and TWS paper connectivity check without placing orders:

```powershell
uv run python scripts/smoke_read_only.py
```

7. Start the backend and dashboard:

```powershell
uv run zq-arb
Set-Location web
npm run dev
```

The dashboard is served at the origin configured by `DASHBOARD_ORIGIN`. The backend binds only to `API_HOST` and `API_PORT` from `.env`.

The coverage gate applies to deterministic pricing, risk, state, security, persistence, and public-data code. The official IBKR callback bridge, long-running orchestration loops, and process entrypoint are excluded from line coverage and are verified by the read-only integration smoke test. That smoke test contains no order call.

Before starting the operator terminal, fill the still-required local values for `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `SESSION_SIGNING_KEY`, `CONTROL_CONFIRMATION_SECRET`, and `IBKR_ACCOUNT_ID`. Authenticated CLOB values remain unnecessary for the current `READ_ONLY` stage.

## Safety Invariants

1. Process startup always begins in `READ_ONLY`, regardless of the previous database state.

2. Exactly 10 ZQ contracts are permitted per child batch, only one batch may be active, and aggregate exposure is capped at 100.

3. ZQ orders are `LMT/DAY` and are never automatically repriced.

4. Polymarket orders cannot precede a confirmed, unique IBKR `execId`.

5. Filled ZQ is never automatically flattened.

6. `LIMITED_LIVE` and `LIVE_ARMED` reject zero reserves, absent L2 credentials, failed wallet classification, non-Hong-Kong geoblock results, delayed IBKR data, stale books, unresolved obligations, and missing operator approval.

7. Credentials and account identifiers are redacted before logging and are never serialized into browser state or persistence payloads.
