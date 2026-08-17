#!/usr/bin/env python3
"""Self-check for retention.py: `python test_retention.py` (stdlib only, no pytest)."""
import os
import tempfile
import time
from pathlib import Path

from retention import Retention


def make_clip(root, cam, name, size, age_days=0):
    d = root / cam
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\0" * size)
    p.with_suffix(".json").write_text('{"tags": []}')
    ts = time.time() - age_days * 86400
    os.utime(p, (ts, ts))
    return p


def names(root):
    return sorted(p.name for p in root.rglob("*.mp4"))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = make_clip(root, "cam1", "old.mp4", 1000, age_days=10)
        mid = make_clip(root, "cam1", "mid.mp4", 1000, age_days=5)
        new = make_clip(root, "cam2", "new.mp4", 1000, age_days=0)

        # no limits -> nothing is touched
        cfg = {"retention_days": 0, "retention_gb": 0, "min_free_gb": 0}
        r = Retention(root, cfg)
        assert r.sweep() == (0, 0)
        assert names(root) == ["mid.mp4", "new.mp4", "old.mp4"]

        # age limit: only the 10-day-old clip goes, sidecar with it
        cfg["retention_days"] = 7
        removed, freed = r.sweep()
        assert (removed, freed) == (1, 1000), (removed, freed)
        assert not old.exists() and not old.with_suffix(".json").exists()
        assert names(root) == ["mid.mp4", "new.mp4"]

        # size cap: 1500 bytes over 2000 bytes of clips -> oldest one goes
        cfg["retention_days"] = 0
        cfg["retention_gb"] = 1500 / 1e9
        assert r.sweep() == (1, 1000)
        assert names(root) == ["new.mp4"] and not mid.exists()

        # a clip being recorded right now is never deleted
        cfg["retention_gb"] = 1 / 1e9   # cap below everything
        r_locked = Retention(root, cfg, in_use=lambda: [new])
        assert r_locked.sweep() == (0, 0)
        assert names(root) == ["new.mp4"]

        # ...but is otherwise fair game
        assert r.sweep() == (1, 1000)
        assert names(root) == []

        # free-space floor: an impossible floor prunes everything
        make_clip(root, "cam1", "a.mp4", 10)
        cfg["retention_gb"] = 0
        cfg["min_free_gb"] = 1e9   # a billion GB free is never true
        assert r.sweep()[0] == 1
        assert names(root) == []

        # garbage limits are treated as "off", not as a crash
        make_clip(root, "cam1", "b.mp4", 10)
        r.config = {"retention_days": None, "retention_gb": "", "min_free_gb": "x"}
        assert r.sweep() == (0, 0)
        assert r.status()["clips"] == 1

    print("ok")


if __name__ == "__main__":
    main()
