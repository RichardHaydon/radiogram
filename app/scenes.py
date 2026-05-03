"""Scenes compose Widgets into a full-canvas layout for one app mode.

The compositor picks the active scene each frame, calls state_key() to
decide whether to repaint, and dispatches taps via hit().

Add a new mode by subclassing Scene, populating self.widgets in
__init__, and registering it with the compositor.
"""
from __future__ import annotations

import os
import re
import socket
import sys
from dataclasses import replace

from PIL import Image, ImageDraw

from alarm import Alarm, days_label
from theme import Theme, color
from widgets import (
    AppTile, BellIconWidget, Button, CheckboxRow, ClockWidget,
    ColorPairWidget, DateWidget, IconButton, IconRow, LAUNCHER_ICONS,
    Rect, SETTINGS_ICONS, TextWidget, TwoLineText, WeatherIconWidget,
    Widget, WifiStatusWidget, WrappedTextWidget, _icon_back_arrow,
)


class Scene:
    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int):
        self.theme = theme
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.widgets: list[Widget] = []

    def add(self, w: Widget) -> Widget:
        self.widgets.append(w)
        return w

    def state_key(self) -> tuple:
        base = tuple(w.state_key() for w in self.widgets)
        # Background providers carry their own state_key (e.g. world map
        # invalidates per minute). Scenes that don't opt in never set
        # _background_provider so the legacy path is unchanged.
        bg_provider = getattr(self, "_background_provider", None)
        if bg_provider is None:
            return base
        sk = getattr(bg_provider, "state_key", None)
        if sk is None:
            return base
        return (sk(),) + base

    def render(self) -> Image.Image:
        img, has_bg_image = self._make_canvas()
        draw = ImageDraw.Draw(img)
        # Hint widgets (e.g. ClockWidget halo) about whether they're
        # being painted over a real image vs. a solid theme bg.
        # Attached to the draw context so widgets don't need a scene
        # back-pointer.
        setattr(draw, "_scene_has_bg_image", has_bg_image)
        for w in self.widgets:
            w.render(draw, self.theme)
        return img

    def _make_canvas(self) -> tuple[Image.Image, bool]:
        """Build the base image. If a background provider is installed
        and returns an image, that becomes the bottom layer; otherwise
        we fall back to a solid theme bg colour. Second tuple element
        is True iff a real bg image was painted."""
        bg_provider = getattr(self, "_background_provider", None)
        if bg_provider is not None:
            try:
                bg = bg_provider(self.theme)
            except Exception as exc:
                print(f"bg provider failed: {exc}",
                      file=sys.stderr, flush=True)
                bg = None
            if bg is not None:
                if bg.size != (self.canvas_w, self.canvas_h):
                    bg = bg.resize((self.canvas_w, self.canvas_h))
                return bg.copy(), True
        return (Image.new("RGB", (self.canvas_w, self.canvas_h),
                          color=color(self.theme, "bg")),
                False)

    def hit(self, cx: float, cy: float) -> Button | None:
        for w in reversed(self.widgets):
            if isinstance(w, Button) and w.rect.contains(cx, cy):
                return w
        return None

    def on_tap(self, cx: float, cy: float) -> bool:
        """Called by the compositor when a tap landed on the scene but
        not on any Button. Default: not handled. IdleScene/RadioScene
        override to open the Apps overlay so the whole screen acts as
        a "go to apps" affordance — no edge-swipe needed."""
        return False

    def on_show(self) -> None:
        """Called by the compositor when this scene becomes the active
        overlay. Default: no-op. Subclasses override to kick a refresh
        of any data they display (e.g. WifiScene rescans wifi)."""
        return None


# --- back-arrow header button (used by every overlay) ----------------

def _back_button(canvas_w: int, head_h: int, on_press) -> IconButton:
    """Standard back-arrow icon button, sized to the scene's header
    band and anchored to the top-left corner. Replaces the historical
    "CLOSE" / "✕" text buttons across every overlay so the navigation
    affordance reads the same everywhere."""
    btn_h = int(head_h * 0.80)
    btn_w = btn_h  # square
    return IconButton(
        Rect(int(canvas_w * 0.025), int(head_h * 0.10),
             btn_w, btn_h),
        on_press=on_press,
        icon_drawer=_icon_back_arrow,
        color_role="fg_accent",
        outline_width=2,
        icon_factor=0.65,
    )


# --- helpers used by IdleScene ---------------------------------------

def _format_next_alarm(alarm_service) -> str:
    # Snooze takes priority — it'll re-fire within minutes and is the
    # most useful thing to surface on the idle header.
    snz = getattr(alarm_service, "snoozed_until", None)
    if snz is not None:
        return f"💤 Snoozed until {snz.hour:02d}:{snz.minute:02d}"
    nf = alarm_service.next_to_fire()
    if nf is None:
        return "No alarm"
    a, fire_time = nf
    label = f"{a.hour:02d}:{a.minute:02d} {days_label(a.days)}"
    if a.skip_next:
        return f"⏰ {label}  (skip next)"
    return f"⏰ {label}"


def _skip_button_label(alarm_service) -> str:
    nf = alarm_service.next_to_fire()
    if nf is None:
        return ""
    return "UNSKIP" if nf[0].skip_next else "SKIP NEXT"


def _format_stream_info(audio: str, bitrate: int) -> str:
    """Render MPD's `audio` field ("44100:16:2") + bitrate as a compact
    one-liner. Returns empty when there's no stream so the underlying
    TextWidget hides itself."""
    if not audio and not bitrate:
        return ""
    parts: list[str] = []
    if audio:
        try:
            sr, _bits, ch = audio.split(":")
            sr_khz = int(sr) / 1000
            parts.append(f"{sr_khz:g} kHz")
            ch_n = int(ch)
            parts.append("mono" if ch_n == 1 else f"{ch_n}ch")
        except (ValueError, AttributeError):
            pass
    if bitrate:
        parts.append(f"{bitrate} kbps")
    return "  ·  ".join(parts)


def _alarm_armed(alarm_service) -> bool:
    """True iff the alarm pill should show its bell (snoozed counts)."""
    if getattr(alarm_service, "snoozed_until", None) is not None:
        return True
    return alarm_service.next_to_fire() is not None


def _format_footer_alarm(alarm_service) -> str:
    """Compact alarm string for the transport footer pill — short
    enough to share a row with PLAY / VOL controls."""
    snz = getattr(alarm_service, "snoozed_until", None)
    if snz is not None:
        return f"Snz {snz.hour:02d}:{snz.minute:02d}"
    nf = alarm_service.next_to_fire()
    if nf is None:
        return "No alarm"
    a, _t = nf
    if a.skip_next:
        return f"{a.hour:02d}:{a.minute:02d} skip"
    return f"{a.hour:02d}:{a.minute:02d}"


def _add_transport_footer(scene: "Scene", mpd_service, station_service,
                          canvas_w: int, canvas_h: int,
                          *, frac: float = 0.10,
                          x_offset: int = 0) -> None:
    """Bottom 4-zone strip: PLAY/STOP | VOL− | volume | VOL+.

    The play/stop button toggles based on MPD state — when stopped it
    plays the currently-selected station (or the first one if none has
    been picked yet), when playing it stops everything. This is the
    quick-access affordance: one tap to start the radio, one tap to
    stop, no menu navigation.

    `x_offset` lets callers reserve space at the left edge for an
    alarm pill or other adornment — the four transport zones then
    share `canvas_w - x_offset` instead of the full width.
    """
    foot_h = int(canvas_h * frac)
    foot_y = canvas_h - foot_h
    inner_w = canvas_w - x_offset
    play_w = int(inner_w * 0.22)
    minus_w = int(inner_w * 0.28)
    readout_w = int(inner_w * 0.22)
    plus_w = inner_w - play_w - minus_w - readout_w

    def play_label() -> str:
        return "STOP" if mpd_service.status.active else "PLAY"

    def play_action() -> None:
        if mpd_service.status.active:
            mpd_service.command(("stop_alarm",))
            return
        cur = station_service.current_id
        if cur:
            station_service.play(cur)
            return
        sts = station_service.stations
        if sts:
            station_service.play(sts[0].id)

    scene.add(Button(
        Rect(x_offset, foot_y, play_w, foot_h),
        label_src=play_label,
        on_press=play_action,
        font_factor=0.42,
        color_role="fg_bright",
    ))
    scene.add(Button(
        Rect(x_offset + play_w, foot_y, minus_w, foot_h),
        label_src="VOL−",
        on_press=lambda: mpd_service.command("vol_down"),
        font_factor=0.42,
    ))
    scene.add(TextWidget(
        Rect(x_offset + play_w + minus_w, foot_y, readout_w, foot_h),
        # Hide the level when stopped — the number is irrelevant without
        # audio. VOL−/+ still work and take effect on the next play.
        text_src=lambda: (f"{mpd_service.status.volume}"
                          if mpd_service.status.active else ""),
        font_factor=0.55,
        color_role="fg_dim",
    ))
    scene.add(Button(
        Rect(x_offset + play_w + minus_w + readout_w,
             foot_y, plus_w, foot_h),
        label_src="VOL+",
        on_press=lambda: mpd_service.command("vol_up"),
        font_factor=0.42,
    ))


