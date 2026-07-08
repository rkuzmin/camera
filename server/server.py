#!/usr/bin/env python3
"""Cloud backend for the camera project.

Runs on a small VPS behind nginx. Responsibilities:
  * receive recorded clips uploaded by the edge app (the local app.py) and store
    them on disk + index them in SQLite;
  * serve an authenticated JSON API to the mobile PWA (list recordings, camera
    list, playback URLs);
  * hand off the actual video bytes to nginx via X-Accel-Redirect so a tiny
    gunicorn worker never has to stream a whole file itself (this box has 1 CPU
    and ~1.6 GB RAM);
  * Web Push (VAPID) subscriptions + delivery for detection alerts.

Everything heavy (capture, YOLO, encoding) stays on the edge; this server only
stores and serves. Stdlib + Flask only for the core; pywebpush is imported
lazily so the app runs without it until push is configured.

Config comes from the environment (systemd EnvironmentFile=/opt/camera-server/.env):
  CAMERA_DATA_DIR              where clips + db live       (default: ./data)
  CAMERA_SECRET                token-signing secret        (required in prod)
  CAMERA_ADMIN_PASSWORD_HASH   pbkdf2 hash (see gen below) (required in prod)
  CAMERA_DEVICE_TOKEN          shared secret for the edge  (required in prod)
  CAMERA_RETENTION_DAYS        auto-delete clips older than (default: 30; 0=off)
  CAMERA_MAX_GB                cap total storage, GB        (default: 0=off)
  CAMERA_VAPID_PUBLIC/PRIVATE/SUBJECT   Web Push keys       (optional; phase: push)

CLI helpers (run with the venv python):
  python server.py gen-secret            -> a random CAMERA_SECRET / device token
  python server.py hash-password [PW]    -> CAMERA_ADMIN_PASSWORD_HASH value
  python server.py gen-vapid             -> VAPID key pair for push
  python server.py init-db               -> create tables (also done on startup)
  python server.py run [--port 8090]     -> dev server (prod uses gunicorn)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, Response, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CAMERA_DATA_DIR", BASE_DIR / "data")).resolve()
RECORDINGS_DIR = DATA_DIR / "recordings"
TMP_DIR = DATA_DIR / "tmp"
LIVE_DIR = DATA_DIR / "live"          # latest live snapshot per camera (ephemeral)
HLS_DIR = LIVE_DIR / "hls"            # per-camera HLS segments for live streaming (ephemeral)
DB_PATH = DATA_DIR / "app.db"
for d in (DATA_DIR, RECORDINGS_DIR, TMP_DIR, LIVE_DIR, HLS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# In prod these are set via the .env; the dev fallbacks let the app boot locally.
SECRET = os.environ.get("CAMERA_SECRET", "dev-insecure-secret-change-me")
ADMIN_PASSWORD_HASH = os.environ.get("CAMERA_ADMIN_PASSWORD_HASH", "")
DEVICE_TOKEN = os.environ.get("CAMERA_DEVICE_TOKEN", "")
RETENTION_DAYS = int(os.environ.get("CAMERA_RETENTION_DAYS", "30") or 0)
MAX_GB = float(os.environ.get("CAMERA_MAX_GB", "0") or 0)

SESSION_MAX_AGE = 30 * 24 * 3600   # PWA login token lifetime
MEDIA_MAX_AGE = 12 * 3600          # signed playback-URL lifetime

session_signer = URLSafeTimedSerializer(SECRET, salt="camera-session")
media_signer = URLSafeTimedSerializer(SECRET, salt="camera-media")

# Filenames the edge produces: 2026-07-05_12-30-00.mp4 . Keep the allowed set
# tight so cam_id / name can never escape the recordings dir (path traversal).
CAM_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,128}\.mp4$")
UPLOAD_CHUNK = 1024 * 1024
LIVE_WANT_TTL = 12       # a viewer's "want" keeps a camera live this many seconds
LIVE_FRAME_FRESH = 8     # a stored live frame is considered current for this long
HLS_SEG_RE = re.compile(r"^(init\.mp4|seg_\d{1,10}\.m4s)$")
HLS_URL_TTL = 3600       # signed live-stream URL lifetime (the PWA refreshes it)
HLS_STALE = 15           # if the playlist hasn't updated in this long, the edge stopped pushing

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None   # uploads are streamed; no in-memory cap


# --------------------------------------------------------------------------- #
# Database (SQLite, WAL, one connection per request)
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,   -- 1 = still present on the edge; 0 = stale, hidden from the PWA
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    started_at  INTEGER NOT NULL,       -- epoch seconds, parsed from the filename
    tags        TEXT NOT NULL DEFAULT '[]',
    uploaded_at INTEGER NOT NULL,
    UNIQUE(cam_id, name)
);
CREATE INDEX IF NOT EXISTS idx_clips_started ON clips(started_at DESC);
CREATE TABLE IF NOT EXISTS push_subs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT UNIQUE NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'detection',
    label       TEXT NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE TABLE IF NOT EXISTS live_wanted (
    cam_id TEXT PRIMARY KEY,
    ts     INTEGER NOT NULL
);
"""


