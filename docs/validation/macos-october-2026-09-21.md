# Mac development and October event migration — 2026-09-21

Implemented locally after owner approval, from clean committed baseline `23b0cc3`.
No source changes were committed or pushed by this task. No VPS deployment, routing order,
wallet transaction or opening-inventory import was performed. Validation services were
stopped cleanly after the checks; start them with the commands in the
[Mac guide](../local-development-macos.md).

## Workstation and configuration

- Retained the installed Python 3.14.7, Node 26.9.0 and npm 11.19.1; uv is 0.12.17.
  No older runtime was installed. The later toolchain-alignment follow-up pins both
  Ubuntu/macOS CI and the production build to Python 3.14.7 and Node 26.9.0, with
  uv 0.12.17. Mac startup remains native; the VPS has not been redeployed.
- Installed locked dependencies in `~/.local/share/zq-arb/venvs/mac-py314` and rebuilt
  `web/node_modules`. Updated Node types to 26. Added `python-socks[asyncio]` 2.8.2
  after the actual Mac system proxy exposed the missing WebSocket dependency.
- Installed official IBKR Mac/Unix API 10.50.01 and verified the archive checksum
  against the Dockerfile. Existing TWS on `127.0.0.1:7496` is a live account connection;
  client 61026 isolates this development process from the old client.
- Added `scripts/dev.sh` and `ZQ_ENV_FILE` support. Preserved existing private credentials
  in `~/.config/zq-arb/development.env` (0600, parent 0700), outside OneDrive. The ignored
  checkout `.env` was initially a non-secret local template. The owner subsequently updated
  it for margin retries; it remains ignored by Git and separate from the protected file.
- Created `~/Library/Application Support/ZQArb/october-2026-dev.sqlite3`, with SQLite
  integrity verified. It contains no orders, fills, hedge obligations or adopted inventory.
  Read-only startup records configuration, market observations and audit data.
- Configured READ_ONLY, all live/submission switches false, wallet deployment disabled,
  and a disarmed startup. Retained the existing five-contract/60-contract limits in the
  protected file. The subsequent successful smoke test used the owner's repo `.env`, which
  selects ten contracts per child.

## October mapping and model

