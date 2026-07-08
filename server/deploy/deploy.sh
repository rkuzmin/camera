#!/usr/bin/env bash
# Sync the backend code to the VPS and restart the service.
# One-time infra (nginx vhost, certbot, .env, systemd install) is set up
# separately — see server/deploy/README.md. This is the repeatable code deploy.
set -euo pipefail

HOST="${CAMERA_HOST:?Set CAMERA_HOST=user@host, e.g. root@your-server-ip}"
DEST="${CAMERA_DEST:-/opt/camera-server}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"   # the server/ directory

echo ">> rsync $SRC/ -> $HOST:$DEST/"
rsync -az --delete \
    --exclude 'data/' --exclude 'venv/' --exclude '.env' \
    --exclude '__pycache__/' --exclude '*.pyc' \
    "$SRC"/ "$HOST:$DEST/"

echo ">> venv + deps + restart on $HOST"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/camera-server
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip >/dev/null
venv/bin/pip install -q -r requirements.txt
chown -R www-data:www-data /opt/camera-server
if [ -f /etc/systemd/system/camera-server.service ]; then
    if systemctl is-active --quiet camera-server; then
        systemctl reload camera-server && echo "reloaded (graceful, no dropped connections)"
    else
        systemctl restart camera-server && echo "restarted"
    fi
    sleep 1
    curl -s -m 5 http://127.0.0.1:8090/api/health && echo
else
    echo "(camera-server.service not installed yet — first-time infra step)"
fi
REMOTE
echo ">> deploy done"
