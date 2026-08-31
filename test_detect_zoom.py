#!/usr/bin/env python3
"""Self-check for detect_zoom.py: `python test_detect_zoom.py` (no pytest)."""
import numpy as np

from detect_zoom import crop_upscale, motion_rect, tile_motion

MASK_W, MASK_H = 160, 90          # the detector's motion grid


def cat_mask():
    """A cat-sized mover: ~40px wide in a 1920x1080 frame == ~3 cells on the grid."""
    m = np.zeros((MASK_H, MASK_W), dtype=bool)
    m[40:44, 60:64] = True
    return m


def test_tile_gate_sees_what_the_whole_frame_misses():
    m = cat_mask()
    whole = np.count_nonzero(m) / m.size * 100
    assert whole < 0.5, whole                      # below the default motion_threshold
    assert tile_motion(m) * 100 >= 0.5             # ...but the tile it walks through isn't
    assert tile_motion(np.zeros((MASK_H, MASK_W), dtype=bool)) == 0.0


def test_motion_rect_wraps_the_small_blob_and_skips_big_ones():
    r = motion_rect(cat_mask())
    x1, y1, x2, y2 = r
    assert x1 < 60 / MASK_W and x2 > 64 / MASK_W   # padded around the blob
    assert y1 < 40 / MASK_H and y2 > 44 / MASK_H
    assert 0.0 <= x1 and x2 <= 1.0 and 0.0 <= y1 and y2 <= 1.0

    big = np.zeros((MASK_H, MASK_W), dtype=bool)
    big[10:80, 10:150] = True                      # a truck rolling past — full-frame pass has it
    assert motion_rect(big) is None
    assert motion_rect(None) is None

    both = cat_mask()
    both[10:80, 100:150] = True                    # big blob present -> still picks the cat
    x1, _, x2, _ = motion_rect(both)
    assert x2 < 100 / MASK_W


def test_crop_upscale_maps_boxes_back_to_full_frame():
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    # small region -> upscaled, and a box found in the crop maps back to where it came from
    crop, ox, oy, scale = crop_upscale(frame, [0.5, 0.4, 0.55, 0.45])
    assert scale > 1.0 and crop.shape[0] > 0.05 * 1080
    assert round((0.5 * 1920 - ox) + 0 / scale) == 0
    assert abs(((crop.shape[1] / scale) + ox) - 0.55 * 1920) <= 2

    # region already larger than the model input: NOT upscaled, so scale must stay 1.0
    # or every mapped box lands in the wrong place
    crop, ox, oy, scale = crop_upscale(frame, [0.0, 0.0, 1.0, 1.0])
    assert scale == 1.0 and crop.shape[:2] == (1080, 1920)
    assert (ox, oy) == (0, 0)

    assert crop_upscale(frame, None) is None
    assert crop_upscale(frame, [0.5, 0.5, 0.5001, 0.5001]) is None   # too small to bother


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all good")
