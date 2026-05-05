"""Widget primitives.

Every renderable piece of UI is a Widget. A Widget owns a Rect and knows
how to draw itself given a Theme. The compositor repaints when any
widget's state_key() changes between frames.

Widgets that should respond to touch are Buttons (which add a hit test
and an on_press callback).

Add a new content type by subclassing Widget. Examples to build later:
    class WeatherWidget(Widget): ...        # icon + temp; reads a service
    class AnalogClockWidget(Widget): ...    # circle + hands
    class CameraWidget(Widget): ...         # latest frame from a feed
    class VerseWidget(Widget): ...          # word-wrapped text
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from theme import Theme, color, font_path


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x2 and self.y <= py < self.y2


# --- text helpers (widget-agnostic, used by several widget classes) ---

def fit_text(draw: ImageDraw.ImageDraw, text: str,
             font: ImageFont.FreeTypeFont, max_w: int) -> str:
    """Truncate text with an ellipsis until its rendered width fits max_w."""
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


def draw_centered(draw: ImageDraw.ImageDraw, text: str,
                  font: ImageFont.FreeTypeFont,
                  cx: float, cy: float,
                  fill: tuple[int, int, int]) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (bbox[2] - bbox[0]) / 2,
               cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
              text, font=font, fill=fill)


# --- halo / glow for foreground text over a dynamic background ------

def _luminance(rgb: tuple[int, int, int]) -> float:
    """Rec. 601 luma — good enough for the light/dark decision."""
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def draw_text_with_halo(target: Image.Image,
                        draw: ImageDraw.ImageDraw,
                        text: str,
                        font: ImageFont.FreeTypeFont,
                        x: float, y: float,
                        fg: tuple[int, int, int], *,
                        halo_alpha: int = 210,
                        halo_radius: float | None = None,
                        halo_color: tuple[int, int, int] | None
                        = None) -> None:
    """Draw `text` with a soft contrasting halo behind it.

    The halo is the standard cartographic / lock-screen approach to
    keeping text legible over imagery whose colour varies under the
    glyphs (e.g. our world-map background): light text gets a dark
    halo, dark text gets a light halo, picked from luminance unless
    the caller overrides. The halo is rendered as a blurred copy of
    the glyph shape on a transparent layer, then composited beneath
    the sharp foreground. Looks like a soft glow rather than a hard
    stroke — much less video-game UI, much more polished.
    """
    if halo_color is None:
        halo_color = (0, 0, 0) if _luminance(fg) > 130 else (255, 255, 255)
    if halo_radius is None:
        # Scale halo with the font's actual em size.
        try:
            asc, desc = font.getmetrics()
            halo_radius = max(2.0, (asc + desc) * 0.06)
        except AttributeError:
            halo_radius = 4.0
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    ldraw.text((x, y), text, font=font,
               fill=halo_color + (halo_alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=halo_radius))
    target.paste(layer, (0, 0), layer)
    draw.text((x, y), text, font=font, fill=fg)


def _draw_centered_with_halo(target: Image.Image,
                             draw: ImageDraw.ImageDraw,
                             text: str,
                             font: ImageFont.FreeTypeFont,
                             cx: float, cy: float,
                             fg: tuple[int, int, int]) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = cx - (bbox[2] - bbox[0]) / 2
    y = cy - (bbox[3] - bbox[1]) / 2 - bbox[1]
    draw_text_with_halo(target, draw, text, font, x, y, fg)


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def get_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """Process-wide cache. Loading a TTF is expensive; sizes repeat."""
    key = (path, size)
    f = _font_cache.get(key)
    if f is None:
        f = ImageFont.truetype(path, size)
        _font_cache[key] = f
    return f


# --- base ------------------------------------------------------------

class Widget:
    """Base class. Subclasses implement render(); override state_key()
    when render output depends on changing data.
    """

    def __init__(self, rect: Rect):
        self.rect = rect

    def render(self, draw: ImageDraw.ImageDraw, theme: Theme) -> None:
        raise NotImplementedError

    def state_key(self) -> tuple:
        """Hashable signature of inputs that affect render output. The
        compositor compares across frames to skip needless repaints.
        Static widgets return ()."""
        return ()


# --- text widgets ----------------------------------------------------

class TextWidget(Widget):
    """Single line, centered, with auto-truncate. Source can be a string
    or a callable returning a string (re-evaluated each render)."""

    def __init__(self, rect: Rect,
                 text_src: Callable[[], str] | str, *,
                 font_role: str = "bold",
                 font_factor: float = 0.5,
                 color_role: str = "fg_bright",
                 max_width_pad: float = 0.04,
                 halo: bool = False):
        super().__init__(rect)
        self._text_src = text_src
        self.font_role = font_role
        self.font_factor = font_factor
        self.color_role = color_role
        self.max_width_pad = max_width_pad
        # Halo only kicks in when the scene paints over a real
        # background image (Scene marks the draw context). Solid-bg
        # themes get a no-op so we don't ring the clock with a stray
        # glow on, say, the black RED_LED background.
        self.halo = halo

    def get_text(self) -> str:
        return (self._text_src() if callable(self._text_src)
                else self._text_src)

    def state_key(self) -> tuple:
        return (self.get_text(),)

    def render(self, draw, theme):
        text = self.get_text()
        if not text:
            return
        size = max(8, int(self.rect.h * self.font_factor))
        font = get_font(font_path(theme, self.font_role), size)
        max_w = int(self.rect.w * (1 - 2 * self.max_width_pad))
        text = fit_text(draw, text, font, max_w)
        fg = color(theme, self.color_role)
        if self.halo and getattr(draw, "_scene_has_bg_image", False):
            target = getattr(draw, "_image", None)
            if target is not None:
                _draw_centered_with_halo(
                    target, draw, text, font,
                    self.rect.cx, self.rect.cy, fg)
                return
        draw_centered(draw, text, font,
                      self.rect.cx, self.rect.cy,
                      color(theme, self.color_role))


# Dot-matrix VFD: sample a fairly dense grid from a real TTF, then run
# the rendered dots through a Gaussian blur. That blur is the trick —
# real VFDs sit behind a diffusing piece of smoked glass that smears
# the discrete fluorescent dots into continuous-looking strokes, with
# only a faint hint of the underlying matrix. Without blur, dots look
# like cheap LEDs; with too much blur, the digits go soft and lose
# definition. Tuning happens in _render_vfd.
_MATRIX_COLS = 22
_MATRIX_ROWS = 32
_MATRIX_THRESHOLD = 96
_MATRIX_SAMPLES_PER_CELL = 6

_MATRIX_GRID_CACHE: dict[tuple, list[str]] = {}


def _build_digit_grid(digit: str, cols: int, rows: int,
                      font_path: str) -> list[str]:
    """Rasterise `digit` in `font_path` into a hi-res bitmap, then
    LANCZOS-downsample to a `cols`×`rows` grid and threshold each cell.
    Cached after first call."""
    key = (digit, cols, rows, font_path)
    cached = _MATRIX_GRID_CACHE.get(key)
    if cached is not None:
        return cached
    sw = cols * _MATRIX_SAMPLES_PER_CELL
    sh = rows * _MATRIX_SAMPLES_PER_CELL
    font = ImageFont.truetype(font_path, int(sh * 0.86))
    img = Image.new("L", (sw, sh), 0)
    bbox = font.getbbox(digit)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (sw - text_w) / 2 - bbox[0]
    y = (sh - text_h) / 2 - bbox[1]
    ImageDraw.Draw(img).text((x, y), digit, font=font, fill=255)
    small = img.resize((cols, rows), Image.LANCZOS)
    px = list(small.getdata())
    grid: list[str] = []
    for r in range(rows):
        row = []
        for c in range(cols):
            row.append("X" if px[r * cols + c] > _MATRIX_THRESHOLD else ".")
        grid.append("".join(row))
    _MATRIX_GRID_CACHE[key] = grid
    return grid


def _dim(rgb, factor: float = 0.10):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def _draw_dot(draw, cx: float, cy: float, r: float,
              on: bool, on_col, off_col) -> None:
    """One matrix dot. At high density we don't paint a separate halo
    — neighbouring lit dots already form an implicit glow when the
    rasterised glyph has anti-aliased edges. Off-dots are kept very
    dim so they're felt rather than seen."""
    col = on_col if on else off_col
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)