def init_db():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # migrations: add columns missing on databases created by older versions
        clip_cols = {r[1] for r in con.execute("PRAGMA table_info(clips)").fetchall()}
        if "duration" not in clip_cols:
            con.execute("ALTER TABLE clips ADD COLUMN duration REAL NOT NULL DEFAULT 0")
        cam_cols = {r[1] for r in con.execute("PRAGMA table_info(cameras)").fetchall()}
        if "active" not in cam_cols:
            con.execute("ALTER TABLE cameras ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        con.commit()
    finally:
        con.close()


@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Auth: password hashing, session tokens, decorators
# --------------------------------------------------------------------------- #
def hash_password(pw, iterations=200_000):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw, stored):
    try:
        algo, iters, salthex, hashhex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salthex), int(iters))
        return hmac.compare_digest(dk.hex(), hashhex)
    except (ValueError, AttributeError):
        return False


def issue_session_token():
    return session_signer.dumps({"u": "admin"})


def valid_session(token):
    if not token:
        return False
    try:
        session_signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.args.get("t", "").strip()


def require_user(fn):
    """PWA endpoints: valid session token in Authorization: Bearer."""
    def wrapper(*a, **kw):
        if not valid_session(bearer_token()):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


def require_device(fn):
    """Edge endpoints: the shared device token (constant-time compared)."""
    def wrapper(*a, **kw):
        tok = request.headers.get("X-Device-Token", "") or bearer_token()
        if not DEVICE_TOKEN or not hmac.compare_digest(tok, DEVICE_TOKEN):
            return jsonify({"error": "unauthorized device"}), 401
        return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_started_at(name):
    """2026-07-05_12-30-00.mp4 -> epoch seconds (local time), or now() on failure."""
    stem = name[:-4] if name.endswith(".mp4") else name
    try:
        return int(datetime.strptime(stem, "%Y-%m-%d_%H-%M-%S").timestamp())
    except ValueError:
        return int(time.time())


def media_url(cam_id, name):
    sig = media_signer.dumps(f"{cam_id}/{name}")
    return f"/api/media/{cam_id}/{name}?t={sig}"


def clip_row_to_json(row):
    try:
        tags = json.loads(row["tags"])
    except (ValueError, TypeError):
        tags = []
    return {
        "cam_id": row["cam_id"],
        "name": row["name"],
        "size_mb": round(row["size"] / 1e6, 1),
        "duration": round(row["duration"] or 0, 1),
        "started_at": row["started_at"],
        "started_iso": datetime.fromtimestamp(row["started_at"]).isoformat(timespec="seconds"),
        "tags": tags,
        "url": media_url(row["cam_id"], row["name"]),
    }


def upsert_camera(con, cam_id, name=None):
    now = int(time.time())
    con.execute(
        "INSERT INTO cameras(id, name, created_at, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  name=COALESCE(NULLIF(excluded.name,''), cameras.name), updated_at=excluded.updated_at",
        (cam_id, name or "", now, now),
    )


