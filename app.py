#!/usr/bin/env python3
"""View IP cameras (one or several) + recording on object detection (YOLO).

Run:  .venv/bin/python app.py
Interface: http://<IP-of-this-machine>:8080 (accessible from any device on the network)
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# RTSP over TCP — more reliable, fewer artifacts
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from uploader import Uploader
from live import LiveRelay
from hls import HlsRelay

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
RECORDINGS_DIR = BASE_DIR / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

FFMPEG = shutil.which("ffmpeg")   # None -> fall back to cv2.VideoWriter(mp4v)


class FFmpegWriter:
    """Writes annotated BGR frames to H.264/mp4 via an ffmpeg subprocess.

    Replaces cv2.VideoWriter's outdated mp4v codec (MPEG-4 ASP): H.264 gives a
    noticeably smaller file size with better quality and browser compatibility.
    Frames (with detection boxes and the REC marker drawn in) are fed to stdin
    as rawvideo.
    """

    def __init__(self, path, width, height, fps):
        self.path = path
        # yuv420p + even dimensions are required for H.264 and playback everywhere
        self.proc = subprocess.Popen(
            [
                FFMPEG, "-y", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", f"{max(1.0, float(fps)):.3f}",
                "-i", "pipe:0",
                "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(path),
            ],
            stdin=subprocess.PIPE,
        )

    def isOpened(self):
        return self.proc.poll() is None

    def write(self, frame):
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, ValueError, OSError):
            pass

    def release(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def open_video_writer(path, width, height, fps):
    """H.264 via ffmpeg if available; otherwise the legacy mp4v via OpenCV."""
    if FFMPEG:
        w = FFmpegWriter(path, width, height, fps)
        if w.isOpened():
            return w
    return cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))


class StreamCopyRecorder:
    """Writes an RTSP/HTTP stream straight to mp4 with no re-encoding (ffmpeg -c copy).

    Maximum quality and minimum CPU (the camera already outputs H.264/H.265 —
    we just remux it into a container), and the audio track is preserved. The
    trade-off is no overlaid boxes/REC marker and no pre-buffer: ffmpeg opens
    its own connection to the camera and starts writing right from that moment.
    """

    def __init__(self, source, path):
        self.path = path
        args = [FFMPEG, "-y", "-loglevel", "error"]
        if isinstance(source, str) and source.lower().startswith("rtsp"):
            args += ["-rtsp_transport", "tcp"]
        args += ["-i", str(source), "-c", "copy",
                 "-movflags", "+faststart", str(path)]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE)

    def isOpened(self):
        return self.proc.poll() is None

    def release(self):
        # 'q' on stdin asks ffmpeg to finalize the container (moov atom) cleanly;
        # otherwise a file left behind by kill() has no index and won't open
        try:
            self.proc.communicate(b"q", timeout=10)
        except Exception:
            self.proc.kill()


DEFAULT_CONFIG = {
    "cameras": [],                 # [{"id": "...", "name": "...", "source": "rtsp://..."}]
    "auto_record": True,           # auto-record on detection (shared setting for all cameras)
    "detect_classes": ["person", "car", "dog", "cat", "truck", "bus", "motorcycle", "bicycle"],
    "confidence": 0.45,           # detector confidence threshold
    "detect_interval": 0.7,       # how often to run YOLO, sec (CPU)
    "detect_on_motion": True,     # run YOLO only when motion is seen — saves a lot of CPU
    "motion_threshold": 0.5,      # % of the frame that must change to count as motion
    # only auto-record objects that are actually MOVING (a parked car is detected but
    # not recorded); kills clips triggered by static cars + a bug waking the motion gate
    "require_object_motion": True,
    "object_motion_threshold": 2.0,  # % of an object's box that must change frame-to-frame
    # a moving object must persist across this many consecutive detection passes before
    # recording starts — a single-pass rain streak / IR flicker / bug that clips an
    # object's box for one frame is ignored. The pre-buffer still backfills the seconds
    # before the trigger, so a genuinely moving object loses no footage. 1 = off.
    "trigger_min_passes": 2,
    "pre_seconds": 5,             # how many seconds BEFORE the event are included in the clip
    "post_seconds": 10,           # how long to keep recording after the last detection
    "max_clip_minutes": 10,       # auto-split long recordings
    # recording mode: "annotated" - H.264 with boxes/REC and pre-buffer (re-encoded);
    #                 "copy" - raw stream with no re-encoding, with audio, no boxes
    "record_mode": "annotated",
    # viewing: "mjpeg" - as before; "webrtc" - low-latency WebRTC via go2rtc
    # (only takes effect if the go2rtc binary is found)
    "view_mode": "mjpeg",
    # relay a low-latency HLS live stream through the cloud backend (ffmpeg -c copy,
    # only while a PWA viewer is watching). Needs ffmpeg + upload_url/upload_token.
    "hls_enabled": True,
    # pull detector frames from the local go2rtc restream instead of the camera
    # directly - then the physical camera gets a single connection for both
    # viewing and detection
    "capture_via_go2rtc": False,
    # --- cloud upload (edge -> VPS backend) ---
    "upload_enabled": False,       # push finished clips to the cloud backend
    "upload_url": "",              # e.g. https://your-domain.example
    "upload_token": "",            # device token from the backend .env
    "upload_scan_interval": 60,    # how often to re-scan for un-uploaded clips, sec
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        raw = CONFIG_PATH.read_text()
        try:
            user = json.loads(raw)
        except ValueError as e:
            # A hand-edited config with a JSON typo must NOT silently fall back to
            # defaults: the next save_config() would then overwrite (wipe) the file.
            # Preserve it, back it up, and refuse to start so it can be fixed.
            backup = CONFIG_PATH.with_name("config.invalid.json")
            try:
                backup.write_text(raw)
            except OSError:
                pass
            raise SystemExit(
                f"\n[config] {CONFIG_PATH.name} is not valid JSON: {e}\n"
                f"[config] It was left untouched (a copy is at {backup.name}).\n"
                f"[config] Fix the JSON and start again.\n")
        if isinstance(user, dict):
            cfg.update(user)
    # migration from the old single-camera format (top-level "source" key)
    old_source = cfg.pop("source", None)
    if old_source and not cfg.get("cameras"):
        cfg["cameras"] = [{"id": "cam1", "name": "Camera 1", "source": old_source}]
    if not cfg.get("cameras"):
        cfg["cameras"] = []
    return cfg


def save_config(cfg):
    # atomic write: an interrupted save can't truncate/corrupt config.json
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    tmp.replace(CONFIG_PATH)


def _config_changed_on_disk(cfg):
    """True if cfg differs from what's already in config.json (so a save is worth doing)."""
    try:
        return json.loads(CONFIG_PATH.read_text()) != cfg
    except (OSError, ValueError):
        return True


def migrate_legacy_recordings(cfg):
    """Old recordings sat directly in recordings/*.mp4 — move them into the first camera's folder."""
    loose = list(RECORDINGS_DIR.glob("*.mp4"))
    if not loose:
        return
    target_id = cfg["cameras"][0]["id"] if cfg["cameras"] else "cam1"
    target_dir = RECORDINGS_DIR / target_id
    target_dir.mkdir(parents=True, exist_ok=True)
    for p in loose:
        p.rename(target_dir / p.name)


# ---------------------------------------------------------------------------
# DVRIP (Sofia / XMEye) — change network settings of cheap DVR/NVR/IP cameras
# (Besder, XMEye, iCSee and kin on Xiongmai firmware). Port 34567, its own
# binary protocol: 20-byte header + JSON. Stdlib only, no dependencies.
# ---------------------------------------------------------------------------

DVRIP_ERRORS = {
    100: "OK",
    101: "unknown camera error",
    102: "protocol version not supported",
    103: "invalid configuration",
    104: "invalid username",
    106: "no permission",
    202: "user not authorized",
    203: "incorrect login or password",
    204: "account is locked",
    205: "account is blacklisted",
    511: "update error",
    512: "update successful",
}


def _dvrip_error(ret):
    return DVRIP_ERRORS.get(ret, f"error code {ret}")


