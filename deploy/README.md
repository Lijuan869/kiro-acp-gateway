# kiro-gw deployment (172.19.129.50)

Reproducible deployment record for the `kiro-gw` container.

## Version matrix

| Component | Version |
|---|---|
| kiro-acp-gateway | 0.1.2 |
| Kiro CLI (inside image) | 3.5.0 (pinned) |
| uv (build helper) | 0.13.9 (pinned) |
| Python | 3.12.16 |
| Docker | 29.4.1 |
| Docker Compose | v5.1.3 |
| Image | `kiro-acp-gateway:local` |
| Image digest (2026-11-20 build) | `sha256:8cdfb5d530266f3acbe7502106626f44918df3f6f7f9e84da01c650ba8a55c8c` |

The digest is informational. Rebuilding after a source or dependency change produces a new digest; tag or save the image before replacing it if you need rollback.

## Current deployment record

- Host: Ubuntu 24.04, user `vm-user`, repo at `/home/vm-user/kiro-acp-gateway`
- Container: `kiro-gw`
- Ports: `127.0.0.1:8000` and `172.19.129.50:8000` (the IP is DHCP)
- Volume: `kiro-gw-home` -> `/home/kiro/.kiro`
- Workspace: `/home/vm-user/kiro-workspace` -> `/workspace`
- Env file: `/home/vm-user/kiro-gw.env` (600)
- Restart policy: `unless-stopped`
- Auth: `KIRO_API_KEY` (Kiro API key) + `KIRO_GATEWAY_API_KEY` (client key)
- Permissions: `KIRO_GATEWAY_PERMISSIONS=deny`, harness permissions `allow-always`

Historical ad-hoc command (before Compose):

```bash
docker run -d --name kiro-gw --restart unless-stopped \
  -p 127.0.0.1:8000:8000 -p 172.19.129.50:8000:8000 \
  -v kiro-gw-home:/home/kiro/.kiro \
  -v /home/vm-user/kiro-workspace:/workspace:z \
  --env-file /home/vm-user/kiro-gw.env \
  kiro-acp-gateway:local
```

## First deploy / migrate to Compose

The existing container was created with `docker run`, so Compose will recreate it once. Expect a short gateway outage.

```bash
cd /home/vm-user/kiro-acp-gateway/deploy

# Reuse the existing secret env file.
export KIRO_GW_ENV_FILE=/home/vm-user/kiro-gw.env
# 0.0.0.0 survives a DHCP address change. Use 127.0.0.1 for local-only.
export KIRO_GW_BIND_IP=0.0.0.0

./deploy.sh
```

`deploy.sh`:

1. checks the env file;
2. removes the old non-Compose `kiro-gw` container once;
3. builds the image (Kiro CLI 3.5.0 pinned);
4. starts the Compose service;
5. waits for `/health` and prints the response.

If you prefer a new env file:

```bash
cp kiro-gw.env.example kiro-gw.env
chmod 600 kiro-gw.env
# edit KIRO_API_KEY and KIRO_GATEWAY_API_KEY
```

## Update

```bash
cd /home/vm-user/kiro-acp-gateway
git pull
cd deploy
export KIRO_GW_ENV_FILE=/home/vm-user/kiro-gw.env
./deploy.sh
```

Before updating, tag the current image for rollback:

```bash
docker tag kiro-acp-gateway:local kiro-acp-gateway:before-$(date +%Y%m%d-%H%M)
```

Rollback:

```bash
cd /home/vm-user/kiro-acp-gateway/deploy
KIRO_GW_IMAGE=kiro-acp-gateway:before-YYYYMMDD-HHMM docker compose up -d
```

## Backup

```bash
cd /home/vm-user/kiro-acp-gateway/deploy
export KIRO_GW_ENV_FILE=/home/vm-user/kiro-gw.env
./backup.sh
```

This writes to `~/kiro-gw-backups` (700):

- `kiro-gw-home-<ts>.tgz` — container volume (agents/settings/state)
- `kiro-gw.env-<ts>` — secret env file (600)
- optionally the image, if `KIRO_GW_BACKUP_IMAGE=1`

Restore the volume:

```bash
cd /home/vm-user/kiro-acp-gateway/deploy
docker compose down
docker run --rm -v kiro-gw-home:/dst -v "$HOME/kiro-gw-backups":/backup \
  alpine:3.20 sh -c 'tar xzf /backup/kiro-gw-home-<ts>.tgz -C /dst'
docker compose up -d
```

Restore the env file by copying the timestamped file back to `~/kiro-gw.env` and setting mode 600.

## Persistent health monitoring

Docker already restarts crashed containers (`unless-stopped`) and has a container healthcheck. For "running but unhealthy" cases, install the user systemd timer:

```bash
mkdir -p ~/.config/systemd/user
cp /home/vm-user/kiro-acp-gateway/deploy/systemd/kiro-gw-healthcheck.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kiro-gw-healthcheck.timer
loginctl enable-linger vm-user   # if it must run without an active login
```

Check it:

```bash
systemctl --user list-timers | grep kiro-gw
tail -f ~/kiro-gw-health.log
```

The check runs every 2 minutes, restarts only a running-but-unhealthy container, and has a 5-minute restart cooldown. A manually stopped container is left stopped.

Useful endpoints:

```bash
curl -s http://127.0.0.1:8000/health
KEY=$(grep '^KIRO_GATEWAY_API_KEY=' ~/kiro-gw.env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/kiro/stats
docker compose logs -f
```

## Networking and security

- Compose defaults to `KIRO_GW_BIND_IP=0.0.0.0` so a DHCP address change does not break port binding. The host has a single private `172.19.129.50/24` interface; restrict access with a host firewall, or set `KIRO_GW_BIND_IP=127.0.0.1` and use an SSH tunnel.
- The service is plain HTTP. Do not expose it to the public internet. Put a TLS reverse proxy in front if remote access is required.
- `/health` is unauthenticated; API routes require `KIRO_GATEWAY_API_KEY`.
- `~/kiro-gw.env` and backups contain two long-lived secrets. Keep them 600/700 and out of git.
- The container runs as uid 1000 (`kiro`). The workspace bind is the only host directory it can write to.

## Key rotation

- `KIRO_API_KEY`: create a new key in the Kiro console, update `~/kiro-gw.env`, then `cd deploy && ./deploy.sh` (or `docker compose up -d`). Revoke the old key after verifying.
- `KIRO_GATEWAY_API_KEY`: generate a new value with `openssl rand -hex 32`, update the env file, restart, and update clients (`~/.kiro-gw-key` on client hosts).

## Source of truth

The code/deploy assets live in the fork:

- `Dockerfile` — includes the fix for the newer Kiro CLI installer env vars (`KIRO_CLI_INSTALL_GLOBAL`, `KIRO_CLI_NO_MODIFY_PATH`) and pre-creates `/home/kiro/.kiro` with the right ownership for the named volume.
- `deploy/docker-compose.yml`
- `deploy/deploy.sh`
- `deploy/backup.sh`
- `deploy/healthcheck.sh`
- `deploy/systemd/*`
- `deploy/kiro-gw.env.example`

Do not commit `deploy/kiro-gw.env`, `.env`, or backups; `.gitignore` covers them.