# Dot diameter as a fraction of cell size. We deliberately let dots
# touch (fill = 1.0) — combined with the diffuser blur in _render_vfd,
# this produces continuous-looking strokes rather than visible dots.
_DOT_FILL = 1.0


def _draw_matrix_digit(draw, x: float, y: float, w: float, h: float,
                       digit: str, on_col, off_col,
                       font_path: str) -> None:
    pattern = _build_digit_grid(digit, _MATRIX_COLS, _MATRIX_ROWS,
                                font_path)
    cell_w = w / _MATRIX_COLS
    cell_h = h / _MATRIX_ROWS
    r = min(cell_w, cell_h) * _DOT_FILL / 2
    for row_i in range(_MATRIX_ROWS):
        row = pattern[row_i]
        cy = y + (row_i + 0.5) * cell_h
        for col_i in range(_MATRIX_COLS):
            cx = x + (col_i + 0.5) * cell_w
            on = col_i < len(row) and row[col_i] == "X"
            _draw_dot(draw, cx, cy, r, on, on_col, off_col)


def _draw_matrix_colon(draw, x: float, y: float, w: float, h: float,
                       on_col, off_col) -> None:
    """Two-dot colon at row indices 5 and 12 of an 18-row grid, so the
    dots sit visually within the digit baseline rather than at extremes."""
    cell_h = h / _MATRIX_ROWS
    r = min(w * 0.55, cell_h) * _DOT_FILL / 2
    cx = x + w / 2
    for row_i in (5, 12):
        cy = y + (row_i + 0.5) * cell_h
        _draw_dot(draw, cx, cy, r, True, on_col, off_col)


class ClockWidget(TextWidget):
    """HH:MM clock that picks its rendering style from theme.clock_style.

    "digital" — TextWidget render (current font), repaint once per minute.
    "vfd"     — seven-segment digits with dim "ghost" of unlit segments.

    State_key is minute-resolution regardless, so the scene only repaints
    when the time actually changes (or theme changes, tracked separately
    by the compositor).
    """

    def __init__(self, rect: Rect, *, fmt: str = "%H:%M",
                 font_factor: float = 0.7,
                 font_role: str = "bold",
                 color_role: str = "fg_bright"):
        self._fmt = fmt
        super().__init__(
            rect,
            text_src=lambda: time.strftime(self._fmt),
            font_role=font_role,
            font_factor=font_factor,
            color_role=color_role,
            halo=True,
        )

    def state_key(self) -> tuple:
        t = time.localtime()
        return (t.tm_year, t.tm_yday, t.tm_hour, t.tm_min)

    def render(self, draw, theme):
        style = getattr(theme, "clock_style", "digital")
        if style == "vfd":
            self._render_vfd(draw, theme)
            return
        super().render(draw, theme)

    def _render_vfd(self, draw, theme):
        text = time.strftime(self._fmt)
        if len(text) != 5 or text[2] != ":":
            super().render(draw, theme)
            return
        # Need access to the underlying PIL Image to paste a blurred
        # composite — ImageDraw exposes it as ._image (stable across
        # PIL versions for years). Fall back to digital if missing.
        target = getattr(draw, "_image", None)
        if target is None:
            super().render(draw, theme)
            return
        digits = text.replace(":", "")
        on_col = color(theme, self.color_role)
        off_col = _dim(on_col, 0.04)         # near-invisible ghost
        bg_col = color(theme, "bg")
        font_path_mono = font_path(theme, "mono")
        r = self.rect

        digit_h = int(r.h * 0.82)
        digit_w = int(digit_h * (_MATRIX_COLS / _MATRIX_ROWS))
        gap = max(3, int(digit_w * 0.10))
        colon_w = int(digit_w * 0.36)
        total_w = 4 * digit_w + colon_w + 4 * gap
        if total_w > r.w:
            scale = r.w / total_w
            digit_h = int(digit_h * scale)
            digit_w = int(digit_w * scale)
            gap = max(2, int(gap * scale))
            colon_w = int(colon_w * scale)
            total_w = 4 * digit_w + colon_w + 4 * gap

        # Paint the matrix to an off-screen layer at the same colour as
        # the scene background, then Gaussian-blur and paste. Blur
        # radius ≈ half a cell so adjacent on-dots merge into smooth
        # strokes — that's what gives the "diffused VFD" look.
        layer_w = total_w
        layer_h = digit_h
        layer = Image.new("RGB", (layer_w, layer_h), bg_col)
        ldraw = ImageDraw.Draw(layer)

        x = 0.0
        _draw_matrix_digit(ldraw, x, 0, digit_w, digit_h,
                           digits[0], on_col, off_col, font_path_mono)
        x += digit_w + gap
        _draw_matrix_digit(ldraw, x, 0, digit_w, digit_h,
                           digits[1], on_col, off_col, font_path_mono)
        x += digit_w + gap
        _draw_matrix_colon(ldraw, x, 0, colon_w, digit_h,
                           on_col, off_col)
        x += colon_w + gap
        _draw_matrix_digit(ldraw, x, 0, digit_w, digit_h,
                           digits[2], on_col, off_col, font_path_mono)
        x += digit_w + gap
        _draw_matrix_digit(ldraw, x, 0, digit_w, digit_h,
                           digits[3], on_col, off_col, font_path_mono)

        cell_px = digit_h / _MATRIX_ROWS
        # Just enough blur to soften the seams between adjacent circular
        # dots so the strokes look continuous — not enough to round off
        # digit edges or make the time hard to read.
        blur_r = max(0.6, cell_px * 0.22)
        blurred = layer.filter(ImageFilter.GaussianBlur(radius=blur_r))

        paste_x = int(r.cx - layer_w / 2)
        paste_y = int(r.cy - layer_h / 2)
        target.paste(blurred, (paste_x, paste_y))


class DateWidget(TextWidget):
    """Day-resolution date. Only redraws when the date rolls over."""

    def __init__(self, rect: Rect, *, fmt: str = "%a %d %b",
                 font_factor: float = 0.5,
                 font_role: str = "regular",
                 color_role: str = "fg_dim"):
        self._fmt = fmt
        # Date uses a thin regular weight; the halo blur eats too much
        # of the stroke and the text reads as fuzzy. Keep it sharp —
        # the date has plenty of contrast without halo, and the clock
        # remains the only widget that needs the legibility boost over
        # the map.
        super().__init__(
            rect,
            text_src=lambda: time.strftime(self._fmt),
            font_role=font_role,
            font_factor=font_factor,
            color_role=color_role,
        )

    def state_key(self) -> tuple:
        t = time.localtime()
        return (t.tm_year, t.tm_yday)


