#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root" >&2
    exit 1
fi
if [ "$#" -ne 1 ]; then
    echo "usage: update_vps.sh ghcr.io/OWNER/IMAGE@sha256:DIGEST" >&2
    exit 2
fi

IMAGE=$1
if ! printf '%s\n' "$IMAGE" | grep -Eq '^ghcr\.io/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$'; then
    echo "image must be an immutable ghcr.io sha256 digest" >&2
    exit 2
fi

umask 022
TEMP_ENV=$(mktemp /etc/zq-arb/deployment.env.XXXXXX)
trap 'rm -f "$TEMP_ENV"' EXIT HUP INT TERM
printf 'ZQ_ARB_IMAGE=%s\n' "$IMAGE" >"$TEMP_ENV"
chmod 0644 "$TEMP_ENV"
mv "$TEMP_ENV" /etc/zq-arb/deployment.env
trap - EXIT HUP INT TERM

docker compose \
    --env-file /etc/zq-arb/deployment.env \
    -f /opt/zq-arb/deploy/compose.production.yml \
    pull
docker compose \
    --env-file /etc/zq-arb/deployment.env \
    -f /opt/zq-arb/deploy/compose.production.yml \
    up --detach --remove-orphans

attempt=0
while [ "$attempt" -lt 30 ]; do
    status=$(docker inspect --format '{{.State.Health.Status}}' zq-arb-engine)
    if [ "$status" = healthy ]; then
        printf 'zq-arb-engine health: %s\n' "$status"
        exit 0
    fi
    if [ "$status" = unhealthy ]; then
        docker logs --tail 100 zq-arb-engine >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 3
done

docker logs --tail 100 zq-arb-engine >&2
echo "container did not become healthy within 90 seconds" >&2
exit 1
