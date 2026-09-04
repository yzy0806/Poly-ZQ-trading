# VPS deployment

This deployment publishes an immutable container to GitHub Container Registry and runs it on the
VPS next to the existing IB Gateway container. The application is bound to `127.0.0.1:8765`; only
Cloudflare Tunnel may reach it remotely. IBKR API and VNC ports remain loopback-only.

## Safety state

The bootstrap configuration is `READ_ONLY`. `LIVE_TRADING_ENABLED`,
`POLYMARKET_ORDER_SUBMISSION_ENABLED`, and `IBKR_ORDER_SUBMISSION_ENABLED` are all `false`.
Container or host restarts cannot arm the engine.

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

Edit `/etc/zq-arb/zq-arb.env` only on the VPS. Keep it mode `0600`. After editing, validate that the
three order gates remain false, restart the container, and check both endpoints:

```sh
docker compose --env-file /etc/zq-arb/deployment.env \
  -f /opt/zq-arb/deploy/compose.production.yml up --detach
curl --fail http://127.0.0.1:8765/healthz
curl --silent http://127.0.0.1:8765/readyz
```

`healthz` proves the process is running. `readyz` is expected to return HTTP 503 until all external
market-data and account prerequisites are satisfied; it must not be used as a container liveness
probe.

## Backup and rollback

The timer creates an online SQLite backup daily at 16:20 America/Chicago, during the CME maintenance
break and after the Gateway restart. Fourteen days are retained. Rollback is performed by running
`update_vps.sh` with the previous image digest; the environment and SQLite volume are not replaced.