def storage_bytes():
    total = 0
    for p in RECORDINGS_DIR.rglob("*.mp4"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------- #
# Retention: delete old clips (and, if over the size cap, oldest-first)
# --------------------------------------------------------------------------- #
def enforce_retention():
    removed = 0
    now = int(time.time())
    with get_db() as con:
        if RETENTION_DAYS > 0:
            cutoff = now - RETENTION_DAYS * 86400
            rows = con.execute(
                "SELECT id, cam_id, name FROM clips WHERE started_at < ?", (cutoff,)
            ).fetchall()
            for r in rows:
                if _delete_clip_file(r["cam_id"], r["name"]):
                    con.execute("DELETE FROM clips WHERE id=?", (r["id"],))
                    removed += 1
        if MAX_GB > 0:
            cap = int(MAX_GB * 1e9)
            while storage_bytes() > cap:
                r = con.execute(
                    "SELECT id, cam_id, name FROM clips ORDER BY started_at ASC LIMIT 1"
                ).fetchone()
                if not r:
                    break
                _delete_clip_file(r["cam_id"], r["name"])
                con.execute("DELETE FROM clips WHERE id=?", (r["id"],))
                removed += 1
    return removed


def _delete_clip_file(cam_id, name):
    try:
        (RECORDINGS_DIR / cam_id / name).unlink(missing_ok=True)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Public / health
# --------------------------------------------------------------------------- #
@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "time": int(time.time())})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    password = data.get("password", "")
    if not ADMIN_PASSWORD_HASH:
        return jsonify({"error": "server has no admin password configured"}), 500
    if not verify_password(password, ADMIN_PASSWORD_HASH):
        return jsonify({"error": "wrong password"}), 401
    return jsonify({"token": issue_session_token()})


@app.route("/api/me")
@require_user
def api_me():
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# PWA read API
# --------------------------------------------------------------------------- #
@app.route("/api/cameras")
@require_user
def api_cameras():
    """Cameras for the PWA. Only active ones (the edge's current set) by default;
    pass ?all=1 to also include stale/inactive cameras (camera-management view)."""
    include_all = request.args.get("all", "") in ("1", "true", "yes")
    sql = (
        "SELECT c.id, c.name, c.active, "
        "  (SELECT COUNT(*) FROM clips WHERE cam_id=c.id) AS clip_count, "
        "  (SELECT MAX(started_at) FROM clips WHERE cam_id=c.id) AS last_clip "
        "FROM cameras c "
    )
    if not include_all:
        sql += "WHERE c.active=1 "
    sql += "ORDER BY c.name, c.id"
    with get_db() as con:
        rows = con.execute(sql).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
@require_user
def api_camera_delete(cam_id):
    """Remove a camera from the PWA. With ?with_clips=1 its recordings are deleted
    too (files + rows); otherwise only the camera entry is dropped and recordings
    stay browsable. A camera that still exists on the edge will reappear on the
    next sync — this is meant for stale cameras no longer present on the device."""
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad camera id"}), 400
    with_clips = request.args.get("with_clips", "") in ("1", "true", "yes")
    removed_clips = 0
    with get_db() as con:
        if with_clips:
            for r in con.execute("SELECT name FROM clips WHERE cam_id=?", (cam_id,)).fetchall():
                if _delete_clip_file(cam_id, r["name"]):
                    removed_clips += 1
            con.execute("DELETE FROM clips WHERE cam_id=?", (cam_id,))
            con.execute("DELETE FROM events WHERE cam_id=?", (cam_id,))
            try:
                (LIVE_DIR / f"{cam_id}.jpg").unlink(missing_ok=True)
                shutil.rmtree(HLS_DIR / cam_id, ignore_errors=True)
                (RECORDINGS_DIR / cam_id).rmdir()   # only succeeds once it's empty
            except OSError:
                pass
        con.execute("DELETE FROM cameras WHERE id=?", (cam_id,))
        con.execute("DELETE FROM live_wanted WHERE cam_id=?", (cam_id,))
    return jsonify({"ok": True, "removed_clips": removed_clips})