class WrappedTextWidget(Widget):
    """Word-wrapped text rendered with absolute font size, vertically
    centered in the rect. Used for the verse-of-the-day body where the
    payload length varies and "fit and ellipsize" wouldn't help.

    Paragraphs in the source are separated by ``\\n\\n``; everything
    else is joined as a single line and re-wrapped to the rect width.
    Long lines that still don't fit get a soft-break at the next space.
    """

    def __init__(self, rect: Rect,
                 text_src: Callable[[], str] | str, *,
                 font_role: str = "regular",
                 font_size: int = 32,
                 color_role: str = "fg_bright",
                 line_spacing: float = 1.30,
                 horizontal_pad: float = 0.04,
                 paragraph_gap: float = 0.6):
        super().__init__(rect)
        self._text_src = text_src
        self.font_role = font_role
        self.font_size = font_size
        self.color_role = color_role
        self.line_spacing = line_spacing
        self.pad = horizontal_pad
        self.paragraph_gap = paragraph_gap

    def get_text(self) -> str:
        return (self._text_src() if callable(self._text_src)
                else self._text_src)

    def state_key(self) -> tuple:
        return (self.get_text(), self.font_size)

    def render(self, draw, theme):
        text = self.get_text()
        if not text:
            return
        font = get_font(font_path(theme, self.font_role), self.font_size)
        max_w = int(self.rect.w * (1 - 2 * self.pad))
        lines: list[str] = []
        for para_idx, para in enumerate(text.split("\n\n")):
            if para_idx:
                lines.append("")  # paragraph spacer
            words = para.split()
            cur: list[str] = []
            for w in words:
                cand = " ".join(cur + [w])
                if draw.textbbox((0, 0), cand, font=font)[2] <= max_w:
                    cur.append(w)
                else:
                    if cur:
                        lines.append(" ".join(cur))
                    cur = [w]
            if cur:
                lines.append(" ".join(cur))
        ascent, descent = font.getmetrics()
        line_h = int((ascent + descent) * self.line_spacing)
        gap_h = int(line_h * self.paragraph_gap)
        # Total height: real lines + smaller gaps where lines are empty.
        total_h = sum(gap_h if not ln else line_h for ln in lines)
        y = self.rect.y + max(0, (self.rect.h - total_h) // 2)
        col = color(theme, self.color_role)
        for ln in lines:
            if not ln:
                y += gap_h
                continue
            bbox = draw.textbbox((0, 0), ln, font=font)
            line_w = bbox[2] - bbox[0]
            x = self.rect.cx - line_w / 2
            draw.text((x, y - bbox[1]), ln, font=font, fill=col)
            y += line_h


class TwoLineText(Widget):
    """Two stacked centered lines (e.g. station + ICY title)."""

    def __init__(self, rect: Rect,
                 line1_src: Callable[[], str],
                 line2_src: Callable[[], str], *,
                 line1_factor: float = 0.28,
                 line2_factor: float = 0.22,
                 line1_role: str = "fg_accent",
                 line2_role: str = "fg_subtle",
                 line1_font: str = "bold",
                 line2_font: str = "regular",
                 max_width_pad: float = 0.04):
        super().__init__(rect)
        self.line1_src = line1_src
        self.line2_src = line2_src
        self.l1f = line1_factor
        self.l2f = line2_factor
        self.l1r = line1_role
        self.l2r = line2_role
        self.l1fr = line1_font
        self.l2fr = line2_font
        self.pad = max_width_pad

    def state_key(self) -> tuple:
        return (self.line1_src(), self.line2_src())

    def render(self, draw, theme):
        max_w = int(self.rect.w * (1 - 2 * self.pad))
        l1 = self.line1_src()
        if l1:
            f = get_font(font_path(theme, self.l1fr),
                         max(8, int(self.rect.h * self.l1f)))
            draw_centered(draw, fit_text(draw, l1, f, max_w), f,
                          self.rect.cx,
                          self.rect.y + self.rect.h * 0.35,
                          color(theme, self.l1r))
        l2 = self.line2_src()
        if l2:
            f = get_font(font_path(theme, self.l2fr),
                         max(8, int(self.rect.h * self.l2f)))
            draw_centered(draw, fit_text(draw, l2, f, max_w), f,
                          self.rect.cx,
                          self.rect.y + self.rect.h * 0.72,
                          color(theme, self.l2r))


# --- weather icons ---------------------------------------------------

def _draw_sun(draw: ImageDraw.ImageDraw, cx: float, cy: float,
              r: float, fill) -> None:
    # Disk
    draw.ellipse([cx - r * 0.55, cy - r * 0.55,
                  cx + r * 0.55, cy + r * 0.55], fill=fill)
    # 8 rays
    width = max(2, int(r * 0.10))
    inner = r * 0.72
    outer = r * 1.05
    for i in range(8):
        a = i * math.pi / 4
        ca, sa = math.cos(a), math.sin(a)
        draw.line([cx + ca * inner, cy + sa * inner,
                   cx + ca * outer, cy + sa * outer],
                  fill=fill, width=width)


def _draw_cloud(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                s: float, fill) -> None:
    """Draw a cloud as the union of a flat base ellipse + two bumps."""
    base_w = s * 1.7
    base_h = s * 0.65
    draw.ellipse([cx - base_w / 2, cy - base_h / 2 + s * 0.1,
                  cx + base_w / 2, cy + base_h / 2 + s * 0.1], fill=fill)
    bump = s * 0.55
    draw.ellipse([cx - s * 0.55 - bump, cy - s * 0.20 - bump,
                  cx - s * 0.55 + bump, cy - s * 0.20 + bump], fill=fill)
    bump2 = s * 0.65
    draw.ellipse([cx + s * 0.10 - bump2, cy - s * 0.30 - bump2,
                  cx + s * 0.10 + bump2, cy - s * 0.30 + bump2], fill=fill)


def _draw_rain(draw: ImageDraw.ImageDraw, cx: float, cy: float,
               s: float, fill) -> None:
    drop_h = s * 0.7
    drop_w = max(3, int(s * 0.10))
    for dx in (-s * 0.55, 0.0, s * 0.55):
        x = cx + dx
        draw.line([x, cy - drop_h / 2, x, cy + drop_h / 2],
                  fill=fill, width=drop_w)


def _draw_snowflake(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                    r: float, fill) -> None:
    w = max(2, int(r * 0.18))
    # 3 axes (60° apart)
    for k in range(3):
        a = k * math.pi / 3
        ca, sa = math.cos(a), math.sin(a)
        draw.line([cx - ca * r, cy - sa * r,
                   cx + ca * r, cy + sa * r], fill=fill, width=w)


def _draw_snow(draw: ImageDraw.ImageDraw, cx: float, cy: float,
               s: float, fill) -> None:
    r = s * 0.30
    for dx in (-s * 0.55, 0.0, s * 0.55):
        _draw_snowflake(draw, cx + dx, cy, r, fill)


def _draw_lightning(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                    s: float, fill) -> None:
    pts = [
        (cx - s * 0.10, cy - s * 0.65),
        (cx + s * 0.25, cy - s * 0.05),
        (cx + s * 0.05, cy - s * 0.05),
        (cx + s * 0.30, cy + s * 0.65),
        (cx - s * 0.05, cy + s * 0.10),
        (cx + s * 0.15, cy + s * 0.10),
    ]
    draw.polygon(pts, fill=fill)


def _draw_fog(draw: ImageDraw.ImageDraw, cx: float, cy: float,
              s: float, fill) -> None:
    width = max(3, int(s * 0.16))
    for k, dy in enumerate((-s * 0.5, -s * 0.1, s * 0.3, s * 0.7)):
        # Slightly varying widths so it doesn't look like a barcode.
        w = s * (1.5 - 0.10 * k)
        draw.line([cx - w / 2, cy + dy, cx + w / 2, cy + dy],
                  fill=fill, width=width)


def draw_weather_icon(draw: ImageDraw.ImageDraw, rect: "Rect",
                      code: int, theme: Theme) -> None:
    """Render a weather icon for a WMO interpretation code, centered
    in `rect`. Uses palette weather_* roles so the colors match theme."""
    cx = rect.cx
    cy = rect.cy
    size = min(rect.w, rect.h)
    s = size * 0.40   # half-extent of the main element
    sun = color(theme, "weather_sun")
    cloud = color(theme, "weather_cloud")
    rain = color(theme, "weather_rain")
    snow = color(theme, "weather_snow")
    storm = color(theme, "weather_storm")
    code = int(code)
    if code == 0:
        _draw_sun(draw, cx, cy, s, sun)
    elif code in (1, 2):
        # Partly cloudy — small sun behind a cloud.
        _draw_sun(draw, cx - s * 0.40, cy - s * 0.30, s * 0.65, sun)
        _draw_cloud(draw, cx + s * 0.10, cy + s * 0.20, s * 0.95, cloud)
    elif code == 3:
        _draw_cloud(draw, cx, cy, s, cloud)
    elif code in (45, 48):
        _draw_fog(draw, cx, cy, s, cloud)
    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
                  80, 81, 82):
        _draw_cloud(draw, cx, cy - s * 0.35, s, cloud)
        _draw_rain(draw, cx, cy + s * 0.45, s, rain)
    elif code in (71, 73, 75, 77, 85, 86):
        _draw_cloud(draw, cx, cy - s * 0.35, s, cloud)
        _draw_snow(draw, cx, cy + s * 0.45, s, snow)
    elif code in (95, 96, 99):
        _draw_cloud(draw, cx, cy - s * 0.35, s, cloud)
        _draw_lightning(draw, cx, cy + s * 0.20, s, storm)
    else:
        _draw_cloud(draw, cx, cy, s, cloud)


