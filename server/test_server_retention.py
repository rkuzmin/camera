#!/usr/bin/env python3
"""Self-check for server retention: `python test_server_retention.py` (needs flask)."""
import os
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(CAMERA_DATA_DIR=tmp, CAMERA_RETENTION_DAYS="0",
                  CAMERA_MAX_GB="0", CAMERA_MIN_FREE_GB="0")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

Usage = namedtuple("Usage", "total used free")


def add_clip(name, age_s, size=1000):
    d = server.RECORDINGS_DIR / "cam1"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"\0" * size)
    with server.get_db() as con:
        con.execute("INSERT INTO clips (cam_id, name, size, started_at, uploaded_at) "
                    "VALUES ('cam1', ?, ?, ?, 0)", (name, size, int(time.time()) - age_s))


def left():
    with server.get_db() as con:
        rows = [r["name"] for r in con.execute("SELECT name FROM clips ORDER BY name")]
    files = sorted(p.name for p in server.RECORDINGS_DIR.rglob("*.mp4"))
    assert rows == files, (rows, files)
    return rows


def main():
    for i, name in enumerate(["a.mp4", "b.mp4", "c.mp4", "d.mp4"]):
        add_clip(name, age_s=400 - i * 100)          # a is the oldest

    # no limits -> nothing touched
    assert server.enforce_retention() == 0 and left() == ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]

    # free-space floor: 1500 B short -> two oldest 1000 B clips go, no more
    server.MIN_FREE_GB = 10_000 / 1e9
    server.shutil.disk_usage = lambda _p: Usage(0, 0, 8_500)
    assert server.enforce_retention() == 2 and left() == ["c.mp4", "d.mp4"]

    # plenty free again -> floor holds, nothing more goes
    server.shutil.disk_usage = lambda _p: Usage(0, 0, 10**12)
    assert server.enforce_retention() == 0 and left() == ["c.mp4", "d.mp4"]

    # size cap: 2000 B stored, cap 1500 B -> only the oldest goes
    server.MAX_GB = 1500 / 1e9
    assert server.enforce_retention() == 1 and left() == ["d.mp4"]
    print("ok")


if __name__ == "__main__":
    main()
