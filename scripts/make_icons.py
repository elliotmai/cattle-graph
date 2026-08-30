#!/usr/bin/env python3
"""Generate the board's icon set from one drawing.

    python scripts/make_icons.py

A longhorn skull as a cattle brand: the mark ranchers actually use, and one of
the few shapes that still reads at 16 pixels. Cream on saddle tan, matching the
board's palette.

Everything is drawn at 8x and downsampled, because Pillow has no antialiasing
of its own -- the supersample is what keeps the horn tips from going ragged.

Outputs into public/, which Netlify serves as static files alongside the
function at "/".
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")

TAN = (168, 118, 58, 255)      # --tan, the board's saddle brown
CREAM = (247, 241, 230, 255)   # --paper

SS = 8            # supersample factor
S = 128 * SS      # working canvas


def bezier(p0, p1, p2, steps=80):
    """Quadratic bezier as a list of points."""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def tapered(points, w0, w1):
    """A stroke along `points` whose width eases from w0 to w1.

    Returned as one closed polygon: the left offsets forward, then the right
    offsets back. Drawing it as a polygon rather than a run of circles keeps
    the outline clean when it is scaled down.
    """
    left, right = [], []
    n = len(points)
    for i, (x, y) in enumerate(points):
        t = i / (n - 1)
        # Ease the taper so the horn keeps its mass before thinning to a point.
        w = (w0 + (w1 - w0) * (t ** 0.7)) / 2
        ax, ay = points[max(0, i - 1)]
        bx, by = points[min(n - 1, i + 1)]
        dx, dy = bx - ax, by - ay
        m = (dx * dx + dy * dy) ** 0.5 or 1.0
        nx, ny = -dy / m, dx / m
        left.append((x + nx * w, y + ny * w))
        right.append((x - nx * w, y - ny * w))
    return left + right[::-1]


def draw_mark(img, scale=1.0, dy=0.0):
    """The longhorn skull, centred, scaled about the canvas centre."""
    d = ImageDraw.Draw(img)
    cx, cy = S / 2, S / 2 + dy * S

    def P(x, y):
        return (cx + x * S * scale, cy + y * S * scale)

    # --- horns: out from the temples, sweeping up to a point ---------------
    for side in (-1, 1):
        curve = bezier(P(side * 0.115, -0.055), P(side * 0.46, -0.10), P(side * 0.415, -0.275))
        d.polygon(tapered(curve, 0.085 * S * scale, 0.012 * S * scale), fill=CREAM)

    # --- skull: cranium, cheeks, muzzle ------------------------------------
    # Cranium, wide and flat-topped the way a bovine skull is.
    d.ellipse([P(-0.175, -0.135), P(0.175, 0.105)], fill=CREAM)
    # Muzzle tapering down from it.
    d.polygon([P(-0.135, 0.02), P(0.135, 0.02), P(0.088, 0.245), P(-0.088, 0.245)], fill=CREAM)
    # Rounded nose.
    d.ellipse([P(-0.093, 0.155), P(0.093, 0.30)], fill=CREAM)
    # Eyes punched back out, which is what makes it read as a skull rather
    # than a blob at small sizes.
    for side in (-1, 1):
        d.ellipse([P(side * 0.115 - 0.038, -0.045), P(side * 0.115 + 0.038, 0.028)], fill=TAN)


def render(size, maskable=False):
    img = Image.new("RGBA", (S, S), TAN)
    # Android masks a maskable icon to whatever shape the launcher likes, so
    # the mark has to sit inside the inner 80%.
    draw_mark(img, scale=0.78 if maskable else 1.0, dy=0.01)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    written = []

    for name, size, mask in [
        ("apple-touch-icon.png", 180, False),   # iOS home screen
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
    ]:
        p = os.path.join(OUT, name)
        render(size, mask).save(p)
        written.append((name, os.path.getsize(p)))

    # One .ico carrying the small sizes browsers actually pick from.
    ico = os.path.join(OUT, "favicon.ico")
    render(64).save(ico, sizes=[(16, 16), (32, 32), (48, 48)])
    written.append(("favicon.ico", os.path.getsize(ico)))

    # An SVG would be sharper, but the mark is drawn with a supersampled
    # raster taper that has no tidy path equivalent; 32px png covers the tab.
    p = os.path.join(OUT, "favicon-32.png")
    render(32).save(p)
    written.append(("favicon-32.png", os.path.getsize(p)))

    for name, size in written:
        print(f"  {name:26} {size:>7,} bytes")


if __name__ == "__main__":
    main()