@app.route("/api/recordings")
@require_user
def api_recordings():
    cam = request.args.get("cam", "").strip()
    tag = request.args.get("tag", "").strip()
    day = request.args.get("day", "").strip()   # YYYY-MM-DD (local day)
    limit = min(max(int(request.args.get("limit", "200") or 200), 1), 2000)
    offset = max(int(request.args.get("offset", "0") or 0), 0)
    sql = "SELECT * FROM clips"
    where, params = [], []
    if cam:
        where.append("cam_id = ?")
        params.append(cam)
    if tag:
        where.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if day:
        where.append("date(started_at, 'unixepoch', 'localtime') = ?")
        params.append(day)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as con:
        rows = con.execute(sql, params).fetchall()
    return jsonify([clip_row_to_json(r) for r in rows])


@app.route("/api/recording-days")
@require_user
def api_recording_days():
    """Days that have at least one clip, with counts — powers the calendar.

    Grouped by local calendar day so it matches what the clock shows. Honours the
    same cam/tag filters as /api/recordings so the calendar reflects the filter.
    """
    cam = request.args.get("cam", "").strip()
    tag = request.args.get("tag", "").strip()
    where, params = [], []
    if cam:
        where.append("cam_id = ?")
        params.append(cam)
    if tag:
        where.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    sql = ("SELECT date(started_at, 'unixepoch', 'localtime') AS day, COUNT(*) AS count "
           "FROM clips")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day"
    with get_db() as con:
        rows = con.execute(sql, params).fetchall()
    return jsonify([{"day": r["day"], "count": r["count"]} for r in rows])


@app.route("/api/media/<cam_id>/<path:name>")
def api_media(cam_id, name):
    """Validate the signed URL, then let nginx stream the file (X-Accel-Redirect).

    Works with a plain <video src> because the signature travels in the query
    string (?t=). The session token itself never appears in a media URL.
    """
    token = request.args.get("t", "")
    try:
        want = media_signer.loads(token, max_age=MEDIA_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"error": "expired or invalid media link"}), 403
    if want != f"{cam_id}/{name}" or not CAM_ID_RE.match(cam_id) or not CLIP_NAME_RE.match(name):
        return jsonify({"error": "not found"}), 404
    target = (RECORDINGS_DIR / cam_id / name)
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    # In production nginx serves the bytes (range requests, sendfile). In dev
    # (no nginx / X-Accel), fall back to sending the file straight from Flask.
    if os.environ.get("CAMERA_XACCEL", "1") == "1":
        resp = Response("")
        resp.headers["X-Accel-Redirect"] = f"/_media/{cam_id}/{name}"
        resp.headers["Content-Type"] = "video/mp4"
        return resp
    return send_from_directory(RECORDINGS_DIR / cam_id, name, conditional=True)


# --------------------------------------------------------------------------- #
# Edge ingest API (device-authenticated)
# --------------------------------------------------------------------------- #
@app.route("/api/ingest/camera", methods=["POST"])
@require_device
def api_ingest_camera():
    data = request.get_json(force=True, silent=True) or {}
    cam_id = (data.get("id") or "").strip()
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad camera id"}), 400
    with get_db() as con:
        upsert_camera(con, cam_id, (data.get("name") or "").strip())
    return jsonify({"ok": True})


