#!/usr/bin/env python3
"""Edge-side HLS relay: a low-latency live stream exposed through the cloud.

Companion to live.py (the JPEG snapshot relay). Where the snapshot relay pushes one
annotated JPEG at a time, this relay remuxes the camera's existing H.264/H.265
bitstream into short fMP4 HLS segments (`ffmpeg -c copy` — no re-encoding, ~zero CPU)
and POSTs them to the backend, which serves them as an ordinary HLS playlist. The PWA
plays it in a <video> (native HLS on iOS, hls.js elsewhere), so glass-to-glass is a
few seconds instead of a snapshot slideshow.

Same demand signal as the snapshot relay: the PWA marks a camera "wanted" (a heartbeat
while someone is watching), this relay polls what's wanted and runs one ffmpeg per
watched camera; nothing runs while nobody is watching, so it costs no CPU/bandwidth
when idle. Stdlib urllib only.

The stream is the CLEAN camera feed — burning detection boxes in would need a full
re-encode. Boxes stay in the snapshot relay and the recorded clips.
"""
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

POLL_INTERVAL = 2.0       # how often to ask the backend what's wanted, seconds
STOP_GRACE = 6.0          # keep a producer alive this long after a camera stops being wanted
RESTART_COOLDOWN = 10.0   # after an ffmpeg dies, wait this long before retrying that camera
SEGMENT_SECONDS = 1       # target HLS segment duration
LIST_SIZE = 6             # segments kept in the live playlist
UPLOAD_POLL = 0.3         # how often the uploader checks the local playlist for changes

SEG_RE = re.compile(r"^(init\.mp4|seg_\d+\.m4s)$")