def _valid_ipv4(s):
    try:
        return isinstance(s, str) and str(ipaddress.ip_address(s.strip())).count(".") == 3
    except ValueError:
        return False


def ip_to_sofia_hex(ip):
    """192.168.0.10 -> '0x0A00A8C0' (little-endian byte order, as Sofia stores it)."""
    a, b, c, d = (int(x) for x in ip.split("."))
    return "0x%02X%02X%02X%02X" % (d, c, b, a)


def sofia_hex_to_ip(h):
    v = int(h, 16)
    return ".".join(str((v >> s) & 0xFF) for s in (0, 8, 16, 24))


class DVRIPClient:
    """Minimal DVRIP protocol client: login, read and write NetWork.NetCommon."""

    LOGIN_REQ = 1000
    CONFIG_SET = 1040
    CONFIG_GET = 1042
    CMD_TIME_SET = 1450      # OPTimeSetting
    CMD_TIME_GET = 1452      # OPTimeQuery
    TIME_FMT = "%Y-%m-%d %H:%M:%S"
    _HEADER = "<BB2xII2xHI"   # flag, version, session(LE u32), seq(LE u32), msgid(LE u16), len(LE u32)

    def __init__(self, host, port=34567, user="admin", password="", timeout=5):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.timeout = timeout
        self.sock = None
        self.session = 0
        self.session_hex = "0x00000000"
        self.seq = 0

    @staticmethod
    def sofia_hash(password):
        """XM's proprietary password hash: md5, then each byte pair is folded into [0-9A-Za-z]."""
        out = ""
        digest = hashlib.md5(password.encode()).digest()
        for i in range(8):
            n = (digest[2 * i] + digest[2 * i + 1]) % 62
            if n > 9:
                n += 7 if n < 36 else 13
            out += chr(48 + n)
        return out

    def __enter__(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        return self

    def __exit__(self, *exc):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed by camera")
            buf += chunk
        return buf

    def _send(self, msgid, payload):
        data = json.dumps(payload).encode() + b"\x0a\x00"
        header = struct.pack(self._HEADER, 0xFF, 0x00, self.session, self.seq, msgid, len(data))
        self.sock.sendall(header + data)
        self.seq += 1

    def _recv(self):
        _, _, session, _, _, length = struct.unpack(self._HEADER, self._recv_exact(20))
        body = self._recv_exact(length)
        self.session = session
        text = body.rstrip(b"\x00\x0a").decode("utf-8", "replace").strip()
        return json.loads(text) if text else {}

    def request(self, msgid, payload):
        self._send(msgid, payload)
        return self._recv()

    def login(self):
        resp = self.request(self.LOGIN_REQ, {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "UserName": self.user,
            "PassWord": self.sofia_hash(self.password),
        })
        if resp.get("Ret") not in (100, 515):
            raise RuntimeError(_dvrip_error(resp.get("Ret")))
        self.session_hex = resp["SessionID"]
        self.session = int(resp["SessionID"], 16)
        return resp

    # ---- generic operations ----
    def get_config(self, name):
        resp = self.request(self.CONFIG_GET, {"Name": name, "SessionID": self.session_hex})
        if resp.get("Ret") not in (100,):
            raise RuntimeError(_dvrip_error(resp.get("Ret")))
        return resp.get(name)

    def set_config(self, name, value):
        resp = self.request(self.CONFIG_SET, {"Name": name, name: value, "SessionID": self.session_hex})
        if resp.get("Ret") not in (100,):
            raise RuntimeError(_dvrip_error(resp.get("Ret")))
        return resp

    def command(self, name, code, data=None):
        payload = {"Name": name, "SessionID": self.session_hex}
        if data is not None:
            payload[name] = data
        resp = self.request(code, payload)
        if resp.get("Ret") not in (100,):
            raise RuntimeError(_dvrip_error(resp.get("Ret")))
        return resp

    # ---- network ----
    def get_network(self):
        return self.get_config("NetWork.NetCommon")

    def set_network(self, net):
        self._send(self.CONFIG_SET, {
            "Name": "NetWork.NetCommon",
            "NetWork.NetCommon": net,
            "SessionID": self.session_hex,
        })
        # after accepting the new address, the camera immediately reconfigures the network
        # and drops the current TCP connection — we may not receive a reply, and that's NOT an error
        try:
            return self._recv()
        except (socket.timeout, OSError):
            return {"Ret": 100, "note": "camera changed address before replying"}

    # ---- time ----
    def get_time(self):
        return self.command("OPTimeQuery", self.CMD_TIME_GET).get("OPTimeQuery", "")

    def set_time(self, dt_str):
        return self.command("OPTimeSetting", self.CMD_TIME_SET, dt_str)

    # ---- overlay (channel title) on top of the video ----
    def set_channel_title_visible(self, show, text=None):
        """Show/hide the channel title overlay (that "AI CAM" label) in the stream.

        EncodeBlend controls the overlay in the encoded (RTSP) stream,
        PreviewBlend — in the preview. Both False → the label disappears from the recording too.
        """
        if text is not None:
            try:  # channel rename — the structure differs on some firmwares
                self.set_config("ChannelTitle", [text])
            except (RuntimeError, KeyError, ValueError):
                pass
        widget = self.get_config("AVEnc.VideoWidget")
        items = widget if isinstance(widget, list) else [widget]
        changed = 0
        for item in items:
            ct = item.get("ChannelTitle") if isinstance(item, dict) else None
            if isinstance(ct, dict):
                ct["EncodeBlend"] = bool(show)
                ct["PreviewBlend"] = bool(show)
                changed += 1
        self.set_config("AVEnc.VideoWidget", widget)
        return changed


_yolo_model = None
_yolo_lock = threading.Lock()
_go2rtc = None   # Go2rtcManager, assigned at startup; None -> capture straight from the camera
_uploader = None  # Uploader, assigned at startup; None -> no cloud upload


def capture_url_for(cam_id, source, shared_cfg):
    """URL to capture from: the local go2rtc restream (single connection to the camera) or the source itself."""
    if (shared_cfg.get("capture_via_go2rtc") and _go2rtc is not None
            and _go2rtc.is_running() and _go2rtc.has_stream(cam_id)):
        return _go2rtc.restream_url(cam_id)
    return source


def get_yolo_model():
    """One model for the whole app — a shared detector across cameras saves memory and CPU."""
    global _yolo_model
    if _yolo_model is None:
        with _yolo_lock:
            if _yolo_model is None:
                from ultralytics import YOLO
                _yolo_model = YOLO(str(BASE_DIR / "yolov8n.pt"))
    return _yolo_model


class Camera:
    """Reads one camera's stream, keeps the latest frame and pre-buffer, and writes video."""

    def __init__(self, cam_id, name, source, shared_cfg):
        self.id = cam_id
        self.name = name
        self.source = source
        self.shared_cfg = shared_cfg   # shared detection settings — reference to the global config

        self.recordings_dir = RECORDINGS_DIR / cam_id
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        self.lock = threading.Lock()
        self.jpeg = None              # latest frame (JPEG bytes) with boxes drawn
        self.raw_frame = None         # latest raw frame for the detector
        self.connected = False
        self.fps = 15.0
        self.error = ""

        self.prebuffer = deque()      # (ts, jpeg_bytes) — for pre-recording
        self.detections = []          # [{label, conf, box}], recent boxes
        self.detections_ts = 0.0
        self.last_trigger_ts = 0.0    # last detection of a wanted class
        self._trigger_streak = 0      # consecutive detect passes with a moving wanted object

        self._prev_gray = None        # previous downscaled frame for motion diff
        self._motion_mask = None      # per-pixel motion mask (160x90) — for per-object motion
        self._motion_frac = 0.0       # whole-frame changed fraction (ambient level) — rain/snow rejection
        self.last_motion_ts = 0.0     # last time motion crossed the threshold
        self.motion = False

        self.recording = False
        self.manual_record = False
        self.writer = None
        self.copy_recorder = None      # ffmpeg -c copy for "copy" mode
        self.record_mode = "annotated"
        self.record_path = None
        self.record_started = 0.0
        self.record_tags = set()       # object classes seen during the current clip (for tagging)
        self.frame_size = None

        # live-view demand: count of local MJPEG viewers plus the time of the last
        # cloud-relay pull. The encoder thread produces preview JPEGs only when
        # someone is actually watching (locally or via the PWA relay).
        self._viewers = 0
        self._remote_view_ts = 0.0
        # the capture thread sets this to hand a fresh frame to the encoder thread
        self._frame_ready = threading.Event()

        self._stop = False
        self._reopen = False
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._encode_loop, daemon=True).start()
        threading.Thread(target=self._detect_loop, daemon=True).start()

    # ---------- capture ----------
    def _open(self):
        src = capture_url_for(self.id, self.source, self.shared_cfg)
        if not src:
            return None
        if isinstance(src, str) and src.isdigit():
            src = int(src)
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG if isinstance(src, str) else cv2.CAP_ANY)
        # timeouts so a dead/wrong RTSP address doesn't hang the thread forever
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # minimal latency, fresh frame
        if not cap.isOpened():
            cap.release()
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
        if 1 <= fps <= 60:
            self.fps = fps
        return cap

    def _capture_loop(self):
        cap = None
        while not self._stop:
            if self._reopen and cap is not None:
                self._reopen = False
                cap.release()
                cap = None
                self._stop_recording()
            if cap is None:
                cap = self._open()
                if cap is None:
                    self.connected = False
                    self.error = "No connection to camera" if self.source else "Camera address not set"
                    time.sleep(2)
                    continue
                self.connected = True
                self.error = ""
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self.connected = False
                self._stop_recording()
                continue
            self._on_captured_frame(frame)
        if cap:
            cap.release()

    def _update_motion(self, frame, now):
        """Cheap frame-difference motion gate on a tiny grayscale image (~sub-ms).

        Runs on every captured frame and lets _detect_loop skip the expensive YOLO
        pass while the scene is static. Sets self.motion / self.last_motion_ts.
        """
        small = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(frame, (160, 90)),
                                              cv2.COLOR_BGR2GRAY), (5, 5), 0)
        prev, self._prev_gray = self._prev_gray, small
        if prev is None:
            return
        moved = cv2.absdiff(prev, small) > 20   # bool mask on the 160x90 grid
        self._motion_mask = moved
        changed = np.count_nonzero(moved) / small.size
        self._motion_frac = changed             # ambient motion level, read by _box_is_moving
        if changed * 100.0 >= float(self.shared_cfg.get("motion_threshold", 0.5)):
            self.last_motion_ts = now
            self.motion = True
        elif now - self.last_motion_ts > 1.0:
            self.motion = False

    def _box_is_moving(self, box, frame_shape):
        """True if the object's box changed markedly MORE than the rest of the frame.

        Filters out statically-present objects (parked cars): their boxes show almost
        no frame-to-frame change, so they never trigger recording — while a car driving
        past or a person walking does. A bug near the lens moves pixels in the grass,
        not inside a wanted object's box, so it doesn't trigger either.

        The box change is measured in EXCESS of the whole-frame ("ambient") motion.
        Rain, snow and night IR-sensor flicker change pixels UNIFORMLY across the whole
        picture, so a parked car's box moves no more than that ambient floor and nets
        out to ~0 — no false recording — while a genuinely moving object still stands
        out well above it. This is what stops the camera from recording all through rain.
        """
        mask = self._motion_mask
        if mask is None:
            return False
        mh, mw = mask.shape
        fh, fw = frame_shape[:2]
        x1, y1, x2, y2 = box
        bx1, bx2 = max(0, int(x1 * mw / fw)), min(mw, int(x2 * mw / fw))
        by1, by2 = max(0, int(y1 * mh / fh)), min(mh, int(y2 * mh / fh))
        if bx2 <= bx1 or by2 <= by1:
            return False
        region = mask[by1:by2, bx1:bx2]
        frac = np.count_nonzero(region) / region.size
        excess = frac - self._motion_frac   # box motion above the ambient (rain/snow) floor
        return excess * 100.0 >= float(self.shared_cfg.get("object_motion_threshold", 2.0))

    def _on_captured_frame(self, frame):
        """Capture-thread hot path — kept deliberately light so cap.read() runs again
        immediately and the RTSP stream never backs up (a slow consumer is exactly what
        causes the jitter/freezes). All JPEG encoding is handed off to the encoder thread;
        only cheap motion detection and the recording write stay here."""
        now = time.time()
        with self.lock:
            self.raw_frame = frame
            self.frame_size = (frame.shape[1], frame.shape[0])
        self._update_motion(frame, now)
        # record the CLEAN frame here, at full capture rate, so the file stays smooth
        # regardless of how fast the encoder thread is keeping up (no boxes/REC burned in)
        self._recording_tick(now, frame)
        # hand the frame to the encoder thread for the preview / pre-buffer JPEG
        self._frame_ready.set()

    def _wants_view(self, now):
        """True when someone is watching this camera: a local MJPEG viewer, or the
        cloud relay pulled a frame within the last few seconds (a remote PWA viewer)."""
        return self._viewers > 0 or (now - self._remote_view_ts) < 5.0

    def add_viewer(self):
        with self.lock:
            self._viewers += 1

    def remove_viewer(self):
        with self.lock:
            if self._viewers > 0:
                self._viewers -= 1

    def mark_remote_view(self):
        """Registers remote interest when the cloud relay pulls a frame, so the encoder
        keeps producing preview frames for a PWA viewer with no local viewer present."""
        self._remote_view_ts = time.time()

    def _encode_loop(self):
        """Encodes the latest raw frame into the preview JPEG (self.jpeg) and the
        pre-buffer, off the capture thread. Runs at most at camera frame-rate and just
        drops frames when it can't keep up — so under CPU load the live view loses fps
        instead of the whole stream freezing. Skips all JPEG work while nobody is
        watching and nothing needs the pre-buffer."""
        last = None
        while not self._stop:
            self._frame_ready.wait(timeout=0.5)
            self._frame_ready.clear()
            with self.lock:
                frame = self.raw_frame
            if frame is None or frame is last:
                continue
            last = frame
            now = time.time()
            wants_view = self._wants_view(now)
            # the pre-buffer is only worth maintaining when a recording could start:
            # auto-record armed, already recording, or a viewer who might hit record
            need_prebuffer = bool(self.shared_cfg.get("auto_record")) or self.recording
            if not (wants_view or need_prebuffer):
                continue   # nobody watching, nothing to record -> no JPEG work at all

            # Clean frame (raw camera image, no overlays) — used for the pre-buffer /
            # recording, and for the live view too when there's nothing to draw on top.
            ok, raw_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                continue
            raw_jpeg = raw_buf.tobytes()

            # Annotated copy (detection boxes + REC marker) — only when actually watched
            # and there's something to overlay; otherwise reuse the clean JPEG and skip
            # the second full-resolution encode.
            if wants_view:
                has_boxes = now - self.detections_ts < 2.0 and self.detections
                if has_boxes or self.recording:
                    shown = frame.copy()
                    if has_boxes:
                        for d in self.detections:
                            x1, y1, x2, y2 = d["box"]
                            cv2.rectangle(shown, (x1, y1), (x2, y2), (0, 80, 255), 2)
                            cv2.putText(shown, f'{d["label"]} {d["conf"]:.2f}', (x1, max(20, y1 - 8)),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 80, 255), 2)
                    if self.recording:
                        cv2.circle(shown, (25, 25), 10, (0, 0, 255), -1)
                        cv2.putText(shown, "REC", (42, 33), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                    ok, buf = cv2.imencode(".jpg", shown, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    view_jpeg = buf.tobytes() if ok else raw_jpeg
                else:
                    view_jpeg = raw_jpeg
                with self.lock:
                    self.jpeg = view_jpeg

            # pre-buffer holds CLEAN frames, so the pre-event seconds are box-free too
            with self.lock:
                self.prebuffer.append((now, raw_jpeg))
                horizon = now - self.shared_cfg["pre_seconds"]
                while self.prebuffer and self.prebuffer[0][0] < horizon:
                    self.prebuffer.popleft()

    # ---------- recording ----------
    def _recording_tick(self, now, frame):
        auto_active = (
            self.shared_cfg["auto_record"]
            and self.last_trigger_ts > 0
            and now - self.last_trigger_ts < self.shared_cfg["post_seconds"]
        )
        should_record = self.manual_record or auto_active
        if should_record and not self.recording:
            self._start_recording()
        elif not should_record and self.recording:
            self._stop_recording()
        if not self.recording:
            return
        # remember which object classes appeared during this clip (for tagging)
        if now - self.detections_ts < 2.0:
            for d in self.detections:
                self.record_tags.add(d["label"])
        rotate = now - self.record_started > self.shared_cfg["max_clip_minutes"] * 60
        if self.record_mode == "copy":
            # ffmpeg writes on its own; we only watch that the process is alive and rotation
            if self.copy_recorder is not None and not self.copy_recorder.isOpened():
                self._stop_recording()  # ffmpeg died (stream drop) — restarts on the next frame
            elif rotate:
                self._stop_recording()
        elif self.writer is not None:
            self.writer.write(frame)
            if rotate:
                self._stop_recording()  # the next frame will open a new file

    def _start_recording(self):
        mode = self.shared_cfg.get("record_mode", "annotated")
        self.record_tags = set()   # fresh tag set for this clip
        name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
        path = self.recordings_dir / name
        # copy mode - only for network streams and when ffmpeg is available
        # (re-encoding is unavoidable for a webcam/local index)
        if mode == "copy" and FFMPEG and isinstance(self.source, str) and not self.source.isdigit():
            rec = StreamCopyRecorder(self.source, path)
            if not rec.isOpened():
                self.error = "Failed to start stream recording (ffmpeg)"
                return
            self.copy_recorder = rec
            self.record_mode = "copy"
            self.record_path = path
            self.record_started = time.time()
            self.recording = True
            return

        self.record_mode = "annotated"
        w, h = self.frame_size
        writer = open_video_writer(path, w, h, self.fps)
        if not writer.isOpened():
            self.error = "Failed to open VideoWriter"
            return
        # flush the pre-buffer to capture the seconds before the event
        # (snapshot under the lock — the encoder thread appends to it concurrently)
        with self.lock:
            prebuffered = list(self.prebuffer)
        for _, jpeg in prebuffered:
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                writer.write(frame)
        self.writer = writer
        self.record_path = path
        self.record_started = time.time()
        self.recording = True

    def _stop_recording(self):
        was_recording = self.recording
        path = self.record_path
        started = self.record_started
        if self.writer is not None:
            self.writer.release()
        self.writer = None
        if self.copy_recorder is not None:
            self.copy_recorder.release()
        self.copy_recorder = None
        self.recording = False
        self.record_path = None
        if was_recording and path is not None:
            self._write_clip_metadata(path, started)
            if _uploader is not None:
                _uploader.enqueue(self.id, path.name)

    def _write_clip_metadata(self, path, started=None):
        """Sidecar <clip>.json: object classes detected during the clip, plus timing
        (start epoch + duration) so the UI can lay clips out on a DVR timeline."""
        meta = {"tags": sorted(self.record_tags)}
        if started:
            meta["started_at"] = int(started)
            meta["duration"] = round(max(0.0, time.time() - started), 1)
        try:
            path.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False))
        except OSError:
            pass

    # ---------- detection ----------
    def _detect_loop(self):
        while not self._stop:
            time.sleep(max(0.1, float(self.shared_cfg["detect_interval"])))
            # motion gate: skip the costly YOLO pass while the scene is static
            # (plus a ~1s grace after the last motion so brief pauses don't drop it)
            if (self.shared_cfg.get("detect_on_motion", True)
                    and time.time() - self.last_motion_ts > 1.0):
                self._trigger_streak = 0   # scene went static — break the persistence run
                continue
            with self.lock:
                frame = self.raw_frame
            if frame is None:
                continue
            model = get_yolo_model()
            names = model.names
            wanted = set(self.shared_cfg["detect_classes"])
            wanted_ids = {i for i, n in names.items() if n in wanted}
            try:
                # shared model across all cameras — serialize inference to avoid
                # a race condition inside ultralytics on parallel calls
                with _yolo_lock:
                    results = model.predict(frame, conf=float(self.shared_cfg["confidence"]),
                                            imgsz=640, verbose=False)
            except Exception as e:
                self.error = f"Detector error: {e}"
                continue
            require_motion = self.shared_cfg.get("require_object_motion", True)
            dets = []
            qualifying = False    # a wanted object that is moving on THIS pass
            trigger_label = ""
            for box in results[0].boxes:
                cls = int(box.cls[0])
                label = names[cls]
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                dets.append({"label": label, "conf": float(box.conf[0]), "box": [x1, y1, x2, y2]})
                # a wanted object triggers recording only if it's actually moving
                # (unless the user turned that off) — parked cars stay unrecorded
                if cls in wanted_ids and (not require_motion
                                          or self._box_is_moving([x1, y1, x2, y2], frame.shape)):
                    qualifying = True
                    if not trigger_label:
                        trigger_label = label
            self.detections = dets
            self.detections_ts = time.time()
            # require the movement to PERSIST across consecutive passes: a real object
            # keeps moving, but a momentary rain streak / IR flicker / bug that clips a
            # box for a single frame does not — so it never starts a recording
            self._trigger_streak = self._trigger_streak + 1 if qualifying else 0
            min_passes = max(1, int(self.shared_cfg.get("trigger_min_passes", 2)))
            if qualifying and self._trigger_streak >= min_passes:
                self.last_trigger_ts = time.time()
                if _uploader is not None:
                    _uploader.notify_event(self.id, self.name, trigger_label)

    # ---------- control ----------
    def update_source(self, source):
        self.source = source
        self.restart_capture()

    def restart_capture(self):
        """After an address change, reconnect to the new source."""
        self._reopen = True
        self.connected = False

    def stop(self):
        self._stop = True

    def status(self):
        return {
            "id": self.id,
            "name": self.name,
            "source_set": bool(self.source),
            "connected": self.connected,
            "recording": self.recording,
            "manual_record": self.manual_record,
            "error": self.error,
            "fps": round(self.fps, 1),
            "motion": self.motion,
            "detections": [
                {"label": d["label"], "conf": round(d["conf"], 2)} for d in self.detections
            ] if time.time() - self.detections_ts < 2.5 else [],
        }


