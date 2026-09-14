# IB Gateway and Passless operations

Verified on 14 September 2026 on VPS **78.142.195.87** (`s62219`, Debian 13, x86_64).
This describes the installed production configuration. The generic `install_vps.sh` bootstrap
does not provision Passless on a new host.

## Automatic startup and ownership

Passless is a host systemd service, separate from the Gateway Docker container. No manual
command is needed to start Passless or the port-6090 desktop during a normal Gateway restart.

| Component | Service or container | Startup behavior |
|---|---|---|
| Private display | `ib-passkey-display.service` | Enabled at VPS boot |
| Approval desktop and VNC | `ib-passkey-desktop.service` | Enabled; depends on the private display |
| Virtual FIDO2 authenticator | `ib-passkey.service` | Enabled; waits for the desktop session to be ready |
| Passless desktop web access | `ib-passkey-novnc.service` | Enabled; listens on VPS loopback port 6090 |
| Gateway configuration application | `ib-gateway-apply.service` | Enabled; depends on Docker and Passless |
| Gateway credential watcher | `ib-gateway-config.path` | Applies saved Gateway environment changes |
| IB Gateway | Docker container `ib-gateway` | `restart: unless-stopped`; uses the mapped virtual HID device |

The Gateway apply script resolves `/dev/ib-passkey` to the current HID device, adds its group,
mounts `/run/udev` read-only, and requires the verified local repaired image. It does not pull an
upstream replacement. Restarting Gateway alone leaves the host Passless service running.

Service startup was verified and the units were validated. A full host reboot and unattended
daily/weekly authentication recovery have **not** been tested. The installed login remains
attended: an available virtual device does not automatically unlock its encrypted credential store.

## Access from Windows

| Browser port | Purpose | Possible prompt |
|---|---|---|
| `6080` | IB Gateway desktop | Gateway VNC password; IBKR authentication screen |
| `6090` | Passless approval desktop | GPG storage passphrase; authentication approval |

Both services remain bound to loopback on the VPS. The Windows `127.0.0.1` addresses work only
while an SSH local-forward connection is running. From a **local Windows PowerShell terminal**,
with both local ports free, run:

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:6080:127.0.0.1:6080 -L 127.0.0.1:6090:127.0.0.1:6090 root@78.142.195.87
```

Keep that terminal connection running, then open both desktops:

1. [Gateway desktop](http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale).
2. [Passless desktop](http://127.0.0.1:6090/vnc.html?autoconnect=true&resize=scale).

Reuse existing forwards instead of starting duplicates. If 6080 is already forwarded and only
6090 is missing, use a separate local terminal with:

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:6090:127.0.0.1:6090 root@78.142.195.87
```

If Windows restarts or the SSH connection closes, reopen the required tunnel. This restores
workstation access; it does not start Passless. Closing either browser tab does not stop the VPS
services, but keep the approval desktop open during authentication so prompts are visible.

## Fresh Gateway login

1. Open both desktops. Unlock Gateway's VNC connection on 6080 if prompted. Its password is
   `VNC_SERVER_PASSWORD` from the protected Gateway environment file.
2. When Gateway requests a passkey, check 6090. A generic “Insert your security key and touch it”
   message on Gateway can mean that Passless is waiting for an unlock or approval; no physical
   USB key needs to be inserted for this configured virtual authenticator.
3. If the Passless desktop displays a GPG passphrase dialog for **IBKR VPS Authenticator**, enter
   the storage passphrase created during enrollment and click **OK**. This is separate from both
   the VNC password and IBKR account password. Enter it directly in the desktop, not in a command,
   chat message, configuration file, or this document.
4. Approve **User Verification Required** for `interactivebrokers.com` when prompted. During the
   verified login, Passless labelled this operation “Credential Management”; it was part of the
   existing Gateway login. Respond promptly because authentication requests can time out.
5. Confirm Gateway's API-server connection is green, then verify the engine's authenticated IBKR
   connection. If the encrypted store is still unlocked, its passphrase prompt may be skipped;
   a fresh login can still require approval. Service startup does not guarantee unattended login.

A blank black screen on 6090 can be the idle approval desktop. If Gateway has already timed out,
complete a fresh Gateway login request with both desktops open; an expired approval cannot
complete the old request.

## Trading recovery after login

Gateway restarts interrupt the API connection. Keep the existing ledger and approved account,
position, order-size, and risk settings. Inspect active orders and hedge obligations before
restarting the engine; a Gateway reconnect is not evidence that venue state is reconciled.

If the engine has exhausted its IBKR reconnect attempts, complete Gateway login first. For a
confirmed-disarmed engine with no unfinished batch, restart it with enough time for the configured
20-second shutdown drain:

```sh
sudo docker restart -t 35 zq-arb-engine
```

An engine or host restart leaves the engine **disarmed**, even with `RUN_MODE=LIVE_ARMED` in its
configuration. Before authorized arming, require fresh clean authenticated venue-ledger
reconciliation, matching positions and accounted-for orders/fills, a current qualified margin
preview, and healthy event processing with no critical alert, pause, or kill switch. `/healthz`
and `/readyz` alone do not cover every prerequisite. Do not override a safety halt or substitute
manual reconciliation for authenticated venue evidence.

## Read-only service checks

On the VPS, check service state and boot enablement without displaying credentials:

```sh
systemctl show ib-passkey.service ib-passkey-display.service ib-passkey-desktop.service ib-passkey-novnc.service ib-gateway-apply.service --property=Id,ActiveState,SubState,UnitFileState
readlink -f /dev/ib-passkey
docker ps --filter name=ib-gateway
```

The four Passless services should be active and enabled. `ib-gateway-apply.service` is a one-shot
unit: `inactive (dead)` after a successful exit can be normal. If startup fails, inspect that
service and its dependencies rather than deleting credentials or re-enrolling the passkey.

The real store is encrypted under `/var/lib/ib-passkey`, owned by the restricted `ibpasskey`
account. Preserve its GPG key and password store together. The temporary port-6091 enrollment
service and token are disabled; they are not needed for routine login. The Windows enrollment
extension is also not required for normal Gateway authentication.