# --- scenes ----------------------------------------------------------

class IdleScene(Scene):
    """Default mode: a full-bleed map background with the clock
    floating on top, and a single transport footer below carrying the
    alarm pill alongside PLAY / VOL controls.

    No top header — the wifi glyph and date were dropped to give the
    map full vertical real estate. The alarm pill in the footer keeps
    the alarm-list overlay one tap away, with a bell icon that lights
    up only when an alarm is armed (or snoozing)."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 alarm_service, mpd_service, station_service,
                 wifi_service, compositor):
        super().__init__(theme, canvas_w, canvas_h)
        # `wifi_service` is accepted for back-compat with the call site
        # but no longer drawn — wifi state surfaces in the dedicated
        # Wifi settings scene now that the header is gone.
        del wifi_service
        self._compositor = compositor

        footer_h = int(canvas_h * 0.10)
        body_h = canvas_h - footer_h

        # Big clock floating over the map, vertically centred in the
        # body. Halo (set on ClockWidget) keeps it readable over both
        # bright land and dark ocean.
        clock_h = int(canvas_h * 0.50)
        self.add(ClockWidget(
            Rect(0, (body_h - clock_h) // 2, canvas_w, clock_h),
            font_factor=0.60,
        ))

        # Alarm pill: tap target spans bell + label so a tap on the
        # bell opens the alarm list too. The Button is added BEFORE
        # the bell widget so the bell paints on top (the button is
        # outline-less / text-only so there's no rectangle to obscure;
        # the centred label also stays well to the right of the bell
        # at this footer width).
        alarm_w = int(canvas_w * 0.26)
        foot_y = canvas_h - footer_h
        bell_size = int(footer_h * 0.55)
        bell_x = int(canvas_w * 0.025)
        self.add(Button(
            Rect(bell_x, foot_y, alarm_w - bell_x, footer_h),
            label_src=lambda: _format_footer_alarm(alarm_service),
            on_press=lambda: compositor.set_overlay("alarm_list"),
            outline_width=0,
            color_role="fg_dim",
            font_factor=0.40,
        ))
        self.add(BellIconWidget(
            Rect(bell_x, foot_y + (footer_h - bell_size) // 2,
                 bell_size, bell_size),
            is_visible_src=lambda: _alarm_armed(alarm_service),
            color_role="fg_dim",
        ))

        # Transport zones share the rest of the footer width.
        _add_transport_footer(self, mpd_service, station_service,
                              canvas_w, canvas_h, x_offset=alarm_w)

    def on_tap(self, cx: float, cy: float) -> bool:
        """Tap on empty area (clock or map background) → open Apps.
        Tap on the alarm pill / transport buttons keeps its own action.
        Replaces the old swipe-up-from-bottom-edge launcher gesture."""
        self._compositor.set_overlay("launcher")
        return True


class RadioScene(Scene):
    """Radio-active mode: clock | station + title | PAUSE / STATIONS | vol.
    Stream listening doesn't have prev/next semantics — the action row is
    PAUSE/PLAY plus a STATIONS button that re-opens the picker. Volume
    lives in the always-available footer."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, mpd_service, station_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        # Reserve the bottom 10% for the volume footer (added below).
        # In the upper 90% give the clock more room (45%), now-playing
        # band moderate (33%), action row trim (22%) — the always-on
        # transport footer covers play/stop, so PAUSE/STATIONS up here
        # don't need to be huge.
        usable_h = int(canvas_h * 0.90)
        clock_h = int(usable_h * 0.45)
        np_h = int(usable_h * 0.33)
        action_h = usable_h - clock_h - np_h

        self.add(ClockWidget(
            Rect(0, 0, canvas_w, clock_h),
            font_factor=0.78,
        ))

        def station_line() -> str:
            cur = station_service.current()
            if cur is not None and cur.name:
                return cur.name
            return mpd_service.status.station or "(unknown station)"

        # Now-playing band: station name (top) / ICY title (middle) /
        # live stream format (bottom). The format subline is empty when
        # stopped — we render it anyway and the empty-text guard hides
        # the row.
        np_y = clock_h
        sl_h = int(np_h * 0.36)   # station
        tl_h = int(np_h * 0.40)   # title
        fmt_h = np_h - sl_h - tl_h
        self.add(TextWidget(
            Rect(0, np_y, canvas_w, sl_h),
            text_src=station_line,
            font_factor=0.65,
            color_role="fg_accent",
        ))
        self.add(TextWidget(
            Rect(0, np_y + sl_h, canvas_w, tl_h),
            text_src=lambda: mpd_service.status.title,
            font_role="regular",
            font_factor=0.55,
            color_role="fg_subtle",
        ))
        self.add(TextWidget(
            Rect(0, np_y + sl_h + tl_h, canvas_w, fmt_h),
            text_src=lambda: _format_stream_info(
                mpd_service.status.audio, mpd_service.status.bitrate),
            font_role="regular",
            font_factor=0.70,
            color_role="fg_dim",
        ))

        bot_y = clock_h + np_h

        def play_label() -> str:
            return ("PLAY" if mpd_service.status.state == "pause"
                    else "PAUSE")

        half = canvas_w // 2
        self.add(Button(
            Rect(0, bot_y, half, action_h),
            label_src=play_label,
            on_press=lambda: mpd_service.command("toggle"),
            font_factor=0.32,
        ))
        self.add(Button(
            Rect(half, bot_y, canvas_w - half, action_h),
            label_src="STATIONS",
            on_press=lambda: compositor.set_overlay("station_list"),
            font_factor=0.32,
        ))
        _add_transport_footer(self, mpd_service, station_service,
                              canvas_w, canvas_h)

    def on_tap(self, cx: float, cy: float) -> bool:
        """Same empty-area-tap-to-Apps as IdleScene so the gesture is
        the same in both home modes (clock & radio)."""
        self._compositor.set_overlay("launcher")
        return True