class CameraManager:
    """Holds all configured cameras and keeps them in sync with config['cameras']."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.cameras = {}
        for cam_cfg in config["cameras"]:
            cam_id = cam_cfg.get("id") or ("cam_" + secrets.token_hex(3))
            cam_cfg["id"] = cam_id
            name = cam_cfg.get("name") or cam_id
            source = cam_cfg.get("source", "")
            self.cameras[cam_id] = Camera(cam_id, name, source, config)

    def all(self):
        with self.lock:
            return sorted(self.cameras.values(), key=lambda c: c.id)

    def get(self, cam_id):
        with self.lock:
            return self.cameras.get(cam_id)

    def existing_sources(self):
        with self.lock:
            return [c.source for c in self.cameras.values()]

    def add(self, name, source):
        cam_id = "cam_" + secrets.token_hex(3)
        with self.lock:
            self.config["cameras"].append({"id": cam_id, "name": name, "source": source})
            save_config(self.config)
            cam = Camera(cam_id, name, source, self.config)
            self.cameras[cam_id] = cam
        return cam

    def update(self, cam_id, name=None, source=None):
        with self.lock:
            cam = self.cameras.get(cam_id)
            if not cam:
                return None
            for entry in self.config["cameras"]:
                if entry["id"] == cam_id:
                    if name is not None:
                        entry["name"] = name
                    if source is not None:
                        entry["source"] = source
                    break
            save_config(self.config)
            if name is not None:
                cam.name = name
            if source is not None and source != cam.source:
                cam.update_source(source)
        return cam

    def remove(self, cam_id):
        with self.lock:
            cam = self.cameras.pop(cam_id, None)
            if not cam:
                return False
            cam.stop()
            self.config["cameras"] = [c for c in self.config["cameras"] if c["id"] != cam_id]
            save_config(self.config)
        return True


class OnvifClient:
    """Minimal stdlib-only ONVIF client: asks the camera for its real RTSP URL.

    Instead of guessing the path (Streaming/Channels/101 and the like), it does
    what ONVIF exists for: GetProfiles -> GetStreamUri. Authentication is
    WS-Security UsernameToken with PasswordDigest = base64(sha1(nonce + created + password)).
    """

    DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
    MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
    SCHEMA_NS = "http://www.onvif.org/ver10/schema"

    def __init__(self, device_xaddr, user, password, timeout=4.0):
        self.device_xaddr = device_xaddr
        self.user = user
        self.password = password
        self.timeout = timeout

    def _security_header(self):
        nonce = os.urandom(16)
        created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        wss = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-wssecurity-secext-1.0.xsd")
        wsu = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-wssecurity-utility-1.0.xsd")
        pw_type = ("http://docs.oasis-open.org/wss/2004/01/"
                   "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
        enc = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-soap-message-security-1.0#Base64Binary")
        return (
            f'<s:Header><wsse:Security s:mustUnderstand="1" xmlns:wsse="{wss}" xmlns:wsu="{wsu}">'
            f'<wsse:UsernameToken><wsse:Username>{self.user}</wsse:Username>'
            f'<wsse:Password Type="{pw_type}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{enc}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f'<wsu:Created>{created}</wsu:Created>'
            f'</wsse:UsernameToken></wsse:Security></s:Header>'
        )

    def _call(self, url, body):
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f'{self._security_header()}<s:Body>{body}</s:Body></s:Envelope>'
        ).encode()
        req = urllib.request.Request(
            url, data=envelope,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode(errors="ignore")

    def _media_xaddr(self):
        """Look up the media service address via GetCapabilities (many devices serve it separately)."""
        try:
            resp = self._call(
                self.device_xaddr,
                f'<GetCapabilities xmlns="{self.DEVICE_NS}"><Category>Media</Category></GetCapabilities>')
            m = re.search(r"<[^>]*XAddr>\s*(https?://[^<\s]+)", resp)
            if m:
                return m.group(1)
        except Exception:
            pass
        return self.device_xaddr  # many cameras serve media from the same endpoint

    def stream_uri(self):
        """Returns the first profile's RTSP URL, or None (wrong creds / no ONVIF)."""
        media = self._media_xaddr()
        try:
            profiles = self._call(media, f'<GetProfiles xmlns="{self.MEDIA_NS}"/>')
        except Exception:
            return None
        tokens = re.findall(r'token="([^"]+)"', profiles)
        for token in tokens:
            body = (
                f'<GetStreamUri xmlns="{self.MEDIA_NS}">'
                f'<StreamSetup xmlns="{self.MEDIA_NS}">'
                f'<Stream xmlns="{self.SCHEMA_NS}">RTP-Unicast</Stream>'
                f'<Transport xmlns="{self.SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport>'
                f'</StreamSetup><ProfileToken>{token}</ProfileToken></GetStreamUri>'
            )
            try:
                resp = self._call(media, body)
            except Exception:
                continue
            m = re.search(r"<[^>]*Uri>\s*(rtsp://[^<\s]+)", resp)
            if m:
                return m.group(1)
        return None


