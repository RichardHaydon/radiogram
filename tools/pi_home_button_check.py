"""Run on Pi: confirm the new home-button font_factor fits all labels."""
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Pi geometry: canvas_h=1280, head_h=128, btn_h=102, btn_w=140, inset=6
btn_h = int(128 * 0.80)
btn_w = int(128 * 1.10)
inset = 6
max_w = int((btn_w - 2 * inset) * 0.92)


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


tmp = Image.new("RGB", (10, 10))
d0 = ImageDraw.Draw(tmp)
print(f"btn_w={btn_w} btn_h={btn_h} max_w={max_w}")
for factor in (0.42, 0.36, 0.32, 0.30):
    size = max(8, int(min(btn_w, btn_h) * factor))
    f = ImageFont.truetype(FONT_PATH, size)
    line = f"factor={factor:.2f} size={size}: "
    for label in ("HOME", "INICIO", "HJEM"):
        bb = d0.textbbox((0, 0), label, font=f)
        w = bb[2] - bb[0]
        fit = fit_text(d0, label, f, max_w)
        flag = "OK" if fit == label else "TRUNC"
        line += f"{label}({w}px,{flag}) "
    print(line)