class LauncherScene(Scene):
    """System launcher (swipe-up from bottom edge). 3×2 grid of app
    tiles (icon + label). Tile actions either swap overlays (RADIO,
    ALARMS, WEATHER, VERSE, SETTINGS) or are placeholders (CAMERA)."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor):
        super().__init__(theme, canvas_w, canvas_h)
        head_h = int(canvas_h * 0.12)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.clear_overlay(),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Apps",
            font_factor=0.55,
            color_role="fg_dim",
        ))

        def stub():
            return None

        # (label, on_press, icon-name) — icons are looked up in
        # LAUNCHER_ICONS so we can swap art without touching this list.
        # Sub-overlays opened from here close back to "launcher" (not
        # all the way to idle), so the user always lands on a single
        # consistent home for navigation.
        cells = [
            ("RADIO",
             lambda: compositor.set_overlay("station_list"), "radio"),
            ("ALARMS",
             lambda: compositor.set_overlay("alarm_list"), "clock"),
            ("WEATHER",
             lambda: compositor.set_overlay("weather"), "partly_cloudy"),
            ("VERSE",
             lambda: compositor.set_overlay("verse"), "book"),
            ("CAMERA", stub, "camera"),
            ("SETTINGS",
             lambda: compositor.set_overlay("settings"), "gear"),
        ]
        cols, rows = 3, 2
        margin_x = int(canvas_w * 0.04)
        margin_y = int(canvas_h * 0.03)
        grid_top = head_h + margin_y
        grid_w = canvas_w - 2 * margin_x
        grid_h = canvas_h - grid_top - margin_y
        cell_w = grid_w / cols
        cell_h = grid_h / rows
        # Per-tile padding so neighbouring tiles aren't shoulder-to-shoulder.
        pad = int(min(cell_w, cell_h) * 0.06)
        for i, (label, action, icon_name) in enumerate(cells):
            col = i % cols
            row = i // cols
            tile_x = margin_x + int(col * cell_w) + pad
            tile_y = grid_top + int(row * cell_h) + pad
            tile_w = int(cell_w) - 2 * pad
            tile_h = int(cell_h) - 2 * pad
            self.add(AppTile(
                Rect(tile_x, tile_y, tile_w, tile_h),
                label_src=label,
                on_press=action,
                icon_drawer=LAUNCHER_ICONS.get(icon_name),
            ))


class QuickPanelScene(Scene):
    """Context-aware quick panel (swipe-down from top edge).

    Renders a header showing the current state plus a short list of action
    buttons that are sensible *right now*. Buttons are rebuilt on each
    render based on live state (mpd / alarms).
    """

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, mpd_service, alarm_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._mpd = mpd_service
        self._alarms = alarm_service
        # Header (always present)
        head_h = int(canvas_h * 0.18)
        self.add(TextWidget(
            Rect(0, 0, canvas_w, head_h),
            text_src=lambda: self._header_text(),
            font_factor=0.42,
            font_role="regular",
            color_role="fg_dim",
        ))
        self._head_h = head_h
        self._action_buttons: list[Button] = []

    # --- live-action plumbing ----------------------------------------

    def _header_text(self) -> str:
        if self._mpd.status.active:
            station = self._mpd.status.station or "Radio"
            return station
        nf = self._alarms.next_to_fire()
        if nf is not None:
            a, _ = nf
            skip = "  (skip next)" if a.skip_next else ""
            return f"Next: {a.hour:02d}:{a.minute:02d} {days_label(a.days)}{skip}"
        return "No alarm"

    def _live_actions(self):
        """List of (label, action) for buttons that make sense right now."""
        comp = self._compositor

        def wrap(fn):
            def _():
                comp.clear_overlay()
                fn()
            return _

        actions: list[tuple[str, callable]] = []
        if self._mpd.status.active:
            actions.append(("STOP RADIO",
                            wrap(lambda: self._mpd.command(
                                ("stop_alarm",)))))
        nf = self._alarms.next_to_fire()
        if nf is not None:
            label = ("UNSKIP NEXT" if nf[0].skip_next
                     else "SKIP NEXT ALARM")
            actions.append((label,
                            wrap(lambda: self._alarms.toggle_skip_next())))
        actions.append(("CLOSE", lambda: comp.clear_overlay()))
        return actions

    def _rebuild_actions(self) -> None:
        for b in self._action_buttons:
            try:
                self.widgets.remove(b)
            except ValueError:
                pass
        self._action_buttons.clear()
        actions = self._live_actions()
        if not actions:
            return
        body_top = self._head_h + int(self.canvas_h * 0.04)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.04)
        cell_h = body_h // len(actions)
        for i, (label, fn) in enumerate(actions):
            btn = Button(
                Rect(int(self.canvas_w * 0.06),
                     body_top + i * cell_h,
                     int(self.canvas_w * 0.88),
                     cell_h - 8),
                label_src=label,
                on_press=fn,
                font_factor=0.36,
            )
            self.widgets.append(btn)
            self._action_buttons.append(btn)

    # --- Scene overrides ---------------------------------------------

    def state_key(self) -> tuple:
        # Build keys off the current live action set so a state change
        # (e.g. radio starts playing while panel is open) refreshes layout.
        labels = tuple(lbl for lbl, _ in self._live_actions())
        return (self._header_text(), labels)

    def render(self) -> Image.Image:
        # Rebuild the action buttons each render so they reflect live state.
        self._rebuild_actions()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        # Make sure hit-test sees the freshly-built buttons.
        self._rebuild_actions()
        return super().hit(cx, cy)


class SettingsScene(Scene):
    """Overlay: top-level settings list. Currently routes to Wifi and
    Theme; remaining rows are placeholders for future settings."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor):
        super().__init__(theme, canvas_w, canvas_h)
        head_h = int(canvas_h * 0.14)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Settings",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        body_top = head_h + int(canvas_h * 0.04)
        body_h = canvas_h - body_top - int(canvas_h * 0.04)
        # (label, action, icon-name) — icons live in widgets.SETTINGS_ICONS
        # so the row label and its glyph stay aligned in one place.
        rows = [
            ("WIFI",
             lambda: compositor.set_overlay("wifi"), "wifi"),
            ("AUDIO",
             lambda: compositor.set_overlay("audio_output"), "speaker"),
            ("THEME",
             lambda: compositor.set_overlay("theme"), "palette"),
            ("BACKGROUND",
             lambda: compositor.set_overlay("background"), "globe"),
            ("BRIGHTNESS",
             lambda: compositor.set_overlay("brightness"), "brightness"),
            ("ABOUT",
             lambda: compositor.set_overlay("about"), "info"),
        ]
        # Reserve enough cell height for any future addition without
        # wobbling the existing layout.
        cell_h = body_h // max(len(rows), 7)
        for i, (label, action, icon_name) in enumerate(rows):
            self.add(IconRow(
                Rect(int(canvas_w * 0.06), body_top + i * cell_h,
                     int(canvas_w * 0.88), cell_h - 8),
                label_src=label,
                on_press=action,
                icon_drawer=SETTINGS_ICONS.get(icon_name),
                font_factor=0.42,
                color_role="fg_bright",
                icon_color_role="fg_accent",
            ))


