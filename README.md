# ZQ–Polymarket Arbitrage Engine

Production-oriented Python and TypeScript implementation of the approved September 2026 ZQ/Polymarket design. The repository is deliberately fail-closed: source checkout, dependency installation, process restart, missing credentials, unqualified subscriptions, or unsynchronized books cannot enable live orders.

## Authorized Stage

Implementation is authorized through `READ_ONLY` and `PAPER`. The local `.env` disables both venue order paths. Live trading is not authorized.

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

Runtime, tests, schema validation, and operational scripts use the same local `.env` as their single configuration source.

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

3. The primary rate signal uses the current `ZQU6` bid, ask, and midpoint against a validated pre-meeting EFFR observation. By default the backend refreshes EFFR from the official New York Fed Markets API; `EFFR_SOURCE=MANUAL` with `PRE_MEETING_EFFR_PERCENT` is the explicit fallback. October and November are optional inputs for the secondary FedWatch diagnostic and are not required for the direct signal.

4. The adjacent-state ZQ probabilities and normalized Polymarket expected move explain the cross-venue difference. Only conservative terminal scenario P&L and the full risk-gate result can qualify an opportunity.

5. IBKR price-change and market-event ages are informational and do not expire a quiet quote. Bid/ask size callbacks count as stream activity even when IBKR correctly omits an unchanged price.

6. A TWS socket connection does not by itself qualify a quote. Qualification also requires market-data type 1, the current subscription generation, an active subscription, a healthy relevant `usfuture*` market-data farm, and a complete uncrossed bid/ask rebuilt after startup or reconnect. The dashboard displays provisional calculations but marks them `NOT EXECUTION-QUALIFIED` whenever those gates are incomplete.

7. The latest IBKR `BUY 10 ZQU6` what-if margin preview is requested no faster than once per minute and refreshed after connectivity recovery or a changed candidate. The dashboard shows `REFRESHING` while awaiting a matching response. It never uses an expired raw `AVAILABLE` response or substitutes zero margin for committed-capital and return calculations.

8. Strategy IBKR open orders, `execId` history, and the authenticated aggregate target-contract position are automatically reconciled with authenticated Polymarket open orders and trades after startup and reconnect. Routine strategy fills and status callbacks update the durable ledger instead of invalidating reconciliation; an order, position, or obligation difference fails the gate.

9. `IBKR_COMMISSION_ESTIMATE=3.64` is the conservative per-contract round-trip ZQ cost floor derived from the published non-member low-volume schedule. For a 10-contract batch the model deducts at least `$36.40`; twice a higher current IBKR entry what-if commission overrides that floor.

10. The cross-venue portfolio aggregates every durable strategy execution, compares the result with venue-reported quantities, and marks long ZQ and Polymarket Yes holdings to their executable best bids every 500 milliseconds. Its combined unrealized P&L is gross of commissions and fees and remains informational.

## Safety Invariants

1. Process startup always begins in `READ_ONLY`, regardless of the previous database state.

2. `ARM` authorizes the engine to wait for a qualifying entry; it does not require the current snapshot to be profitable and it does not itself place an order. The dashboard distinguishes `ARMED · WAITING`, `ARMED · READY`, and `ARMED · WORKING`. Structural routing blockers such as read-only or shadow mode, emergency halt, disabled venue submission, or a disabled live-trading switch reject the action with their exact cause.

3. Exactly 10 ZQ contracts are permitted per child batch, only one batch may be active, and aggregate exposure is capped at 100. The aggregate ZQ position comes from authenticated IBKR portfolio callbacks and therefore includes both hedged and unhedged contracts. A new batch is prohibited whenever any durable hedge obligation remains below its required confirmed share quantity, even if that obligation is not part of the batch currently displayed.

4. ZQ orders are `BUY LMT/DAY` at the qualified best bid and are never automatically repriced. If the still-resting quantity no longer passes the scaled profit, return, fee, and exact-ask hedge-size gates, only that unfilled remainder is cancelled. Once IBKR confirms the cancellation and every fill is hedged and reconciled, the still-armed engine may submit a fresh 10-contract order when a later snapshot passes every gate.

5. Version 1 is structurally long-only: the engine can submit only `BUY` ZQ entries and may hedge confirmed fills only by buying the approved Polymarket Yes legs. Bid-side and No-token data are diagnostic and cannot create an order.

6. Polymarket orders cannot precede a confirmed, unique IBKR `execId`. Each fill creates durable INC25 and INC50PLUS obligations, and each hedge is a non-post-only GTC BUY limit at the latest lowest ask.

7. Filled ZQ is never automatically flattened.

8. `LIMITED_LIVE` and `LIVE_ARMED` require a deployment country of Hong Kong (`HK`) or the Netherlands (`NL`) and an explicit unblocked venue eligibility result. They reject failed CLOB authentication, failed wallet classification, delayed IBKR data, disconnected or unsynchronized Polymarket books, unresolved obligations, and missing operator approval.

9. Credentials and account identifiers are redacted before logging and are never serialized into browser state or persistence payloads.

10. Every failed qualification is transported as typed actual-versus-required evidence. The dashboard renders all blocking gates and never truncates the failure list.

