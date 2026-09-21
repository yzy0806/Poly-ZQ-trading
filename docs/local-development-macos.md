# Local development on macOS

Reviewed against the working tree on 2026-09-21. The workstation is Apple Silicon
(`Darwin arm64`); production remains Linux on an amd64 VPS. All local commands below
use zsh/sh syntax. Linux `systemctl` and production Docker commands belong on the VPS.

## 1. Locate the repository and select the toolchain

The current workspace contains archives and reports beside the actual Git repository.
Start in `code/`, not its parent:

```sh
cd "/Users/leoying/Library/CloudStorage/OneDrive-Personal/ZQ_Polymarket/code"
git status --short
```

For credentialed development, use an unsynchronized checkout, for example
`$HOME/Developer/Poly-ZQ-trading`, and run all subsequent commands from that checkout's
root. Git's `.gitignore` does not prevent OneDrive from uploading `.env`, databases,
logs, or order journals. Keep secrets and runtime state outside cloud-synchronized
folders; the existing OneDrive checkout can still be used for source review and offline tests.

| Component | Repository requirement / development target |
|---|---|
| Python | `>=3.12,<3.15`; use **3.12** to match CI and the production image |
| Python dependencies | `uv.lock`; install the `dev` extra with `--locked` |
| Node.js | **24**, matching CI and the dashboard image build |
| JavaScript dependencies | `web/package-lock.json`; install with `npm ci --ignore-scripts` |
| IBKR Python API | Official Mac/Unix source; the Dockerfile pins **10.50.01** |
| Database | SQLite through `aiosqlite`; no database server required |

At review time, `/usr/bin/python3` was 3.9.6, the repository `.venv` used 3.14.7,
and the shell's Node was 26.9.0. These are not the CI toolchain. `uv` was installed
at `$HOME/.local/bin/uv`; Homebrew was under `/opt/homebrew`. Select versions explicitly.

If these tools are missing and Homebrew is installed:

```sh
brew install uv node@24
```

In each development terminal, make the installed tools available and select Node 24:

```sh
export PATH="$HOME/.local/bin:$PATH"
export PATH="$(brew --prefix node@24)/bin:$PATH"
uv --version
node --version
npm --version
```

Homebrew's [keg-only guidance](https://docs.brew.sh/How-to-Build-Software-Outside-Homebrew-with-Homebrew-keg-only-Dependencies)
explains the version-specific PATH. Do not assume installing `node@24` changes the
default `node` executable.

## 2. Rebuild dependencies for macOS

Windows `.venv/Scripts/python.exe` and native `node_modules` binaries cannot be reused
on macOS. Keep the lockfiles and recreate dependencies for the host architecture.
Use a dedicated Python environment outside OneDrive, with one environment per checkout:

```sh
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/zq-arb/venvs/code-py312"
uv python install 3.12
uv sync --locked --python 3.12 --extra dev
uv run --locked python --version
(cd web && npm ci --ignore-scripts)
```

