#!/bin/sh
# Idempotent deploy/update for the kiro-gw container.
set -eu
cd "$(dirname "$0")"

export KIRO_GW_ENV_FILE="${KIRO_GW_ENV_FILE:-$HOME/kiro-gw.env}"
export KIRO_GW_BIND_IP="${KIRO_GW_BIND_IP:-0.0.0.0}"

if [ ! -f "$KIRO_GW_ENV_FILE" ]; then
  echo "env file not found: $KIRO_GW_ENV_FILE" >&2
  echo "copy kiro-gw.env.example to that path, chmod 600, and fill in the keys" >&2
  exit 1
fi

# One-time migration from the ad-hoc `docker run` container (no compose labels).
if docker inspect kiro-gw >/dev/null 2>&1; then
  project=$(docker inspect kiro-gw \
    --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
  if [ -z "$project" ]; then
    echo "removing pre-compose kiro-gw container (one-time migration)"
    docker rm -f kiro-gw
  fi
fi

docker compose build
docker compose up -d

case "$KIRO_GW_BIND_IP" in
  0.0.0.0|"") CHECK_HOST=127.0.0.1 ;;
  *) CHECK_HOST="$KIRO_GW_BIND_IP" ;;
esac
URL="http://$CHECK_HOST:8000/health"

i=0
until curl -fsS -m 5 "$URL" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "gateway did not become healthy at $URL; last logs:" >&2
    docker compose logs --tail=80 >&2
    exit 1
  fi
  sleep 2
done

echo "gateway healthy at $URL:"
curl -sS "$URL"
echo
