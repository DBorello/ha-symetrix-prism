#!/usr/bin/env python3
"""Generate the integration's brand icons (a three-fader mixer).

Writes custom_components/symetrix_prism/brand/icon.png (256 px) and
icon@2x.png (512 px). Drawn at 4x and downsampled for smooth edges.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BRAND_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "symetrix_prism"
    / "brand"
)
SUPERSAMPLE = 4

BODY = (30, 58, 95, 255)  # deep blue
SLOT = (14, 30, 52, 255)  # fader track
KNOB = (244, 247, 251, 255)  # off-white
ACCENT = (245, 166, 35, 255)  # amber: the "active" fader
# Fader knob positions, 0 = top, 1 = bottom of the track.
KNOBS = (0.62, 0.25, 0.45)


def draw(size: int) -> Image.Image:
    """Draw the icon at ``size`` pixels square."""
    s = size * SUPERSAMPLE
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=round(s * 0.22), fill=BODY)

    top, bottom = s * 0.18, s * 0.82
    slot_w = s * 0.06
    knob_w, knob_h = s * 0.2, s * 0.11
    for i, position in enumerate(KNOBS):
        x = s * (0.25 + 0.25 * i)
        d.rounded_rectangle(
            (x - slot_w / 2, top, x + slot_w / 2, bottom),
            radius=slot_w / 2,
            fill=SLOT,
        )
        y = top + knob_h / 2 + position * (bottom - top - knob_h)
        d.rounded_rectangle(
            (x - knob_w / 2, y - knob_h / 2, x + knob_w / 2, y + knob_h / 2),
            radius=knob_h * 0.3,
            fill=ACCENT if i == 1 else KNOB,
        )
        # Centre line on the knob, like a real fader cap.
        line_h = max(knob_h * 0.1, 1)
        d.rectangle(
            (x - knob_w * 0.32, y - line_h / 2, x + knob_w * 0.32, y + line_h / 2),
            fill=BODY,
        )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    BRAND_DIR.mkdir(exist_ok=True)
    draw(256).save(BRAND_DIR / "icon.png", optimize=True)
    draw(512).save(BRAND_DIR / "icon@2x.png", optimize=True)
    print(f"Wrote icons to {BRAND_DIR}")


if __name__ == "__main__":
    main()
