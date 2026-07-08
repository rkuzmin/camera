#!/usr/bin/env python3
"""Edge-side uploader: pushes finished recordings to the cloud backend.

Design goals:
  * durable    — a persisted set of already-uploaded clips survives restarts;
  * backfill   — on start it scans every recording and queues whatever the
                 server doesn't have yet (this is the "upload all my videos" bit);
  * idempotent — asks the server `have?` before sending, and the server dedups
                 by (cam_id, name) anyway;
  * gentle     — a single background worker uploads sequentially with backoff,
                 so it never saturates the home uplink or blocks recording;
  * no deps    — stdlib urllib only (the edge already avoids heavy deps).

Only "finished" clips are uploaded: a clip is considered done once its sidecar
`<clip>.json` exists (written when recording stops) or the file hasn't been
touched for a while — so an in-progress recording is never sent half-written.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

STATE_FILE = ".upload_state.json"
READY_AFTER_SECONDS = 30      # a sidecar-less file older than this is deemed done
RETRY_BASE = 5                # backoff base, seconds
RETRY_MAX = 300               # backoff ceiling, seconds
EVENT_THROTTLE = 30           # min seconds between detection events per camera (push)
CAMERA_SYNC_INTERVAL = 600    # re-assert the active camera set at least this often


class Uploader:
    def __init__(self, recordings_dir, config, camera_name=None, camera_list=None):
        # config: the shared app config dict. Reads keys:
        #   upload_enabled (bool), upload_url (str), upload_token (str),
        #   upload_scan_interval (int seconds, default 60)
        # camera_name: optional callable(cam_id) -> display name
        # camera_list: optional callable() -> [{"id","name"}, ...] of current cameras
        self.recordings_dir = Path(recordings_dir)
        self.config = config
        self.camera_name = camera_name or (lambda cid: cid)
        self.camera_list = camera_list or (lambda: [])
        self.state_path = self.recordings_dir / STATE_FILE

        self.lock = threading.Lock()
        self.queue = []                 # list of "cam_id/name" pending this run
        self.uploaded = self._load_state()   # set of "cam_id/name"
        self.inflight = None
        self.last_error = ""
        self.last_upload_ts = 0.0
        self.uploaded_count = len(self.uploaded)
        self._event_ts = {}      # cam_id -> last detection-event ts (throttle)
        self._cam_sig = None     # last-synced camera set signature (skip no-op posts)
        self._cam_sync_ts = 0.0  # when we last posted the camera set

        self._stop = False
        self._wake = threading.Event()
        self._thread = None

    # ---------- persistence ----------
    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text())
            return set(data.get("uploaded", []))
        except (OSError, ValueError):
            return set()

    def _save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"uploaded": sorted(self.uploaded)}))
            tmp.replace(self.state_path)
        except OSError:
            pass

    # ---------- config helpers ----------
    def _enabled(self):
        c = self.config
        return bool(c.get("upload_enabled") and c.get("upload_url") and c.get("upload_token"))

    def _base(self):
        return self.config.get("upload_url", "").rstrip("/")

    def _token(self):
        return self.config.get("upload_token", "")

    # ---------- public API ----------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def enqueue(self, cam_id, name):
        """Called when a clip finishes recording — upload it promptly."""
        key = f"{cam_id}/{name}"
        with self.lock:
            if key not in self.uploaded and key not in self.queue:
                self.queue.append(key)
        self._wake.set()

    def backfill(self):
        """Re-scan all recordings and queue anything not yet uploaded."""
        self._scan()
        self._wake.set()

    def status(self):
        with self.lock:
            return {
                "enabled": bool(self.config.get("upload_enabled")),
                "configured": self._enabled(),
                "url": self._base(),
                "pending": len(self.queue) + (1 if self.inflight else 0),
                "inflight": self.inflight,
                "uploaded_count": self.uploaded_count,
                "last_error": self.last_error,
                "last_upload_ts": int(self.last_upload_ts),
            }

    # ---------- worker ----------
    def _run(self):
        # small initial delay so the app finishes booting first
        self._wake.wait(timeout=5)
        while not self._stop:
            if not self._enabled():
                self._wake.wait(timeout=10)
                self._wake.clear()
                continue
            self.sync_cameras()   # keep the server's active camera set current
            self._scan()
            drained = self._drain()
            interval = int(self.config.get("upload_scan_interval", 60) or 60)
            # if we uploaded something, loop again promptly to keep draining
            self._wake.wait(timeout=1 if drained else interval)
            self._wake.clear()

    def _scan(self):
        """Queue every ready, not-yet-uploaded clip."""
        if not self.recordings_dir.exists():
            return
        with self.lock:
            uploaded = set(self.uploaded)
            queued = set(self.queue)
        for cam_dir in sorted(self.recordings_dir.iterdir()):
            if not cam_dir.is_dir():
                continue
            for path in sorted(cam_dir.glob("*.mp4")):
                key = f"{cam_dir.name}/{path.name}"
                if key in uploaded or key in queued:
                    continue
                if not self._is_ready(path):
                    continue
                with self.lock:
                    if key not in self.uploaded and key not in self.queue:
                        self.queue.append(key)
                        queued.add(key)

    def _is_ready(self, path):
        if path.with_suffix(".json").exists():
            return True
        try:
            return (time.time() - path.stat().st_mtime) > READY_AFTER_SECONDS
        except OSError:
            return False

    def _drain(self):
        uploaded_any = False
        fails = 0
        while not self._stop and self._enabled():
            with self.lock:
                if not self.queue:
                    break
                key = self.queue[0]
                self.inflight = key
            cam_id, _, name = key.partition("/")
            ok, err = self._upload_one(cam_id, name)
            with self.lock:
                self.inflight = None
                # remove this key from the queue regardless; on failure re-append
                if self.queue and self.queue[0] == key:
                    self.queue.pop(0)
                if ok:
                    self.uploaded.add(key)
                    self.uploaded_count = len(self.uploaded)
                    self.last_upload_ts = time.time()
                    self.last_error = ""
                    self._save_state()
                    uploaded_any = True
                    fails = 0
                else:
                    self.last_error = err
                    self.queue.append(key)   # retry later
                    fails += 1
            if not ok:
                # back off, but stay responsive to stop/wake
                delay = min(RETRY_MAX, RETRY_BASE * (2 ** min(fails, 6)))
                self._wake.wait(timeout=delay)
                self._wake.clear()
                if fails >= 3:
                    break   # give the periodic loop a turn; try again next scan
        return uploaded_any

    def _upload_one(self, cam_id, name):
        path = self.recordings_dir / cam_id / name
        if not path.exists():
            return True, ""   # vanished (deleted/rotated) — consider it handled
        base, token = self._base(), self._token()
        try:
            if self._server_has(base, token, cam_id, name):
                return True, ""
        except urllib.error.URLError as e:
            return False, f"have-check failed: {e.reason}"
        except Exception as e:  # noqa: BLE001 - never let the worker die
            return False, f"have-check error: {e}"

        tags, duration = self._read_meta(path)
        cam_name = self.camera_name(cam_id) or cam_id
        try:
            size = path.stat().st_size
        except OSError as e:
            return False, f"stat failed: {e}"
        headers = {
            "X-Device-Token": token,
            "X-Cam-Id": cam_id,
            "X-Cam-Name": quote(str(cam_name), safe=""),
            "X-Clip-Name": name,
            "X-Clip-Tags": quote(",".join(tags), safe=""),
            "X-Clip-Duration": str(duration),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        fh = None
        try:
            fh = open(path, "rb")
            req = urllib.request.Request(
                f"{base}/api/ingest/clip", data=fh, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=600) as r:
                if r.status == 200:
                    return True, ""
                return False, f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(200).decode("utf-8", "replace")
            except Exception:
                pass
            return False, f"HTTP {e.code}: {body}"
        except urllib.error.URLError as e:
            return False, f"network: {e.reason}"
        except Exception as e:  # noqa: BLE001
            return False, f"error: {e}"
        finally:
            if fh is not None:
                fh.close()

    def _server_has(self, base, token, cam_id, name):
        req = urllib.request.Request(
            f"{base}/api/ingest/have/{quote(cam_id)}/{quote(name)}",
            headers={"X-Device-Token": token})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return bool(data.get("have"))

    def _read_meta(self, path):
        """Read the sidecar <clip>.json -> (tags, duration_seconds)."""
        meta = path.with_suffix(".json")
        try:
            data = json.loads(meta.read_text())
            return list(data.get("tags", []) or []), float(data.get("duration", 0) or 0)
        except (OSError, ValueError):
            return [], 0.0

    # ---------- detection events (for push notifications) ----------
    def notify_event(self, cam_id, cam_name, label):
        """Throttled fire-and-forget: tell the backend a camera detected something."""
        if not self._enabled():
            return
        now = time.time()
        with self.lock:
            if now - self._event_ts.get(cam_id, 0) < EVENT_THROTTLE:
                return
            self._event_ts[cam_id] = now
        threading.Thread(target=self._post_event,
                         args=(cam_id, cam_name, label), daemon=True).start()

    def _post_event(self, cam_id, cam_name, label):
        try:
            body = json.dumps({
                "cam_id": cam_id, "cam_name": cam_name,
                "label": label, "ts": int(time.time()),
            }).encode()
            req = urllib.request.Request(
                f"{self._base()}/api/ingest/event", data=body, method="POST",
                headers={"X-Device-Token": self._token(), "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:  # noqa: BLE001 - push is best-effort, never disrupt detection
            pass

    # ---------- authoritative camera-set sync ----------
    def sync_cameras(self, force=False):
        """Tell the server which cameras currently exist so the PWA hides removed
        ones. Fire-and-forget; skips the post when nothing changed (unless forced
        or the periodic re-assert is due). Call with force=True right after the
        camera list changes (add/rename/remove)."""
        if not self._enabled():
            return
        try:
            cams = [{"id": c["id"], "name": c.get("name") or c["id"]}
                    for c in (self.camera_list() or []) if c.get("id")]
        except Exception:  # noqa: BLE001 - never let a bad callback break the worker
            return
        sig = json.dumps(sorted((c["id"], c["name"]) for c in cams))
        now = time.time()
        if not force and sig == self._cam_sig and (now - self._cam_sync_ts) < CAMERA_SYNC_INTERVAL:
            return
        self._cam_sig = sig
        self._cam_sync_ts = now
        threading.Thread(target=self._post_cameras, args=(cams,), daemon=True).start()

    def _post_cameras(self, cams):
        try:
            body = json.dumps({"cameras": cams}).encode()
            req = urllib.request.Request(
                f"{self._base()}/api/ingest/cameras", data=body, method="POST",
                headers={"X-Device-Token": self._token(), "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:  # noqa: BLE001 - best-effort; retried on the next loop
            self._cam_sig = None   # force a re-try next cycle

    def stop(self):
        self._stop = True
        self._wake.set()
