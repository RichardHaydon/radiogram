"""Guided demo / tour.

A scripted walk through the device's main features. The service is a
small state machine driven by per-frame ticks from the main loop. Each
step has a target overlay scene + a dwell duration + a caption shown
in a translucent band over the live UI; when the dwell elapses the
service advances to the next step. The caption band also exposes
EXIT and NEXT buttons so the user can bail or skip ahead.

Snapshot/restore policy
-----------------------
Background is the only preference the demo actually mutates (cycling
globe → atlas at the start to showcase the visual). On `start()` we
capture the current background mode + overlays + center longitude;
on `exit()` (whether triggered by EXIT, the natural last step, or the
caller) we put them back. Theme and brightness are *navigated to* but
never written, so they need no snapshot.

Why a separate service rather than another Scene
------------------------------------------------
The demo isn't one screen — it sequences existing scenes. Living
above the compositor lets it call set_overlay() to drive whatever
already works (Wifi, BackgroundScene, etc.) instead of re-implementing
those screens in a tour-only mode.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageDraw

from background_service import VALID_OVERLAYS
from theme import Theme, color, font_path


# Special "scene name" the demo uses to mean: clear the overlay and
# show the underlying idle/radio home screen. Real scenes never use a
# name with a leading underscore.
HOME_SENTINEL = "_home"


@dataclass(frozen=True)
class DemoStep:
    scene: str
    dwell_s: float
    caption: str
    # pre_action signature: (background_service) -> None. Only the
    # background is mutated during the tour (see module docstring).
    pre_action: Optional[Callable] = None


def _build_steps(*, length: str, include_wifi: bool) -> list[DemoStep]:
    """Compose the step list from the user's intro-screen choices.

    `length` is "full" or "short" (short skips the secondary visual
    styles + the verse/weather mini-tours). `include_wifi` shows the
    Wifi setup screen as a step — typically off when already
    connected, since the user has nothing left to do there.
    """
    full = (length == "full")
    steps: list[DemoStep] = []

    # Intro: home screen with the globe forced on so the user sees
    # the headline visual feature first regardless of what bg they
    # had before. Subsequent steps walk through the variants.
    steps.append(DemoStep(
        HOME_SENTINEL, 6.0,
        "Welcome. This is the clock face — the world map "
        "shows real-time daylight across the planet.",
        pre_action=lambda bg: bg.set_mode("world_map_globe"),
    ))
    if full:
        steps.append(DemoStep(
            HOME_SENTINEL, 5.0,
            "The lit hemisphere follows the sun in real time.",
        ))
        steps.append(DemoStep(
            HOME_SENTINEL, 5.0,
            "Other map styles include atlas, slate, vintage and blueprint.",
            pre_action=lambda bg: bg.set_mode("world_map_atlas"),
        ))
    else:
        steps.append(DemoStep(
            HOME_SENTINEL, 5.0,
            "Map styles include globe, atlas, slate, vintage and blueprint.",
            pre_action=lambda bg: bg.set_mode("world_map_atlas"),
        ))

    # Settings tour
    steps.append(DemoStep(
        "settings", 5.0,
        "Settings — wifi, audio, themes, background, brightness, about.",
    ))
    steps.append(DemoStep(
        "background", 6.0,
        "Pick a base map style and stack overlays "
        "(city lights, water, borders, annotations).",
    ))
    if full:
        steps.append(DemoStep(
            "theme", 5.0,
            "Themes change the colour palette across every screen.",
        ))
    steps.append(DemoStep(
        "brightness", 5.0,
        "Two brightness levels — active and idle dim — "
        "and a night-red mode that preserves dark adaptation.",
    ))
    if include_wifi:
        steps.append(DemoStep(
            "wifi", 8.0,
            "Wifi: tap RESCAN, pick a network, enter its password to connect.",
        ))

    # Apps tour
    steps.append(DemoStep(
        "launcher", 5.0,
        "Tap anywhere on the clock face to open Apps.",
    ))
    steps.append(DemoStep(
        "station_list", 7.0,
        "Internet radio — tap a station to start streaming.",
    ))
    steps.append(DemoStep(
        "alarm_list", 7.0,
        "Alarms — set a time, days of the week, and which station plays.",
    ))
    if full:
        steps.append(DemoStep(
            "weather", 5.0,
            "A short forecast for your saved location.",
        ))
        steps.append(DemoStep(
            "verse", 5.0,
            "A daily verse — quiet bedside reading.",
        ))

    # Outro: back home, with closing caption.
    steps.append(DemoStep(
        HOME_SENTINEL, 6.0,
        "That's the tour. Your previous settings have been restored.",
    ))
    return steps


class DemoService:
    """Drives the demo state machine and surfaces caption text.

    Lifecycle:
        attach(compositor)          — wire once at startup
        start(length, include_wifi) — begin a run
        tick()                      — call every frame; auto-advance
        next_step()                 — fired by the NEXT button
        exit()                      — fired by EXIT or natural end
    """

    def __init__(self, *, background_service):
        self._bg = background_service
        self._compositor = None
        self._steps: list[DemoStep] = []
        self._idx: int = -1
        self._step_started: float = 0.0
        self._active: bool = False
        self._snapshot: Optional[dict] = None

    def attach(self, compositor) -> None:
        self._compositor = compositor

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def caption(self) -> str:
        if not self._active or self._idx < 0:
            return ""
        return self._steps[self._idx].caption

    @property
    def step_index(self) -> int:
        return self._idx

    @property
    def step_count(self) -> int:
        return len(self._steps)

    def remaining_s(self) -> float:
        if not self._active or self._idx < 0:
            return 0.0
        step = self._steps[self._idx]
        return max(0.0, step.dwell_s
                   - (time.monotonic() - self._step_started))

    def start(self, *, length: str, include_wifi: bool) -> None:
        if self._active:
            return
        self._snapshot = self._capture_snapshot()
        self._steps = _build_steps(
            length=length, include_wifi=include_wifi)
        self._idx = -1
        self._active = True
        self._advance()

    def tick(self) -> None:
        if not self._active or self._idx < 0:
            return
        if self.remaining_s() <= 0.0:
            self._advance()

    def next_step(self) -> None:
        if not self._active:
            return
        self._advance()

    def exit(self) -> None:
        if not self._active:
            return
        self._active = False
        self._idx = -1
        self._restore_snapshot()
        if self._compositor is not None:
            self._compositor.clear_overlay()

    def _advance(self) -> None:
        self._idx += 1
        if self._idx >= len(self._steps):
            self.exit()
            return
        step = self._steps[self._idx]
        if step.pre_action is not None:
            try:
                step.pre_action(self._bg)
            except Exception:
                # A failed pre_action shouldn't derail the tour — the
                # caption still describes the feature even if the
                # backing state didn't change.
                pass
        if self._compositor is not None:
            if step.scene == HOME_SENTINEL:
                self._compositor.clear_overlay()
            else:
                self._compositor.set_overlay(step.scene)
        self._step_started = time.monotonic()

    def _capture_snapshot(self) -> dict:
        return {
            "bg_mode": self._bg.mode,
            "bg_overlays": tuple(o for o in VALID_OVERLAYS
                                 if self._bg.is_overlay(o)),
            "bg_center_lon": self._bg.center_lon,
        }

    def _restore_snapshot(self) -> None:
        if self._snapshot is None:
            return
        s = self._snapshot
        try:
            self._bg.set_mode(s["bg_mode"])
            cur = set(o for o in VALID_OVERLAYS
                      if self._bg.is_overlay(o))
            tgt = set(s["bg_overlays"])
            for name in cur.symmetric_difference(tgt):
                self._bg.toggle_overlay(name)
            self._bg.set_center_lon(s["bg_center_lon"])
        except Exception:
            pass


# =====================================================================
# Caption overlay — the translucent band painted on top of whatever
# scene is showing while the demo is active.
# =====================================================================
class CaptionOverlay:
    """Bottom-of-screen translucent caption band with EXIT / NEXT
    buttons. Owned by the compositor; not a Scene because it composites
    over whatever scene is currently presented rather than replacing it.

    The band is always painted in a fixed dark+light scheme rather than
    theme colours so the caption stays readable regardless of what
    palette the user lands on (a "Marine Chart" caption over a black
    "RED LED" theme would otherwise be invisible).
    """

    # Fixed colours so legibility doesn't depend on the active theme.
    PANEL_RGBA = (16, 18, 24, 220)
    TEXT_FG = (235, 235, 240)
    META_FG = (170, 175, 185)
    BTN_OUTLINE = (210, 215, 225)
    BTN_TEXT = (235, 235, 240)
    BTN_PRESSED_FILL = (235, 235, 240)
    BTN_PRESSED_TEXT = (16, 18, 24)

    def __init__(self, canvas_w: int, canvas_h: int):
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        # Band height: enough for 3 lines of caption + a button row.
        self.band_h = int(canvas_h * 0.24)
        self.band_y = canvas_h - self.band_h
        # Buttons sit in the bottom 38% of the band, padded.
        btn_h = int(self.band_h * 0.34)
        btn_w = int(canvas_w * 0.24)
        margin_x = int(canvas_w * 0.04)
        btn_y = canvas_h - btn_h - int(self.band_h * 0.10)
        self.exit_x = margin_x
        self.exit_y = btn_y
        self.exit_w = btn_w
        self.exit_h = btn_h
        self.next_x = canvas_w - margin_x - btn_w
        self.next_y = btn_y
        self.next_w = btn_w
        self.next_h = btn_h
        self._service: Optional[DemoService] = None

    def attach(self, service: DemoService) -> None:
        self._service = service

    # --- hit testing -------------------------------------------------

    def hit(self, cx: float, cy: float) -> str:
        """Return "exit" / "next" / "swallow". The compositor swallows
        everything else during a demo so underlying-scene buttons can't
        accidentally fire under the caption band."""
        if (self.exit_x <= cx < self.exit_x + self.exit_w
                and self.exit_y <= cy < self.exit_y + self.exit_h):
            return "exit"
        if (self.next_x <= cx < self.next_x + self.next_w
                and self.next_y <= cy < self.next_y + self.next_h):
            return "next"
        return "swallow"

    # --- rendering ---------------------------------------------------

    def render(self, image: Image.Image, theme: Theme) -> None:
        """Composite the caption band onto `image` in-place."""
        if self._service is None or not self._service.is_active:
            return
        # Translucent panel via RGBA paste with self as mask.
        panel = Image.new(
            "RGBA", (self.canvas_w, self.band_h), self.PANEL_RGBA)
        image.paste(panel, (0, self.band_y), panel)

        draw = ImageDraw.Draw(image)
        # Step indicator top-right inside the band.
        meta = (f"{self._service.step_index + 1} / "
                f"{self._service.step_count}")
        meta_size = max(14, int(self.band_h * 0.14))
        meta_font = _safe_font(theme, "regular", meta_size)
        bbox = draw.textbbox((0, 0), meta, font=meta_font)
        meta_x = self.canvas_w - int(self.canvas_w * 0.04) - (
            bbox[2] - bbox[0])
        meta_y = self.band_y + int(self.band_h * 0.08)
        draw.text((meta_x, meta_y), meta, font=meta_font, fill=self.META_FG)

        # Caption: word-wrap into the available width, vertically centred
        # in the upper 60% of the band (above the buttons).
        cap_x = int(self.canvas_w * 0.06)
        cap_w = self.canvas_w - 2 * cap_x
        cap_y = self.band_y + int(self.band_h * 0.10)
        cap_h = int(self.band_h * 0.50)
        cap_size = max(18, int(self.band_h * 0.18))
        cap_font = _safe_font(theme, "bold", cap_size)
        text = self._service.caption or ""
        lines = _wrap_text(draw, text, cap_font, cap_w)
        # Drop down to two lines max — extras get truncated with ellipsis
        # on the last allowed line. Captions are authored short so this
        # is mostly a defensive cap.
        max_lines = 3
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            # Re-fit last line with ellipsis.
            last = lines[-1] + "…"
            while (draw.textbbox((0, 0), last, font=cap_font)[2] > cap_w
                   and len(last) > 2):
                last = last[:-2] + "…"
            lines[-1] = last
        ascent, descent = cap_font.getmetrics()
        line_h = int((ascent + descent) * 1.18)
        total_h = line_h * len(lines)
        y = cap_y + max(0, (cap_h - total_h) // 2)
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=cap_font)
            line_w = bbox[2] - bbox[0]
            x = cap_x + (cap_w - line_w) // 2
            draw.text((x, y - bbox[1]), ln, font=cap_font, fill=self.TEXT_FG)
            y += line_h

        # Buttons.
        self._draw_button(draw, theme,
                          self.exit_x, self.exit_y,
                          self.exit_w, self.exit_h, "EXIT")
        self._draw_button(draw, theme,
                          self.next_x, self.next_y,
                          self.next_w, self.next_h, "NEXT")

    def _draw_button(self, draw: ImageDraw.ImageDraw, theme: Theme,
                     x: int, y: int, w: int, h: int, label: str) -> None:
        radius = max(6, min(w, h) // 8)
        draw.rounded_rectangle(
            [x, y, x + w, y + h],
            radius=radius,
            outline=self.BTN_OUTLINE,
            width=2,
        )
        size = max(14, int(h * 0.42))
        f = _safe_font(theme, "bold", size)
        bbox = draw.textbbox((0, 0), label, font=f)
        tx = x + (w - (bbox[2] - bbox[0])) // 2
        ty = y + (h - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((tx, ty), label, font=f, fill=self.BTN_TEXT)


def _safe_font(theme: Theme, role: str, size: int):
    """Wrap font_path + ImageFont.truetype with a sensible fallback so
    a missing role doesn't break the caption render. Imported here
    rather than re-using widgets._font_cache so the demo module
    doesn't depend on widget internals."""
    try:
        path = font_path(theme, role)
    except Exception:
        path = font_path(theme, "regular")
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, size)
    except Exception:
        from PIL import ImageFont
        return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int
               ) -> list[str]:
    """Greedy word-wrap into lines that fit within max_w pixels."""
    if not text:
        return []
    words = text.split()
    lines: list[str] = []
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
    return lines


# Suppress "unused" hint for the color() import — kept for future
# theme-aware accents on the caption frame if we ever want one.
_ = color