@app.route("/api/ingest/cameras", methods=["POST"])
@require_device
def api_ingest_cameras():
    """Authoritative camera-set sync from the edge.

    Body: {"cameras": [{"id": "...", "name": "..."}, ...]}. Every listed camera is
    upserted and marked active; every camera NOT listed is marked inactive, so the
    PWA stops showing cameras that were removed on the device. Rows and recordings
    are kept — only visibility (active) changes. Idempotent.
    """
    data = request.get_json(force=True, silent=True) or {}
    cams = data.get("cameras")
    if not isinstance(cams, list):
        return jsonify({"error": "cameras must be a list"}), 400
    now = int(time.time())
    ids = []
    with get_db() as con:
        for c in cams:
            cid = ((c or {}).get("id") or "").strip()
            if not CAM_ID_RE.match(cid):
                continue
            name = ((c or {}).get("name") or "").strip()
            con.execute(
                "INSERT INTO cameras(id, name, active, created_at, updated_at) "
                "VALUES(?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET "
                "  name=COALESCE(NULLIF(excluded.name,''), cameras.name), "
                "  active=1, updated_at=excluded.updated_at",
                (cid, name, now, now),
            )
            ids.append(cid)
        if ids:
            placeholders = ",".join("?" * len(ids))
            con.execute(f"UPDATE cameras SET active=0 WHERE id NOT IN ({placeholders})", ids)
        else:
            con.execute("UPDATE cameras SET active=0")
    return jsonify({"ok": True, "active": ids})


@app.route("/api/ingest/have/<cam_id>/<path:name>")
@require_device
def api_ingest_have(cam_id, name):
    """Edge asks 'do you already have this clip?' so it can skip re-uploading."""
    if not CAM_ID_RE.match(cam_id) or not CLIP_NAME_RE.match(name):
        return jsonify({"error": "bad name"}), 400
    with get_db() as con:
        row = con.execute(
            "SELECT size FROM clips WHERE cam_id=? AND name=?", (cam_id, name)
        ).fetchone()
    have = bool(row) and (RECORDINGS_DIR / cam_id / name).exists()
    return jsonify({"have": have, "size": row["size"] if row else 0})


@app.route("/api/ingest/clip", methods=["POST"])
@require_device
def api_ingest_clip():
    """Receive one clip. Metadata in headers, raw mp4 bytes as the body.

    Streamed straight to a temp file in UPLOAD_CHUNK pieces, then atomically
    moved into place — nothing large is ever held in memory.
    Idempotent by (cam_id, name).
    """
    cam_id = request.headers.get("X-Cam-Id", "").strip()
    name = request.headers.get("X-Clip-Name", "").strip()
    cam_name = unquote(request.headers.get("X-Cam-Name", "")).strip()      # may be Cyrillic
    tags_raw = unquote(request.headers.get("X-Clip-Tags", "")).strip()     # comma-separated
    if not CAM_ID_RE.match(cam_id) or not CLIP_NAME_RE.match(name):
        return jsonify({"error": "bad cam_id/name"}), 400
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    try:
        duration = round(float(request.headers.get("X-Clip-Duration", "0") or 0), 1)
    except ValueError:
        duration = 0.0

    cam_dir = RECORDINGS_DIR / cam_id
    cam_dir.mkdir(parents=True, exist_ok=True)
    final = cam_dir / name
    started = parse_started_at(name)

    if final.exists():   # already have the bytes — just make sure it's indexed
        size = final.stat().st_size
        with get_db() as con:
            upsert_camera(con, cam_id, cam_name)
            con.execute(
                "INSERT INTO clips(cam_id,name,size,started_at,tags,duration,uploaded_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(cam_id,name) DO NOTHING",
                (cam_id, name, size, started, json.dumps(tags), duration, int(time.time())),
            )
        return jsonify({"ok": True, "already": True, "size": size})

    tmp = TMP_DIR / f"{cam_id}__{name}.{secrets.token_hex(4)}.part"
    size = 0
    try:
        with open(tmp, "wb") as fh:
            while True:
                chunk = request.stream.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                size += len(chunk)
        if size == 0:
            tmp.unlink(missing_ok=True)
            return jsonify({"error": "empty body"}), 400
        os.replace(tmp, final)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": f"write failed: {e}"}), 500

    with get_db() as con:
        upsert_camera(con, cam_id, cam_name)
        con.execute(
            "INSERT INTO clips(cam_id,name,size,started_at,tags,duration,uploaded_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(cam_id,name) DO UPDATE SET "
            "  size=excluded.size, tags=excluded.tags, duration=excluded.duration, "
            "  uploaded_at=excluded.uploaded_at",
            (cam_id, name, size, started, json.dumps(tags), duration, int(time.time())),
        )
    return jsonify({"ok": True, "size": size})