class ThemeScene(Scene):
    """Overlay: pick a UI theme. Tap a row to apply + persist. The
    selected row is shown bright; the rest are dim."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, theme_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._theme_service = theme_service
        head_h = int(canvas_h * 0.14)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Theme",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self._rows: list[Button] = []

    def _apply(self, name: str) -> None:
        self._theme_service.set(name)

    def _rebuild_rows(self) -> None:
        for b in self._rows:
            try:
                self.widgets.remove(b)
            except ValueError:
                pass
        self._rows.clear()
        themes = self._theme_service.themes
        cur = self._theme_service.current.name
        body_top = self._head_h + int(self.canvas_h * 0.04)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.04)
        # Reserve cell height for up to 5 rows so layout is stable.
        slots = max(len(themes), 5)
        cell_h = body_h // slots
        # Layout: [ swatch ] [ button ]   (swatch ~22% wide)
        margin_x = int(self.canvas_w * 0.06)
        swatch_w = int(self.canvas_w * 0.22)
        gap = int(self.canvas_w * 0.02)
        btn_x = margin_x + swatch_w + gap
        btn_w = self.canvas_w - btn_x - margin_x
        for i, t in enumerate(themes):
            selected = t.name == cur
            mark = "▶ " if selected else "  "
            label = f"{mark}{t.name}"
            color_role = "fg_bright" if selected else "fg_dim"
            row_y = body_top + i * cell_h
            row_h = cell_h - 8
            sw = ColorPairWidget(
                Rect(margin_x, row_y, swatch_w, row_h),
                color_a=t.palette.fg_bright,
                color_b=t.palette.fg_accent,
                outline_color=t.palette.outline,
                outline_width=2,
            )
            btn = Button(
                Rect(btn_x, row_y, btn_w, row_h),
                label_src=label,
                on_press=lambda n=t.name: self._apply(n),
                font_factor=0.34,
                color_role=color_role,
            )
            self.widgets.append(sw)
            self.widgets.append(btn)
            self._rows.append(sw)
            self._rows.append(btn)

    def state_key(self) -> tuple:
        return (self._theme_service.current.name,
                tuple(t.name for t in self._theme_service.themes))

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


class WifiScene(Scene):
    """Overlay: current connection + scanned networks list. Tap a row to
    attempt connect — if a profile already exists nmcli reuses it; for
    new secured networks step 2 will hand off to a password scene."""

    MAX_ROWS = 5

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, wifi_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._wifi = wifi_service
        head_h = int(canvas_h * 0.12)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.42), head_h),
            text_src="Wifi",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.74), int(head_h * 0.10),
                 int(canvas_w * 0.24), int(head_h * 0.80)),
            label_src="RESCAN",
            on_press=lambda: wifi_service.rescan(),
            font_factor=0.42,
        ))
        # Status line
        stat_h = int(canvas_h * 0.10)
        self._stat_h = stat_h
        self.add(TextWidget(
            Rect(int(canvas_w * 0.04), head_h,
                 int(canvas_w * 0.92), stat_h),
            text_src=lambda: self._status_line(),
            font_factor=0.42,
            font_role="regular",
            color_role="fg_subtle",
        ))
        self._row_widgets: list[Widget] = []

    def _status_line(self) -> str:
        s = self._wifi.status
        if s.busy:
            return "Connecting…"
        if s.last_error:
            return f"Error: {s.last_error}"
        if s.ssid:
            return f"On: {s.ssid}   {s.signal}%   {s.ip}"
        return f"Not connected ({s.state})"

    def _on_pick(self, ssid: str, security: str) -> None:
        s = self._wifi.status
        if ssid == s.ssid:
            return  # already connected
        if not security or ssid in s.saved:
            # Open network or saved profile — connect directly. nmcli
            # reuses the stored credentials when a profile name matches.
            self._wifi.connect(ssid, None)
            return
        pw_scene = self._compositor.scenes.get("wifi_password")
        if pw_scene is None:
            self._wifi.connect(ssid, None)  # fallback
            return
        pw_scene.open(ssid)
        self._compositor.set_overlay("wifi_password")

    def on_show(self) -> None:
        # Auto-rescan when the user opens the wifi scene. Without this,
        # nmcli's cache may only contain the active SSID until something
        # triggers a fresh scan — confusing on first entry after boot.
        self._wifi.rescan()

    def _rebuild_rows(self) -> None:
        for w in self._row_widgets:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._row_widgets.clear()
        body_top = self._head_h + self._stat_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        nets = list(self._wifi.status.networks)
        if not nets:
            empty = TextWidget(
                Rect(0, body_top, self.canvas_w, body_h),
                text_src="(no networks — tap RESCAN)",
                font_factor=0.05,
                color_role="fg_dim",
                font_role="regular",
            )
            self.widgets.append(empty)
            self._row_widgets.append(empty)
            return
        cell_h = body_h // self.MAX_ROWS
        for i, n in enumerate(nets[:self.MAX_ROWS]):
            mark = "▶ " if n.in_use else "  "
            sec = n.security or "open"
            label = f"{mark}{n.ssid}   {n.signal}%   {sec}"
            color_role = "fg_bright" if n.in_use else "fg_dim"
            btn = Button(
                Rect(int(self.canvas_w * 0.04), body_top + i * cell_h,
                     int(self.canvas_w * 0.92), cell_h - 8),
                label_src=label,
                on_press=lambda ssid=n.ssid, sec=n.security:
                    self._on_pick(ssid, sec),
                font_factor=0.28,
                color_role=color_role,
            )
            self.widgets.append(btn)
            self._row_widgets.append(btn)

    def state_key(self) -> tuple:
        s = self._wifi.status
        return (
            s.ssid, s.signal, s.state, s.ip, s.busy, s.last_error,
            tuple((n.ssid, n.signal, n.in_use, n.security)
                  for n in s.networks),
        )

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


class WifiPasswordScene(Scene):
    """Overlay: enter a wifi password via on-screen QWERTY. Opens via
    WifiScene._on_pick when the network is secured and not yet saved.

    Layout for a 1280×720 landscape canvas:
        header (10%)   title + CANCEL
        entry  (10%)   masked field + SHOW/HIDE + OK
        keyboard (rest) — 5 rows
            digits | qwerty | asdfghjkl | shift+zxcvbnm+bksp | symbols+space

    Shift toggles uppercase letters AND maps digits to common shifted
    symbols (1→! 2→@ etc.) the same way a hardware keyboard does — that
    covers most home wifi password symbol needs without a separate mode.
    """

    DIGIT_SHIFT = {"1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
                   "6": "^", "7": "&", "8": "*", "9": "(", "0": ")"}
    MAX_PASSWORD = 63   # WPA2/WPA3 passphrase max

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, wifi_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._wifi = wifi_service
        self._ssid: str = ""
        self._password: str = ""
        self._shift: bool = False
        self._show: bool = False

    def open(self, ssid: str) -> None:
        """Reset state for a fresh entry attempt."""
        self._ssid = ssid
        self._password = ""
        self._shift = False
        self._show = False

    # --- mutations -----------------------------------------------------

    def _add_char(self, c: str) -> None:
        if len(self._password) >= self.MAX_PASSWORD:
            return
        if self._shift:
            if c.isalpha():
                c = c.upper()
            elif c in self.DIGIT_SHIFT:
                c = self.DIGIT_SHIFT[c]
        self._password += c

    def _backspace(self) -> None:
        self._password = self._password[:-1]

    def _toggle_shift(self) -> None:
        self._shift = not self._shift

    def _toggle_show(self) -> None:
        self._show = not self._show

    def _cancel(self) -> None:
        self._compositor.set_overlay("wifi")

    def _connect(self) -> None:
        if not self._password:
            return
        self._wifi.connect(self._ssid, self._password)
        # Send the user back to the wifi scene so they can watch the
        # status line (Connecting… → On/Error).
        self._compositor.set_overlay("wifi")

    # --- layout --------------------------------------------------------

    def _build(self) -> None:
        self.widgets.clear()
        cw, ch = self.canvas_w, self.canvas_h

        head_h = int(ch * 0.10)
        self.add(_back_button(
            cw, head_h,
            on_press=self._cancel,
        ))
        self.add(TextWidget(
            Rect(int(cw * 0.14), 0, int(cw * 0.84), head_h),
            text_src=f"Wifi password — {self._ssid}",
            font_factor=0.50,
            color_role="fg_dim",
            font_role="regular",
        ))

        entry_y = head_h + int(ch * 0.01)
        entry_h = int(ch * 0.10)
        display = (self._password if self._show
                   else "•" * len(self._password))
        self.add(TextWidget(
            Rect(int(cw * 0.04), entry_y, int(cw * 0.62), entry_h),
            text_src=(display or "(tap keys)"),
            font_factor=0.55,
            color_role=("fg_bright" if self._password else "fg_dim"),
        ))
        self.add(Button(
            Rect(int(cw * 0.68), entry_y + int(entry_h * 0.10),
                 int(cw * 0.14), int(entry_h * 0.80)),
            label_src=("HIDE" if self._show else "SHOW"),
            on_press=self._toggle_show,
            font_factor=0.42,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(cw * 0.84), entry_y + int(entry_h * 0.10),
                 int(cw * 0.14), int(entry_h * 0.80)),
            label_src="OK",
            on_press=self._connect,
            font_factor=0.55,
            color_role=("fg_bright" if self._password else "fg_dim"),
        ))

        kb_top = entry_y + entry_h + int(ch * 0.02)
        kb_h = ch - kb_top - int(ch * 0.01)
        row_h = kb_h // 5
        cell_w = cw // 10

        # Row 1: digits, with shifted-symbol labels when shift is on.
        self._row(kb_top, row_h, cell_w, "1234567890", offset=0)
        # Row 2: qwerty
        self._row(kb_top + row_h, row_h, cell_w, "qwertyuiop", offset=0)
        # Row 3: asdfghjkl, slight inset for visual stagger.
        self._row(kb_top + 2 * row_h, row_h, cell_w, "asdfghjkl",
                  offset=cell_w // 2)

        # Row 4: SHIFT (1.5w) + 7 letters + BKSP (1.5w)
        row_y = kb_top + 3 * row_h
        sw = int(cell_w * 1.5)
        self.add(Button(
            Rect(0, row_y, sw, row_h),
            label_src="SHIFT",
            on_press=self._toggle_shift,
            font_factor=0.30,
            color_role=("fg_bright" if self._shift else "fg_dim"),
            outline_width=(3 if self._shift else 1),
        ))
        for i, c in enumerate("zxcvbnm"):
            x = sw + i * cell_w
            label = c.upper() if self._shift else c
            self.add(Button(
                Rect(x, row_y, cell_w, row_h),
                label_src=label,
                on_press=lambda ch=c: self._add_char(ch),
                font_factor=0.55,
            ))
        bk_x = sw + 7 * cell_w
        self.add(Button(
            Rect(bk_x, row_y, cw - bk_x, row_h),
            label_src="DEL",
            on_press=self._backspace,
            font_factor=0.42,
            color_role="fg_dim",
        ))

        # Row 5: a few common symbols + a wide space bar.
        row_y = kb_top + 4 * row_h
        symbols_left = ".@-_"
        for i, c in enumerate(symbols_left):
            self.add(Button(
                Rect(i * cell_w, row_y, cell_w, row_h),
                label_src=c,
                on_press=lambda ch=c: self._add_char(ch),
                font_factor=0.55,
            ))
        sp_x = len(symbols_left) * cell_w
        self.add(Button(
            Rect(sp_x, row_y, cw - sp_x, row_h),
            label_src="SPACE",
            on_press=lambda: self._add_char(" "),
            font_factor=0.36,
            color_role="fg_dim",
        ))

    def _row(self, y: int, h: int, cell_w: int, chars: str,
             *, offset: int) -> None:
        for i, c in enumerate(chars):
            x = offset + i * cell_w
            if self._shift:
                if c.isalpha():
                    label = c.upper()
                elif c in self.DIGIT_SHIFT:
                    label = self.DIGIT_SHIFT[c]
                else:
                    label = c
            else:
                label = c
            self.add(Button(
                Rect(x, y, cell_w, h),
                label_src=label,
                on_press=lambda ch=c: self._add_char(ch),
                font_factor=0.55,
            ))

    # --- Scene overrides -----------------------------------------------

    def state_key(self) -> tuple:
        return (self._ssid, len(self._password), self._shift, self._show)

    def render(self) -> Image.Image:
        self._build()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._build()
        return super().hit(cx, cy)


class VerseScene(Scene):
    """Overlay: verse of the day, word-wrapped, with a tap-to-cycle
    translation button (KJV / WEB / ASV / BBE / YLT / DRA / OEB).
    Translation choice is persisted by VerseService."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, verse_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._verse = verse_service
        head_h = int(canvas_h * 0.10)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.46), head_h),
            text_src="Verse of the Day",
            font_factor=0.50,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.62), int(head_h * 0.10),
                 int(canvas_w * 0.16), int(head_h * 0.80)),
            label_src=lambda: self._verse.translation.upper(),
            on_press=lambda: verse_service.cycle_translation(),
            font_factor=0.42,
            color_role="fg_bright",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.80), int(head_h * 0.10),
                 int(canvas_w * 0.18), int(head_h * 0.80)),
            label_src="REFRESH",
            on_press=lambda: verse_service.refresh(),
            font_factor=0.42,
        ))
        # Reference line
        ref_h = int(canvas_h * 0.10)
        self.add(TextWidget(
            Rect(0, head_h, canvas_w, ref_h),
            text_src=lambda: self._ref_line(),
            font_factor=0.55,
            color_role="fg_accent",
        ))
        # Body — wrapped text
        body_y = head_h + ref_h
        body_h = canvas_h - body_y - int(canvas_h * 0.02)
        self.add(WrappedTextWidget(
            Rect(int(canvas_w * 0.06), body_y,
                 int(canvas_w * 0.88), body_h),
            text_src=lambda: self._verse.status.text,
            font_size=int(canvas_h * 0.055),
            font_role="regular",
            color_role="fg_bright",
            line_spacing=1.30,
        ))

    def _ref_line(self) -> str:
        s = self._verse.status
        if s.busy and not s.reference:
            return "Loading…"
        if s.last_error and not s.reference:
            return f"Error: {s.last_error}"
        return s.reference

    def state_key(self) -> tuple:
        s = self._verse.status
        return (s.translation, s.reference, s.text,
                s.busy, s.last_error)

    def on_show(self) -> None:
        self._verse.refresh()


