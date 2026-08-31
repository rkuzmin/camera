"""Small-object helpers for the detector: motion gating and zoomed re-inspection.

A cat 40px wide in a 1080p frame is ~0.1% of the picture. It can't clear a
whole-frame motion threshold, and after the detector's downscale to 640px it is
~13px across — far too few pixels for YOLO to classify. Both problems go away by
looking at a *region* instead of the whole frame: gate on the busiest tile, then
crop that region out and upscale it before inference.

Pure functions (no camera state), self-checked by `python test_detect_zoom.py`.
"""
import cv2
import numpy as np


def tile_motion(mask, grid=8):
    """Largest changed fraction of any single grid tile of the motion mask.

    The whole-frame fraction is scale-blind: the same moving cat is ~0.1% of the
    frame but several percent of one 64th of it, so one threshold can serve both
    a person walking past and an animal in the distance.
    """
    tiles = cv2.resize(mask.astype(np.float32), (grid, grid), interpolation=cv2.INTER_AREA)
    return float(tiles.max())


def motion_rect(mask, max_frac=0.25, pad=0.5):
    """Normalized [x1,y1,x2,y2] around the largest SMALL motion blob, or None.

    That region is the one worth a zoomed second detector pass. Blobs larger than
    max_frac of the frame are skipped: an object that big is already classified
    fine at full-frame resolution, so cropping to it buys nothing. The box is
    padded by `pad` of its own size — frame differencing marks a moving animal's
    leading and trailing edges, not its whole body.
    """
    if mask is None:
        return None
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    mh, mw = mask.shape
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w > max_frac * mw or h > max_frac * mh:
            continue
        if best is None or area > best[-1]:
            best = (x, y, w, h, area)
    if best is None:
        return None
    x, y, w, h, _ = best
    px, py = w * pad, h * pad
    return [max(0.0, (x - px) / mw), max(0.0, (y - py) / mh),
            min(1.0, (x + w + px) / mw), min(1.0, (y + h + py) / mh)]


def crop_upscale(frame, rect, target=640, max_scale=4.0):
    """Crop the normalized rect out of the frame and upscale it, so a small/distant
    object fills far more pixels — YOLO's classification confidence scales with how
    many pixels the object actually occupies.

    Returns (crop, x_offset, y_offset, scale) in full-frame pixel terms — a box found
    in the crop maps back as box/scale + offset — or None if the rect is too small to
    be worth a pass.
    """
    if not rect:
        return None
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = rect
    px1, py1 = max(0, int(x1 * fw)), max(0, int(y1 * fh))
    px2, py2 = min(fw, int(x2 * fw)), min(fh, int(y2 * fh))
    if px2 - px1 < 8 or py2 - py1 < 8:
        return None
    crop = frame[py1:py2, px1:px2]
    scale = min(max_scale, target / max(crop.shape[0], crop.shape[1]))
    if scale <= 1.0:
        # already >= target px on the long side: the detector downscales it itself,
        # and the mapping back must not pretend an upscale happened
        return crop, px1, py1, 1.0
    return (cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC),
            px1, py1, scale)
