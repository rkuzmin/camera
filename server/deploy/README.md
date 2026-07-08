# Server deployment

Cloud backend + PWA for the camera project. Flask (gunicorn) behind the VPS's
existing nginx, SQLite, clips on disk. It's a small box (1 CPU / 1.6 GB) — keep
it light; the edge does the heavy lifting.

## Layout on the VPS

```
/opt/camera-server/
  server.py             backend (Flask)
  requirements.txt
  pwa/                  the PWA (static, served directly by nginx)
  venv/                 python venv
  .env                  secrets + settings (chmod 600) — NOT in git
  data/
    app.db              SQLite (clips index, cameras, push subs, events)
    recordings/<cam>/   uploaded clips (served to the PWA via nginx X-Accel)
    live/<cam>.jpg      latest live snapshot (ephemeral)
    live/hls/<cam>/     live HLS segments — init.mp4 + seg_*.m4s + stream.m3u8 (ephemeral)
    vapid_private.pem   Web Push private key
    tmp/                streamed-upload staging
  deploy/               these files
```

- Service: `camera-server.service` → gunicorn on `127.0.0.1:8090`.
- Retention: `camera-retention.timer` (daily) → `camera-retention.service`.
- nginx vhost: `deploy/nginx-camera.conf` → `/etc/nginx/sites-available/your-domain.example`
  (matched by `server_name`; the `00-default-deny.conf` catch-all owns `default_server`).
- TLS: certbot (`/etc/letsencrypt/live/your-domain.example/`), auto-renews.

## Deploy code updates

From the repo root:

```bash
CAMERA_HOST=root@your-server-ip bash server/deploy/deploy.sh
```

rsyncs `server/` and **gracefully reloads** gunicorn (HUP — no dropped
connections). Changes to `.env` or the unit file need a full restart:
`systemctl restart camera-server`.

`deploy.sh` does **not** touch nginx. When `nginx-camera.conf` changes (e.g. the
`/_hls/` internal location added for live HLS), re-apply the vhost and reload:

```bash
cp deploy/nginx-camera.conf /etc/nginx/sites-available/your-domain.example
nginx -t && systemctl reload nginx
```

## First-time provisioning (already done — recorded for reproducibility)

```bash
# 1) code + venv
rsync -az server/ root@HOST:/opt/camera-server/
ssh root@HOST 'cd /opt/camera-server && python3 -m venv venv && \
  venv/bin/pip install -r requirements.txt && venv/bin/pip install pywebpush'

# 2) secrets -> /opt/camera-server/.env  (copy .env.example, then fill:)
venv/bin/python server.py gen-secret        # CAMERA_SECRET and CAMERA_DEVICE_TOKEN
venv/bin/python server.py hash-password 'PW' # CAMERA_ADMIN_PASSWORD_HASH
venv/bin/python server.py gen-vapid         # CAMERA_VAPID_PUBLIC / _PRIVATE (+ writes data/vapid_private.pem)

# 3) systemd
cp deploy/camera-server.service deploy/camera-retention.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now camera-server camera-retention.timer

# 4) nginx + TLS
cp deploy/nginx-camera.conf /etc/nginx/sites-available/your-domain.example
ln -s ../sites-available/your-domain.example /etc/nginx/sites-enabled/
certbot --nginx -d your-domain.example -n --agree-tos -m admin@your-domain.example --redirect

chown -R www-data:www-data /opt/camera-server
```

## Operations

- **Logs**: `journalctl -u camera-server -f`
- **Health**: `https://your-domain.example/api/health`
- **Retention**: tune `CAMERA_RETENTION_DAYS` (default 30) and/or `CAMERA_MAX_GB`
  (0 = off) in `.env`, then restart. Run once now: `systemctl start camera-retention`.
- **Change the admin password**: `venv/bin/python server.py hash-password 'NEW'`,
  paste into `CAMERA_ADMIN_PASSWORD_HASH` in `.env`, `systemctl restart camera-server`.
- **The edge's device token** is `CAMERA_DEVICE_TOKEN` in `.env` — put the same
  value in the edge's `config.json` as `upload_token`.

## .env keys

See [`../.env.example`](../.env.example). Secrets (`.env`, `vapid_private.pem`)
are never committed.
