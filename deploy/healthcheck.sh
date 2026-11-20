#!/bin/sh
# Restart kiro-gw when it is running but /health stops answering.
# Docker's restart policy handles crashed/stopped containers; this handles a
# live container that is stuck or whose HTTP server is unhealthy.
set -eu

CONTAINER=kiro-gw
URL=http://127.0.0.1:8000/health
LOG="${KIRO_GW_HEALTH_LOG:-$HOME/kiro-gw-health.log}"
STAMP="$HOME/.kiro-gw-last-restart"
now=$(date +%s)

log() {
  printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"
}

running=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
if [ "$running" != "true" ]; then
  log "container not running; leaving restart policy to Docker"
  exit 0
fi

if curl -fsS -m 10 "$URL" >/dev/null 2>&1; then
  exit 0
fi

log "health check failed"
last=0
if [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
fi
if [ $((now - last)) -lt 300 ]; then
  log "restart cooldown active"
  exit 0
fi

date +%s >"$STAMP"
docker restart "$CONTAINER" || true
log "restarted $CONTAINER"
