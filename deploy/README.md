# VPS deployment

This deployment publishes an immutable container to GitHub Container Registry and runs it on the
VPS next to the existing IB Gateway container. The application is bound to `127.0.0.1:8765`; only
Cloudflare Tunnel may reach it remotely. IBKR API and VNC ports remain loopback-only.

## Safety state

The bootstrap configuration is `READ_ONLY`. `LIVE_TRADING_ENABLED`,
`POLYMARKET_ORDER_SUBMISSION_ENABLED`, and `IBKR_ORDER_SUBMISSION_ENABLED` are all `false`.
Container or host restarts cannot arm the engine.

Use a distinct `DATABASE_URL` for every IBKR environment/account and Polymarket wallet or
simulation mode, such as `/var/lib/zq-arb/paper.sqlite3` and `/var/lib/zq-arb/live.sqlite3`.
Execution databases bind themselves to that identity and refuse incompatible replay.
Existing databases containing orders or fills without an identity remain inspectable in
`READ_ONLY`, but require reconstruction and venue reconciliation before execution. Do not label
old simulated or clipped fill history as live data. Start a fresh live ledger only after confirming
that no outstanding strategy orders or positions need to be recovered.

The example environment allows 20 seconds to cancel ZQ, process late fills, and reconcile before
shutdown, within the container's 30-second stop grace period. An unresolved stop is recorded
explicitly; a restart cannot treat it as clean. See [execution-safety.md](../docs/execution-safety.md).

Before upgrading an existing installation, ensure `/etc/zq-arb/zq-arb.env` explicitly includes
these required settings. Bootstrap does not update an existing environment file, and the
application no longer supplies fallback values for them:

```dotenv
EXECUTION_REQUEST_TIMEOUT_SECONDS=10
RECONCILIATION_MAX_AGE_SECONDS=60
SHUTDOWN_DRAIN_SECONDS=20
```

## Initial host preparation

Copy the repository deployment directory to `/opt/zq-arb`, then run:

```sh
sudo /opt/zq-arb/deploy/install_vps.sh
```

The script creates `/etc/zq-arb/zq-arb.env` once, generates independent dashboard secrets, prepares
the SQLite and backup directories, and enables the online-backup timer. It never overwrites an
existing environment file.

The Cloudflare Tunnel token is stored only at `/etc/cloudflared/trade.token` with mode `0600`.
The hardened `cloudflared-trade.service` loads it through systemd credentials, so the token is not
placed in the service command line or the repository.

## Immutable rollout

Authenticate Docker to `ghcr.io` with a credential limited to `read:packages`, then deploy the
exact digest emitted by GitHub Actions:

```sh
sudo /opt/zq-arb/deploy/update_vps.sh \
  ghcr.io/yzy0806/poly-zq-trading@sha256:REPLACE_WITH_64_HEX_DIGEST
```

Do not deploy mutable tags such as `latest` or `staging` directly.

## Configuration and verification

On the active VPS **78.142.195.87**, edit `/etc/zq-arb/zq-arb.env` and keep it mode `0600`.
Saving this file does not reload it automatically, and `docker restart` keeps the old environment.
After editing, retain `READ_ONLY` and the three false order gates, validate Compose, and recreate
the engine to load the saved values (including `IBKR_ACCOUNT_ID`):

```sh
sudo docker compose --env-file /etc/zq-arb/deployment.env \
  -f /opt/zq-arb/deploy/compose.production.yml config --quiet && \
sudo docker compose --env-file /etc/zq-arb/deployment.env \
  -f /opt/zq-arb/deploy/compose.production.yml \
  up -d --no-deps --force-recreate --pull never engine
```

After startup, check the process and public health/readiness endpoints:

```sh
sudo docker ps --filter name=zq-arb-engine
curl --fail --max-time 5 http://127.0.0.1:8765/healthz
curl --silent --show-error --max-time 5 http://127.0.0.1:8765/readyz
```

Recreation briefly interrupts the dashboard and engine connections, retains the database and
installed image, and leaves Gateway and Cloudflare Tunnel running. `healthz` checks process
liveness. `readyz` may return HTTP 503 for the separate geographic-eligibility block and does not
cover all account or margin prerequisites; it must not be used as a container liveness probe.

Gateway credentials in `/opt/ib-gateway/.env` use a separate automatic watcher. Detailed steps,
including checking that an account ID was loaded without displaying it, are in
[Section 5.3 of CURRENT_DEPLOYMENT_AND_SECURITY.md](CURRENT_DEPLOYMENT_AND_SECURITY.md#53-configuration-changes).

## Backup and rollback

The timer creates an online SQLite backup daily at 16:20 America/Chicago, during the CME maintenance
break and after the Gateway restart. Every `*.sqlite3` database directly under `/var/lib/zq-arb`
is backed up separately, including paper and live ledgers. Fourteen days are retained. Install the
updated deployment script when adopting separate database files. Rollback is performed by running
`update_vps.sh` with the previous image digest; the environment and SQLite volume are not replaced.
