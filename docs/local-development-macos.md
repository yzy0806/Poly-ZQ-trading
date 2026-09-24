# Local development on macOS

Configured and validated on 2026-09-21 on Apple Silicon. Source lives in the OneDrive
workspace. The launcher uses a protected configuration and Python environment outside
OneDrive; SQLite, logs and existing sensitive history are also outside OneDrive. The owner
subsequently updated the repo `.env`, which is independent of the protected file. The
Mac runs the app natively; the Ubuntu VPS retains its container deployment. Both use
the same Python and Node versions and the same dependency lockfiles.

## Installed toolchain and working directory

```sh
cd "/Users/leoying/Library/CloudStorage/OneDrive-Personal/ZQ_Polymarket/code"
```

| Component | This Mac | Repository policy |
|---|---|---|
| Python | Homebrew 3.14.7 | `.python-version` pins 3.14.7; Python 3.14 supported |
| Node / npm | 26.9.0 / 11.19.1 | `.node-version` pins 26.9.0; Node `>=26.9.0,<27`, npm 11 |
| uv | 0.12.17 | `uv.lock`, `--locked`, development extra |
| IBKR API | Official Mac/Unix 10.50.01 | Same distribution/checksum as the container |
| Database | SQLite / aiosqlite | Fresh October ledger, no database server needed |

Use the installed newer tools; the Mac does not need Docker or Colima. CI checks both
Ubuntu and macOS with **Python 3.14.7 and Node 26.9.0**. The production Dockerfile pins
those versions and uv 0.12.17. Node builds the dashboard; the final production image
runs Python and serves the built static files. The VPS only adopts the upgraded
toolchain after a new image is built and deployed. `/usr/bin/python3` is Apple's 3.9.6; use the launcher
below so commands run under the project interpreter.

## Daily commands

`scripts/dev.sh` sets the project directory, Homebrew/uv PATH, external Python environment
and protected env-file path. It works from any directory when invoked by its full path.
It selects the pinned Python without automatically downloading another interpreter and
checks the Node version before frontend commands. When upgrading the installed Mac
toolchain, update `.python-version`, `.node-version` and the Dockerfile pins together;
CI reads the version files on both operating systems.

```sh
scripts/dev.sh sync
scripts/dev.sh check
scripts/dev.sh run python --version
scripts/dev.sh run python -c 'from zq_arb.config import get_settings; get_settings(); print("Configuration schema OK")'
```

`sync` installs the locked Python development dependencies and runs `npm ci --ignore-scripts`.
`check` runs Ruff, mypy, all backend tests with coverage, ESLint, dashboard tests and the
production dashboard build. Tests use isolated databases, mocked venues and the explicit
historical September fixture; October regressions verify the new model and runtime.
Do not export production/trading settings into the test shell: process variables override
settings loaded from files. The 85% coverage gate excludes the IBKR adapter, runtime
orchestrator and process entrypoint; dedicated tests still exercise those boundaries.

The environment is at `$HOME/.local/share/zq-arb/venvs/mac-py314`. Set
`UV_PROJECT_ENVIRONMENT` to a different path when using another checkout, because the
project is installed editable. A plain `uv run` outside the launcher creates a checkout
`.venv` unless this variable is exported. Windows virtual environments and native Node
binaries must not be copied into this setup. Only `web/node_modules` is needed by the app.

## Configuration and data locations

| Purpose | Path |
|---|---|
| Default launcher settings | `~/.config/zq-arb/development.env` (0600; parent 0700) |
| Owner-edited local settings / Vite configuration | repository `.env` (ignored by Git) |
| Python environment | `~/.local/share/zq-arb/venvs/mac-py314` |
| IBKR source | `~/.local/share/zq-arb/twsapi-10.50.01/IBJts/source/pythonclient` |
| New database | `~/Library/Application Support/ZQArb/october-2026-dev.sqlite3` |
| Logs / audit output | `~/Library/Application Support/ZQArb/logs` and `audit` |
| Existing historical records | `~/Library/Application Support/ZQArb/history` |

