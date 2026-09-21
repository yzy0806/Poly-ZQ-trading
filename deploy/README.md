# VPS deployment

This deployment publishes an immutable container to GitHub Container Registry and runs it on the
VPS next to the existing IB Gateway container. The application is bound to `127.0.0.1:8765`; only
Cloudflare Tunnel may reach it remotely. IBKR API and VNC ports remain loopback-only.

Use [CURRENT_DEPLOYMENT_AND_SECURITY.md](CURRENT_DEPLOYMENT_AND_SECURITY.md) as the single
deployment and production-readiness record. It combines the original readiness review,
implemented fixes, dated VPS validation, accepted deferrals, and outstanding operational evidence.
The [combined release acceptance procedure](CURRENT_DEPLOYMENT_AND_SECURITY.md#12-release-acceptance-and-recovery-procedure)
applies alongside the deployment commands below.

For native workstation setup, use [macOS development](../docs/local-development-macos.md).
Host preparation, service management, and rollout commands below run on the **Linux VPS**,
not in a local Mac terminal. Connect from the Mac with `ssh root@78.142.195.87` first.

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
IBKR_ACCOUNT_REFRESH_TIMEOUT_SECONDS=30
IBKR_MAINTENANCE_ENABLED=true
IBKR_MAINTENANCE_TIMEZONE=America/Chicago
IBKR_MAINTENANCE_START=16:00
IBKR_MAINTENANCE_END=17:00
IBKR_GATEWAY_RESTART_TIME=16:10
IBKR_MAINTENANCE_DRAIN_SECONDS=60
IBKR_MAINTENANCE_RECOVERY_SECONDS=300
RECONCILIATION_MAX_AGE_SECONDS=60
IBKR_CALLBACK_SETTLE_SECONDS=2
SHUTDOWN_DRAIN_SECONDS=20
```

## Initial host preparation

Copy the repository `deploy/` directory to `/opt/zq-arb/deploy/` on the VPS, then run there:

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
For a new bootstrap installation, retain `READ_ONLY` and the three false order gates. For the
existing production installation, preserve the approved account, trading limits, and execution
settings. Validate Compose and recreate the engine to load saved values (including `IBKR_ACCOUNT_ID`):

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
installed image, and leaves Gateway and Cloudflare Tunnel running. The engine starts disarmed,
including when its configured mode is `LIVE_ARMED`; fresh venue reconciliation and margin checks
are required before an authorized operator arms it. `healthz` checks process
liveness. `readyz` may return HTTP 503 for the separate geographic-eligibility block and does not
cover all account or margin prerequisites; it must not be used as a container liveness probe.

Gateway credentials in `/opt/ib-gateway/.env` use a separate automatic watcher. Detailed steps,
including checking that an account ID was loaded without displaying it, are in
[Section 5.3 of CURRENT_DEPLOYMENT_AND_SECURITY.md](CURRENT_DEPLOYMENT_AND_SECURITY.md#53-configuration-changes).

## Gateway passkey login and desktop access

The production VPS uses Passless for attended IBKR passkey login. Passless, its private desktop,
and noVNC on port **6090** run as host systemd services, outside the Gateway Docker container.
They are enabled at boot; no manual Passless start command is needed for a normal Gateway restart.
Gateway's apply service starts after Passless and attaches the current virtual HID device.

Gateway's desktop is on port **6080**. A fresh passkey login can require the operator to unlock
the encrypted Passless store and approve authentication on **6090**. These localhost browser
addresses depend on an SSH tunnel from the Mac; reopen the tunnel if its connection ends,
the Mac restarts, or sleep interrupts the connection. Automatic service startup does not make login fully unattended.

See [IBKR_GATEWAY_PASSLESS.md](IBKR_GATEWAY_PASSLESS.md) for the two-port SSH command, password
distinctions, login procedure, and service checks. The last verified production state and image
references are recorded in [CURRENT_DEPLOYMENT_AND_SECURITY.md](CURRENT_DEPLOYMENT_AND_SECURITY.md).

## Backup and rollback

The timer creates an online SQLite backup daily at 16:20 America/Chicago, during the CME maintenance
break and after the Gateway restart. Every `*.sqlite3` database directly under `/var/lib/zq-arb`
is backed up separately, including paper and live ledgers. Fourteen days are retained. Install the
updated deployment script when adopting separate database files. Rollback is performed by running
`update_vps.sh` with the previous image digest; the environment and SQLite volume are not replaced.