The [Federal Reserve October calendar](https://www.federalreserve.gov/newsevents/2026-october.htm)
lists October 27–28 and the statement at October 28, 14:00 EDT (18:00 UTC).
The [2026 FOMC schedule](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
identifies November as a non-meeting month and December as a meeting month.

| Setting | New value |
|---|---|
| ZQ target | October 2026, `202610` / `ZQV6` |
| Subscriptions | October, November, December |
| Statement / new-entry cutoff | October 28, 18:00 / 17:00 UTC |
| Assumed rate-effective date | October 29, 2026 |
| Settlement day counts | 28 pre-decision, 3 post-decision, total 31 |
| FedWatch anchor | November 2026; December is not used as the post-October rate |
| Polymarket event | `606422`, Fed Decision in October? |
| Event slug | `fed-decision-in-october-20260617190323537` |
| EFFR | Dynamic New York Fed API; fetched 3.88%, effective September 17, during validation |

The October 29 effective date remains an explicit assumption pending the actual FOMC
implementation notice. The full public market snapshot, five markets, Yes/No token IDs,
condition IDs, sizes, ticks and rules are in [october-2026-market.json](october-2026-market.json).
Source: [Polymarket October event](https://polymarket.com/event/fed-decision-in-october-20260617190323537).
The fetched description hash was
`aef6b5c5a5f96b0671c51926d2cbccdc9bb10ff82330b7b8b95d41d50ea8bf8e`.

An explicit calendar replaces September constants in implied moves/probabilities, scenario
settlement, fee quantities, proposed hedges, actual partial-fill obligations, residual orders
and opening-inventory coverage. For one contract, individually rounded INC25/INC50PLUS
hedges are 100.82/201.63 shares. A five-contract fill needs 504.08/1008.15 shares: round the
whole obligation once, rather than multiplying already rounded per-contract values.
The existing 0/+25/+50 bp terminal-scenario scope is retained.

Backend state and dashboard labels identify the configured contract/event. The secondary
FedWatch calculation uses an explicit non-meeting anchor and optional intervening calendars.
Database identity now also includes event, contract month and effective date. Earlier ledgers
remain historical evidence and cannot be silently re-labelled for October execution.
Historical September tests use an explicit fixture to preserve their original economics.

## Validation results

| Check | Result |
|---|---|
| Ruff (`src tests scripts`) | Passed |
| mypy (`src`) | Passed, 39 source files |
| Backend suite on Python 3.14.7 | **422 passed** |
| Configured branch coverage | **87.57%**, above 85% threshold |
| ESLint | Passed |
| Dashboard Vitest on Node 26.9.0 | **20 passed**, seven files |
| TypeScript / Vite build | Passed |
| npm dependency audit at install | Zero reported vulnerabilities |
| Official IBKR API import | Passed |
| SQLite integrity / private-file permissions | Passed |
| Source/configuration scan | No newly introduced private credential values; no Windows runtime paths |
| Shell syntax, local documentation links, whitespace diff | Passed |

The coverage scope still excludes the IBKR adapter, runtime orchestrator and process
entrypoint. Their tests are not equivalent to live trading acceptance. Pytest/pytest-asyncio
emit Python 3.14 event-loop-policy deprecation warnings (2,431 in this run); all tests pass.
The new GitHub Actions matrix was updated but has not run remotely in this task. The Linux
container was not rebuilt or deployed.

New regressions cover calendar validation, leap days/weekends, October implied probabilities,
rounding of whole hedge obligations, September and October partial-fill/late-fill handling,
the runtime's October signal and sizing, November anchor behavior, mismatched ledger identity,
external env-file loading and October dashboard labels.

## Actual read-only connectivity

- TWS connected; October live bid/ask arrived, with real-time market-data type 1.
- October Polymarket mapping and description hash matched. REST returned ten books and
  the public WebSocket synchronized all ten through the Mac's existing proxy configuration.
- Eligibility returned HK with `blocked=false` during this check.
- Backend `/healthz` and `/readyz` returned 200. Unauthenticated state returned 401;
  authentication using the retained local login succeeded and state returned 200.
- State showed READ_ONLY, disarmed, October target, exact 3/31 weight, valid direct and
  FedWatch calculations, validated EFFR and no active batch. See the sanitized
  [API evidence](macos-read-only-api-2026-09-21.json).
- Vite served the dashboard on 5173 and proxied health/authentication endpoints correctly.
  Component tests and a production build validate the dashboard; no browser visual review
  or control-button activation was performed.
- Both local processes were stopped after validation. SQLite still has zero execution rows.

**Broker prerequisite resolved:** earlier what-if requests returned error 201 requiring
Client Portal verification. The retry at **2026-09-21 08:12:22 UTC succeeded**, with
`AVAILABLE`, initial-margin requirement `5923.12`, no error 201 and smoke exit code zero.
The margin amount's currency was not captured. The request was BUY 10 ZQV6 at 96.105,
LMT/DAY, `whatIf=True`, `transmit=True`; contract ID 523805377, expiry 20261030. It used the
owner-updated repo `.env` and temporary client 61027 to avoid the already connected process.
Both configuration files were left unchanged. The [sanitized request/result](macos-margin-preview-2026-09-21.json)
records this follow-up; the earlier [API snapshot](macos-read-only-api-2026-09-21.json)
retains its original failed-preview status as historical evidence.

No executable order was routed or verification flow bypassed. `/readyz` is a data-readiness
check and does not establish margin or live-execution readiness. Full live execution and
recovery were not exercised.

## Cleanup

Removed 15,232 regenerable/obsolete files totaling 571,141,623 bytes (about 571 MB), including
copied dependencies, virtual environments, caches, obsolete build/download output and Windows
launch/install utilities. No Windows backup was created, as requested. Existing database,
order-journal, passkey and historical records were moved outside OneDrive, not duplicated.
Spreadsheets, articles, source and Git history were retained. The exact removal/move manifest
is [macos-cleanup-2026-09-21.json](macos-cleanup-2026-09-21.json).

Follow-up cleanup removed regenerated caches and incidental formatting-only diff changes,
updated quantity labels to reflect the configured batch, and removed an unused probability
constant. After the owner closed TWS and approved runtime cleanup, the remaining local
backend was stopped gracefully and the recreated repo `.venv`, `web/node_modules` and
`web/dist` were removed. This follow-up removed 17,207 generated files totaling
273,871,479 bytes. Both env files, databases, historical records and Git history were
preserved, along with the external Mac Python environment and official IBKR API. Restore
repo dependencies with `scripts/dev.sh sync`; `scripts/dev.sh check` rebuilds the dashboard.
Ruff, mypy (39 files) and 108 focused backend tests passed after the cleanup edits. Local
Markdown links, shell examples and the whitespace diff also passed validation.

## Shared toolchain follow-up

The owner clarified that parity means the same Python and Node versions while keeping
native Mac development. The production Dockerfile and both Ubuntu/macOS CI jobs now
select **Python 3.14.7 and Node 26.9.0**, with uv 0.12.17 and shared lockfiles. Python
package/tool configuration targets 3.14; the existing locked package versions were
retained. Two async-generator annotations were simplified for the Python 3.14 lint target.
CI also checks that the production image tags match the native version files.

The temporary Mac container setup was removed, including the newly installed Docker,
Colima and Lima packages. No VM was created. Production Compose and both private env
files are unchanged. Native `scripts/dev.sh` commands and the external Python environment
remain the local development path.

Follow-up native validation: **422 backend tests passed, 87.55% coverage**, source/script
Ruff and source mypy passed, and **20 dashboard tests**, ESLint and the production
frontend build passed. The same existing pytest-asyncio deprecation warnings remain.
Ignore-rule checks confirmed local env files, database journals, macOS metadata and
scratch files are excluded, while configuration templates, the historical test fixture
and both dependency lockfiles remain eligible for version control.

No GitHub Actions run, Linux container execution, image publication or VPS rollout was
performed in this follow-up. Those checks belong to the next release; the deployed VPS
still uses its previous image.