11. The executable payoff matrix remains limited by owner decision to the defined `0`, `+25`, and `+50` basis-point states. No tail-scenario gate is implemented.

## Settlement Lifecycle

1. The engine does not automatically sell or flatten filled ZQ or Polymarket positions. Polymarket positions are held through the FOMC market's resolution and settlement process.

2. ZQ positions are held through CME month-end final settlement. Position and execution reconciliation remains active while both legs are outstanding.

3. Delayed, disputed, failed, or otherwise abnormal venue settlement requires manual operator handling; automatic exit and redemption logic is outside the current approved scope.

## Operational TODO

1. Implement out-of-band critical-alert delivery through an approved webhook, email, or paging channel. `ALERT_WEBHOOK_URL` and `ALERT_EMAIL_TO` are reserved configuration fields only; the current implementation exposes alerts on the dashboard but does not send them externally.

## VPS deployment

The production container, immutable GHCR workflow, loopback-only Compose service, fail-closed
environment template, and SQLite backup timer are documented in `deploy/README.md`. The initial VPS
release remains `READ_ONLY`; deployment does not authorize paper or live order submission.

## Event pipeline performance and recovery

The September 2026 overflow remedy, safety boundaries, authenticated event diagnostics, offline load-test commands, and validation evidence are documented in [event-pipeline-remedy.md](docs/event-pipeline-remedy.md).

## Polymarket authentication configuration

1. Configure `POLYMARKET_PRIVATE_KEY` and the existing account's `POLYMARKET_FUNDER_ADDRESS`. On first authenticated use, the official SDK creates or derives CLOB credentials using `POLYMARKET_CREDENTIAL_NONCE` (default zero). The client caches them in memory; the engine does not write derived credentials into the environment file. A missing funder is rejected rather than selecting a different wallet implicitly.

2. Existing CLOB credentials remain an optional override: supply all of `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, and `POLYMARKET_API_PASSPHRASE`, or omit all three. A partial set is rejected. When the complete override is supplied, the nonce is not passed to the SDK because that combination is unsupported.

3. For SDK gasless wallet setup, set `POLYMARKET_RELAYER_ENABLED=true` and supply `POLYMARKET_RELAYER_API_KEY` plus `POLYMARKET_RELAYER_API_KEY_ADDRESS`. These use the SDK's `RelayerApiKey` authentication. The SDK may deploy an undeployed supported deposit wallet during initialization when this is enabled. The engine still does not automatically split, merge, redeem, or approve token allowances; trading preflight continues to require sufficient allowance and funds.

4. Builder credentials are unnecessary for this configuration. Remove obsolete `POLYMARKET_BUILDER_API_KEY`, `POLYMARKET_BUILDER_API_SECRET`, `POLYMARKET_BUILDER_API_PASSPHRASE`, `POLYMARKET_BUILDER_CODE`, `POLYMARKET_RELAYER_HOST`, and `POLYMARKET_RELAYER_TX_TYPE` from environment files when adopting this version. They were previously declared but unused. The official SDK selects its relayer endpoint and wallet transaction type. Unknown environment variables remain rejected to catch configuration mistakes.

5. The integration follows the [official Python SDK](https://docs.polymarket.com/getting-started/python) and [wallet authentication documentation](https://docs.polymarket.com/trading/wallets-auth). Automated authentication tests replace the SDK network boundary; they do not create live credentials or submit wallet transactions.
# Local Polymarket credential diagnostic

For an operator-run five-share real-order test, see
[Manual Polymarket order test](docs/manual-polymarket-order-test.md). The script
provides preview, placement and cancellation commands; it does not start the engine.

From the repository directory, run in PowerShell:

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe scripts/check_polymarket_auth.py
```

This uses the local `.env` to check that the private key matches the configured signer,
that the funder is a supported wallet for that signer, and that a contract wallet is
already deployed. It then validates CLOB authentication through active API keys,
the first page of open orders, and collateral balance/allowances. It does not start
the trading engine or connect to IBKR. An absent CLOB override uses the installed
SDK's create-or-derive credential flow; its only POST is `/auth/api-key`.
Explicit credentials are tested as supplied, without silently replacing them.

The command prints and saves HTTP statuses and JSON response bodies to
`runtime/polymarket-auth-check.json`, with credentials/signatures redacted. Exit 0
means both checks passed; exit 1 indicates failure or incomplete verification.
Network failures are local errors, not venue responses. Non-JSON response bodies
are withheld because they cannot be safely redacted. The report can contain wallet
addresses, balances and open orders, so it stays in the ignored runtime directory.
Use `--env-file` and `--output` to select different local paths.
Add `--include-history` to test authenticated CLOB trade history (first page) and
public Polymarket wallet activity (latest 20 records, including transaction hashes).
The latter is Polymarket's indexed activity feed, not a complete blockchain audit,
and does not validate relayer credentials. These checks also affect the exit status.

No order submission, fund transfer, wallet deployment or approval endpoint is
permitted. Passing confirms wallet/authentication checks, not order acceptance,
execution or relayer credential validity. The API's funder identifies the wallet
backing orders; use the application's deposit instructions when adding funds.
