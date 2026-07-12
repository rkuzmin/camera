# Cameras — viewing and detection-based recording

A local application: live viewing of several IP cameras in your browser, manual
recording with a button on each camera, and automatic recording when YOLO
spots a person/car/animal in frame. The clip also includes a few seconds
**before** the event (pre-buffer).

## Requirements / Setup

You need Python 3. Create a virtual environment, install the dependencies, and
start the app:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./start.sh
```

The YOLO weights (`yolov8n.pt`) are downloaded automatically by ultralytics on
first run, so no manual model setup is required.

`ffmpeg` is optional but recommended: if it's on `PATH`, recordings are made in
H.264 instead of the legacy `mp4v`, and the `copy` recording mode (no
re-encoding, with audio) becomes available. Without `ffmpeg` the app falls back
to `mp4v` via OpenCV and keeps working. The [`go2rtc`](https://github.com/AlexxIT/go2rtc)
binary is also optional, for low-latency WebRTC viewing — see below.

## Security note

The web UI has no authentication and binds to `0.0.0.0:8080`, meaning it is
reachable by any device on your network. It is intended for a **trusted local
network only** — do not expose it to the internet. In addition, the network
scanner actively tries common default camera passwords on the local subnet, so
run it only on networks you own.

## Running

```bash
./start.sh
```

Open in your browser: **http://<IP-of-this-computer>:8080** — the page is
accessible from any device on the local network, not only from this computer.

## Multiple cameras

Each configured camera gets its own tile with video and its own record button —
they can be recorded simultaneously and independently. Detection settings (what
to look for, sensitivity, how many seconds before/after the event) are shared
across all cameras.

There are three ways to add a camera:
- click **🔍 Find cameras on the network** — if a working address is found, the
  **➕ Add this camera** button adds it right away;
- if no cameras are configured yet, the app scans the network itself on startup
  and adds everything it finds and can connect to — no action required;
- enter the address manually in the "Add camera" form in settings.

A camera can be renamed, have its address changed, or be removed in the
"⚙️ Settings → My cameras" section (recordings remain on disk when a camera is
removed).

## Finding cameras on the network

The **🔍 Find cameras on the network** button:

- broadcasts an ONVIF discovery request (WS-Discovery) — cameras respond with
  their own address, name, and model;
- scans the local subnet on camera ports (554, 8554, 80, 8000, 34567…);
- for RTSP cameras it requests the banner (`Server:`) to identify the vendor;
- for ONVIF cameras it makes a full `GetProfiles` → `GetStreamUri` request, so
  the camera reports its **exact** RTSP URL instead of the path being guessed
  from a vendor template;
- **tries to connect itself** — for non-ONVIF cameras it iterates through the
  typical paths for that vendor together with common default passwords (empty,
  admin, 12345, 123456); if it succeeds, you can add the camera in one click
  without entering a login/password;
- if it can't guess the password or path, it shows an address template — click
  it to paste it into the "Add camera" field, and fill in the login/password in
  place of `user:pass` (or `YOUR_PASSWORD` for XMEye/iCSee family DVRs).

## First-time setup

1. Click "🔍 Find cameras on the network" **or** enter the camera address (RTSP)
   into the "Add camera" form manually, for example:
   - Hikvision: `rtsp://login:password@192.168.1.64:554/Streaming/Channels/101`
   - Dahua / Imou: `rtsp://login:password@IP:554/cam/realmonitor?channel=1&subtype=0`
   - TP-Link Tapo: `rtsp://login:password@IP:554/stream1`
   - XMEye/iCSee (cheap DVR/NVR): `rtsp://IP:554/user=admin&password=password&channel=1&stream=0.sdp?real_stream`
2. The "🔌 Test" button shows whether the address works, even before saving.
3. "➕ Add camera" — the image appears within a couple of seconds.

You can find the exact RTSP path for your camera in its app/web interface
(ONVIF/RTSP section) or look it up by model.

## Configuring cheap DVR/XMEye cameras (DVRIP)

Xiongmai/Sofia/XMEye-family cameras (also sold as Besder, iCSee) can be
configured directly from the app's Settings UI over the DVRIP protocol
(TCP port 34567), no vendor app required. In Settings you can:

- **Change the camera's network settings** — IP address, subnet mask, and
  gateway. This is handy for cameras that ship with a fixed default IP such as
  `192.168.1.10`. Your computer must be on the same subnet as the camera at the
  time you change it.
- **Remove or rename the on-video channel-title overlay** — for example the
  "AI CAM" label burned into the picture.
- **Set the camera's clock** — sync it with this computer or set it manually,
  and read back its current time.

These controls appear in the Settings section. In addition, the network scanner
flags such cameras with a **Change IP** button so you can jump straight to their
DVRIP configuration.

## How it works

- **Viewing** runs continuously; nothing is recorded.
- **⏺ Start recording** on a camera tile — manual recording, writes until you
  stop it.
- **🤖 Auto-record: on** (a single toggle for all cameras) — YOLO (the yolov8n
  model, CPU, one instance for all cameras) checks each camera's frame roughly
  every 0.7 s; if a target object appears (by default: person, car, cat,
  dog…), that camera starts recording. It stops `post_seconds` (10 s) after the
  object disappears.
- **Motion gating** (`detect_on_motion`, on by default) runs a cheap
  frame-difference check on every frame and skips the costly YOLO pass while
  the scene is static, so idle cameras cost almost no CPU. Lower
  `motion_threshold` = more sensitive to small movements.