class WeatherScene(Scene):
    """Overlay: current conditions + 5-day forecast strip with icons.

    Header row: back arrow + title + REFRESH.
    Current block: location top, then [icon | temp + condition] split.
    Forecast strip: 5 columns (next 5 days, today excluded), each
    stacked weekday / icon / hi-lo / precip-prob.
    """

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, weather_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._weather = weather_service
        head_h = int(canvas_h * 0.12)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.40), head_h),
            text_src="Weather",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.74), int(head_h * 0.10),
                 int(canvas_w * 0.24), int(head_h * 0.80)),
            label_src="REFRESH",
            on_press=lambda: weather_service.refresh(),
            font_factor=0.42,
        ))
        # Current conditions block
        cur_y = head_h
        cur_h = int(canvas_h * 0.36)
        self._cur_y = cur_y
        self._cur_h = cur_h
        # Location across the top
        loc_h = int(cur_h * 0.20)
        self.add(TextWidget(
            Rect(0, cur_y, canvas_w, loc_h),
            text_src=lambda: self._loc_line(),
            font_factor=0.50,
            color_role="fg_dim",
            font_role="regular",
        ))
        # Below location: icon (left) + temp/condition (right).
        body_y = cur_y + loc_h
        body_h = cur_h - loc_h
        icon_w = int(canvas_w * 0.34)
        self.add(WeatherIconWidget(
            Rect(int(canvas_w * 0.04), body_y, icon_w, body_h),
            code_src=lambda: self._weather.status.cur_code,
        ))
        text_x = icon_w + int(canvas_w * 0.06)
        text_w = canvas_w - text_x - int(canvas_w * 0.04)
        self.add(TextWidget(
            Rect(text_x, body_y + int(body_h * 0.05),
                 text_w, int(body_h * 0.55)),
            text_src=lambda: self._temp_line(),
            font_factor=0.80,
            color_role="fg_bright",
        ))
        self.add(TextWidget(
            Rect(text_x, body_y + int(body_h * 0.62),
                 text_w, int(body_h * 0.32)),
            text_src=lambda: self._cond_line(),
            font_factor=0.45,
            color_role="fg_subtle",
            font_role="regular",
        ))
        self._day_widgets: list[Widget] = []

    def _loc_line(self) -> str:
        s = self._weather.status
        if s.busy and not s.location:
            return "Locating…"
        if s.last_error:
            return f"Error: {s.last_error}"
        return s.location or ""

    def _temp_line(self) -> str:
        s = self._weather.status
        if s.cur_temp_c is None:
            return "—"
        return f"{round(s.cur_temp_c)}°C"

    def _cond_line(self) -> str:
        s = self._weather.status
        if s.cur_temp_c is None:
            return ""
        return f"{s.cur_label}    wind {round(s.cur_wind_kmh)} km/h"

    def _rebuild_days(self) -> None:
        for w in self._day_widgets:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._day_widgets.clear()
        days = list(self._weather.status.days)
        # Skip today (the "current" block already covers it); show the
        # next 5 days.
        future = days[1:6] if len(days) > 1 else days
        if not future:
            return
        body_top = self._head_h + self._cur_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        n = len(future)
        col_w = self.canvas_w // n
        # Weighted rows: weekday / icon / hi-lo / precip%.
        h_weekday = int(body_h * 0.18)
        h_icon = int(body_h * 0.42)
        h_hilo = int(body_h * 0.22)
        h_precip = body_h - (h_weekday + h_icon + h_hilo)
        for i, d in enumerate(future):
            x = i * col_w
            y = body_top
            self._add_day_text(
                Rect(x, y, col_w, h_weekday),
                self._weekday(d.date), 0.55, "fg_bright")
            y += h_weekday
            self._add_day_widget(WeatherIconWidget(
                Rect(x, y, col_w, h_icon),
                code_src=d.code,
            ))
            y += h_icon
            self._add_day_text(
                Rect(x, y, col_w, h_hilo),
                f"{round(d.high_c)}° / {round(d.low_c)}°",
                0.55, "fg_accent")
            y += h_hilo
            self._add_day_text(
                Rect(x, y, col_w, h_precip),
                f"{d.precip_pct}%", 0.50, "fg_subtle")

    def _add_day_text(self, rect: Rect, text: str, factor: float,
                      role: str, *, font_role: str = "bold") -> None:
        w = TextWidget(rect, text_src=text,
                       font_factor=factor, color_role=role,
                       font_role=font_role)
        self.widgets.append(w)
        self._day_widgets.append(w)

    def _add_day_widget(self, widget: Widget) -> None:
        self.widgets.append(widget)
        self._day_widgets.append(widget)

    @staticmethod
    def _weekday(iso: str) -> str:
        try:
            from datetime import date
            return date.fromisoformat(iso).strftime("%a")
        except Exception:
            return iso

    def state_key(self) -> tuple:
        s = self._weather.status
        return (
            s.location, s.busy, s.last_error,
            (round(s.cur_temp_c) if s.cur_temp_c is not None else None),
            s.cur_code, round(s.cur_wind_kmh),
            tuple((d.date, d.code, round(d.high_c), round(d.low_c),
                   d.precip_pct) for d in s.days),
        )

    def render(self) -> Image.Image:
        self._rebuild_days()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_days()
        return super().hit(cx, cy)

    def on_show(self) -> None:
        self._weather.refresh()


class StationListScene(Scene):
    """Overlay: pick a station to play. Tap a row → play + close."""

    MAX_ROWS = 6

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, station_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._stations = station_service
        head_h = int(canvas_h * 0.14)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Stations",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self._row_widgets: list[Widget] = []

    def _play_and_close(self, sid: str) -> None:
        # Picking a station drops to the underlying RadioScene (which
        # the default picker selects once MPD goes active) — the user
        # immediately sees what's now playing. The back arrow takes
        # the no-pick path, going to the Apps launcher instead.
        self._stations.play(sid)
        self._compositor.clear_overlay()

    def _rebuild_rows(self) -> None:
        for w in self._row_widgets:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._row_widgets.clear()
        sts = self._stations.stations
        body_top = self._head_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        if not sts:
            empty = TextWidget(
                Rect(0, body_top, self.canvas_w, body_h),
                text_src="(no stations — see README)",
                font_factor=0.06,
                color_role="fg_dim",
                font_role="regular",
            )
            self.widgets.append(empty)
            self._row_widgets.append(empty)
            return
        cell_h = body_h // self.MAX_ROWS
        cur = self._stations.current_id
        for i, s in enumerate(sts[:self.MAX_ROWS]):
            mark = "▶ " if s.id == cur else "  "
            label = f"{mark}{s.name or s.url}"
            color_role = "fg_bright" if s.id == cur else "fg_dim"
            btn = Button(
                Rect(int(self.canvas_w * 0.04), body_top + i * cell_h,
                     int(self.canvas_w * 0.92), cell_h - 8),
                label_src=label,
                on_press=lambda sid=s.id: self._play_and_close(sid),
                font_factor=0.30,
                color_role=color_role,
            )
            self.widgets.append(btn)
            self._row_widgets.append(btn)

    def state_key(self) -> tuple:
        return (
            tuple((s.id, s.name) for s in self._stations.stations),
            self._stations.current_id,
        )

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


class AlarmListScene(Scene):
    """Overlay: list of all configured alarms + ADD. Tap a row to edit it.

    The header (back / title / ADD) is built in __init__ and stays put;
    rows are rebuilt each render so the list reflects live AlarmService
    state without a manual refresh.
    """

    MAX_ROWS = 5  # arbitrary cap — beyond this would need scrolling

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, alarm_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._alarms = alarm_service
        head_h = int(canvas_h * 0.14)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.50), head_h),
            text_src="Alarms",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.74), int(head_h * 0.10),
                 int(canvas_w * 0.24), int(head_h * 0.80)),
            label_src="+ ADD",
            on_press=lambda: self._open_edit(None),
            font_factor=0.42,
        ))
        self._row_buttons: list[Button] = []

    def _open_edit(self, existing: Alarm | None) -> None:
        edit = self._compositor.scenes["alarm_edit"]
        if existing is None:
            edit.set_draft(Alarm(), is_new=True)
        else:
            edit.set_draft(existing, is_new=False)
        self._compositor.set_overlay("alarm_edit")

    @staticmethod
    def _row_label(a: Alarm) -> str:
        on = "ON " if a.enabled else "OFF"
        skip = "  (skip next)" if a.skip_next else ""
        return f"{on}   {a.hour:02d}:{a.minute:02d}   {days_label(a.days)}{skip}"

    def _rebuild_rows(self) -> None:
        for b in self._row_buttons:
            try:
                self.widgets.remove(b)
            except ValueError:
                pass
        self._row_buttons.clear()
        alarms = self._alarms.alarms
        body_top = self._head_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        if not alarms:
            empty = TextWidget(
                Rect(0, body_top, self.canvas_w, body_h),
                text_src="(no alarms — tap +ADD)",
                font_factor=0.06,
                color_role="fg_dim",
                font_role="regular",
            )
            self.widgets.append(empty)
            self._row_buttons.append(empty)  # tracked so it gets cleared next pass
            return
        cell_h = body_h // self.MAX_ROWS
        for i, a in enumerate(alarms[:self.MAX_ROWS]):
            row_a = a  # bind per-iteration for closure
            color_role = "fg_bright" if a.enabled else "fg_dim"
            btn = Button(
                Rect(int(self.canvas_w * 0.04), body_top + i * cell_h,
                     int(self.canvas_w * 0.92), cell_h - 8),
                label_src=self._row_label(a),
                on_press=lambda a=row_a: self._open_edit(a),
                font_factor=0.30,
                color_role=color_role,
            )
            self.widgets.append(btn)
            self._row_buttons.append(btn)

    def state_key(self) -> tuple:
        return tuple(
            (a.id, a.enabled, a.hour, a.minute, a.days, a.skip_next)
            for a in self._alarms.alarms
        )

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


