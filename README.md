# ZQ–Polymarket Arbitrage Engine

Production-oriented Python and TypeScript implementation of the approved September 2026 ZQ/Polymarket design. The repository is deliberately fail-closed: source checkout, dependency installation, process restart, missing credentials, missing reserves, unqualified subscriptions, or unsynchronized books cannot enable live orders.

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

The coverage gate applies to deterministic pricing, risk, state, security, persistence, and public-data code. The official IBKR callback bridge, long-running orchestration loops, and process entrypoint are excluded from line coverage and are verified by the read-only integration smoke test. That smoke test contains no routing order call; when `IBKR_ACCOUNT_ID` is configured it requests one non-routing `whatIf=True` margin preview.

Before starting the operator terminal, fill the still-required local values for `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `SESSION_SIGNING_KEY`, `CONTROL_CONFIRMATION_SECRET`, and `IBKR_ACCOUNT_ID`. Authenticated CLOB values remain unnecessary for the current `READ_ONLY` stage.

## Market Data and Signal Authority

1. Polymarket REST seeds and periodically reconciles each configured book. The public market WebSocket is the authoritative intraday path and applies complete books, price-level changes, level deletions, and tick-size changes directly to immutable backend state.

2. A WebSocket disconnect or book-integrity failure marks every affected book unsynchronized. REST data may remain visible for diagnosis, but a new ZQ order remains prohibited until a valid WebSocket update restores synchronization.

3. The primary rate signal uses the current `ZQU6` bid, ask, and midpoint against the August pre-meeting EFFR anchor. October and November are optional inputs for the secondary FedWatch diagnostic and are not required for the direct signal.

4. The adjacent-state ZQ probabilities and normalized Polymarket expected move explain the cross-venue difference. Only conservative terminal scenario P&L and the full risk-gate result can qualify an opportunity.

5. IBKR price-change and market-event ages are informational and do not expire a quiet quote. Bid/ask size callbacks count as stream activity even when IBKR correctly omits an unchanged price.

6. A TWS socket connection does not by itself qualify a quote. Qualification also requires market-data type 1, the current subscription generation, an active subscription, a healthy relevant `usfuture*` market-data farm, and a complete uncrossed bid/ask rebuilt after startup or reconnect. The dashboard displays provisional calculations but marks them `NOT EXECUTION-QUALIFIED` whenever those gates are incomplete.

7. The latest IBKR `BUY 10 ZQU6` what-if margin preview is requested no faster than once per minute and refreshed after connectivity recovery or a changed candidate. The dashboard shows `REFRESHING` while awaiting a matching response. It never uses an expired raw `AVAILABLE` response or substitutes zero margin for committed-capital and return calculations.

8. Strategy capital, equity, high-water mark, fees, daily P&L, and drawdown are maintained in a persistent strategy ledger separate from IBKR account-level metrics. A `$2,000` drawdown cancels only unfilled ZQ, preserves fills, pauses and disarms the engine, and requires audited manual review.

9. Version-one cross-venue discrepancies are resolved manually. The authenticated reconciliation confirmation is recorded with its source snapshot and is invalidated by reconnects, order changes, executions, cancellations, and unresolved hedge obligations.

## Safety Invariants

1. Process startup always begins in `READ_ONLY`, regardless of the previous database state.

2. Exactly 10 ZQ contracts are permitted per child batch, only one batch may be active, and aggregate exposure is capped at 100.

3. ZQ orders are `LMT/DAY` and are never automatically repriced.

4. Version 1 is structurally long-only: the engine can submit only `BUY` ZQ entries and may hedge confirmed fills only by buying the approved Polymarket Yes legs. Bid-side and No-token data are diagnostic and cannot create an order.

5. Polymarket orders cannot precede a confirmed, unique IBKR `execId`.

6. Filled ZQ is never automatically flattened.

7. `LIMITED_LIVE` and `LIVE_ARMED` reject zero reserves, absent L2 credentials, failed wallet classification, non-Hong-Kong geoblock results, delayed IBKR data, disconnected or unsynchronized Polymarket books, unresolved obligations, and missing operator approval.

8. Credentials and account identifiers are redacted before logging and are never serialized into browser state or persistence payloads.

9. Every failed qualification is transported as typed actual-versus-required evidence. The dashboard renders all blocking gates and never truncates the failure list.

10. `STRATEGY_ALLOCATED_CAPITAL_USD` is the approved strategy-equity baseline and must match the persisted risk ledger. Changing it requires an audited risk reset rather than silently rewriting historical drawdown.