- Clips are saved to `recordings/<camera-id>/` as `YYYY-MM-DD_HH-MM-SS.mp4`
  (each with a `<clip>.json` sidecar holding the detected object tags and the
  clip's duration). The **🎬 Recordings** section browses them DVR-style: pick a
  camera, choose a day on the calendar (days with footage are dotted), and see
  that day's clips laid out on a 24-hour timeline as colour-coded blocks (by
  detected object) next to a list with inline playback and delete. A **☁️ Sync to
  cloud** button there pushes recordings to the backend on demand and shows the
  upload status (queued / uploaded / last error).

## Settings (config.json or via the interface)

| Parameter | Meaning |
|---|---|
| `cameras` | list of cameras: `id`, `name`, `source` (RTSP/HTTP/webcam number) |
| `detect_classes` | which objects trigger recording (COCO classes, in English) — shared across all cameras |
| `confidence` | detector confidence threshold (0.45 by default) |
| `pre_seconds` | how many seconds before the event are included in the clip |
| `post_seconds` | how long to keep recording after the object disappears |
| `max_clip_minutes` | splitting long recordings into files |
| `detect_on_motion` | run YOLO only when motion is detected — saves CPU (on by default) |
| `motion_threshold` | % of the frame that must change to count as motion |
| `record_mode` | `annotated` — H.264 re-encode with pre-buffer (clean image, no boxes); `copy` — raw stream with no re-encoding, with audio (see below) |
| `view_mode` | `mjpeg` (default) or `webrtc` — low-latency viewing via go2rtc |
| `capture_via_go2rtc` | pull the detector's frames from the go2rtc restream — a single connection to the camera for both viewing and detection |

## Codec and recording modes

Recording is done in **H.264** (via `ffmpeg`, if installed) — files are
noticeably smaller and better quality than the legacy `mp4v`. If `ffmpeg` isn't
found, it falls back to `mp4v` via OpenCV and the app keeps working.

Two recording modes (`record_mode`):

- **`annotated`** (default) — frames are re-encoded to H.264 and the pre-buffer
  works (seconds before the event). The recorded image is **clean**: detection
  boxes and the REC marker are drawn only on the live view, never burned into
  the file. Uses a bit more CPU. Detected object classes are saved as tags
  alongside the clip.
- **`copy`** — `ffmpeg` writes the camera's stream directly (`-c copy`),
  **with no re-encoding**: maximum quality, minimum CPU, **audio is
  preserved**. The trade-off is no boxes in the file and no pre-buffer. Only
  for network streams (not a webcam), and only when `ffmpeg` is available.

## Low-latency viewing (WebRTC via go2rtc) — optional

By default, viewing goes through MJPEG (as before). If you drop the
[`go2rtc`](https://github.com/AlexxIT/go2rtc) binary into `PATH` or next to
`app.py` and set `"view_mode": "webrtc"`, the app will start go2rtc as a media
server: cameras are served to the browser via **WebRTC** (low latency, with
audio), and `go2rtc.yaml` is generated automatically from the camera list.
Without the go2rtc binary, everything keeps working on MJPEG as before —
nothing is required to install.

## Cloud backend + mobile app (PWA)

The app can push its recordings to a small cloud server and expose them — plus
live view and push alerts — through an installable mobile web app (PWA). All the
heavy work (capture, detection, encoding) stays on this local **edge** machine;
the server only stores and serves.

Deployed instance: your own domain, e.g. **https://your-domain.example** (backend + PWA).

### Architecture

```
camera → edge (app.py: detect / record) → upload → VPS backend → PWA on phone
                          └─ live snapshots (on demand) ─┘
```

- **Edge** (this app): each finished clip is queued and uploaded to the backend
  (durable, retried, idempotent; pre-existing clips are backfilled on start). On
  a detection it posts an event (for push); while someone watches a camera in
  the app it relays live JPEG frames.
- **Backend** (`server/`, Flask + SQLite behind nginx): stores clips, serves an
  authenticated JSON API + the PWA, sends Web Push, relays live frames. Video is
  streamed by nginx via `X-Accel-Redirect` (HTTP range requests, tiny memory).

### Enabling it on the edge

Set three keys in `config.json` (or from the UI via `POST /api/upload/config`):

```json
{
  "upload_enabled": true,
  "upload_url": "https://your-domain.example",
  "upload_token": "<device token — CAMERA_DEVICE_TOKEN from the backend .env>"
}
```

`upload_enabled` toggles archiving; **live view and push work whenever
`upload_url` + `upload_token` are set**, even with archiving off.

### The mobile app (PWA)

Open **https://your-domain.example** (your deployed URL) on the phone, log in with the admin
password, then **Add to Home Screen**. Installed, it runs like a native app and
(on iOS 16.4+, only once installed) can receive push notifications.

- **Записи** — DVR-style browsing: a calendar (days with footage are marked) and
  a 24-hour timeline of colour-coded blocks for the chosen day, with filters by
  camera and object. **Настройки → Синхронизировать с сервером** re-pulls the
  latest recordings from the backend.
- **Камеры** — live view (on-demand snapshot relay, ~3 fps, works on iOS).
- **Настройки** — enable push notifications, log out.

### Security

The backend is internet-facing and **requires a password** (the local UI has
none). Traffic is HTTPS (Let's Encrypt). The edge only makes **outbound**
connections, so the home network is never exposed. Clips are auto-deleted by a
retention timer (default 30 days).

Server operations — deploy, logs, retention, VAPID keys — are documented in
[`server/deploy/README.md`](server/deploy/README.md).

## Power-outage / battery alerts to Telegram (macOS)

An optional launchd service for the Mac that runs this app: it watches the
power state and messages a Telegram bot when the machine switches to battery
(likely a power outage — useful to know your cameras are about to go dark),
when the battery runs low, and when power comes back.

```bash
cd battery-monitor && ./install.sh
```

See [`battery-monitor/README.md`](battery-monitor/README.md).