class AlarmEditScene(Scene):
    """Overlay: edit a draft alarm. Header (title + enabled toggle + close),
    HH:MM with ▲/▼ on each digit, 7 day-of-week toggles (M T W T F S S),
    and an action row (CANCEL / [DELETE for existing] / SAVE).

    The draft is a copy of the alarm passed via set_draft; mutations only
    hit AlarmService on SAVE/DELETE, so CANCEL is a true discard.
    """

    DAY_LABELS = ["M", "T", "W", "T", "F", "S", "S"]

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, alarm_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._alarms = alarm_service
        self._draft: Alarm | None = None
        self._is_new = True

    def set_draft(self, alarm: Alarm, *, is_new: bool) -> None:
        self._draft = replace(alarm)
        self._is_new = is_new

    # --- mutators (operate on draft only — never persist mid-edit) ---

    def _bump_hour(self, delta: int) -> None:
        if self._draft is None:
            return
        self._draft.hour = (self._draft.hour + delta) % 24

    def _bump_minute(self, delta: int) -> None:
        if self._draft is None:
            return
        self._draft.minute = (self._draft.minute + delta) % 60

    def _toggle_day(self, idx: int) -> None:
        if self._draft is None:
            return
        self._draft.days ^= (1 << idx)

    def _toggle_enabled(self) -> None:
        if self._draft is None:
            return
        self._draft.enabled = not self._draft.enabled

    # --- finalisers ---

    def _save(self) -> None:
        if self._draft is None:
            return
        self._alarms.upsert_alarm(self._draft)
        self._compositor.set_overlay("alarm_list")

    def _cancel(self) -> None:
        self._compositor.set_overlay("alarm_list")

    def _delete(self) -> None:
        if self._draft is None:
            return
        self._alarms.delete_alarm(self._draft.id)
        self._compositor.set_overlay("alarm_list")

    # --- layout / rebuild ---

    def _build(self) -> None:
        # Wipe and rebuild every render: button labels (enabled, day toggles,
        # HH/MM digits) all depend on draft state. State_key gates repaints.
        self.widgets.clear()
        if self._draft is None:
            return
        cw, ch = self.canvas_w, self.canvas_h
        d = self._draft

        # Header
        head_h = int(ch * 0.12)
        self.add(_back_button(
            cw, head_h,
            on_press=self._cancel,
        ))
        self.add(TextWidget(
            Rect(int(cw * 0.14), 0, int(cw * 0.40), head_h),
            text_src=("New Alarm" if self._is_new else "Edit Alarm"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(cw * 0.68), int(head_h * 0.10),
                 int(cw * 0.30), int(head_h * 0.80)),
            label_src=("ENABLED" if d.enabled else "DISABLED"),
            on_press=self._toggle_enabled,
            font_factor=0.42,
            color_role=("fg_bright" if d.enabled else "fg_dim"),
        ))

        # Time picker — two columns of three (▲ / digits / ▼) with a colon.
        time_top = head_h + int(ch * 0.02)
        time_h = int(ch * 0.48)
        col_w = int(cw * 0.26)
        col_x_h = int(cw * 0.14)
        col_x_m = int(cw * 0.60)
        btn_h = int(time_h * 0.22)
        disp_y = time_top + btn_h
        disp_h = time_h - 2 * btn_h
        # ▲ row — repeatable so a long hold ramps the value.
        self.add(Button(
            Rect(col_x_h, time_top, col_w, btn_h),
            label_src="▲", on_press=lambda: self._bump_hour(1),
            font_factor=0.55,
            repeatable=True,
        ))
        self.add(Button(
            Rect(col_x_m, time_top, col_w, btn_h),
            label_src="▲", on_press=lambda: self._bump_minute(1),
            font_factor=0.55,
            repeatable=True,
        ))
        # digits
        self.add(TextWidget(
            Rect(col_x_h, disp_y, col_w, disp_h),
            text_src=lambda: f"{self._draft.hour:02d}",
            font_factor=0.85,
            color_role="fg_bright",
        ))
        self.add(TextWidget(
            Rect(int(cw * 0.40), disp_y, int(cw * 0.20), disp_h),
            text_src=":",
            font_factor=0.85,
            color_role="fg_bright",
        ))
        self.add(TextWidget(
            Rect(col_x_m, disp_y, col_w, disp_h),
            text_src=lambda: f"{self._draft.minute:02d}",
            font_factor=0.85,
            color_role="fg_bright",
        ))
        # ▼ row — repeatable so a long hold ramps the value.
        self.add(Button(
            Rect(col_x_h, time_top + time_h - btn_h, col_w, btn_h),
            label_src="▼", on_press=lambda: self._bump_hour(-1),
            font_factor=0.55,
            repeatable=True,
        ))
        self.add(Button(
            Rect(col_x_m, time_top + time_h - btn_h, col_w, btn_h),
            label_src="▼", on_press=lambda: self._bump_minute(-1),
            font_factor=0.55,
            repeatable=True,
        ))

        # Day toggles
        days_y = time_top + time_h + int(ch * 0.02)
        days_h = int(ch * 0.16)
        cell_w = cw // 7
        for i, lab in enumerate(self.DAY_LABELS):
            on = bool(d.days & (1 << i))
            self.add(Button(
                Rect(i * cell_w, days_y, cell_w, days_h),
                label_src=lab,
                on_press=lambda i=i: self._toggle_day(i),
                font_factor=0.55,
                color_role=("fg_bright" if on else "fg_dim"),
                outline_width=(3 if on else 1),
            ))

        # Action row — CANCEL | (DELETE for existing) | SAVE
        act_y = days_y + days_h + int(ch * 0.02)
        act_h = ch - act_y - int(ch * 0.02)
        if self._is_new:
            half = cw // 2
            self.add(Button(
                Rect(0, act_y, half, act_h),
                label_src="CANCEL", on_press=self._cancel,
                font_factor=0.40,
            ))
            self.add(Button(
                Rect(half, act_y, cw - half, act_h),
                label_src="SAVE", on_press=self._save,
                font_factor=0.40,
                color_role="fg_bright",
            ))
        else:
            third = cw // 3
            self.add(Button(
                Rect(0, act_y, third, act_h),
                label_src="CANCEL", on_press=self._cancel,
                font_factor=0.40,
            ))
            self.add(Button(
                Rect(third, act_y, third, act_h),
                label_src="DELETE", on_press=self._delete,
                font_factor=0.40,
                color_role="fg_dim",
            ))
            self.add(Button(
                Rect(2 * third, act_y, cw - 2 * third, act_h),
                label_src="SAVE", on_press=self._save,
                font_factor=0.40,
                color_role="fg_bright",
            ))

    def state_key(self) -> tuple:
        if self._draft is None:
            return ("nodraft",)
        d = self._draft
        return (d.id, self._is_new, d.enabled,
                d.hour, d.minute, d.days, d.skip_next)

    def render(self) -> Image.Image:
        self._build()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._build()
        return super().hit(cx, cy)