@app.route("/api/ingest/event", methods=["POST"])
@require_device
def api_ingest_event():
    """Edge reports a detection. Stored, and (phase: push) fans out a Web Push."""
    data = request.get_json(force=True, silent=True) or {}
    cam_id = (data.get("cam_id") or "").strip()
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad camera id"}), 400
    label = (data.get("label") or "").strip()[:64]
    cam_name = (data.get("cam_name") or "").strip()
    ts = int(data.get("ts") or time.time())
    with get_db() as con:
        upsert_camera(con, cam_id, cam_name)
        con.execute(
            "INSERT INTO events(cam_id, kind, label, ts, meta) VALUES(?,?,?,?,?)",
            (cam_id, "detection", label, ts, json.dumps(data.get("meta") or {})),
        )
    sent = send_push_all(
        title=f"{cam_name or cam_id}",
        body=f"Обнаружено: {label}" if label else "Движение",
        data={"cam_id": cam_id, "ts": ts},
    )
    return jsonify({"ok": True, "pushed": sent})


# --------------------------------------------------------------------------- #
# Live view — snapshot relay. The PWA "wants" a camera (heartbeat); the edge
# polls what's wanted and POSTs JPEG frames; the PWA pulls the latest frame.
# All plain HTTP through nginx — no media server, works on iOS. Frames live on
# disk so both gunicorn workers see them.
# --------------------------------------------------------------------------- #
@app.route("/api/live/want", methods=["POST"])
@require_user
def api_live_want():
    cam_id = (request.get_json(force=True, silent=True) or {}).get("cam_id", "").strip()
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad camera id"}), 400
    with get_db() as con:
        con.execute("INSERT INTO live_wanted(cam_id, ts) VALUES(?,?) "
                    "ON CONFLICT(cam_id) DO UPDATE SET ts=excluded.ts", (cam_id, int(time.time())))
    return jsonify({"ok": True})


@app.route("/api/live/wanted")
@require_device
def api_live_wanted():
    cutoff = int(time.time()) - LIVE_WANT_TTL
    with get_db() as con:
        rows = con.execute("SELECT cam_id FROM live_wanted WHERE ts >= ?", (cutoff,)).fetchall()
    return jsonify([r["cam_id"] for r in rows])


@app.route("/api/live/frame", methods=["POST"])
@require_device
def api_live_frame_put():
    cam_id = request.headers.get("X-Cam-Id", "").strip()
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad cam id"}), 400
    data = request.get_data(cache=False)
    if not data:
        return jsonify({"error": "empty"}), 400
    tmp = LIVE_DIR / f".{cam_id}.{secrets.token_hex(3)}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, LIVE_DIR / f"{cam_id}.jpg")
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/live/frame/<cam_id>")
@require_user
def api_live_frame_get(cam_id):
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad cam id"}), 400
    f = LIVE_DIR / f"{cam_id}.jpg"
    try:
        if time.time() - f.stat().st_mtime > LIVE_FRAME_FRESH:
            return ("", 204)   # stale/no frame — the edge isn't pushing (yet)
        data = f.read_bytes()
    except OSError:
        return ("", 204)
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# Live HLS — a low-latency stream relayed edge -> cloud -> browser. The edge
# remuxes the camera's H.264/H.265 into short fMP4 segments and POSTs them here;
# nginx serves the segment bytes (X-Accel), Flask serves the tiny playlist. The
# <video>/hls.js sub-requests can't send an auth header, so access rides on a
# signed ?t= token (same trick as signed media URLs). Segments live on disk so
# both gunicorn workers see them.
# --------------------------------------------------------------------------- #
def hls_playlist_url(cam_id):
    sig = media_signer.dumps(f"hls/{cam_id}")
    return f"/api/live/hls/{cam_id}/stream.m3u8?t={sig}"


def _hls_token_ok(cam_id):
    try:
        want = media_signer.loads(request.args.get("t", ""), max_age=HLS_URL_TTL)
    except (BadSignature, SignatureExpired):
        return False
    return want == f"hls/{cam_id}"


