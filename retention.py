#!/usr/bin/env python3
"""Edge-side retention: keep the local archive inside a size / age / free-space budget.

Without this the recordings dir grows until the filesystem is full and recording
silently stops (the writer just fails to open). A background sweep deletes clips
oldest-first while any limit is exceeded:

  retention_days  — delete clips older than N days   (0 = off)
  retention_gb    — cap the total size of recordings/ (0 = off)
  min_free_gb     — keep at least N GB free on the filesystem (the safety net:
                    it only bites when the disk is nearly full, so it protects
                    recording without surprising anyone with early deletions)

A clip currently being recorded is never touched. Clips already sent to the cloud
aren't preferred over pending ones — a full disk is worse than a missed upload,
and the uploader treats a vanished file as handled.
"""
import shutil
import threading
import time
from pathlib import Path

SWEEP_INTERVAL = 300   # how often the pruner runs, seconds


class Retention:
    def __init__(self, recordings_dir, config, in_use=None):
        # config: the shared app config dict (retention_days/retention_gb/min_free_gb)
        # in_use: optional callable() -> iterable of paths that must not be deleted
        self.dir = Path(recordings_dir)
        self.config = config
        self.in_use = in_use or (lambda: ())
        self.last_sweep_ts = 0.0
        self.last_removed = 0
        self.last_freed = 0
        self._stop = False
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop = True

    def _run(self):
        while not self._stop:
            try:
                removed, freed = self.sweep()
                if removed:
                    print(f"[retention] removed {removed} clip(s), "
                          f"freed {freed / 1e9:.2f} GB")
            except Exception as e:  # noqa: BLE001 - the pruner must never die
                print(f"[retention] sweep failed: {e}")
            time.sleep(SWEEP_INTERVAL)

    # ---------- the sweep ----------
    def sweep(self):
        """Delete oldest-first while any limit is exceeded. -> (removed, freed_bytes)"""
        days = _num(self.config.get("retention_days"))
        cap = _num(self.config.get("retention_gb")) * 1e9
        floor = _num(self.config.get("min_free_gb")) * 1e9
        clips = self._clips_oldest_first()
        total = sum(size for _, size, _ in clips)
        free = self._free_bytes()
        cutoff = time.time() - days * 86400 if days > 0 else 0
        protected = self._protected()
        removed = freed = 0
        for mtime, size, path in clips:
            # clips are oldest-first, so once one is inside every limit the rest are too
            over_age = cutoff > 0 and mtime < cutoff
            over_cap = cap > 0 and total > cap
            low_free = floor > 0 and free is not None and free < floor
            if not (over_age or over_cap or low_free):
                break
            if path.resolve() in protected or not _delete_clip(path):
                continue
            total -= size
            if free is not None:
                free += size
            removed += 1
            freed += size
        self.last_sweep_ts = time.time()
        self.last_removed, self.last_freed = removed, freed
        return removed, freed

    def status(self):
        clips = self._clips_oldest_first()
        return {
            "clips": len(clips),
            "used_bytes": sum(size for _, size, _ in clips),
            "free_bytes": self._free_bytes() or 0,
            "oldest_ts": int(clips[0][0]) if clips else 0,
            "last_sweep_ts": int(self.last_sweep_ts),
            "last_removed": self.last_removed,
        }

    # ---------- helpers ----------
    def _clips_oldest_first(self):
        out = []
        if not self.dir.exists():
            return out
        for cam_dir in self.dir.iterdir():
            if not cam_dir.is_dir():
                continue
            for path in cam_dir.glob("*.mp4"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, path))
        out.sort(key=lambda t: t[0])
        return out

    def _free_bytes(self):
        try:
            return shutil.disk_usage(self.dir).free
        except OSError:
            return None

    def _protected(self):
        try:
            return {Path(p).resolve() for p in (self.in_use() or ()) if p}
        except Exception:  # noqa: BLE001 - a bad callback must not stall pruning
            return set()


def _num(v):
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def _delete_clip(path):
    """Remove a clip and its sidecar metadata."""
    try:
        path.unlink()
    except OSError:
        return False
    try:
        path.with_suffix(".json").unlink(missing_ok=True)
    except OSError:
        pass
    return True
