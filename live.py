#!/usr/bin/env python3
"""Edge-side live relay: push JPEG snapshots to the cloud for remote live view.

The PWA marks a camera as "wanted" on the backend (a heartbeat while someone is
watching). This relay polls what's wanted and, for each such camera, POSTs its
current annotated frame at a few fps. When nobody is watching, nothing is sent —
so it costs no bandwidth while idle. Stdlib urllib only.
"""
import json
import threading
import time
import urllib.error
import urllib.request

POLL_INTERVAL = 2.0      # how often to ask the backend what's wanted, seconds
FRAME_FPS = 3.0          # frames per second to push for a watched camera


class LiveRelay:
    def __init__(self, config, frame_getter):
        # config: shared app config (reads upload_url, upload_token)
        # frame_getter: callable(cam_id) -> latest annotated JPEG bytes (or None)
        self.config = config
        self.frame_getter = frame_getter
        self._stop = False
        self._thread = None

    # live view rides on the same backend/creds as upload; it works even if
    # archiving (upload_enabled) is off — viewing live doesn't require storing.
    def _enabled(self):
        c = self.config
        return bool(c.get("upload_url") and c.get("upload_token"))

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
                time.sleep(3)
                continue
            wanted = self._poll_wanted()
            if not wanted:
                time.sleep(POLL_INTERVAL)
                continue
            # stream frames for ~POLL_INTERVAL, then re-check who's still watching
            deadline = time.time() + POLL_INTERVAL
            interval = 1.0 / FRAME_FPS
            while time.time() < deadline and not self._stop:
                for cam_id in wanted:
                    self._push_frame(cam_id)
                time.sleep(interval)

    def _poll_wanted(self):
        try:
            req = urllib.request.Request(f"{self._base()}/api/live/wanted",
                                         headers={"X-Device-Token": self._token()})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data if isinstance(data, list) else []
        except (urllib.error.URLError, ValueError, OSError):
            return []

    def _push_frame(self, cam_id):
        try:
            jpeg = self.frame_getter(cam_id)
        except Exception:  # noqa: BLE001 - never let the relay thread die
            jpeg = None
        if not jpeg:
            return
        try:
            req = urllib.request.Request(
                f"{self._base()}/api/live/frame", data=jpeg, method="POST",
                headers={"X-Device-Token": self._token(), "X-Cam-Id": cam_id,
                         "Content-Type": "image/jpeg", "Content-Length": str(len(jpeg))})
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.URLError, OSError):
            pass

    def stop(self):
        self._stop = True
