#!/usr/bin/env python3
"""Generate the PWA icons (no external deps — hand-rolled PNG encoder).

Draws a simple camera glyph on the app's dark brand background. Produces the
sizes a PWA needs: 192 / 512 (any), 512 (maskable), and a 180 apple-touch icon.
Re-run after tweaking colors/shape:  python3 make_icons.py
"""
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

BG = (13, 17, 23)        # #0d1117  app background
BODY = (230, 237, 243)   # #e6edf3  camera body (light)
RING = (31, 111, 235)    # #1f6feb  lens ring (blue)
GLASS = (88, 166, 255)   # #58a6ff  lens glass


def write_png(path, w, h, pixels):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8-bit RGBA
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                     # filter: none
        raw += pixels[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 9)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def rrect(px, py, x0, y0, x1, y1, r):
    if x0 + r <= px <= x1 - r and y0 <= py <= y1:
        return True
    if x0 <= px <= x1 and y0 + r <= py <= y1 - r:
        return True
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
        if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
            return True
    return False


def circle(px, py, cx, cy, r):
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def render(n):
    px = bytearray(n * n * 4)
    for y in range(n):
        fy = y / n
        for x in range(n):
            fx = x / n
            color = BG
            # camera body + top viewfinder bump
            if rrect(fx, fy, 0.16, 0.34, 0.84, 0.77, 0.06) or rrect(fx, fy, 0.36, 0.28, 0.53, 0.35, 0.01):
                color = BODY
            # flash dot
            if circle(fx, fy, 0.73, 0.40, 0.025):
                color = RING
            # lens: ring -> body gap -> blue glass -> highlight
            if circle(fx, fy, 0.50, 0.555, 0.155):
                color = RING
            if circle(fx, fy, 0.50, 0.555, 0.120):
                color = BODY
            if circle(fx, fy, 0.50, 0.555, 0.093):
                color = GLASS
            if circle(fx, fy, 0.463, 0.518, 0.028):
                color = BODY
            i = (y * n + x) * 4
            px[i:i + 4] = bytes((color[0], color[1], color[2], 255))
    return px


def main():
    for n, names in [(512, ["icon-512.png", "icon-512-maskable.png"]),
                     (192, ["icon-192.png"]),
                     (180, ["apple-touch-icon.png"])]:
        buf = render(n)
        for name in names:
            write_png(HERE / name, n, n, buf)
            print("wrote", name, f"({n}x{n})")


if __name__ == "__main__":
    main()