class _Producer:
    """One camera: an `ffmpeg -c copy` -> local fMP4 HLS dir, plus a thread that
    uploads new segments (then the playlist) to the backend."""

    def __init__(self, cam_id, source_url, out_dir, base, token, ffmpeg):
        self.cam_id = cam_id
        self.source_url = source_url
        self.dir = Path(out_dir)
        self.base = base
        self.token = token
        self.ffmpeg = ffmpeg
        self.proc = None
        self._stop = False
        self._uploaded = set()       # segment names already POSTed this run
        self._last_playlist = None   # last playlist text uploaded (skip no-op posts)
        self._thread = None

    def start(self):
        # start from a clean dir so a previous run's segments can't leak upstream
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        args = [self.ffmpeg, "-nostdin", "-loglevel", "error"]
        if isinstance(self.source_url, str) and self.source_url.lower().startswith("rtsp"):
            args += ["-rtsp_transport", "tcp"]
        args += [
            "-i", str(self.source_url),
            "-an", "-c:v", "copy",
            "-f", "hls",
            "-hls_time", str(SEGMENT_SECONDS),
            "-hls_list_size", str(LIST_SIZE),
            "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(self.dir / "seg_%d.m4s"),
            str(self.dir / "stream.m3u8"),
        ]
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            return
        self._thread = threading.Thread(target=self._upload_loop, daemon=True)
        self._thread.start()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # ---------- upload ----------
    def _upload_loop(self):
        playlist = self.dir / "stream.m3u8"
        while not self._stop:
            time.sleep(UPLOAD_POLL)
            try:
                text = playlist.read_text()
            except OSError:
                continue
            if text == self._last_playlist:
                continue
            # Upload every segment the playlist references that we haven't sent yet
            # (init.mp4 comes from the EXT-X-MAP line). ffmpeg only lists a segment
            # once it's fully written, so anything named here is safe to read whole.
            names = self._segments_in(text)
            if not all(self._ensure_uploaded(n) for n in names):
                continue   # a segment failed — retry the whole set next tick
            # Publish the playlist only after its segments are up, so the backend's
            # playlist never points at bytes it doesn't have yet.
            if self._put_playlist(text):
                self._last_playlist = text
                self._uploaded &= set(names) | {"init.mp4"}   # forget rotated-out names

    def _segments_in(self, text):
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-MAP:"):
                m = re.search(r'URI="([^"]+)"', line)
                if m and SEG_RE.match(m.group(1)):
                    out.append(m.group(1))
            elif line and not line.startswith("#") and SEG_RE.match(line):
                out.append(line)
        return out

    def _ensure_uploaded(self, name):
        if name in self._uploaded:
            return True
        try:
            data = (self.dir / name).read_bytes()
        except OSError:
            return False
        if not data:
            return False
        if self._post("/api/live/hls/segment", data, "application/octet-stream",
                      {"X-Seg-Name": name}):
            self._uploaded.add(name)
            return True
        return False

    def _put_playlist(self, text):
        return self._post("/api/live/hls/playlist", text.encode("utf-8"),
                          "application/vnd.apple.mpegurl")

    def _post(self, path, data, ctype, extra=None):
        headers = {"X-Device-Token": self.token, "X-Cam-Id": self.cam_id,
                   "Content-Type": ctype, "Content-Length": str(len(data))}
        if extra:
            headers.update(extra)
        try:
            req = urllib.request.Request(self.base + path, data=data,
                                         method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except (urllib.error.URLError, OSError):
            return False

    def stop(self):
        self._stop = True
        p = self.proc
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001 - never let a stuck ffmpeg wedge the relay
                try:
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(self.dir, ignore_errors=True)


class HlsRelay:
    def __init__(self, config, source_resolver, tmp_dir):
        # config: shared app config (reads upload_url, upload_token, hls_enabled)
        # source_resolver: callable(cam_id) -> capture URL string, or None to skip
        #                  (e.g. a local webcam has no encoded stream to remux)
        # tmp_dir: local scratch dir for per-camera HLS output
        self.config = config
        self.source_resolver = source_resolver
        self.tmp_dir = Path(tmp_dir)
        self.ffmpeg = shutil.which("ffmpeg")
        self._producers = {}   # cam_id -> _Producer
        self._drop_at = {}     # cam_id -> ts after which an unwanted producer is stopped
        self._cooldown = {}    # cam_id -> ts before which we won't (re)start a producer
        self._stop = False
        self._thread = None

    # Live view rides on the same backend/creds as upload, but works even when
    # archiving (upload_enabled) is off — viewing live doesn't require storing.
    def _enabled(self):
        c = self.config
        return bool(self.ffmpeg and c.get("hls_enabled", True)
                    and c.get("upload_url") and c.get("upload_token"))

    def _base(self):
        return self.config.get("upload_url", "").rstrip("/")

    def _token(self):
        return self.config.get("upload_token", "")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop:
            if not self._enabled():
                self._stop_all()
                time.sleep(3)
                continue
            self._reconcile(set(self._poll_wanted()))
            time.sleep(POLL_INTERVAL)
        self._stop_all()

    def _poll_wanted(self):
        try:
            req = urllib.request.Request(f"{self._base()}/api/live/wanted",
                                         headers={"X-Device-Token": self._token()})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data if isinstance(data, list) else []
        except (urllib.error.URLError, ValueError, OSError):
            return []

    def _resolve(self, cam_id):
        try:
            return self.source_resolver(cam_id)
        except Exception:  # noqa: BLE001 - a bad callback must not kill the relay
            return None

    def _reconcile(self, wanted):
        now = time.time()
        base, token = self._base(), self._token()
        # start (or keep) a producer for every wanted camera
        for cam_id in wanted:
            self._drop_at.pop(cam_id, None)
            prod = self._producers.get(cam_id)
            if prod is not None:
                if prod.alive():
                    continue
                prod.stop()                            # ffmpeg died — reap and cool down
                self._producers.pop(cam_id, None)
                self._cooldown[cam_id] = now + RESTART_COOLDOWN
                continue
            if now < self._cooldown.get(cam_id, 0):
                continue                               # recently failed — hold off
            src = self._resolve(cam_id)
            if not src:
                continue
            prod = _Producer(cam_id, src, self.tmp_dir / cam_id, base, token, self.ffmpeg)
            prod.start()
            if prod.proc is not None:
                self._producers[cam_id] = prod
            else:
                self._cooldown[cam_id] = now + RESTART_COOLDOWN
        # stop producers no longer wanted, after a short grace period (avoids flapping
        # when a viewer briefly navigates away and comes back)
        for cam_id, prod in list(self._producers.items()):
            if cam_id in wanted:
                continue
            drop = self._drop_at.get(cam_id)
            if drop is None:
                self._drop_at[cam_id] = now + STOP_GRACE
            elif now >= drop:
                prod.stop()
                self._producers.pop(cam_id, None)
                self._drop_at.pop(cam_id, None)

    def _stop_all(self):
        for prod in list(self._producers.values()):
            prod.stop()
        self._producers.clear()
        self._drop_at.clear()

    def stop(self):
        self._stop = True