# --- launcher app icons ----------------------------------------------
#
# All take (draw, rect, theme, col) and render centred in `rect`. Use the
# theme's fg_accent so the icon shares the active "controls" colour
# (amber on VFD theme, etc.). Procedural so they scale to any cell size.

def _icon_radio(draw, rect: "Rect", theme: Theme, col) -> None:
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.40
    w = max(2, int(s * 0.07))
    # Speaker box
    bw, bh = s * 0.95, s * 1.30
    draw.rounded_rectangle(
        [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2],
        radius=int(s * 0.12), outline=col, width=w)
    # Tweeter (small) and woofer (large)
    tr = s * 0.16
    wr = s * 0.30
    draw.ellipse([cx - tr, cy - bh * 0.30 - tr,
                  cx + tr, cy - bh * 0.30 + tr],
                 outline=col, width=w)
    draw.ellipse([cx - wr, cy + bh * 0.18 - wr,
                  cx + wr, cy + bh * 0.18 + wr],
                 outline=col, width=w)


def _icon_clock(draw, rect: "Rect", theme: Theme, col) -> None:
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.44
    w = max(2, int(s * 0.07))
    r = s * 0.90
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=col, width=w)
    # Hour ticks at 12/3/6/9
    tick = s * 0.12
    for ang in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        x1 = cx + math.cos(ang) * (r - tick)
        y1 = cy + math.sin(ang) * (r - tick)
        x2 = cx + math.cos(ang) * r
        y2 = cy + math.sin(ang) * r
        draw.line([x1, y1, x2, y2], fill=col, width=w)
    # Hands at "10:10"
    a_h = math.radians(-90 + 10 * 30)
    a_m = math.radians(-90 + 10 * 6)
    draw.line([cx, cy,
               cx + math.cos(a_h) * r * 0.50,
               cy + math.sin(a_h) * r * 0.50],
              fill=col, width=w)
    draw.line([cx, cy,
               cx + math.cos(a_m) * r * 0.72,
               cy + math.sin(a_m) * r * 0.72],
              fill=col, width=w)
    # Pivot
    pr = max(3, int(s * 0.07))
    draw.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=col)


def _icon_partly_cloudy(draw, rect: "Rect", theme: Theme, col) -> None:
    # Reuse the existing weather icon (sun + cloud), which uses its
    # own palette roles for sun/cloud colours so it always reads right.
    draw_weather_icon(draw, rect, 1, theme)


def _icon_book(draw, rect: "Rect", theme: Theme, col) -> None:
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.07))
    half_w = s * 0.95
    half_h = s * 0.70
    # Two pages — slightly raised at the spine to imply a curl
    draw.polygon([
        (cx - half_w, cy + half_h),
        (cx - half_w + s * 0.10, cy - half_h),
        (cx - s * 0.04, cy - half_h * 0.85),
        (cx - s * 0.04, cy + half_h),
    ], outline=col, width=w)
    draw.polygon([
        (cx + half_w, cy + half_h),
        (cx + half_w - s * 0.10, cy - half_h),
        (cx + s * 0.04, cy - half_h * 0.85),
        (cx + s * 0.04, cy + half_h),
    ], outline=col, width=w)
    # Three short lines on each page
    line_w = max(2, int(s * 0.05))
    for i in range(3):
        y = cy - half_h * 0.45 + i * s * 0.22
        draw.line([cx - half_w * 0.70, y, cx - s * 0.10, y],
                  fill=col, width=line_w)
        draw.line([cx + s * 0.10, y, cx + half_w * 0.70, y],
                  fill=col, width=line_w)


def _icon_camera(draw, rect: "Rect", theme: Theme, col) -> None:
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.07))
    bw, bh = s * 1.70, s * 1.20
    # Viewfinder bump on top
    draw.rounded_rectangle(
        [cx - bw * 0.18, cy - bh * 0.50 - s * 0.20,
         cx + bw * 0.18, cy - bh * 0.50 + s * 0.04],
        radius=int(s * 0.05), outline=col, width=w)
    # Body
    draw.rounded_rectangle(
        [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2],
        radius=int(s * 0.12), outline=col, width=w)
    # Lens (concentric rings)
    for lr in (s * 0.42, s * 0.26):
        draw.ellipse([cx - lr, cy - lr, cx + lr, cy + lr],
                     outline=col, width=w)


def _icon_gear(draw, rect: "Rect", theme: Theme, col) -> None:
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.07))
    # 8-tooth gear: alternate outer/inner radii every quarter-tooth so
    # the polygon traces a square-tooth profile.
    teeth = 8
    R_out = s * 0.95
    R_in = s * 0.70
    pts = []
    n = teeth * 4
    for i in range(n):
        a = (i * 2 * math.pi / n) - math.pi / 2
        # 0,1 are tooth tip (outer); 2,3 are gap (inner)
        r = R_out if (i % 4) in (1, 2) else R_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(pts, outline=col, width=w)
    # Hub
    hub = s * 0.32
    draw.ellipse([cx - hub, cy - hub, cx + hub, cy + hub],
                 outline=col, width=w)


