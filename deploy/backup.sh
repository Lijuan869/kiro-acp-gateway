#!/bin/sh
# Backup the gateway state volume, env file, and optionally the image.
set -eu
cd "$(dirname "$0")"

BACKUP_DIR="${KIRO_GW_BACKUP_DIR:-$HOME/kiro-gw-backups}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

ts=$(date +%Y%m%d-%H%M%S)
echo "backing up volume kiro-gw-home -> $BACKUP_DIR"
docker run --rm \
  -v kiro-gw-home:/src:ro \
  -v "$BACKUP_DIR":/backup \
  alpine:3.20 \
  tar czf "/backup/kiro-gw-home-$ts.tgz" -C /src .

ENV_FILE="${KIRO_GW_ENV_FILE:-$HOME/kiro-gw.env}"
if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$BACKUP_DIR/kiro-gw.env-$ts"
  chmod 600 "$BACKUP_DIR/kiro-gw.env-$ts"
  echo "backed up env file"
fi

if [ "${KIRO_GW_BACKUP_IMAGE:-0}" = "1" ]; then
  IMAGE="${KIRO_GW_IMAGE:-kiro-acp-gateway:local}"
  echo "saving image $IMAGE (large; this can take a while)"
  docker image save "$IMAGE" | gzip > "$BACKUP_DIR/kiro-gw-image-$ts.tgz"
fi

echo "backup complete: $BACKUP_DIR (timestamp $ts)"