def embed_credentials(url, user, password):
    """rtsp://host/path -> rtsp://user:pass@host/path (if the URL doesn't already have creds)."""
    parts = urlsplit(url)
    if "@" in parts.netloc or not user:
        return url
    auth = quote(user, safe="") + ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, f"{auth}@{parts.netloc}",
                       parts.path, parts.query, parts.fragment))


class NetworkScanner:
    """Finds cameras on the local network: ONVIF WS-Discovery + port scan + RTSP banner."""

    PORTS = [554, 8554, 2020, 80, 8000, 8080, 34567]
    RTSP_PORTS = {554, 8554, 2020}
    DVRIP_PORT = 34567   # Sofia/XMEye — lets you change the camera's IP right from the app

    VENDOR_PATHS = {
        "hikvision": ["rtsp://user:pass@{ip}:554/Streaming/Channels/101"],
        "dahua": ["rtsp://user:pass@{ip}:554/cam/realmonitor?channel=1&subtype=0"],
        "imou": ["rtsp://user:pass@{ip}:554/cam/realmonitor?channel=1&subtype=0"],
        "tp-link": ["rtsp://user:pass@{ip}:554/stream1"],
        "tapo": ["rtsp://user:pass@{ip}:554/stream1"],
        "vigi": ["rtsp://user:pass@{ip}:554/stream1"],
        "reolink": ["rtsp://user:pass@{ip}:554/h264Preview_01_main"],
        "uniview": ["rtsp://user:pass@{ip}:554/unicast/c1/s0/live"],
        # XMEye/iCSee family (cheap DVR/NVR): login and password are passed
        # not as user:pass@host, but inside the request path
        "h264dvr": [
            "rtsp://{ip}:554/user=admin&password=YOUR_PASSWORD&channel=1&stream=0.sdp?real_stream",
            "rtsp://{ip}:554/user=admin&password=YOUR_PASSWORD&channel=1&stream=1.sdp?real_stream",
        ],
        "xmeye": [
            "rtsp://{ip}:554/user=admin&password=YOUR_PASSWORD&channel=1&stream=0.sdp?real_stream",
            "rtsp://{ip}:554/user=admin&password=YOUR_PASSWORD&channel=1&stream=1.sdp?real_stream",
        ],
    }
    GENERIC_PATHS = [
        "rtsp://user:pass@{ip}:{port}/Streaming/Channels/101",
        "rtsp://user:pass@{ip}:{port}/cam/realmonitor?channel=1&subtype=0",
        "rtsp://user:pass@{ip}:{port}/stream1",
        "rtsp://user:pass@{ip}:{port}/live",
        "rtsp://user:pass@{ip}:{port}/h264",
        "rtsp://user:pass@{ip}:{port}/",
    ]

    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.Lock()
        self.running = False
        self.progress = 0.0
        self.results = {}   # ip -> info
        self.error = ""
        self.auto_connected = []   # [{ip, id, name}] added automatically during this scan

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.progress = 0.0
            self.results = {}
            self.error = ""
            self.auto_connected = []
        threading.Thread(target=self._scan, daemon=True).start()
        return True

    def state(self):
        with self.lock:
            return {
                "running": self.running,
                "progress": round(self.progress, 2),
                "error": self.error,
                "auto_connected": self.auto_connected,
                "results": sorted(self.results.values(),
                                  key=lambda r: ipaddress.ip_address(r["ip"])),
            }

    # ---------- stages ----------
    def _local_networks(self):
        nets = []
        try:
            data = json.loads(subprocess.run(
                ["ip", "-j", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=5).stdout)
            for iface in data:
                if iface.get("ifname", "").startswith(("lo", "docker", "veth", "br-")):
                    continue
                for a in iface.get("addr_info", []):
                    net = ipaddress.ip_network(f'{a["local"]}/{a["prefixlen"]}', strict=False)
                    if net.is_private and net.num_addresses <= 1024:
                        nets.append(net)
        except Exception as e:
            self.error = f"Failed to determine network: {e}"
        return nets

    def _onvif_discover(self, timeout=3.0):
        """WS-Discovery: multicast probe, cameras reply with their address and name."""
        probe = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
            ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
            ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            '<e:Header><w:MessageID>uuid:claude-cam-scan-1</w:MessageID>'
            '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
            '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
            '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
            '</d:Probe></e:Body></e:Envelope>'
        ).encode()
        found = {}
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(0.5)
            s.sendto(probe, ("239.255.255.250", 3702))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    continue
                text = data.decode(errors="ignore")
                info = {"onvif": True}
                m = re.search(r"onvif://www\.onvif\.org/name/([^\s<\"]+)", text)
                if m:
                    info["name"] = m.group(1).replace("%20", " ")
                m = re.search(r"onvif://www\.onvif\.org/hardware/([^\s<\"]+)", text)
                if m:
                    info["hardware"] = m.group(1).replace("%20", " ")
                # XAddrs — the ONVIF device service address, needed for GetProfiles/GetStreamUri
                m = re.search(r"<[^>]*XAddrs>\s*([^<]+)", text)
                if m:
                    xaddr = next((u for u in m.group(1).split()
                                  if u.startswith("http")), "")
                    if xaddr:
                        info["xaddr"] = xaddr.strip()
                found[addr[0]] = info
            s.close()
        except Exception:
            pass
        return found

    def _check_port(self, ip, port, timeout=0.6):
        try:
            with socket.create_connection((str(ip), port), timeout=timeout):
                return True
        except OSError:
            return False

    def _rtsp_banner(self, ip, port):
        """OPTIONS request: many cameras identify themselves in the Server header."""
        try:
            with socket.create_connection((str(ip), port), timeout=1.5) as s:
                s.sendall(f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
                s.settimeout(1.5)
                resp = s.recv(2048).decode(errors="ignore")
            m = re.search(r"^Server:\s*(.+)$", resp, re.M | re.I)
            return m.group(1).strip() if m else ("RTSP" if resp.startswith("RTSP/") else "")
        except OSError:
            return ""

    def _macs(self):
        macs = {}
        try:
            out = subprocess.run(["ip", "neigh", "show"], capture_output=True,
                                 text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                if "lladdr" in parts:
                    macs[parts[0]] = parts[parts.index("lladdr") + 1]
        except Exception:
            pass
        return macs

    def _suggestions(self, ip, info):
        text = " ".join(str(v) for v in info.values()).lower()
        for vendor, urls in self.VENDOR_PATHS.items():
            if vendor in text:
                return [u.format(ip=ip) for u in urls]
        port = next((p for p in info.get("ports", []) if p in self.RTSP_PORTS), 554)
        return [u.format(ip=ip, port=port) for u in self.GENERIC_PATHS]

    def _existing_ips(self):
        ips = set()
        for src in self.manager.existing_sources():
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", src or "")
            if m:
                ips.add(m.group(1))
        return ips

    # ---------- auto-connect ----------
    CRED_VARIANTS = [("admin", ""), ("admin", "admin"), ("admin", "12345"), ("admin", "123456")]

    def _candidate_urls(self, suggestions):
        urls = []
        for tmpl in suggestions:
            if "YOUR_PASSWORD" in tmpl:
                for _, pwd in self.CRED_VARIANTS:
                    urls.append(tmpl.replace("YOUR_PASSWORD", pwd))
            elif "user:pass" in tmpl:
                for user, pwd in self.CRED_VARIANTS:
                    urls.append(tmpl.replace("user:pass", f"{user}:{pwd}"))
            else:
                urls.append(tmpl)
        return urls

    def _try_connect(self, url, timeout_ms=1500):
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            if not cap.isOpened():
                return None
            ok, frame = cap.read()
            if not ok or frame is None:
                return None
            h, w = frame.shape[:2]
            return (w, h)
        finally:
            cap.release()

    def _find_working_url(self, suggestions):
        for url in self._candidate_urls(suggestions)[:12]:
            size = self._try_connect(url)
            if size:
                return url, size
        return None, None

    def _onvif_working_url(self, info):
        """Asks for the exact RTSP URL via ONVIF, trying common default credentials."""
        if not info.get("onvif"):
            return None, None
        xaddr = info.get("xaddr") or f'http://{info["ip"]}/onvif/device_service'
        for user, pwd in self.CRED_VARIANTS:
            try:
                uri = OnvifClient(xaddr, user, pwd).stream_uri()
            except Exception:
                uri = None
            if not uri:
                continue
            url = embed_credentials(uri, user, pwd)
            size = self._try_connect(url)
            if size:
                return url, size
        return None, None

    def _scan(self):
        try:
            onvif = self._onvif_discover()
            nets = self._local_networks()
            hosts = [ip for net in nets for ip in net.hosts()]
            done = [0]
            total = max(1, len(hosts) * len(self.PORTS))
            open_ports = {}   # ip -> [ports]
            plock = threading.Lock()

            def probe(ip_port):
                ip, port = ip_port
                if self._check_port(ip, port):
                    with plock:
                        open_ports.setdefault(str(ip), []).append(port)
                with plock:
                    done[0] += 1
                    self.progress = done[0] / total * 0.9

            with ThreadPoolExecutor(max_workers=128) as pool:
                list(pool.map(probe, [(ip, p) for ip in hosts for p in self.PORTS]))

            macs = self._macs()
            existing_ips = self._existing_ips()
            results = {}
            candidates = set(open_ports) | set(onvif)
            for ip in candidates:
                ports = sorted(open_ports.get(ip, []))
                info = dict(onvif.get(ip, {}))
                info["ip"] = ip
                info["ports"] = ports
                info["mac"] = macs.get(ip, "")
                info["already_added"] = ip in existing_ips
                rtsp_port = next((p for p in ports if p in self.RTSP_PORTS), None)
                if rtsp_port:
                    info["server"] = self._rtsp_banner(ip, rtsp_port)
                # DVRIP camera (Sofia/XMEye) — port 34567 open, the IP can be changed
                info["dvrip"] = self.DVRIP_PORT in ports
                # we treat as a camera anything that responds via RTSP, DVRIP, or was found via ONVIF
                info["likely_camera"] = bool(rtsp_port or info["dvrip"] or info.get("onvif"))
                if info["likely_camera"]:
                    info["suggestions"] = self._suggestions(ip, info)
                results[ip] = info

            # try to connect ourselves (common paths + frequent default passwords),
            # only for cameras not yet in the configured list
            likely = [r for r in results.values() if r["likely_camera"] and not r["already_added"]]
            self.progress = 0.9

            def probe_connect(r):
                # first ask the camera via ONVIF (exact path), then fall back to templates
                url, size = self._onvif_working_url(r)
                if not url:
                    url, size = self._find_working_url(r["suggestions"])
                return r["ip"], url, size

            if likely:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for ip, url, size in pool.map(probe_connect, likely):
                        if url:
                            results[ip]["working_url"] = url
                            results[ip]["working_size"] = f"{size[0]}x{size[1]}"

            with self.lock:
                self.results = results

            self._auto_add_new(results)
        except Exception as e:
            self.error = f"Scan error: {e}"
        finally:
            with self.lock:
                self.progress = 1.0
                self.running = False

    def _auto_add_new(self, results):
        """Automatically add every found and verified camera that isn't in the list yet."""
        added = []
        for r in results.values():
            if not r.get("working_url"):
                continue
            name = r.get("name") or r.get("hardware") or f"Camera {r['ip']}"
            cam = self.manager.add(name, r["working_url"])
            added.append({"ip": r["ip"], "id": cam.id, "name": cam.name})
        if added and _go2rtc is not None and _go2rtc.is_running():
            _go2rtc.sync()
        with self.lock:
            self.auto_connected = added


GO2RTC_BIN = shutil.which("go2rtc") or (
    str(BASE_DIR / "go2rtc") if (BASE_DIR / "go2rtc").exists() else None)


class Go2rtcManager:
    """Optional go2rtc media server: RTSP cameras -> WebRTC/HLS in the browser.

    A modern transport instead of ancient MJPEG: go2rtc pulls the stream from the
    camera once and serves it as WebRTC (low latency, with audio). Active only if
    the go2rtc binary is found (on PATH or next to app.py) — otherwise the app
    simply keeps working on MJPEG as before. The config is written as JSON (which
    is valid YAML), which avoids having to escape special characters in RTSP URLs
    (@, &, ?).
    """

    API_PORT = 1984
    RTSP_PORT = 8554

    def __init__(self, manager):
        self.manager = manager
        self.bin = GO2RTC_BIN
        self.config_path = BASE_DIR / "go2rtc.yaml"
        self.proc = None
        self.lock = threading.Lock()

    def available(self):
        return self.bin is not None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def _stream_sources(self):
        # network streams only; go2rtc can't pick up a local webcam
        return {c.id: c.source for c in self.manager.all()
                if isinstance(c.source, str) and c.source and not c.source.isdigit()}

    def _write_config(self):
        cfg = {
            "log": {"level": "warn"},
            "api": {"listen": f":{self.API_PORT}"},
            "rtsp": {"listen": f":{self.RTSP_PORT}"},
            "streams": self._stream_sources(),
        }
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

    def start(self):
        if not self.available() or self.is_running():
            return
        with self.lock:
            if self.is_running():
                return
            self._write_config()
            try:
                self.proc = subprocess.Popen(
                    [self.bin, "-config", str(self.config_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.proc = None

    def sync(self):
        """Cameras changed — rewrite the config and restart go2rtc."""
        if not self.available():
            return
        with self.lock:
            self._write_config()
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
                self.proc = None
        self.start()

    def has_stream(self, cam_id):
        return cam_id in self._stream_sources()

    def restream_url(self, cam_id):
        return f"rtsp://127.0.0.1:{self.RTSP_PORT}/{cam_id}"

    def info(self):
        return {
            "available": self.available(),
            "running": self.is_running(),
            "api_port": self.API_PORT,
        }


config = load_config()
migrate_legacy_recordings(config)
# Persist only if load/migration actually changed the on-disk config — don't
# rewrite (and reformat) a valid config.json on every single startup.
if _config_changed_on_disk(config):
    save_config(config)
manager = CameraManager(config)
scanner = NetworkScanner(manager)
go2rtc = Go2rtcManager(manager)
_go2rtc = go2rtc   # for capture_url_for (see Camera._open)
if config.get("view_mode") == "webrtc" or config.get("capture_via_go2rtc"):
    go2rtc.start()
uploader = Uploader(RECORDINGS_DIR, config,
                    lambda cid: (manager.get(cid).name if manager.get(cid) else cid),
                    camera_list=lambda: [{"id": c.id, "name": c.name} for c in manager.all()])
_uploader = uploader
uploader.start()


def _live_frame(cam_id):
    """Latest annotated JPEG for a camera — fed to the cloud live relay.

    Pulling a frame registers remote interest, so the encoder thread produces
    preview frames for a PWA viewer even when there's no local MJPEG viewer."""
    cam = manager.get(cam_id)
    if not cam:
        return None
    cam.mark_remote_view()
    with cam.lock:
        return cam.jpeg


live_relay = LiveRelay(config, _live_frame)
live_relay.start()


def _hls_source(cam_id):
    """Capture URL for the HLS relay to remux, or None to skip this camera.

    Reuses capture_url_for so it honours the go2rtc funnel (a single upstream
    connection). Only encoded network streams can be `-c copy`'d — a local webcam
    (numeric source) has no bitstream to remux, so it falls back to snapshots."""
    cam = manager.get(cam_id)
    if not cam:
        return None
    src = capture_url_for(cam.id, cam.source, cam.shared_cfg)
    if not isinstance(src, str) or not re.match(r"^(rtsp|rtmp|https?)://", src, re.I):
        return None
    return src


hls_relay = HlsRelay(config, _hls_source, BASE_DIR / ".hls")
hls_relay.start()
app = Flask(__name__)

if not manager.all():
    scanner.start()  # no cameras yet — scan and try to connect ourselves right away


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.route("/stream/<cam_id>")
def stream(cam_id):
    cam = manager.get(cam_id)
    if not cam:
        return "camera not found", 404

    def gen():
        cam.add_viewer()   # tells the encoder thread this camera is being watched
        try:
            last = None
            while True:
                with cam.lock:
                    jpeg = cam.jpeg
                if jpeg is None or jpeg is last:
                    time.sleep(0.03)
                    continue
                last = jpeg
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
        finally:
            cam.remove_viewer()
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def api_status():
    return jsonify([c.status() for c in manager.all()])


@app.route("/api/test_source", methods=["POST"])
def api_test_source():
    src = request.get_json(force=True).get("source", "").strip()
    if not src:
        return jsonify({"ok": False, "error": "Address not set"})
    real_src = int(src) if src.isdigit() else src
    cap = cv2.VideoCapture(real_src, cv2.CAP_FFMPEG if isinstance(real_src, str) else cv2.CAP_ANY)
    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
    cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    try:
        if not cap.isOpened():
            return jsonify({"ok": False, "error": "Failed to open the stream (check the address, login/password)"})
        ok, frame = cap.read()
        if not ok or frame is None:
            return jsonify({"ok": False, "error": "Connected, but failed to get a frame"})
        h, w = frame.shape[:2]
        fps = cap.get(cv2.CAP_PROP_FPS)
        return jsonify({"ok": True, "width": w, "height": h, "fps": round(fps, 1) if fps else None})
    finally:
        cap.release()


@app.route("/api/cameras", methods=["GET"])
def api_cameras_list():
    return jsonify([{"id": c.id, "name": c.name, "source": c.source} for c in manager.all()])


@app.route("/api/cameras", methods=["POST"])
def api_cameras_add():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip() or "Camera"
    source = (data.get("source") or "").strip()
    if not source:
        return jsonify({"error": "Camera address not set"}), 400
    cam = manager.add(name, source)
    _cameras_changed()
    return jsonify({"id": cam.id, "name": cam.name, "source": cam.source})


@app.route("/api/cameras/<cam_id>", methods=["POST"])
def api_cameras_update(cam_id):
    data = request.get_json(force=True)
    cam = manager.update(cam_id, name=data.get("name"), source=data.get("source"))
    if not cam:
        return jsonify({"error": "not found"}), 404
    _cameras_changed()
    return jsonify({"id": cam.id, "name": cam.name, "source": cam.source})


@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
def api_cameras_delete(cam_id):
    ok = manager.remove(cam_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    _cameras_changed()
    return jsonify({"ok": True})


def _sync_go2rtc():
    if go2rtc.is_running():
        go2rtc.sync()


def _cameras_changed():
    """Propagate a camera add/rename/remove: refresh go2rtc and re-assert the
    current camera set to the cloud so the PWA hides cameras that no longer exist."""
    _sync_go2rtc()
    uploader.sync_cameras(force=True)


@app.route("/api/media_info")
def api_media_info():
    return jsonify({**go2rtc.info(), "view_mode": config.get("view_mode", "mjpeg")})


@app.route("/api/upload/status")
def api_upload_status():
    return jsonify(uploader.status())


@app.route("/api/upload/config", methods=["GET", "POST"])
def api_upload_config():
    if request.method == "POST":
        data = request.get_json(force=True)
        for key in ("upload_enabled", "upload_url", "upload_token", "upload_scan_interval"):
            if key in data:
                config[key] = data[key]
        if isinstance(config.get("upload_url"), str):
            config["upload_url"] = config["upload_url"].strip().rstrip("/")
        save_config(config)
        uploader.backfill()   # kick a scan/backfill with the new settings
    return jsonify({
        "upload_enabled": config.get("upload_enabled", False),
        "upload_url": config.get("upload_url", ""),
        "has_token": bool(config.get("upload_token")),   # never echo the secret back
        "upload_scan_interval": config.get("upload_scan_interval", 60),
        "status": uploader.status(),
    })


@app.route("/api/upload/backfill", methods=["POST"])
def api_upload_backfill():
    uploader.backfill()
    return jsonify({"ok": True, "status": uploader.status()})


@app.route("/api/record/<cam_id>", methods=["POST"])
def api_record(cam_id):
    cam = manager.get(cam_id)
    if not cam:
        return jsonify({"error": "not found"}), 404
    cam.manual_record = bool(request.get_json(force=True).get("on"))
    return jsonify({"manual_record": cam.manual_record})


@app.route("/api/auto", methods=["POST"])
def api_auto():
    config["auto_record"] = bool(request.get_json(force=True).get("on"))
    save_config(config)
    return jsonify({"auto_record": config["auto_record"]})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json(force=True)
        for key in DEFAULT_CONFIG:
            if key == "cameras":
                continue
            if key in data:
                config[key] = data[key]
        if isinstance(config["detect_classes"], str):
            config["detect_classes"] = [c.strip() for c in config["detect_classes"].split(",") if c.strip()]
        save_config(config)
        # WebRTC/go2rtc capture was enabled — start (or rebuild) the server
        if config.get("view_mode") == "webrtc" or config.get("capture_via_go2rtc"):
            go2rtc.sync() if go2rtc.is_running() else go2rtc.start()
    return jsonify({k: v for k, v in config.items() if k != "cameras"})


@app.route("/api/scan", methods=["POST"])
def api_scan_start():
    started = scanner.start()
    return jsonify({"started": started, **scanner.state()})


@app.route("/api/scan")
def api_scan_state():
    return jsonify(scanner.state())


@app.route("/api/scan/connect", methods=["POST"])
def api_scan_connect():
    data = request.get_json(force=True)
    ip = data.get("ip", "")
    with scanner.lock:
        info = scanner.results.get(ip)
    if not info or not info.get("working_url"):
        return jsonify({"ok": False, "error": "No working address found for this camera"}), 404
    name = (data.get("name") or "").strip() or info.get("name") or info.get("hardware") or f"Camera {ip}"
    cam = manager.add(name, info["working_url"])
    _cameras_changed()
    return jsonify({"ok": True, "id": cam.id, "name": cam.name, "source": cam.source})


def _dvrip_target(data):
    return (
        (data.get("ip") or "").strip(),
        int(data.get("port") or 34567),
        (data.get("user") or "admin").strip() or "admin",
        data.get("password") or "",
    )


@app.route("/api/dvrip/config", methods=["POST"])
def api_dvrip_config():
    """Read the current network settings of a DVRIP camera (Sofia/XMEye)."""
    ip, port, user, password = _dvrip_target(request.get_json(force=True))
    if not _valid_ipv4(ip):
        return jsonify({"ok": False, "error": "Enter a valid camera IP"}), 400
    try:
        with DVRIPClient(ip, port, user, password) as c:
            c.login()
            net = c.get_network()
        return jsonify({
            "ok": True,
            "ip": sofia_hex_to_ip(net["HostIP"]),
            "mask": sofia_hex_to_ip(net["Submask"]),
            "gateway": sofia_hex_to_ip(net["GateWay"]),
            "mac": net.get("MAC", ""),
            "http_port": net.get("HttpPort"),
            "tcp_port": net.get("TCPPort"),
        })
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        return jsonify({"ok": False, "error": str(e) or "Failed to connect to the camera"})


@app.route("/api/dvrip/set_ip", methods=["POST"])
def api_dvrip_set_ip():
    """Change the IP/mask/gateway of a DVRIP camera. The camera applies the address and reconnects on its own."""
    data = request.get_json(force=True)
    ip, port, user, password = _dvrip_target(data)
    new_ip = (data.get("new_ip") or "").strip()
    new_mask = (data.get("new_mask") or "255.255.255.0").strip()
    new_gw = (data.get("new_gateway") or "").strip()
    if not new_gw:                                   # default gateway — .1 of the new subnet
        new_gw = new_ip.rsplit(".", 1)[0] + ".1" if _valid_ipv4(new_ip) else ""
    for label, val in (("the camera's current IP", ip), ("the new IP", new_ip),
                       ("the mask", new_mask), ("the gateway", new_gw)):
        if not _valid_ipv4(val):
            return jsonify({"ok": False, "error": f"Check {label}"}), 400
    try:
        with DVRIPClient(ip, port, user, password) as c:
            c.login()
            net = c.get_network()
            net["HostIP"] = ip_to_sofia_hex(new_ip)
            net["Submask"] = ip_to_sofia_hex(new_mask)
            net["GateWay"] = ip_to_sofia_hex(new_gw)
            c.set_network(net)
        return jsonify({"ok": True, "new_ip": new_ip, "new_mask": new_mask, "new_gateway": new_gw})
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        return jsonify({"ok": False, "error": str(e) or "Failed to change the IP"})


@app.route("/api/dvrip/osd_title", methods=["POST"])
def api_dvrip_osd_title():
    """Remove or set the channel title overlay ("AI CAM") on top of the video.

    Empty text → the label is removed; non-empty → it is shown and renamed.
    """
    data = request.get_json(force=True)
    ip, port, user, password = _dvrip_target(data)
    text = (data.get("text") or "").strip()
    if not _valid_ipv4(ip):
        return jsonify({"ok": False, "error": "Enter a valid camera IP"}), 400
    try:
        with DVRIPClient(ip, port, user, password) as c:
            c.login()
            changed = c.set_channel_title_visible(show=bool(text), text=text)
        if not changed:
            return jsonify({"ok": False, "error": "The camera did not return the overlay setting (AVEnc.VideoWidget)"})
        return jsonify({"ok": True, "shown": bool(text), "channels": changed})
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        return jsonify({"ok": False, "error": str(e) or "Failed to change the overlay"})


@app.route("/api/dvrip/time", methods=["POST"])
def api_dvrip_time():
    """Read the camera's time and, if the time field is provided, set a new one."""
    data = request.get_json(force=True)
    ip, port, user, password = _dvrip_target(data)
    new_time = (data.get("time") or "").strip()
    if not _valid_ipv4(ip):
        return jsonify({"ok": False, "error": "Enter a valid camera IP"}), 400
    if new_time:
        try:
            datetime.strptime(new_time, DVRIPClient.TIME_FMT)
        except ValueError:
            return jsonify({"ok": False, "error": "Time in the format YYYY-MM-DD HH:MM:SS"}), 400
    try:
        with DVRIPClient(ip, port, user, password) as c:
            c.login()
            if new_time:
                c.set_time(new_time)
            try:
                current = c.get_time()
            except (RuntimeError, OSError, KeyError):
                current = ""
        return jsonify({"ok": True, "set": bool(new_time), "camera_time": current})
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        return jsonify({"ok": False, "error": str(e) or "Failed to get/set the time"})


def _parse_clip_start(name):
    """'2026-07-05_12-30-00.mp4' -> epoch seconds (local time), or 0 if unparseable."""
    stem = name[:-4] if name.endswith(".mp4") else name
    try:
        return int(datetime.strptime(stem, "%Y-%m-%d_%H-%M-%S").timestamp())
    except ValueError:
        return 0


@app.route("/api/recordings")
def api_recordings():
    files = []
    for cam_dir in sorted(RECORDINGS_DIR.iterdir()):
        if not cam_dir.is_dir():
            continue
        cam = manager.get(cam_dir.name)
        cam_name = cam.name if cam else cam_dir.name
        for p in cam_dir.glob("*.mp4"):
            tags, duration, started = [], 0, 0
            meta = p.with_suffix(".json")
            if meta.exists():
                try:
                    m = json.loads(meta.read_text())
                    tags = m.get("tags", []) or []
                    duration = m.get("duration", 0) or 0
                    started = m.get("started_at", 0) or 0
                except (OSError, ValueError):
                    pass
            if not started:
                started = _parse_clip_start(p.name)
            files.append({
                "cam_id": cam_dir.name,
                "cam_name": cam_name,
                "name": p.name,
                "size_mb": round(p.stat().st_size / 1e6, 1),
                "tags": tags,
                "duration": round(duration, 1),
                "started": started,
                "started_iso": (datetime.fromtimestamp(started).isoformat(timespec="seconds")
                                if started else ""),
            })
    files.sort(key=lambda f: f["started"], reverse=True)
    return jsonify(files)


@app.route("/recordings/<cam_id>/<path:name>")
def get_recording(cam_id, name):
    cam_dir = (RECORDINGS_DIR / cam_id).resolve()
    if cam_dir.parent != RECORDINGS_DIR.resolve():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(cam_dir, name)


@app.route("/api/recordings/<cam_id>/<path:name>", methods=["DELETE"])
def delete_recording(cam_id, name):
    cam_dir = (RECORDINGS_DIR / cam_id).resolve()
    if cam_dir.parent != RECORDINGS_DIR.resolve():
        return jsonify({"error": "not found"}), 404
    target = (cam_dir / name).resolve()
    if target.parent != cam_dir or not target.exists():
        return jsonify({"error": "not found"}), 404
    cam = manager.get(cam_id)
    if cam and cam.record_path and target == cam.record_path.resolve():
        return jsonify({"error": "file is currently being recorded"}), 409
    target.unlink()
    meta = target.with_suffix(".json")
    if meta.exists():
        try:
            meta.unlink()
        except OSError:
            pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