@app.route("/api/live/hls/start", methods=["POST"])
@require_user
def api_hls_start():
    """PWA asks to watch: hand back a signed playlist URL it can give to <video>."""
    cam_id = (request.get_json(force=True, silent=True) or {}).get("cam_id", "").strip()
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad camera id"}), 400
    return jsonify({"url": hls_playlist_url(cam_id), "ttl": HLS_URL_TTL})


@app.route("/api/live/hls/segment", methods=["POST"])
@require_device
def api_hls_segment_put():
    cam_id = request.headers.get("X-Cam-Id", "").strip()
    name = request.headers.get("X-Seg-Name", "").strip()
    if not CAM_ID_RE.match(cam_id) or not HLS_SEG_RE.match(name):
        return jsonify({"error": "bad cam id/seg"}), 400
    data = request.get_data(cache=False)
    if not data:
        return jsonify({"error": "empty"}), 400
    d = HLS_DIR / cam_id
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.{secrets.token_hex(3)}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, d / name)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/live/hls/playlist", methods=["POST"])
@require_device
def api_hls_playlist_put():
    cam_id = request.headers.get("X-Cam-Id", "").strip()
    if not CAM_ID_RE.match(cam_id):
        return jsonify({"error": "bad cam id"}), 400
    text = request.get_data(cache=False).decode("utf-8", "replace")
    if "#EXTM3U" not in text:
        return jsonify({"error": "bad playlist"}), 400
    d = HLS_DIR / cam_id
    d.mkdir(parents=True, exist_ok=True)
    # bound the dir: drop any segment this playlist no longer references (keep init)
    keep = set(re.findall(r"(?m)^(seg_\d{1,10}\.m4s)$", text)) | {"init.mp4"}
    for f in d.glob("*.m4s"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
    tmp = d / f".stream.{secrets.token_hex(3)}.tmp"
    try:
        tmp.write_text(text)
        os.replace(tmp, d / "stream.m3u8")
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/live/hls/<cam_id>/<seg>")
def api_hls_get(cam_id, seg):
    """Serve the playlist (Flask, rewritten to carry the token) or a segment (nginx)."""
    if not CAM_ID_RE.match(cam_id) or not _hls_token_ok(cam_id):
        return jsonify({"error": "forbidden"}), 403
    d = HLS_DIR / cam_id
    if seg == "stream.m3u8":
        pl = d / "stream.m3u8"
        try:
            if time.time() - pl.stat().st_mtime > HLS_STALE:
                return ("", 404)   # edge stopped pushing — PWA falls back to snapshots
            text = pl.read_text()
        except OSError:
            return ("", 404)
        # the player requests segments without an auth header, so stamp the same
        # signed token onto every URI it will fetch from this playlist
        t = request.args.get("t", "")
        text = re.sub(r"(?m)^(seg_\d{1,10}\.m4s)$", lambda m: f"{m.group(1)}?t={t}", text)
        text = re.sub(r'(URI=")(init\.mp4)(")',
                      lambda m: f"{m.group(1)}{m.group(2)}?t={t}{m.group(3)}", text)
        resp = Response(text, mimetype="application/vnd.apple.mpegurl")
        resp.headers["Cache-Control"] = "no-store"
        return resp
    if not HLS_SEG_RE.match(seg) or not (d / seg).exists():
        return jsonify({"error": "not found"}), 404
    # In prod nginx serves the bytes; in dev (no X-Accel) Flask sends them itself.
    if os.environ.get("CAMERA_XACCEL", "1") == "1":
        resp = Response("")
        resp.headers["X-Accel-Redirect"] = f"/_hls/{cam_id}/{seg}"
        resp.headers["Content-Type"] = "video/mp4"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return send_from_directory(d, seg, conditional=True)


# --------------------------------------------------------------------------- #
# Web Push (VAPID) — lazy import so the app runs before push is configured
# --------------------------------------------------------------------------- #
def _vapid():
    # CAMERA_VAPID_PRIVATE may be a path to a PEM file (what gen-vapid writes) or
    # the PEM itself — pywebpush wants the PEM string, so resolve a path to text.
    priv = os.environ.get("CAMERA_VAPID_PRIVATE", "")
    if priv and os.path.exists(priv):
        try:
            priv = Path(priv).read_text()
        except OSError:
            pass
    return (
        os.environ.get("CAMERA_VAPID_PUBLIC", ""),
        priv,
        os.environ.get("CAMERA_VAPID_SUBJECT", "mailto:admin@example.com"),
    )


@app.route("/api/push/vapid_public")
@require_user
def api_push_vapid():
    return jsonify({"public_key": _vapid()[0]})


@app.route("/api/push/subscribe", methods=["POST"])
@require_user
def api_push_subscribe():
    sub = request.get_json(force=True, silent=True) or {}
    endpoint = sub.get("endpoint", "")
    keys = sub.get("keys", {}) or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"error": "bad subscription"}), 400
    with get_db() as con:
        con.execute(
            "INSERT INTO push_subs(endpoint,p256dh,auth,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth",
            (endpoint, keys["p256dh"], keys["auth"], int(time.time())),
        )
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
@require_user
def api_push_unsubscribe():
    endpoint = (request.get_json(force=True, silent=True) or {}).get("endpoint", "")
    with get_db() as con:
        con.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
    return jsonify({"ok": True})