`ZQ_ENV_FILE` selects the backend file and is also honored by operational scripts unless
`--env-file` overrides it. An explicitly selected missing file is an error. A plain command
without `ZQ_ENV_FILE` falls back to `.env`. The owner updated that file during margin
verification; it is independent of the protected launcher configuration. To select it
explicitly, run `ZQ_ENV_FILE="$PWD/.env" scripts/dev.sh backend` from the repo root.
Process environment variables always take precedence. Use literal absolute paths inside dotenv
values; `$HOME` and `~` are not expanded there. Absolute SQLite URLs use four slashes:
`sqlite+aiosqlite:////Users/YOUR_USER/Library/Application Support/ZQArb/october-2026-dev.sqlite3`.

The protected file retains the original local risk limits (five ZQ contracts per child,
60-contract cap); the latest owner-edited repo `.env` selects ten contracts per child.
Confirm which file is selected before interpreting displayed limits. Local operation is
`READ_ONLY`, with live trading,
both venue submission switches, wallet deployment and simulated fills disabled. The new
ledger contains no imported opening inventory or historical executions. It is bound to the
configured account/client, wallet, event, contract month and rate-effective date.

This Mac connects to the existing **live TWS socket at 127.0.0.1:7496**, using a separate
API client ID, **61026**. `READ_ONLY` is the application's operating mode, not an assertion
that TWS is a paper account. For paper development, configure the actual paper port/account
and use a separate database. Preserve maintenance settings in `America/Chicago`; the Mac's
Shanghai time zone does not change exchange maintenance or FOMC UTC deadlines.

For a different new workstation, generate a protected file with
`deploy/bootstrap_env.py` from the current example, then review all paths, ports and venue
identity before use. The bootstrap example is for Linux and must not be launched unchanged
on macOS. Keep secrets and runtime files outside synchronized folders; `.gitignore` only
controls Git. An unsynchronized source checkout is also supported.

## October event and calculation