class AlarmFiringScene(Scene):
    """Alarm-firing: small clock + alarm label + side-by-side
    SNOOZE / STOP buttons. Snooze silences for SNOOZE_MINUTES (9) and
    re-fires; STOP cancels for good. A hard cap in AlarmService also
    auto-stops after 30 min so an unattended alarm can't run all day."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 alarm_service):
        super().__init__(theme, canvas_w, canvas_h)
        h_top = int(canvas_h * 0.22)
        h_mid = int(canvas_h * 0.20)
        h_btm = canvas_h - h_top - h_mid

        self.add(ClockWidget(
            Rect(0, 0, canvas_w, h_top),
            font_factor=0.70,
        ))

        def alarm_label() -> str:
            a = alarm_service.firing_alarm
            if a is None:
                return ""
            return f"ALARM  {a.hour:02d}:{a.minute:02d}"

        # Bell glyph + label sit on the same row. The bell is drawn via
        # PIL primitives so it renders regardless of font emoji coverage
        # — DejaVu Sans Bold has no ⏰/🔔, which is why the previous
        # version showed an empty glyph box.
        bell_size = int(h_mid * 0.55)
        bell_x = int(canvas_w * 0.04)
        self.add(BellIconWidget(
            Rect(bell_x,
                 h_top + (h_mid - bell_size) // 2,
                 bell_size, bell_size),
            color_role="fg_accent",
        ))
        self.add(TextWidget(
            Rect(0, h_top, canvas_w, h_mid),
            text_src=alarm_label,
            font_factor=0.55,
            color_role="fg_accent",
        ))

        # Two side-by-side buttons. Snooze on the left (smaller blast
        # radius — easy to recover from), Stop on the right.
        btn_y = h_top + h_mid
        snooze_w = canvas_w // 2
        self.add(Button(
            Rect(0, btn_y, snooze_w, h_btm),
            label_src="SNOOZE",
            on_press=lambda: alarm_service.snooze(),
            font_factor=0.50,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(snooze_w, btn_y, canvas_w - snooze_w, h_btm),
            label_src="STOP",
            on_press=lambda: alarm_service.stop_firing(),
            font_factor=0.50,
        ))


def _local_ip() -> str:
    """Best-effort local IP for the About screen. We don't actually send
    anything — connect() on a UDP socket is enough for the kernel to pick
    the outbound interface and bind a source address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "—"


class AboutScene(Scene):
    """Read-only info screen — host, IP, kernel, theme, content counts.
    No mutations; values re-evaluate each render so theme/MPD changes
    are reflected without a manual refresh."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, theme_service, alarm_service,
                 station_service, mpd_service):
        super().__init__(theme, canvas_w, canvas_h)
        head_h = int(canvas_h * 0.14)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="About",
            font_factor=0.55,
            color_role="fg_dim",
        ))

        # Hostname + kernel are stable for the lifetime of the process so
        # we capture them once; everything else is a callable so the row
        # reflects current state on each render.
        host = socket.gethostname()
        try:
            kernel = os.uname().release
        except (AttributeError, OSError):
            kernel = "—"

        rows: list = [
            (lambda h=host: f"Host    {h}"),
            (lambda k=kernel: f"Kernel  {k}"),
            (lambda: f"IP      {_local_ip()}"),
            (lambda: f"Theme   {theme_service.current.name}"),
            (lambda: f"Alarms  {len(alarm_service.alarms)}"),
            (lambda: f"Stations  {len(station_service.stations)}"),
            (lambda: f"MPD     {mpd_service.status.state}"),
        ]
        body_top = head_h + int(canvas_h * 0.04)
        body_h = canvas_h - body_top - int(canvas_h * 0.04)
        # Reserve room for ~8 rows so layout doesn't wobble if we add more.
        cell_h = body_h // max(len(rows), 8)
        for i, src in enumerate(rows):
            self.add(TextWidget(
                Rect(int(canvas_w * 0.06), body_top + i * cell_h,
                     int(canvas_w * 0.88), cell_h - 6),
                text_src=src,
                font_role="regular",
                font_factor=0.42,
                color_role="fg_dim",
            ))


class BrightnessScene(Scene):
    """Adjust active + idle-dim backlight levels in 10% steps. Both
    persist immediately (BrightnessService writes on every change) so
    the next boot keeps the user's preference."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, brightness_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._svc = brightness_service
        head_h = int(canvas_h * 0.14)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Brightness",
            font_factor=0.55,
            color_role="fg_dim",
        ))

        body_top = head_h + int(canvas_h * 0.04)
        body_h = canvas_h - body_top - int(canvas_h * 0.04)
        # Two settings: each gets a label + −/value/+ control row, with
        # vertical breathing room above and below. Three vertical bands.
        band_h = body_h // 3
        self._build_setting(
            label="Active",
            band_y=body_top,
            band_h=band_h,
            value_fn=lambda: f"{self._svc.config.active_pct}%",
            minus=lambda: self._svc.step_active(-1),
            plus=lambda: self._svc.step_active(+1),
        )
        self._build_setting(
            label="Idle dim",
            band_y=body_top + band_h + int(body_h * 0.04),
            band_h=band_h,
            value_fn=lambda: f"{self._svc.config.dim_pct}%",
            minus=lambda: self._svc.step_dim(-1),
            plus=lambda: self._svc.step_dim(+1),
        )

    def _build_setting(self, *, label: str, band_y: int, band_h: int,
                       value_fn, minus, plus) -> None:
        cw = self.canvas_w
        # Label takes the top half, control row the bottom half.
        lbl_h = int(band_h * 0.40)
        ctl_h = band_h - lbl_h
        self.add(TextWidget(
            Rect(int(cw * 0.06), band_y, int(cw * 0.88), lbl_h),
            text_src=label,
            font_factor=0.55,
            color_role="fg_dim",
        ))
        ctl_y = band_y + lbl_h
        # Four zones: − | value | + spread across 88% of width.
        margin = int(cw * 0.06)
        usable = cw - 2 * margin
        btn_w = int(usable * 0.22)
        val_w = usable - 2 * btn_w
        self.add(Button(
            Rect(margin, ctl_y, btn_w, ctl_h - 8),
            label_src="−",
            on_press=minus,
            font_factor=0.55,
        ))
        self.add(TextWidget(
            Rect(margin + btn_w, ctl_y, val_w, ctl_h - 8),
            text_src=value_fn,
            font_factor=0.55,
            color_role="fg_bright",
        ))
        self.add(Button(
            Rect(margin + btn_w + val_w, ctl_y, btn_w, ctl_h - 8),
            label_src="+",
            on_press=plus,
            font_factor=0.55,
        ))


def _read_mpd_outputs(path: str = "/etc/mpd.conf") -> dict:
    """Parse audio_output { ... } blocks from /etc/mpd.conf.

    Returns {output_name: {key: value, ...}} so the picker can show the
    `device` line (which MPD's wire protocol doesn't expose) for each
    output. Tolerant of comments + missing fields."""
    out: dict = {}
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for m in re.finditer(r"audio_output\s*\{([^}]*)\}", text, re.S):
        block = m.group(1)
        kv: dict = {}
        for line in block.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            mm = re.match(r'(\w+)\s+"([^"]*)"', s)
            if mm:
                kv[mm.group(1)] = mm.group(2)
        nm = kv.get("name", "").strip()
        if nm:
            out[nm] = kv
    return out


class AudioOutputScene(Scene):
    """Overlay: pick the active MPD audio output (e.g. USB DAC vs.
    Bluetooth speaker).

    UX: tapping a row expands it inline to show plugin + device path
    (the latter parsed from /etc/mpd.conf). The expanded panel offers
    a "USE THIS" button that sends ('set_output', id), enabling the
    chosen output exclusively. Tapping the same header again collapses.
    """

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, mpd_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._mpd = mpd_service
        head_h = int(canvas_h * 0.14)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Audio Output",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.06), head_h + int(canvas_h * 0.06),
                 int(canvas_w * 0.88), int(canvas_h * 0.12)),
            text_src=lambda: ("" if self._mpd.status.outputs
                              else "No outputs reported by MPD"),
            font_role="regular",
            font_factor=0.50,
            color_role="fg_dim",
        ))
        self._expanded_id: int | None = None
        self._rows: list = []

    def _toggle_expand(self, oid: int) -> None:
        self._expanded_id = None if self._expanded_id == oid else oid

    def _apply(self, oid: int) -> None:
        self._mpd.command(("set_output", oid))

    def _rebuild_rows(self) -> None:
        for w in self._rows:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._rows.clear()
        outs = self._mpd.status.outputs
        if not outs:
            return
        cfg = _read_mpd_outputs()
        cw = self.canvas_w
        ch = self.canvas_h
        body_top = self._head_h + int(ch * 0.04)
        margin_x = int(cw * 0.06)
        inner_w = cw - 2 * margin_x
        collapsed_h = int(ch * 0.09)
        detail_h = int(ch * 0.22)
        y = body_top
        for o in outs:
            # Header — tap to expand/collapse.
            mark = "▶ " if o.enabled else "  "
            suffix = f"  [{o.plugin}]" if o.plugin else ""
            label = f"{mark}{o.name}{suffix}"
            color_role = "fg_bright" if o.enabled else "fg_dim"
            head = Button(
                Rect(margin_x, y, inner_w, collapsed_h - 6),
                label_src=label,
                on_press=lambda oid=o.id: self._toggle_expand(oid),
                font_factor=0.32,
                color_role=color_role,
            )
            self.widgets.append(head)
            self._rows.append(head)
            y += collapsed_h

            if o.id == self._expanded_id:
                kv = cfg.get(o.name, {})
                device = kv.get("device", "—")
                d_x = margin_x + int(cw * 0.04)
                d_w = inner_w - int(cw * 0.04)
                line_h = detail_h // 4
                self.widgets.append(TextWidget(
                    Rect(d_x, y, d_w, line_h),
                    text_src=f"Plugin   {o.plugin or '—'}",
                    font_role="regular",
                    font_factor=0.55,
                    color_role="fg_subtle",
                ))
                self._rows.append(self.widgets[-1])
                self.widgets.append(TextWidget(
                    Rect(d_x, y + line_h, d_w, line_h),
                    text_src=f"Device   {device}",
                    font_role="regular",
                    font_factor=0.55,
                    color_role="fg_subtle",
                ))
                self._rows.append(self.widgets[-1])
                btn_y = y + 2 * line_h + 4
                btn_h = detail_h - 2 * line_h - 8
                if o.enabled:
                    self.widgets.append(TextWidget(
                        Rect(d_x, btn_y, d_w, btn_h),
                        text_src="ACTIVE",
                        font_factor=0.55,
                        color_role="fg_accent",
                    ))
                    self._rows.append(self.widgets[-1])
                else:
                    use_btn = Button(
                        Rect(d_x, btn_y, d_w, btn_h),
                        label_src="USE THIS OUTPUT",
                        on_press=lambda oid=o.id: self._apply(oid),
                        font_factor=0.36,
                    )
                    self.widgets.append(use_btn)
                    self._rows.append(use_btn)
                y += detail_h

    def state_key(self) -> tuple:
        return (self._expanded_id,) + tuple(
            (o.id, o.name, o.enabled, o.plugin)
            for o in self._mpd.status.outputs)

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


