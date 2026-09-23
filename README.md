# ZQ–Polymarket Arbitrage Engine

Python and TypeScript ZQ/Polymarket engine, configured for the October 28, 2026 FOMC decision and October ZQ (`ZQV6`). The repository is deliberately fail-closed: source checkout, dependency installation, process restart, missing credentials, unqualified subscriptions, or unsynchronized books cannot enable live orders.

## Current scope and documentation

The code supports read-only, paper, and gated live operation. New installations default to
`READ_ONLY` with submission disabled; an existing local `.env` may have different settings.
Every engine process starts disarmed, including when its configured mode is live.

- [macOS development](docs/local-development-macos.md): workstation setup, configuration, tests, and local startup.
- [Deployment and production readiness](deploy/CURRENT_DEPLOYMENT_AND_SECURITY.md): dated VPS observations, approved production limits, and release acceptance.
- [Execution safety](docs/execution-safety.md): ledger identity, reconciliation, halt, and recovery.
- [Original strategy design](ZQ_POLYMARKET_ARBITRAGE_ENGINE_DESIGN.md): historical design baseline and a guide to superseding implementation records.

The local and example configurations target October ZQ (`202610`) and Polymarket event
`606422`, **Fed Decision in October?**. The statement is scheduled for October 28 at
18:00 UTC; new entries stop at 17:00 UTC. The configured rate-effective date is October 29,
so the settlement model uses **28 pre-decision and 3 post-decision calendar days**.
The October rollout changes the calculation and hedge quantities as well as the market IDs.
See [migration and validation](docs/validation/macos-october-2026-09-21.md).

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

## Local setup on macOS

Run from the Git repository root (`code/` in this workspace). The Mac uses its installed
**Python 3.14.7 and Node 26.9.0** natively. The Ubuntu production build uses the same
versions and dependency lockfiles; Docker is only used for production. The
[macOS guide](docs/local-development-macos.md) documents the external Python environment,
protected configuration, fresh SQLite database, IBKR API, and verification results.

```sh
scripts/dev.sh sync    # reproduce locked dependencies
scripts/dev.sh check   # lint, types, backend coverage, dashboard tests and build
```