The [Federal Reserve calendar](https://www.federalreserve.gov/newsevents/2026-october.htm)
lists the October 27–28 meeting and the October 28 statement at 14:00 New York time.
The configured strategy values are:

```dotenv
IBKR_ZQ_CONTRACT_MONTH=202610
IBKR_ZQ_SUBSCRIPTION_MONTHS=202610,202611,202612
FOMC_STATEMENT_UTC=2026-10-28T18:00:00Z
FOMC_TRADING_CUTOFF_UTC=2026-10-28T17:00:00Z
FOMC_RATE_EFFECTIVE_DATE=2026-10-29
FEDWATCH_ANCHOR_CONTRACT_MONTH=202611
FEDWATCH_INTERVENING_RATE_EFFECTIVE_DATES=
POLYMARKET_EVENT_ID=606422
POLYMARKET_EVENT_SLUG=fed-decision-in-october-20260617190323537
POLYMARKET_EVENT_TITLE="Fed Decision in October?"
```

The October 29 effective date is an explicit modeling assumption pending the actual FOMC
implementation notice. October has 31 calendar days, including weekends: 28 before and
3 after the assumed change. The model, payoff matrix, actual-fill hedge obligations,
opening-inventory checks and dashboard use this calendar. New orders size each hedge from
the difference between CME-rounded no-change and hike settlements, multiplied by $4,167.
At 3.88% EFFR the exact per-contract ratios are 100.008 INC25 and 200.016 INC50PLUS;
a five-contract batch needs 500.04 and 1000.08 shares. Ratios are saved with each order.
Partial-fill obligations use differences of cumulative rounded quantities, so splitting
a batch or restarting does not add extra rounding or change its sizing basis. Pre-upgrade
orders and historical opening-inventory evidence retain their original calendar sizing.
New inventory evidence can specify `hedge_pre_meeting_effr_percent` for settlement-based validation.
The implemented terminal states remain 0, +25 and +50 bp, as in the approved strategy.

November is the non-meeting FedWatch anchor. December has its own scheduled FOMC meeting
and is not a direct proxy for the post-October rate. The direct October signal does not
require either diagnostic contract. EFFR refreshes from the New York Fed API; the old
September manual rate is not reused. On September 21 the fetched observation was 3.88%,
effective September 17; this is dated validation evidence, not a fixed strategy input.

The example and local files contain all five refreshed market IDs, condition IDs, Yes/No
token IDs, ticks, minimum sizes, event dates and rule hash. The public source snapshot is
[october-2026-market.json](validation/october-2026-market.json). Review the
[market's resolution rules](https://polymarket.com/event/fed-decision-in-october-20260617190323537)
when changing events. Market selection is explicit; no automatic monthly rollover is enabled.
A different event/calendar requires a separate ledger; see [execution safety](execution-safety.md).

## IBKR API and network checks

The official [Mac/Unix API](https://www.interactivebrokers.com/docs/tws-api/doc/download-the-tws-api/introduction)
was installed outside the workspace and its archive SHA-256 matched the Dockerfile:
`aa065722ca732a41aab202c7bb72932e179b86e7ec51cefa063eb1983fe9f597`.
`IBKR_PYTHON_API_PATH` points to the directory containing `ibapi/`. `uv sync` does not
install this vendor source. To reproduce the installation on another Mac:

```sh
mkdir -p "$HOME/.local/share/zq-arb/twsapi-10.50.01"
(
  cd "$HOME/.local/share/zq-arb/twsapi-10.50.01" || exit 1
  curl --fail --location https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.01.zip --output twsapi.zip &&
  printf '%s\n' 'aa065722ca732a41aab202c7bb72932e179b86e7ec51cefa063eb1983fe9f597  twsapi.zip' | shasum -a 256 -c - &&
  unzip -q -n twsapi.zip
)
```

With the intended TWS/Gateway session logged in and API sockets enabled:

```sh
scripts/dev.sh run python scripts/smoke_read_only.py
```

This checks public Polymarket mapping, REST and WebSocket books, live ZQ quotes and a
non-routing IBKR `whatIf=True` margin preview. It submits no routing order. The Mac's
existing SOCKS proxy is supported by the locked `python-socks[asyncio]` dependency;
no system proxy or routing settings were changed.

On September 21, TWS connected, October live bid/ask arrived, the October mapping/rule hash
matched and all ten books synchronized. Earlier margin requests returned IBKR error 201
requiring Client Portal verification. The retry at **08:12:22 UTC succeeded**: BUY 10 ZQV6
at 96.105 returned `AVAILABLE`, with reported initial-margin requirement 5923.12. The check
did not capture the currency of the margin amount. It exited zero and routed no executable
order. See the [sanitized request/result](validation/macos-margin-preview-2026-09-21.json).

That successful retry explicitly selected the owner's repo `.env` and temporary client ID
61027 because another process was already connected using the normal development client.
To repeat with the same selection, use an unused diagnostic client ID:

```sh
ZQ_ENV_FILE="$PWD/.env" IBKR_CLIENT_ID=61027 scripts/dev.sh run python scripts/smoke_read_only.py
```

The client override applies only to this diagnostic process; it does not alter `.env` or
the running engine. Passing this check does not certify live execution or reconciliation.

## Start and stop

Run in two separate terminals at the repository root:

```sh
# Terminal 1
scripts/dev.sh backend
```

```sh
# Terminal 2
scripts/dev.sh web
```

Open `http://127.0.0.1:5173` and use the existing dashboard login retained in the protected
file. Vite listens on 5173 and proxies API/WebSocket traffic to `127.0.0.1:8765`. Use the
configured origin consistently. If changing the backend port, update private `API_PORT`
and the root `.env` used by Vite. `DASHBOARD_ORIGIN`/`CORS_ALLOWED_ORIGINS` must
match the browser origin; `COOKIE_SECURE=false` is required for local HTTP.

Stop with Ctrl-C and let backend shutdown finish. Restart starts disarmed. For Linux
services, Docker deployment and remote desktop access, use the [VPS guide](../deploy/README.md)
and [Mac SSH forwarding instructions](../deploy/IBKR_GATEWAY_PASSLESS.md#access-from-macos).

## Cleanup and validation evidence

Approved cleanup removed copied dependencies, caches, Windows launch/install utilities,
obsolete downloaded sources and build output. No Windows backup was created. Existing
ledgers, order journals, passkey material and historical records were moved rather than
duplicated. Spreadsheets, articles, source and Git history were retained. See the
[cleanup manifest](validation/macos-cleanup-2026-09-21.json) and
[implementation validation](validation/macos-october-2026-09-21.md).

After the owner closed TWS, follow-up cleanup stopped the remaining local backend and
removed regenerated caches, the repo `.venv`, `web/node_modules` and `web/dist`. Both local
env files, SQLite and historical records were preserved, as were the external Python
environment and official IBKR API. Run `scripts/dev.sh sync` to restore the dashboard
dependencies before launching; `scripts/dev.sh check` also rebuilds `web/dist`.