@app.route("/api/push/test", methods=["POST"])
@require_user
def api_push_test():
    sent = send_push_all("Камеры", "Тестовое уведомление", {"test": True})
    return jsonify({"ok": True, "sent": sent})


def send_push_all(title, body, data=None):
    """Deliver a push to every stored subscription. Returns count delivered.

    Silently no-ops if VAPID isn't configured or pywebpush isn't installed, so
    the ingest path never fails just because push isn't set up yet.
    """
    pub, priv, subject = _vapid()
    if not (pub and priv):
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0
    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    sent, dead = 0, []
    with get_db() as con:
        subs = con.execute("SELECT * FROM push_subs").fetchall()
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=priv,
                vapid_claims={"sub": subject},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            # 404/410 → the subscription is gone; prune it
            if getattr(e, "response", None) is not None and e.response.status_code in (404, 410):
                dead.append(s["endpoint"])
        except Exception:
            pass
    if dead:
        with get_db() as con:
            con.executemany("DELETE FROM push_subs WHERE endpoint=?", [(e,) for e in dead])
    return sent


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv):
    cmd = argv[0] if argv else "run"
    if cmd == "gen-secret":
        print(secrets.token_urlsafe(48))
    elif cmd == "hash-password":
        import getpass
        pw = argv[1] if len(argv) > 1 else getpass.getpass("Password: ")
        print(hash_password(pw))
    elif cmd == "gen-vapid":
        # EC P-256 keypair for Web Push. Private key -> PEM file (pywebpush reads
        # it); public key -> base64url raw point (the browser's applicationServerKey).
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        pem_path = DATA_DIR / "vapid_private.pem"
        pem_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        pem_path.chmod(0o600)
        pub_raw = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        pub_b64 = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()
        print("CAMERA_VAPID_PUBLIC=" + pub_b64)
        print("CAMERA_VAPID_PRIVATE=" + str(pem_path))
        print(f"# private key saved to {pem_path}", file=sys.stderr)
    elif cmd == "init-db":
        init_db()
        print(f"db ready at {DB_PATH}")
    elif cmd == "retention":
        print(f"removed {enforce_retention()} clip(s)")
    elif cmd == "run":
        port = 8090
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        os.environ.setdefault("CAMERA_XACCEL", "0")   # dev: Flask serves media itself
        app.run(host="127.0.0.1", port=port, threaded=True, debug="--debug" in argv)
    else:
        sys.exit(f"unknown command: {cmd}")


init_db()   # ensure tables exist whether launched via gunicorn or the CLI

if __name__ == "__main__":
    _cli(sys.argv[1:])