Tests use the explicit historical fixture `tests/fixtures/september-2026.env` plus
October regression cases, isolated databases and mocked venues. They need no broker login.
Use a shell without exported trading settings, which override env-file values.
The coverage threshold is 85%. CI validates both Ubuntu and macOS with Python 3.14.7
and Node 26.9.0, reading the exact versions from `.python-version` and `.node-version`.
The production Dockerfile pins those same versions. The matching toolchain was deployed on
September 22; the September 23 margin-preview update and current readiness are in the
[deployment record](deploy/CURRENT_DEPLOYMENT_AND_SECURITY.md#september-23-current-deployment).

Start these in **two separate terminals**, each at the repository root:

```sh
scripts/dev.sh backend
```

```sh
scripts/dev.sh web
```

Open `http://127.0.0.1:5173`. The launcher defaults to
`~/.config/zq-arb/development.env`. The ignored root `.env` supplies Vite configuration
and can be selected explicitly with `ZQ_ENV_FILE="$PWD/.env" scripts/dev.sh backend`.
These files are independent; updating one does not update the other. The backend and Vite
proxy both use port 8765. Both local configurations are `READ_ONLY`, start disarmed, and
have all order-submission switches disabled. The latest successful margin check used the
owner-updated repo `.env`; see the [validation report](docs/validation/macos-october-2026-09-21.md).

## Market Data and Signal Authority

1. Polymarket REST seeds and periodically reconciles each configured book. The public market WebSocket is the authoritative intraday path and applies complete books, price-level changes, level deletions, and tick-size changes directly to immutable backend state.

2. A WebSocket disconnect or book-integrity failure marks every affected book unsynchronized. REST data may remain visible for diagnosis, but a new ZQ order remains prohibited until a valid WebSocket update restores synchronization.

3. The primary rate signal uses the configured `ZQV6` bid, ask, and midpoint against a validated pre-meeting EFFR observation. By default the backend refreshes EFFR from the official New York Fed Markets API; `EFFR_SOURCE=MANUAL` with `PRE_MEETING_EFFR_PERCENT` is the explicit fallback. The secondary FedWatch diagnostic uses November as the non-meeting anchor; December is displayed as diagnostic data but is not used as an October post-meeting rate. Only October is required for the direct signal.

4. The adjacent-state ZQ probabilities and normalized Polymarket expected move explain the cross-venue difference. Only conservative terminal scenario P&L and the full risk-gate result can qualify an opportunity.

5. IBKR price-change and market-event ages are informational and do not expire a quiet quote. Bid/ask size callbacks count as stream activity even when IBKR correctly omits an unchanged price.

6. A TWS socket connection does not by itself qualify a quote. Qualification also requires market-data type 1, the current subscription generation, an active subscription, a healthy relevant `usfuture*` market-data farm, and a complete uncrossed bid/ask rebuilt after startup or reconnect. The dashboard displays provisional calculations but marks them `NOT EXECUTION-QUALIFIED` whenever those gates are incomplete.

7. The latest IBKR BUY what-if margin preview for the configured child quantity and ZQ contract is requested no faster than once per minute and refreshed after connectivity recovery or a changed candidate. The dashboard shows `REFRESHING` while awaiting a matching response. It never uses an expired raw `AVAILABLE` response or substitutes zero margin for committed-capital and return calculations.

8. IBKR open and completed orders, `execId` history, and the target-contract position are reconciled with authenticated Polymarket orders, trades, and event-token positions after startup, reconnect, and periodically. Execution events invalidate the previous clean result. Missing, stale, or incomplete evidence is `UNKNOWN`; unexplained inventory, orders, or excess fills block new entries and cancel working strategy ZQ orders.

9. `IBKR_COMMISSION_ESTIMATE` sets the per-contract round-trip ZQ cost floor (3.64 in the example environment). At that setting a five-contract batch deducts at least `$18.20`; twice a higher current IBKR entry what-if commission overrides that floor. This configured estimate is not a fresh verification of broker pricing.

10. The cross-venue portfolio aggregates every durable strategy execution, compares the result with venue-reported quantities, and marks long ZQ and Polymarket Yes holdings to their executable best bids on each analytics cycle. Its combined unrealized P&L is gross of commissions and fees and remains informational. The analytics cadence is described in Safety Invariant 4 below.

## Safety Invariants

1. Process startup uses the configured run mode and always starts disarmed and unreconciled. Real hedge recovery waits for fresh venue evidence. Paper and live execution databases must be separate; unidentified legacy execution data cannot be replayed automatically.

2. `ARM` authorizes the engine to wait for a qualifying entry; it does not require the current snapshot to be profitable and it does not itself place an order. The dashboard distinguishes `ARMED · WAITING`, `ARMED · READY`, and `ARMED · WORKING`. Structural routing blockers such as read-only or shadow mode, emergency halt, disabled venue submission, or a disabled live-trading switch reject the action with their exact cause.

3. `IBKR_ZQ_CHILD_ORDER_QUANTITY` sets the original child quantity, only one batch may be active, and `MAX_ZQ_POSITION` caps aggregate exposure (with a code ceiling of 100). The September 14 production record specifies five-contract children and a 60-contract cap; the bootstrap example uses 10 and 20. The aggregate ZQ position comes from authenticated IBKR portfolio callbacks and therefore includes both hedged and unhedged contracts. A new batch is prohibited whenever any durable hedge obligation remains below its required confirmed share quantity, even if that obligation is not part of the batch currently displayed.

4. When no active batch exists, the engine calculates a new opportunity using the qualified current ZQ best bid and current Polymarket hedge costs. Minimum net profit must be at least `MIN_NET_PROFIT_USD`, and every other entry gate must pass before submission. The ZQ order is `BUY LMT/DAY` at that best bid and is never automatically repriced.

   While a ZQ order rests, the engine recalculates profit for its unfilled quantity at the order's original fixed limit, using current Polymarket hedge costs and depth. The required dollar profit is `MIN_NET_PROFIT_USD × remaining quantity ÷ original quantity`. A result below that threshold, or unavailable profit, triggers a cancellation request for the unfilled remainder. Hedge-depth, fee, return and safety failures can also trigger cancellation. Once IBKR confirms cancellation and every fill is hedged and reconciled, the still-armed engine may submit a fresh batch when a later snapshot passes every gate.

   Monitoring runs in the periodic analytics loop. After each calculation and execution cycle, the loop sleeps for `ENGINE_STATE_PUBLISH_INTERVAL_MS` (500 ms in the example configuration). Processing time adds to that interval; checks are not invoked directly for every market-data update and have no guaranteed 500 ms deadline.

5. Version 1 is structurally long-only: the engine can submit only `BUY` ZQ entries and may hedge confirmed fills only by buying the approved Polymarket Yes legs. Polymarket bid-side and No-token data are used for valuation and diagnostics, not hedge entry pricing. The ZQ best bid is the price used to qualify and submit a new ZQ entry.

6. Polymarket orders cannot precede a confirmed, unique IBKR `execId`. Each fill creates durable INC25 and INC50PLUS obligations. INC25 requires full size at the lowest ask. INC50PLUS can combine the lowest ask and exactly one tick above it, within the configured price cap. Each hedge is a non-post-only GTC BUY limit at the highest price needed for its available fill plan. Scenario P&L uses the actual quantity at each consumed price level; taker-fee estimates also use those quantities and prices. The dashboard shows the entry VWAP, total cash cost, and fill breakdown.

7. Filled ZQ is never automatically flattened.

8. `LIMITED_LIVE` and `LIVE_ARMED` require a successful geographic eligibility request identifying Hong Kong (`HK`) or the Netherlands (`NL`). The venue's `blocked` flag is retained for diagnostics and does not gate submission. Failed requests and missing or unsupported countries still block submission. The modes reject failed CLOB authentication, failed wallet classification, delayed IBKR data, disconnected or unsynchronized Polymarket books, unresolved obligations, and missing operator approval.

9. Credentials and account identifiers are redacted before logging and are never serialized into browser state or persistence payloads.

10. Every failed qualification is transported as typed actual-versus-required evidence. The dashboard renders all blocking gates and never truncates the failure list.

11. The executable payoff matrix remains limited by owner decision to the defined `0`, `+25`, and `+50` basis-point states. No tail-scenario gate is implemented.

12. Signed Polymarket orders and their hashes are saved before submission. An uncertain submission keeps its reservation; recovery may retransmit the identical signed order but cannot sign a replacement until terminal order status and cumulative fills agree. Full fill quantities and costs are retained, with pending settlement and excess exposure reported separately.

13. Emergency halt cancels unfilled ZQ independently of hedge network requests and profitability. Late fills remain actionable within the existing hedge mandate. Shutdown drains callbacks and reconciles for a bounded period; unresolved shutdowns leave an audit record and require recovery on restart. See [execution-safety.md](docs/execution-safety.md) for deployment and recovery procedures.

## Settlement Lifecycle

1. The engine does not automatically sell or flatten filled ZQ or Polymarket positions. Polymarket positions are held through the FOMC market's resolution and settlement process.

2. ZQ positions are held through CME month-end final settlement. Position and execution reconciliation remains active while both legs are outstanding.

3. Delayed, disputed, failed, or otherwise abnormal venue settlement requires manual operator handling; automatic exit and redemption logic is outside the current approved scope.

## Operational TODO

1. Implement out-of-band critical-alert delivery through an approved webhook, email, or paging channel. `ALERT_WEBHOOK_URL` and `ALERT_EMAIL_TO` are reserved configuration fields only; the current implementation exposes alerts on the dashboard but does not send them externally.

## VPS deployment

The production container, immutable GHCR workflow, loopback-only Compose service, fail-closed
environment template, and SQLite backup timer are documented in [deploy/README.md](deploy/README.md). A new bootstrap starts `READ_ONLY`;
existing deployments retain their configured mode and start disarmed. See the
[dated deployment record](deploy/CURRENT_DEPLOYMENT_AND_SECURITY.md) for production status.

## Event pipeline performance and recovery

The September 2026 overflow remedy, safety boundaries, authenticated event diagnostics, offline load-test commands, and validation evidence are documented in [event-pipeline-remedy.md](docs/event-pipeline-remedy.md).

## Polymarket authentication configuration

1. Configure `POLYMARKET_PRIVATE_KEY` and the existing account's `POLYMARKET_FUNDER_ADDRESS`. On first authenticated use, the official SDK creates or derives CLOB credentials using `POLYMARKET_CREDENTIAL_NONCE` (default zero). The client caches them in memory; the engine does not write derived credentials into the environment file. A missing funder is rejected rather than selecting a different wallet implicitly.

2. Existing CLOB credentials remain an optional override: supply all of `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, and `POLYMARKET_API_PASSPHRASE`, or omit all three. A partial set is rejected. When the complete override is supplied, the nonce is not passed to the SDK because that combination is unsupported.

3. For SDK gasless wallet setup, set `POLYMARKET_RELAYER_ENABLED=true` and supply `POLYMARKET_RELAYER_API_KEY` plus `POLYMARKET_RELAYER_API_KEY_ADDRESS`. These use the SDK's `RelayerApiKey` authentication. The SDK may deploy an undeployed supported deposit wallet during initialization when this is enabled. The engine still does not automatically split, merge, redeem, or approve token allowances; trading preflight continues to require sufficient allowance and funds.

4. Builder credentials are unnecessary for this configuration. Remove obsolete `POLYMARKET_BUILDER_API_KEY`, `POLYMARKET_BUILDER_API_SECRET`, `POLYMARKET_BUILDER_API_PASSPHRASE`, `POLYMARKET_BUILDER_CODE`, `POLYMARKET_RELAYER_HOST`, and `POLYMARKET_RELAYER_TX_TYPE` from environment files when adopting this version. They were previously declared but unused. The official SDK selects its relayer endpoint and wallet transaction type. Unknown environment variables remain rejected to catch configuration mistakes.

5. The integration follows the [official Python SDK](https://docs.polymarket.com/getting-started/python) and [wallet authentication documentation](https://docs.polymarket.com/trading/wallets-auth). Automated authentication tests replace the SDK network boundary; they do not create live credentials or submit wallet transactions.

## Local Polymarket credential diagnostic

For an operator-run five-share real-order test, see
[Manual Polymarket order test](docs/manual-polymarket-order-test.md). The script
provides preview, placement and cancellation commands; it does not start the engine.

From the configured Mac development checkout:

```sh
scripts/dev.sh run python scripts/check_polymarket_auth.py \
  --output "$HOME/Library/Application Support/ZQArb/diagnostics/polymarket-auth-check.json"
```

This uses the protected `ZQ_ENV_FILE` selected by the launcher to check that the private key matches the configured signer,
that the funder is a supported wallet for that signer, and that a contract wallet is
already deployed. It then validates CLOB authentication through active API keys,
the first page of open orders, and collateral balance/allowances. It does not start
the trading engine or connect to IBKR. An absent CLOB override uses the installed
SDK's create-or-derive credential flow; its only POST is `/auth/api-key`.
Explicit credentials are tested as supplied, without silently replacing them.

The command prints and saves HTTP statuses and JSON response bodies to
the selected output file, with credentials/signatures redacted. Without `--output`, the default is
`runtime/polymarket-auth-check.json` relative to the working directory. Exit 0
means both checks passed; exit 1 indicates failure or incomplete verification.
Network failures are local errors, not venue responses. Non-JSON response bodies
are withheld because they cannot be safely redacted. The report can contain wallet
addresses, balances and open orders; keep it outside Git and cloud-synchronized folders.
Use `--env-file` and `--output` to select different local paths.
Add `--include-history` to test authenticated CLOB trade history (first page) and
public Polymarket wallet activity (latest 20 records, including transaction hashes).
The latter is Polymarket's indexed activity feed, not a complete blockchain audit,
and does not validate relayer credentials. These checks also affect the exit status.

No order submission, fund transfer, wallet deployment or approval endpoint is
permitted. Passing confirms wallet/authentication checks, not order acceptance,
execution or relayer credential validity. The API's funder identifies the wallet
backing orders; use the application's deposit instructions when adding funds.