class BackgroundScene(Scene):
    """Overlay: choose what (if anything) is composited behind idle/radio
    scenes. Two side-by-side columns — Style (exclusive radio) on the
    left, Overlays (independent checkboxes) on the right. Style change
    is exclusive; overlays stack. Both persist immediately and the next
    opted-in scene picks them up via its background provider."""

    STYLES = (
        ("none", "None"),
        ("world_map_slate", "Slate"),
        ("world_map_atlas", "Atlas"),
        ("world_map_vintage", "Vintage"),
        ("world_map_blueprint", "Blueprint"),
    )
    OVERLAYS = (
        ("city_lights", "City Lights"),
        ("water", "Lakes & Rivers"),
        ("political", "Political Borders"),
        ("clouds", "Clouds (live) — soon"),
        ("annotations", "Latitudes & Terminator"),
    )

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, background_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._bg = background_service
        head_h = int(canvas_h * 0.10)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Background",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self._rows: list = []

    def _set_mode(self, mode: str) -> None:
        self._bg.set_mode(mode)

    def _toggle_overlay(self, name: str) -> None:
        # Clouds overlay isn't wired yet — silently no-op so the toggle
        # can sit on the screen without misleading the user. Will go
        # live once a cloud-cover service is added.
        if name == "clouds":
            return
        self._bg.toggle_overlay(name)

    def _rebuild_rows(self) -> None:
        for w in self._rows:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._rows.clear()
        # Single column, portrait-friendly: full-width rows so the
        # label has room to render at a readable size. 5 styles + 5
        # overlays + 1 "Map Centre" navigation = 11 option rows plus
        # two compact section labels.
        body_top = self._head_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        label_h = int(self.canvas_h * 0.045)
        gap = int(self.canvas_h * 0.012)
        # Gaps: between body_top and label, after label, between
        # sections (×2), before centre button. Total 5 gaps.
        rows_h = body_h - 2 * label_h - 5 * gap
        row_h = max(28, rows_h // 11)
        margin_x = int(self.canvas_w * 0.05)
        content_w = self.canvas_w - 2 * margin_x
        # Section 1: STYLE — radio (exclusive). Labels use a narrow
        # left-anchored rect so the centered text reads as left-aligned
        # over the option rows below (rather than floating mid-screen).
        label_w = int(content_w * 0.45)
        y = body_top
        self._add_label(margin_x, y, label_w, label_h, "Style")
        y += label_h + gap
        for mode, label in self.STYLES:
            self._add_check(
                x=margin_x, y=y, w=content_w, h=row_h,
                label=label,
                shape="radio",
                is_on_src=(lambda m=mode: self._bg.mode == m),
                on_press=(lambda m=mode: self._set_mode(m)),
            )
            y += row_h
        # Section 2: OVERLAYS — checkboxes (independent / combinable).
        # Overlays only apply atop a world map; dim when mode == "none"
        # so the dependency is visible.
        y += gap
        self._add_label(margin_x, y, label_w, label_h, "Overlays")
        y += label_h + gap
        for name, label in self.OVERLAYS:
            self._add_check(
                x=margin_x, y=y, w=content_w, h=row_h,
                label=label,
                shape="check",
                is_on_src=(lambda n=name: self._bg.is_overlay(n)),
                on_press=(lambda n=name: self._toggle_overlay(n)),
                disabled_src=(lambda n=name:
                              self._bg.mode == "none" or n == "clouds"),
            )
            y += row_h
        # Section 3: MAP CENTRE — single navigation row that opens the
        # picker. Disabled when no map is selected.
        y += gap
        centre_btn = Button(
            Rect(margin_x, y, content_w, row_h),
            label_src=lambda: f"Centre: {_format_centre_lon(self._bg.center_lon)}  ▸",
            on_press=lambda: self._compositor.set_overlay("map_center"),
            font_factor=0.42,
            color_role=("fg_dim" if self._bg.mode == "none"
                        else "fg_bright"),
            outline_width=1,
        )
        self.widgets.append(centre_btn)
        self._rows.append(centre_btn)

    def _add_label(self, x: int, y: int, w: int, h: int,
                   text: str) -> None:
        widget = TextWidget(
            Rect(x, y, w, h),
            text_src=text,
            font_factor=0.55,
            color_role="fg_subtle",
        )
        self.widgets.append(widget)
        self._rows.append(widget)

    def _add_check(self, *, x: int, y: int, w: int, h: int,
                   label: str, shape: str, is_on_src, on_press,
                   disabled_src=None) -> None:
        row = CheckboxRow(
            Rect(x, y, w, h),
            label_src=label,
            on_press=on_press,
            is_on_src=is_on_src,
            shape=shape,
            font_factor=0.6,
            disabled_src=disabled_src,
        )
        self.widgets.append(row)
        self._rows.append(row)

    def state_key(self) -> tuple:
        return (self._bg.mode, self._bg.active_overlays(),
                int(round(self._bg.center_lon)))

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


# ---------------------------------------------------------------------
# Map centre picker
# ---------------------------------------------------------------------

# Cities with reasonably broad geographic spread. Values are decimal
# degrees east (so western longitudes are negative). The list is the
# whole option set the user can pick — easy to extend without touching
# the scene class or the persistence layer.
MAP_CENTERS = (
    (0.0, "London (Greenwich)"),
    (35.2, "Jerusalem"),
    (39.8, "Mecca"),
    (-74.0, "New York"),
    (-87.6, "Chicago"),
    (-122.3, "Seattle"),
    (-157.9, "Honolulu"),
    (139.7, "Tokyo"),
    (116.4, "Beijing"),
    (151.2, "Sydney"),
    (18.4, "Cape Town"),
    (-58.4, "Buenos Aires"),
)


def _format_centre_lon(lon: float) -> str:
    """Closest predefined centre name, falling back to decimal degrees."""
    best = None
    best_dist = 360.0
    for clon, name in MAP_CENTERS:
        d = abs(((lon - clon + 180) % 360) - 180)
        if d < best_dist:
            best_dist = d
            best = name
    if best is not None and best_dist < 0.6:
        return best
    # Off-list value (shouldn't happen via this picker, but possible
    # from a hand-edited config file): show the raw degrees.
    sign = "E" if lon >= 0 else "W"
    return f"{abs(lon):.1f}°{sign}"


class MapCenterScene(Scene):
    """Overlay: pick the longitude shown at the horizontal centre of
    the world-map background. Single column of radio rows for the
    predefined cities in MAP_CENTERS."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, background_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._bg = background_service
        head_h = int(canvas_h * 0.10)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("background"),
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.14), 0,
                 int(canvas_w * 0.72), head_h),
            text_src="Map Centre",
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self._rows: list = []

    def _set_centre(self, lon: float) -> None:
        self._bg.set_center_lon(lon)

    def _rebuild_rows(self) -> None:
        for w in self._rows:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._rows.clear()
        body_top = self._head_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        n = len(MAP_CENTERS)
        row_h = max(28, body_h // n)
        margin_x = int(self.canvas_w * 0.05)
        content_w = self.canvas_w - 2 * margin_x
        y = body_top
        for clon, label in MAP_CENTERS:
            row = CheckboxRow(
                Rect(margin_x, y, content_w, row_h),
                label_src=label,
                on_press=(lambda v=clon: self._set_centre(v)),
                is_on_src=(lambda v=clon:
                           abs(((self._bg.center_lon - v + 180) % 360)
                               - 180) < 0.6),
                shape="radio",
                font_factor=0.55,
            )
            self.widgets.append(row)
            self._rows.append(row)
            y += row_h

    def state_key(self) -> tuple:
        return (int(round(self._bg.center_lon)),)

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)