# Look-up so LauncherScene can reference icons by name.
LAUNCHER_ICONS = {
    "radio": _icon_radio,
    "clock": _icon_clock,
    "partly_cloudy": _icon_partly_cloudy,
    "book": _icon_book,
    "camera": _icon_camera,
    "gear": _icon_gear,
}


class WeatherIconWidget(Widget):
    """Renders a procedural weather icon for a given WMO code."""

    def __init__(self, rect: Rect, code_src: Callable[[], int] | int):
        super().__init__(rect)
        self._code_src = code_src

    def get_code(self) -> int:
        return (int(self._code_src()) if callable(self._code_src)
                else int(self._code_src))

    def state_key(self) -> tuple:
        return (self.get_code(),)

    def render(self, draw: ImageDraw.ImageDraw, theme: Theme) -> None:
        draw_weather_icon(draw, self.rect, self.get_code(), theme)


# --- button ----------------------------------------------------------

class Button(Widget):
    """Outlined rectangle with a label. on_press is invoked with no args
    when the user taps inside rect (hit-tested by the scene).

    Corner shape comes from theme.button_style: "rounded" (default) uses
    PIL's rounded_rectangle, "outline" falls back to a square rectangle.
    """

    def __init__(self, rect: Rect,
                 label_src: Callable[[], str] | str,
                 on_press: Callable[[], None], *,
                 font_role: str = "bold",
                 font_factor: float = 0.22,
                 color_role: str = "fg_accent",
                 outline_role: str = "outline",
                 outline_width: int = 2,
                 inset: int = 6,
                 repeatable: bool = False,
                 halo: bool = False):
        super().__init__(rect)
        self._label_src = label_src
        self.on_press = on_press
        self.font_role = font_role
        self.font_factor = font_factor
        self.color_role = color_role
        self.outline_role = outline_role
        self.outline_width = outline_width
        self.inset = inset
        # Set briefly by Compositor when a tap is dispatched so the
        # next render shows a pressed visual — the user gets immediate
        # feedback even if the action's follow-up render takes a beat
        # (heavy scene transitions, world-map recomputes, etc.).
        self._pressed = False
        # Hold-to-repeat: when True, Compositor will fire on_press at
        # an accelerating cadence while the finger remains over this
        # button. Used for ▲/▼ digit bumpers and similar bulk-edit
        # affordances. The release-fires-action tap path is suppressed
        # for this press if any repeat has fired (prevents a final
        # double-bump on lift-off).
        self.repeatable = repeatable
        # Halo: same luminance-aware glow as TextWidget. Used on the
        # idle-screen alarm pill where the label sits over a busy
        # world-map background — fg_dim on the map alone reads as
        # mush; halo + fg_bright reads cleanly. Only kicks in when
        # the scene has a real bg image (see TextWidget for details).
        self.halo = halo

    def get_label(self) -> str:
        return (self._label_src() if callable(self._label_src)
                else self._label_src)

    def state_key(self) -> tuple:
        return (self.get_label(), bool(self._pressed))

    def render(self, draw, theme):
        # Empty label = button is contextually inactive; hide outline + text
        # so the scene doesn't show a meaningless box (e.g. SKIP NEXT row
        # when there's no alarm).
        label = self.get_label()
        if not label:
            return
        r = self.rect
        i = self.inset
        bbox = [r.x + i, r.y + i, r.x2 - i, r.y2 - i]
        pressed = self._pressed
        # When pressed, fill the button with the foreground colour and
        # flip the label to the theme bg colour. High-contrast inversion
        # is the most reliably visible feedback over either solid or
        # image backgrounds — no alpha tricks required.
        fill_col = color(theme, self.color_role) if pressed else None
        text_col = (color(theme, "bg") if pressed
                    else color(theme, self.color_role))
        # outline_width=0 → text-only button (still hit-testable). Used
        # in tight headers where a box would feel heavy. Pressed text-
        # only buttons still need to draw a backplate so the press is
        # visible — promote outline_width to 2 in that case.
        ow = self.outline_width or (2 if pressed else 0)
        if ow > 0 or pressed:
            outline_col = color(theme, self.outline_role)
            style = getattr(theme, "button_style", "rounded")
            if style == "rounded":
                radius = max(6, min(r.w, r.h) // 8)
                draw.rounded_rectangle(
                    bbox, radius=radius,
                    outline=outline_col,
                    fill=fill_col,
                    width=max(ow, 2 if pressed else ow))
            else:
                draw.rectangle(bbox, outline=outline_col,
                               fill=fill_col,
                               width=max(ow, 2 if pressed else ow))
        size = max(8, int(min(r.w, r.h) * self.font_factor))
        f = get_font(font_path(theme, self.font_role), size)
        # Truncate so long labels never spill past the button outline.
        max_w = int((r.w - 2 * i) * 0.92)
        text = fit_text(draw, label, f, max_w)
        if (self.halo and not pressed
                and getattr(draw, "_scene_has_bg_image", False)):
            target = getattr(draw, "_image", None)
            if target is not None:
                _draw_centered_with_halo(
                    target, draw, text, f, r.cx, r.cy, text_col)
                return
        draw_centered(draw, text, f, r.cx, r.cy, text_col)


# --- checkbox / radio row --------------------------------------------

class CheckboxRow(Button):
    """Row with a visible square checkbox (or circular radio) on the
    left and a label to the right. Tap anywhere on the row fires
    on_press. Use shape='check' for independent toggles, 'radio' for
    one-of-N choices."""

    def __init__(self, rect: Rect,
                 label_src: Callable[[], str] | str,
                 on_press: Callable[[], None],
                 is_on_src: Callable[[], bool], *,
                 shape: str = "check",
                 font_role: str = "bold",
                 font_factor: float = 0.5,
                 label_role: str = "fg_bright",
                 box_role: str = "fg_dim",
                 check_role: str = "fg_accent",
                 disabled_src: Callable[[], bool] | None = None):
        super().__init__(rect, label_src, on_press,
                         font_role=font_role,
                         font_factor=font_factor,
                         color_role=label_role,
                         outline_width=0,
                         inset=0)
        self._is_on = is_on_src
        self._disabled = disabled_src
        self.shape = shape
        self.label_role = label_role
        self.box_role = box_role
        self.check_role = check_role

    def state_key(self) -> tuple:
        return (self.get_label(), bool(self._is_on()),
                bool(self._disabled() if self._disabled else False),
                bool(self._pressed))

    def render(self, draw, theme):
        label = self.get_label()
        if not label:
            return
        r = self.rect
        on = bool(self._is_on())
        disabled = bool(self._disabled() if self._disabled else False)
        # Press feedback: paint a soft accent-coloured backplate
        # behind the row before drawing the box + label so the user
        # sees the tap landed even if the action is slow.
        if self._pressed:
            press_col = color(theme, "fg_accent")
            style = getattr(theme, "button_style", "rounded")
            if style == "rounded":
                radius = max(6, min(r.w, r.h) // 8)
                draw.rounded_rectangle(
                    [r.x, r.y, r.x2, r.y2],
                    radius=radius, fill=press_col)
            else:
                draw.rectangle([r.x, r.y, r.x2, r.y2], fill=press_col)
        box_side = max(12, int(r.h * 0.72))
        box_x = r.x + 4
        box_y = r.y + (r.h - box_side) // 2
        # When pressed we sit on top of an accent-coloured plate, so
        # the box/label switch to the theme bg colour for contrast.
        if self._pressed:
            col_label = color(theme, "bg")
            col_box = color(theme, "bg")
            col_check = color(theme, "bg")
        else:
            col_label = color(theme,
                              "fg_subtle" if disabled else self.label_role)
            col_box = color(theme,
                            "fg_subtle" if disabled else self.box_role)
            col_check = color(theme,
                              "fg_subtle" if disabled else self.check_role)
        if self.shape == "radio":
            draw.ellipse([box_x, box_y,
                          box_x + box_side, box_y + box_side],
                         outline=col_box, width=2)
            if on:
                pad = box_side // 4
                draw.ellipse([box_x + pad, box_y + pad,
                              box_x + box_side - pad,
                              box_y + box_side - pad],
                             fill=col_check)
        else:
            draw.rectangle([box_x, box_y,
                            box_x + box_side, box_y + box_side],
                           outline=col_box, width=2)
            if on:
                # Two-segment check, drawn as lines so it looks crisp
                # at small sizes regardless of font glyph availability.
                pad = max(2, box_side // 5)
                p1 = (box_x + pad,
                      box_y + box_side // 2)
                p2 = (box_x + box_side // 2 - 1,
                      box_y + box_side - pad - 1)
                p3 = (box_x + box_side - pad,
                      box_y + pad)
                lw = max(2, box_side // 7)
                draw.line([p1, p2], fill=col_check, width=lw)
                draw.line([p2, p3], fill=col_check, width=lw)
        # Label sits to the right of the box, vertically centered.
        text_x = box_x + box_side + 8
        size = max(10, int(r.h * self.font_factor))
        f = get_font(font_path(theme, self.font_role), size)
        max_w = max(0, r.x2 - text_x - 4)
        text = fit_text(draw, label, f, max_w)
        bbox = draw.textbbox((0, 0), text, font=f)
        ty = r.cy - (bbox[3] - bbox[1]) / 2 - bbox[1]
        draw.text((text_x, ty), text, font=f, fill=col_label)


# --- launcher tile (icon + label) ------------------------------------

class AppTile(Button):
    """Button with an icon drawn above the label — used for the
    launcher grid so each app gets a visual identity instead of a
    bare-text rectangle. icon_drawer signature: (draw, rect, theme,
    color)."""

    def __init__(self, rect: Rect,
                 label_src,
                 on_press,
                 icon_drawer, *,
                 font_role: str = "bold",
                 font_factor: float = 0.18,
                 color_role: str = "fg_accent",
                 outline_role: str = "outline",
                 outline_width: int = 2,
                 inset: int = 6,
                 icon_color_role: str = "fg_accent"):
        super().__init__(rect, label_src, on_press,
                         font_role=font_role,
                         font_factor=font_factor,
                         color_role=color_role,
                         outline_role=outline_role,
                         outline_width=outline_width,
                         inset=inset)
        self._icon_drawer = icon_drawer
        self.icon_color_role = icon_color_role

    def render(self, draw, theme):
        r = self.rect
        i = self.inset
        bbox = [r.x + i, r.y + i, r.x2 - i, r.y2 - i]
        outline_col = color(theme, self.outline_role)
        style = getattr(theme, "button_style", "rounded")
        if style == "rounded":
            radius = max(8, min(r.w, r.h) // 8)
            draw.rounded_rectangle(bbox, radius=radius,
                                   outline=outline_col,
                                   width=self.outline_width)
        else:
            draw.rectangle(bbox, outline=outline_col,
                           width=self.outline_width)
        # Split the inner area: icon on top (~64%), label at the bottom
        # (~28%), with a small gap between the two.
        inner_h = r.h - 2 * i
        inner_w = r.w - 2 * i
        icon_h = int(inner_h * 0.64)
        gap_h = int(inner_h * 0.06)
        label_h = inner_h - icon_h - gap_h
        icon_rect = Rect(r.x + i, r.y + i, inner_w, icon_h)
        if self._icon_drawer is not None:
            self._icon_drawer(draw, icon_rect, theme,
                              color(theme, self.icon_color_role))
        # Label, fit-to-width. Pick the largest font size whose
        # measured width still fits within `avail_w` (binary-search
        # by shrinking from the height-derived cap). This lets short
        # labels like "RADIO" use the full label-band height while
        # 8-letter labels like "SETTINGS" auto-shrink rather than
        # ellipsis-truncate.
        avail_w = int(inner_w * 0.92)
        label = self.get_label()
        size = max(8, int(label_h * 0.78))
        f = get_font(font_path(theme, self.font_role), size)
        while size > 10:
            bbox = draw.textbbox((0, 0), label, font=f)
            if bbox[2] - bbox[0] <= avail_w:
                break
            size -= 2
            f = get_font(font_path(theme, self.font_role), size)
        label_y = r.y + i + icon_h + gap_h
        text = fit_text(draw, label, f, avail_w)
        draw_centered(draw, text, f,
                      r.cx, label_y + label_h / 2,
                      color(theme, self.color_role))


# --- color-pair swatch (used by ThemeScene) --------------------------

class ColorPairWidget(Widget):
    """Two filled rounded rectangles side-by-side, previewing the
    primary/accent colour pair of a theme. Static (state_key reflects
    the chosen colours so the compositor only repaints on change)."""

    def __init__(self, rect: Rect,
                 color_a: tuple, color_b: tuple, *,
                 outline_color: tuple = (0, 0, 0),
                 outline_width: int = 1,
                 gap: int = 4,
                 corner_factor: float = 0.18):
        super().__init__(rect)
        self.color_a = color_a
        self.color_b = color_b
        self.outline_color = outline_color
        self.outline_width = outline_width
        self.gap = gap
        self.corner_factor = corner_factor

    def state_key(self) -> tuple:
        return (self.color_a, self.color_b)

    def render(self, draw, theme):
        r = self.rect
        half = (r.w - self.gap) // 2
        radius = max(3, int(min(half, r.h) * self.corner_factor))
        # Left swatch
        draw.rounded_rectangle(
            [r.x, r.y, r.x + half, r.y2],
            radius=radius,
            fill=self.color_a,
            outline=self.outline_color,
            width=self.outline_width)
        # Right swatch
        draw.rounded_rectangle(
            [r.x + half + self.gap, r.y, r.x2, r.y2],
            radius=radius,
            fill=self.color_b,
            outline=self.outline_color,
            width=self.outline_width)


# --- bell icon (alarm indicator) -------------------------------------

def _icon_bell(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Filled bell shape sized to fit `rect`, centred. Drawn directly
    in PIL primitives so it works regardless of the font's emoji
    coverage (DejaVu Sans Bold has no 🔔/⏰ glyphs)."""
    s = min(rect.w, rect.h)
    # Bell occupies ~75% of the rect height to leave space for the
    # clapper at the bottom.
    body_h = int(s * 0.66)
    body_w = int(s * 0.62)
    cx = int(rect.cx)
    # Vertical layout: small bow up top, body, small clapper at bottom.
    top_y = int(rect.cy - s * 0.42)
    body_top = top_y + max(2, s // 12)
    body_bot = body_top + body_h
    # Bow / handle: a short stem above the bell body.
    stem_w = max(2, s // 12)
    draw.rectangle(
        [cx - stem_w // 2, top_y,
         cx + (stem_w + 1) // 2, body_top + 1],
        fill=col)
    # Body: rounded-top rectangle (PIL has rounded_rectangle since 8.2).
    draw.rounded_rectangle(
        [cx - body_w // 2, body_top,
         cx + body_w // 2, body_bot],
        radius=body_w // 2,
        fill=col)
    # Skirt flare: small triangles on each side of the lower body
    # that give a bell its silhouette.
    flare = max(2, s // 14)
    draw.polygon(
        [(cx - body_w // 2, body_bot - flare),
         (cx + body_w // 2, body_bot - flare),
         (cx + body_w // 2 + flare, body_bot),
         (cx - body_w // 2 - flare, body_bot)],
        fill=col)
    # Clapper: small filled circle just below the body.
    clap_r = max(2, s // 12)
    draw.ellipse(
        [cx - clap_r, body_bot + clap_r // 2,
         cx + clap_r, body_bot + clap_r // 2 + clap_r * 2],
        fill=col)


class BellIconWidget(Widget):
    """Static filled-bell glyph. Visible iff `is_visible_src()` returns
    truthy — used as the "alarm armed" indicator beside the alarm time
    on AlarmFiringScene + IdleScene's next-alarm preview."""

    def __init__(self, rect: Rect, *,
                 is_visible_src=None,
                 color_role: str = "fg_accent"):
        super().__init__(rect)
        self._is_visible = is_visible_src or (lambda: True)
        self.color_role = color_role

    def state_key(self) -> tuple:
        return (bool(self._is_visible()),)

    def render(self, draw, theme):
        if not self._is_visible():
            return
        _icon_bell(draw, self.rect, color(theme, self.color_role))


# --- wifi status indicator -------------------------------------------

def _icon_wifi(draw: ImageDraw.ImageDraw, rect: Rect,
               col, *, connected: bool) -> None:
    """Three concentric arcs over a dot (wifi fan) when connected.
    When disconnected: outer arc only with a slash through it.

    The dot anchors the bottom and the fan opens upward; cy is offset
    so the *visual* midpoint of the fan+dot lands on rect.cy, not the
    dot itself — keeps the glyph aligned with adjacent text baselines.
    """
    cx = rect.cx
    s = min(rect.w, rect.h) * 0.42
    # Visual midpoint of (top arc, dot) = cy - 0.475*s; we want that on
    # rect.cy, so push cy down by half the fan height.
    cy = rect.cy + s * 0.475
    w = max(2, int(s * 0.20))
    if connected:
        for r in (s * 0.95, s * 0.65, s * 0.35):
            draw.arc([cx - r, cy - r, cx + r, cy + r],
                     215, 325, fill=col, width=w)
        d = max(2, int(s * 0.16))
        draw.ellipse([cx - d, cy - d, cx + d, cy + d], fill=col)
    else:
        r = s * 0.95
        draw.arc([cx - r, cy - r, cx + r, cy + r],
                 215, 325, fill=col, width=w)
        # Slash bottom-left → top-right; long enough to clearly cross.
        pad_x = rect.w * 0.18
        pad_y = rect.h * 0.20
        draw.line([rect.x + pad_x, rect.y + rect.h - pad_y,
                   rect.x + rect.w - pad_x, rect.y + pad_y],
                  fill=col, width=w)


class WifiStatusWidget(Widget):
    """Wifi connectivity glyph for header bars. Reads state from a
    WifiService — `state == "connected"` lights up the full fan;
    anything else renders the disconnected variant."""

    def __init__(self, rect: Rect, wifi_service, *,
                 color_role: str = "fg_dim",
                 disconnected_role: str = "fg_dim"):
        super().__init__(rect)
        self._wifi = wifi_service
        self.color_role = color_role
        self.disconnected_role = disconnected_role

    def state_key(self) -> tuple:
        return (self._wifi.status.state == "connected",)

    def render(self, draw, theme):
        connected = self._wifi.status.state == "connected"
        role = self.color_role if connected else self.disconnected_role
        _icon_wifi(draw, self.rect, color(theme, role),
                   connected=connected)


# --- header / settings icons (3-arg drawers: draw, rect, col) --------
#
# These drawers take a colour directly so they can be reused outside a
# theme context (e.g. inside an IconButton that has already resolved
# its own colour roles). Anything that wants palette-aware colouring
# resolves it before the call.

class RenderingIndicatorWidget(Widget):
    """Small dot painted while the background provider is mid-render.

    Static (no animation) so the compositor doesn't burn cycles
    re-rendering several times per second while the worker is also
    doing heavy work — the dot's transition on/off is the signal.

    is_active_src returns True iff the bg provider is currently
    rendering or last served a stale image. Tied into state_key so the
    compositor invalidates exactly on the transition."""

    def __init__(self, rect: Rect,
                 is_active_src: "Callable[[], bool]", *,
                 color_role: str = "fg_dim"):
        super().__init__(rect)
        self._is_active = is_active_src
        self.color_role = color_role

    def _active(self) -> bool:
        try:
            return bool(self._is_active())
        except Exception:
            return False

    def state_key(self) -> tuple:
        return ("ind", self._active())

    def render(self, draw: ImageDraw.ImageDraw, theme: Theme) -> None:
        if not self._active():
            return
        col = color(theme, self.color_role)
        r = self.rect
        radius = max(3, min(r.w, r.h) // 2)
        draw.ellipse(
            [r.cx - radius, r.cy - radius,
             r.cx + radius, r.cy + radius],
            fill=col,
        )


def _icon_back_arrow(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Chevron-style back arrow ("<"), centred in rect."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h)
    w = max(3, int(s * 0.12))
    arm = s * 0.30
    draw.line([(cx + arm * 0.5, cy - arm), (cx - arm, cy)],
              fill=col, width=w)
    draw.line([(cx + arm * 0.5, cy + arm), (cx - arm, cy)],
              fill=col, width=w)


def _icon_chevron_up(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Chevron pointing up ("^"). Used as a paging affordance on long
    list overlays."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h)
    w = max(3, int(s * 0.12))
    arm = s * 0.30
    draw.line([(cx - arm, cy + arm * 0.5), (cx, cy - arm)],
              fill=col, width=w)
    draw.line([(cx + arm, cy + arm * 0.5), (cx, cy - arm)],
              fill=col, width=w)


def _icon_chevron_down(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Chevron pointing down — paging counterpart to _icon_chevron_up."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h)
    w = max(3, int(s * 0.12))
    arm = s * 0.30
    draw.line([(cx - arm, cy - arm * 0.5), (cx, cy + arm)],
              fill=col, width=w)
    draw.line([(cx + arm, cy - arm * 0.5), (cx, cy + arm)],
              fill=col, width=w)


def _icon_speaker(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Speaker silhouette + two sound arcs."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.10))
    box_x = cx - s * 0.62
    box_w = s * 0.22
    box_top = cy - s * 0.22
    box_bot = cy + s * 0.22
    cone_top = cy - s * 0.50
    cone_bot = cy + s * 0.50
    cone_x = box_x + box_w
    cone_w = s * 0.30
    draw.polygon(
        [(box_x, box_top),
         (cone_x, box_top),
         (cone_x + cone_w, cone_top),
         (cone_x + cone_w, cone_bot),
         (cone_x, box_bot),
         (box_x, box_bot)],
        fill=col,
    )
    arc_x = cone_x + cone_w + s * 0.10
    for r in (s * 0.32, s * 0.55):
        draw.arc([arc_x - r, cy - r, arc_x + r, cy + r],
                 -50, 50, fill=col, width=w)


def _icon_brightness(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Sun glyph: filled disc + 8 rays."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.10))
    r = s * 0.40
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    ray_in = r * 1.40
    ray_out = r * 1.95
    for k in range(8):
        a = k * math.pi / 4
        x1 = cx + ray_in * math.cos(a)
        y1 = cy + ray_in * math.sin(a)
        x2 = cx + ray_out * math.cos(a)
        y2 = cy + ray_out * math.sin(a)
        draw.line([x1, y1, x2, y2], fill=col, width=w)


def _icon_palette(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Three swatches in a row — generic 'theme' / 'colours' glyph."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    sq = s * 0.50
    gap = s * 0.12
    total_w = 3 * sq + 2 * gap
    x = cx - total_w / 2
    for i in range(3):
        sx = x + i * (sq + gap)
        draw.rounded_rectangle(
            [sx, cy - sq / 2, sx + sq, cy + sq / 2],
            radius=int(sq * 0.20),
            fill=col,
        )


def _icon_globe(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Stylised globe — circle with equator + meridians."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.09))
    r = s * 0.85
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=col, width=w)
    # Equator
    draw.line([cx - r, cy, cx + r, cy], fill=col, width=w)
    # Meridian (drawn as narrow ellipse to suggest curvature)
    draw.ellipse([cx - r * 0.45, cy - r,
                  cx + r * 0.45, cy + r],
                 outline=col, width=w)


def _icon_info(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """Info glyph: 'i' inside a circle."""
    cx, cy = rect.cx, rect.cy
    s = min(rect.w, rect.h) * 0.42
    w = max(2, int(s * 0.09))
    r = s * 0.85
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=col, width=w)
    dot_r = max(2, int(s * 0.10))
    dot_y = cy - s * 0.36
    draw.ellipse([cx - dot_r, dot_y - dot_r,
                  cx + dot_r, dot_y + dot_r], fill=col)
    stem_w = max(3, int(s * 0.16))
    draw.rectangle(
        [cx - stem_w / 2, cy - s * 0.10,
         cx + stem_w / 2, cy + s * 0.42],
        fill=col,
    )


def _icon_wifi_full(draw: ImageDraw.ImageDraw, rect: Rect, col) -> None:
    """3-arg wrapper around _icon_wifi for use as a settings-row glyph."""
    _icon_wifi(draw, rect, col, connected=True)


# Settings-list icon table — referenced by name from SettingsScene so
# the row labels and their glyphs stay together as a single mapping.
SETTINGS_ICONS = {
    "wifi": _icon_wifi_full,
    "speaker": _icon_speaker,
    "palette": _icon_palette,
    "globe": _icon_globe,
    "brightness": _icon_brightness,
    "info": _icon_info,
}


# --- icon-only button + icon-row button -------------------------------

class IconButton(Button):
    """Button whose face is a procedural icon instead of text. Used for
    the universal back arrow and any other glyph-only affordance.
    `icon_drawer` signature: (draw, rect, col)."""

    def __init__(self, rect: Rect,
                 on_press: Callable[[], None],
                 icon_drawer: Callable, *,
                 color_role: str = "fg_accent",
                 outline_role: str = "outline",
                 outline_width: int = 2,
                 inset: int = 6,
                 icon_factor: float = 0.55):
        super().__init__(rect, label_src="", on_press=on_press,
                         color_role=color_role,
                         outline_role=outline_role,
                         outline_width=outline_width,
                         inset=inset)
        self._icon_drawer = icon_drawer
        self.icon_factor = icon_factor

    def state_key(self) -> tuple:
        return ("icon", bool(self._pressed))

    def render(self, draw, theme):
        r = self.rect
        i = self.inset
        bbox = [r.x + i, r.y + i, r.x2 - i, r.y2 - i]
        pressed = self._pressed
        fill_col = color(theme, self.color_role) if pressed else None
        icon_col = (color(theme, "bg") if pressed
                    else color(theme, self.color_role))
        ow = self.outline_width or (2 if pressed else 0)
        if ow > 0 or pressed:
            outline_col = color(theme, self.outline_role)
            style = getattr(theme, "button_style", "rounded")
            if style == "rounded":
                radius = max(6, min(r.w, r.h) // 8)
                draw.rounded_rectangle(
                    bbox, radius=radius,
                    outline=outline_col, fill=fill_col,
                    width=max(ow, 2 if pressed else ow))
            else:
                draw.rectangle(
                    bbox, outline=outline_col, fill=fill_col,
                    width=max(ow, 2 if pressed else ow))
        iw = (r.w - 2 * i) * self.icon_factor
        ih = (r.h - 2 * i) * self.icon_factor
        icon_rect = Rect(int(r.cx - iw / 2), int(r.cy - ih / 2),
                         int(iw), int(ih))
        if self._icon_drawer is not None:
            self._icon_drawer(draw, icon_rect, icon_col)


class IconRow(Button):
    """Settings-list row: icon on the left, label left-anchored to its
    right. Tap anywhere on the row fires on_press. The button outline
    extends across the whole row so the press-state inversion lands on
    a single coherent shape (icon + label both flip)."""

    def __init__(self, rect: Rect,
                 label_src: Callable[[], str] | str,
                 on_press: Callable[[], None],
                 icon_drawer: Callable, *,
                 font_role: str = "bold",
                 font_factor: float = 0.32,
                 color_role: str = "fg_bright",
                 outline_role: str = "outline",
                 outline_width: int = 2,
                 inset: int = 6,
                 icon_color_role: str = "fg_accent"):
        super().__init__(rect, label_src, on_press,
                         font_role=font_role,
                         font_factor=font_factor,
                         color_role=color_role,
                         outline_role=outline_role,
                         outline_width=outline_width,
                         inset=inset)
        self._icon_drawer = icon_drawer
        self.icon_color_role = icon_color_role

    def render(self, draw, theme):
        label = self.get_label()
        if not label:
            return
        r = self.rect
        i = self.inset
        bbox = [r.x + i, r.y + i, r.x2 - i, r.y2 - i]
        pressed = self._pressed
        fill_col = color(theme, self.color_role) if pressed else None
        text_col = (color(theme, "bg") if pressed
                    else color(theme, self.color_role))
        icon_col = (color(theme, "bg") if pressed
                    else color(theme, self.icon_color_role))
        outline_col = color(theme, self.outline_role)
        style = getattr(theme, "button_style", "rounded")
        if style == "rounded":
            radius = max(6, min(r.w, r.h) // 8)
            draw.rounded_rectangle(
                bbox, radius=radius,
                outline=outline_col, fill=fill_col,
                width=self.outline_width)
        else:
            draw.rectangle(
                bbox, outline=outline_col, fill=fill_col,
                width=self.outline_width)
        # Icon zone — square area on the left, sized to the row height.
        inner_h = r.h - 2 * i
        icon_zone = inner_h
        icon_pad = int(icon_zone * 0.18)
        icon_rect = Rect(r.x + i + icon_pad,
                         r.y + i + icon_pad,
                         icon_zone - 2 * icon_pad,
                         icon_zone - 2 * icon_pad)
        if self._icon_drawer is not None:
            self._icon_drawer(draw, icon_rect, icon_col)
        # Label area: everything to the right of the icon zone.
        label_x = r.x + i + icon_zone + int(r.w * 0.02)
        label_w = r.x2 - i - label_x
        size = max(8, int(r.h * self.font_factor))
        f = get_font(font_path(theme, self.font_role), size)
        text = fit_text(draw, label, f, int(label_w * 0.96))
        bbox_t = draw.textbbox((0, 0), text, font=f)
        text_h = bbox_t[3] - bbox_t[1]
        text_y = r.cy - text_h / 2 - bbox_t[1]
        draw.text((label_x, text_y), text, font=f, fill=text_col)
