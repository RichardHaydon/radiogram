"""Render the home button at the three label widths to confirm none get
ellipsis-truncated after the font_factor change."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Replicate the home button geometry from scenes._home_button.
canvas_w = 720
head_h = int(720 * 0.10)  # 72 — but real layout uses canvas_h*0.10; canvas_h=1280
head_h = int(1280 * 0.10)  # 128 — same as Pi
btn_h = int(head_h * 0.80)
btn_w = int(head_h * 1.10)
inset = 6

# fit_text equivalent (lifted from widgets.fit_text).
def fit_text(draw, text, font, max_w):
    if not text:
        return text
    if draw.textbbox((0, 0), text, font=font)[2] <= max_w:
        return text
    ell = "…"
    s = text
    while len(s) > 1:
        s = s[:-1]
        cand = s.rstrip() + ell
        if draw.textbbox((0, 0), cand, font=font)[2] <= max_w:
            return cand
    return ell


# Two font factors: old (0.42) vs new (0.32).
FONT = "C:/Windows/Fonts/seguibl.ttf"
try:
    img = Image.new("RGB", (btn_w * 2 + 60, btn_h * 4 + 60), (20, 20, 30))
except Exception:
    pass
img = Image.new("RGB", (btn_w * 4 + 80, btn_h * 4 + 60), (20, 20, 30))
d = ImageDraw.Draw(img)
labels = ["HOME", "INICIO", "HJEM"]
for ri, factor in enumerate([0.42, 0.32]):
    size = max(8, int(min(btn_w, btn_h) * factor))
    f = ImageFont.truetype(FONT, size)
    for ci, label in enumerate(labels):
        x = 20 + ci * (btn_w + 20)
        y = 20 + ri * (btn_h + 30)
        d.rectangle([x, y, x + btn_w, y + btn_h], outline=(180, 180, 220), width=2)
        max_w = int((btn_w - 2 * inset) * 0.92)
        fit = fit_text(d, label, f, max_w)
        bb = d.textbbox((0, 0), fit, font=f)
        tx = x + (btn_w - (bb[2] - bb[0])) / 2
        ty = y + (btn_h - (bb[3] - bb[1])) / 2 - bb[1]
        d.text((tx, ty), fit, font=f, fill=(220, 230, 250))
        d.text((x, y + btn_h + 5), f"f={factor} L={label} -> {fit!r}",
               fill=(160, 200, 240),
               font=ImageFont.truetype(FONT, 14))

out = Path(__file__).parent / "smoke_out" / "home_button.png"
out.parent.mkdir(exist_ok=True)
img.save(out)
print(f"saved {out}")