Repeat the `UV_PROJECT_ENVIRONMENT` export in each backend/test terminal, including the
terminal used to launch the backend. No virtual-environment activation or `PYTHONPATH`
override is needed: uv installs this project as an editable package. Without the export,
uv uses the checkout's `.venv`; on macOS its interpreter is `.venv/bin/python`.
See uv's [project environment configuration](https://docs.astral.sh/uv/concepts/projects/config/)
and [locked synchronization](https://docs.astral.sh/uv/concepts/projects/sync/).

`npm ci` replaces `web/node_modules` using the lockfile. Avoid sharing generated
dependencies between Windows and Mac; an unsynchronized checkout is preferable for
day-to-day work. Native-module errors mentioning `win32`, `darwin`, esbuild, or Rolldown
usually require reinstalling dependencies with the correct Node and architecture.

## 3. Run offline checks first

From the repository root, in a shell without exported application/trading variables:

```sh
uv run --locked ruff check src tests
uv run --locked mypy src
uv run --locked pytest
(cd web && npm run lint && npm test -- --run && npm run build)
git diff --check
```

The shared test fixture reads `deploy/zq-arb.env.example`, substitutes deterministic
values, and uses mocked venues and temporary databases. A credentialed `.env`, running
TWS/Gateway, and downloaded IBKR API source are unnecessary for these offline checks.
Operating-system variables override values loaded from an env file, including in tests.

For the separate configured coverage gate, run `uv run --locked pytest --cov`.
The threshold is 85%; the IBKR callback adapter, engine orchestration, and entrypoint
are excluded from line coverage. CI currently runs pytest without `--cov` and mypy on
`src` only. Historical Windows validation counts remain historical; see the
[September 21 maintenance validation](validation/ibkr-maintenance-2026-09-21.md)
for the existing macOS/Python 3.12 evidence and its release scope.

## 4. Prepare local configuration before starting the engine

The engine loads `.env` relative to its working directory, with process environment
variables taking precedence. Operational scripts with `--env-file` can select another
file; the `zq-arb` entrypoint and read-only smoke script use the root `.env`.
Do not copy the CI step that overwrites `.env` into a configured development checkout.

For a **new unsynchronized checkout** without `.env`, generate local dashboard secrets:

```sh
uv run --locked python deploy/bootstrap_env.py deploy/zq-arb.env.example .env
```

The bootstrap refuses to overwrite an existing file. It generates three dashboard secrets
and sets file mode `0600`, but otherwise copies the **Linux deployment** template. Edit
its local settings before launch. For an existing `.env`, preserve the original securely
and review it against the current example instead of replacing it wholesale.

The following are local development values, not a complete env file. Replace
`YOUR_MAC_USER` with the actual home-directory name and use literal absolute paths in
`.env`; do not rely on shell expansion of `~` or `$HOME` in path fields.

```dotenv
APP_ENV=development
RUN_MODE=READ_ONLY
LIVE_TRADING_ENABLED=false
IBKR_ORDER_SUBMISSION_ENABLED=false
POLYMARKET_ORDER_SUBMISSION_ENABLED=false
API_HOST=127.0.0.1
API_PORT=8765
DASHBOARD_ORIGIN=http://127.0.0.1:5173
CORS_ALLOWED_ORIGINS=http://127.0.0.1:5173
COOKIE_SECURE=false
RUNTIME_DATA_DIR="/Users/YOUR_MAC_USER/Library/Application Support/ZQArb"
DATABASE_URL="sqlite+aiosqlite:////Users/YOUR_MAC_USER/Library/Application Support/ZQArb/read-only.sqlite3"
LOG_DIR="/Users/YOUR_MAC_USER/Library/Application Support/ZQArb/logs"
AUDIT_EXPORT_DIR="/Users/YOUR_MAC_USER/Library/Application Support/ZQArb/audit"
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_TRADING_MODE=paper
IBKR_PYTHON_API_PATH="/Users/YOUR_MAC_USER/.local/share/zq-arb/twsapi/IBJts/source/pythonclient"
```

The four slashes in the SQLite URL specify an absolute Unix filesystem path. Create
the local data directories before launch:

```sh
mkdir -p "$HOME/Library/Application Support/ZQArb/logs" \
  "$HOME/Library/Application Support/ZQArb/audit"
```

Use the socket port actually configured in your paper TWS/Gateway; `7497` above is a
paper-TWS example. The deployment hostname `ib-gateway` is a Docker-network name, not
the local Mac hostname. Set `IBKR_ACCOUNT_ID` for that paper session and choose an API
client ID that is not already in use. Use distinct ledgers for paper/live, accounts,
wallets, and simulation modes; see [execution safety](execution-safety.md).

Keep all required settings from the current example, including the separate execution,
account-refresh, and callback deadlines and the eight September 21 maintenance settings.
Maintenance times use `America/Chicago`, not the Mac's Shanghai time zone; they must
agree with the Gateway schedule. Schema validation can run without connecting to venues:

```sh
uv run --locked python -c 'from zq_arb.config import get_settings; get_settings(); print("Configuration schema OK")'
```

Schema success does not check that an API directory exists, that a broker is reachable,
or that event dates are current. At this review, the existing local `.env` still had
Windows `C:/` and `D:/` paths and enabled live submission. These settings were not
changed by the documentation update. They must be reviewed before a local engine launch.

The checked-in market mapping and FOMC cutoff are for **September 16, 2026**. As of
September 21, the configured entry window has ended. Keep the historical fixture for
tests; do not extend a cutoff or change only a contract month to reuse it for a new event.

## 5. Install the official IBKR API for connectivity checks

Use the official [TWS API Mac/Unix distribution](https://www.interactivebrokers.com/docs/tws-api/doc/download-the-tws-api/introduction).
The repository loads its source directory directly; it is not installed by `uv sync`.
To match the version and checksum pinned in the Dockerfile, extract into a fresh
`twsapi` directory (preserve any existing installation separately):

```sh
mkdir -p "$HOME/.local/share/zq-arb/twsapi"
(
  cd "$HOME/.local/share/zq-arb/twsapi" || exit 1
  curl --fail --location \
    https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.01.zip \
    --output twsapi.zip &&
  printf '%s\n' 'aa065722ca732a41aab202c7bb72932e179b86e7ec51cefa063eb1983fe9f597  twsapi.zip' | shasum -a 256 -c - &&
  unzip -q -n twsapi.zip
)
```

`IBKR_PYTHON_API_PATH` must point to the parent of `ibapi/`, containing both
`ibapi/client.py` and `ibapi/order_cancel.py`. After configuring `.env`, verify imports
without opening a broker connection:

```sh
uv run --locked python -c 'from zq_arb.config import get_settings; from zq_arb.adapters.ibkr import _load_official_api; _load_official_api(get_settings().ibkr_python_api_path); print("Official IBKR API imports OK")'
```

TWS/Gateway must be installed, logged in to the intended paper account, and configured
to accept API socket connections for the optional network check. With `RUN_MODE=READ_ONLY`
and all three submission/live switches false, run:

```sh
uv run --locked python scripts/smoke_read_only.py
```

This contacts public Polymarket endpoints and IBKR; it submits no routing order. If an
account ID and qualified quote are available, it requests a non-routing `whatIf=True`
margin preview. A stale/resolved event may fail market checks even when connectivity works.
The smoke result does not certify the full execution/recovery path.

## 6. Launch and operate

From the repository root, run `uv run --locked zq-arb` in the configured backend terminal.
In a second terminal, select Node 24, run `cd web`, then `npm run dev`. Open
`http://127.0.0.1:5173` and use the local dashboard credentials. Vite listens on 5173
and proxies API/WebSocket traffic to `127.0.0.1` and `API_PORT`; use the exact configured
origin rather than switching between `localhost` and `127.0.0.1`.

Stop the frontend with Ctrl-C. Let the backend finish its configured shutdown drain
after Ctrl-C. A restart always starts disarmed. Docker is optional for native local
development; the production Compose file, Linux service installation, and deployment
scripts are documented in [the VPS guide](../deploy/README.md).

For remote Gateway/Passless desktops, use the [macOS SSH forwarding instructions](../deploy/IBKR_GATEWAY_PASSLESS.md#access-from-macos).
Those desktops and services run on the VPS, independently of your local development process.

## Documentation review verification

The September 21 documentation review checked local links and anchors, zsh syntax for
shell examples, CLI option names, the Mac dotenv example against the current configuration
schema, and the IBKR archive/checksum against the Dockerfile. All 24 configuration/security
tests passed on macOS/Python 3.12. No broker connection, credential diagnostic, order command,
engine startup, or VPS deployment was performed. Existing application and configuration
changes were preserved.
