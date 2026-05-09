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
    Rect, RenderingIndicatorWidget, SETTINGS_ICONS, TextWidget,
    TwoLineText, WeatherIconWidget, Widget, WifiStatusWidget,
    WrappedTextWidget, _icon_back_arrow, _icon_chevron_down,
    _icon_chevron_up,
)


# Single module-level handle to the I18nService. Wired from main() at
# startup before any Scene is constructed. Scenes call _t("key", **fmt)
# rather than holding their own reference — every text_src lambda then
# closes over _t and picks up language switches automatically (no
# per-scene plumbing of the service object).
_i18n = None


def set_i18n(service) -> None:
    """Called once from clockradio.main() after I18nService init."""
    global _i18n
    _i18n = service


def _t(key: str, **fmt) -> str:
    """Translate `key` with the active language, or fall back to the
    English source string if the i18n service hasn't been wired yet
    (defensive — scenes import this at module load before main runs)."""
    if _i18n is None:
        # Fall back to English so unit-test / import-time use still
        # produces sensible strings.
        from translations import EN
        s = EN.get(key, key)
        return s.format(**fmt) if fmt else s
    return _i18n.t(key, **fmt)


def _lang_version() -> int:
    """Cache-key contribution: bumps every time the user switches
    language. Folded into Scene.state_key by the helper used below
    so all scenes invalidate together."""
    return _i18n.version if _i18n is not None else 0


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
        # Fold the i18n version into every scene's key so a language
        # switch invalidates the cache without touching each subclass.
        base = (_lang_version(),) + base
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


def _rendering_indicator(scene: "Scene", canvas_w: int,
                         canvas_h: int) -> RenderingIndicatorWidget:
    """Small dot in the canvas top-right corner that appears while a
    background render is in flight. Same widget plugged into Idle /
    Radio / AlarmFiring so the visual cue is consistent across home
    modes. Reads the scene's _background_provider lazily — provider is
    attached after construction in clockradio.main()."""
    size = max(10, int(min(canvas_w, canvas_h) * 0.022))
    pad = max(8, int(min(canvas_w, canvas_h) * 0.020))

    def _active() -> bool:
        bg = getattr(scene, "_background_provider", None)
        if bg is None:
            return False
        fn = getattr(bg, "is_rendering", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    return RenderingIndicatorWidget(
        Rect(canvas_w - pad - size, pad, size, size),
        is_active_src=_active,
        color_role="fg_dim",
    )


def _scene_bg_is_globe(scene: "Scene") -> bool:
    """True when the scene's background provider is currently rendering
    the orthographic globe. Used by IdleScene/RadioScene to swap the
    clock to a top-left alt-layout that doesn't sit over the disc.
    Returns False if no provider is installed or the provider doesn't
    expose `style_name()` — i.e. the legacy / no-bg path."""
    bg = getattr(scene, "_background_provider", None)
    if bg is None:
        return False
    sn = getattr(bg, "style_name", None)
    if sn is None:
        return False
    try:
        return sn() == "globe"
    except Exception:
        return False


def _home_button(canvas_w: int, head_h: int, compositor) -> Button:
    """"HOME" text button, placed next to the back arrow on every
    overlay. Always clears all overlays back to the underlying idle/
    radio scene — a one-tap escape from a deep nav stack (e.g.
    Settings → Wifi → password keyboard back to clock). Was a house-
    glyph icon; the small drawing was hard to read at bedside distance
    so it's now the literal word, matching the other all-caps action
    buttons in the UI (RESCAN, OK, ADD…)."""
    btn_h = int(head_h * 0.80)
    # Sized for the longest of the three home labels (Spanish "INICIO"
    # is 6 chars; HOME / HJEM are only 4). Font factor tuned so the
    # 6-char label fits without ellipsis at every resolution.
    btn_w = int(head_h * 1.10)
    # Sit just to the right of the back button. Back is at x=0.025
    # with width = head_h*0.80 (square); this starts past it with a
    # small gap.
    back_w = int(head_h * 0.80)
    back_end = int(canvas_w * 0.025) + back_w
    gap = int(canvas_w * 0.012)
    return Button(
        Rect(back_end + gap, int(head_h * 0.10),
             btn_w, btn_h),
        label_src=lambda: _t("button.home"),
        on_press=lambda: compositor.clear_overlay(),
        font_factor=0.32,
        color_role="fg_accent",
        outline_width=2,
    )


# --- helpers used by IdleScene ---------------------------------------

def _format_next_alarm(alarm_service) -> str:
    # Snooze takes priority — it'll re-fire within minutes and is the
    # most useful thing to surface on the idle header.
    snz = getattr(alarm_service, "snoozed_until", None)
    if snz is not None:
        time_s = f"{snz.hour:02d}:{snz.minute:02d}"
        return "💤 " + _t("alarm.snoozed_until", time=time_s)
    nf = alarm_service.next_to_fire()
    if nf is None:
        return _t("alarm.no_alarm")
    a, fire_time = nf
    label = f"{a.hour:02d}:{a.minute:02d} {days_label(a.days)}"
    if a.skip_next:
        return f"⏰ {label}  ({_t('alarm.skip_marker')})"
    return f"⏰ {label}"


def _skip_button_label(alarm_service) -> str:
    nf = alarm_service.next_to_fire()
    if nf is None:
        return ""
    return _t("button.unskip") if nf[0].skip_next else _t("button.skip_next")


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
        return f"{_t('alarm.snz_short')} {snz.hour:02d}:{snz.minute:02d}"
    nf = alarm_service.next_to_fire()
    if nf is None:
        return _t("alarm.no_alarm")
    a, _ft = nf
    if a.skip_next:
        return f"{a.hour:02d}:{a.minute:02d} {_t('alarm.skip_marker')}"
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
        return (_t("button.stop") if mpd_service.status.active
                else _t("button.play"))

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
        label_src=lambda: _t("button.vol_down"),
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
        label_src=lambda: _t("button.vol_up"),
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
        # Globe view alt-layout: tuck a smaller clock into the top-left
        # corner so the daylit hemisphere stays unobstructed. Both
        # rect/font pairs are precomputed; render() picks one each
        # frame based on the current background style.
        clock_h = int(canvas_h * 0.50)
        self._clock_rect_default = Rect(
            0, (body_h - clock_h) // 2, canvas_w, clock_h)
        self._clock_factor_default = 0.60
        self._clock_rect_globe = Rect(
            int(canvas_w * 0.02), int(canvas_h * 0.02),
            int(canvas_w * 0.34), int(canvas_h * 0.18),
        )
        self._clock_factor_globe = 0.78
        self._clock = ClockWidget(
            self._clock_rect_default,
            font_factor=self._clock_factor_default,
        )
        self.add(self._clock)

        # Alarm pill: tap target spans bell + label so a tap on the
        # bell opens the alarm list too. The Button is added BEFORE
        # the bell widget so the bell paints on top (the button is
        # outline-less / text-only so there's no rectangle to obscure;
        # the centred label also stays well to the right of the bell
        # at this footer width).
        alarm_w = int(canvas_w * 0.30)
        foot_y = canvas_h - footer_h
        bell_size = int(footer_h * 0.55)
        bell_x = int(canvas_w * 0.025)
        # Bigger label + fg_bright + halo so the time reads cleanly
        # over the world map. The previous fg_dim at 0.40 vanished
        # against complex coastlines; halo gives it the same
        # contrast guarantee as the main clock.
        self.add(Button(
            Rect(bell_x, foot_y, alarm_w - bell_x, footer_h),
            label_src=lambda: _format_footer_alarm(alarm_service),
            on_press=lambda: compositor.set_overlay("alarm_list"),
            outline_width=0,
            color_role="fg_bright",
            font_factor=0.52,
            halo=True,
        ))
        self.add(BellIconWidget(
            Rect(bell_x, foot_y + (footer_h - bell_size) // 2,
                 bell_size, bell_size),
            is_visible_src=lambda: _alarm_armed(alarm_service),
            color_role="fg_bright",
        ))

        # Transport zones share the rest of the footer width.
        _add_transport_footer(self, mpd_service, station_service,
                              canvas_w, canvas_h, x_offset=alarm_w)

        # "Updating bg" indicator — paints last so it sits on top.
        self.add(_rendering_indicator(self, canvas_w, canvas_h))

    def on_tap(self, cx: float, cy: float) -> bool:
        """Tap on empty area (clock or map background) → open Apps.
        Tap on the alarm pill / transport buttons keeps its own action.
        Replaces the old swipe-up-from-bottom-edge launcher gesture."""
        self._compositor.set_overlay("launcher")
        return True

    def _apply_clock_layout(self) -> None:
        # Pick the alt top-left rect when the globe background is
        # active so the clock doesn't sit over the daylit disc; fall
        # back to the centred rect for the flat map styles and "none".
        if _scene_bg_is_globe(self):
            self._clock.rect = self._clock_rect_globe
            self._clock.font_factor = self._clock_factor_globe
        else:
            self._clock.rect = self._clock_rect_default
            self._clock.font_factor = self._clock_factor_default

    def render(self) -> Image.Image:
        self._apply_clock_layout()
        return super().render()


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

        # Globe view shrinks the clock into the top-left corner so the
        # daylit disc reads cleanly; flat-map styles keep the wide
        # banner. render() chooses between the two each frame.
        self._clock_rect_default = Rect(0, 0, canvas_w, clock_h)
        self._clock_factor_default = 0.78
        self._clock_rect_globe = Rect(
            int(canvas_w * 0.02), int(canvas_h * 0.02),
            int(canvas_w * 0.34), int(canvas_h * 0.18),
        )
        self._clock_factor_globe = 0.78
        self._clock = ClockWidget(
            self._clock_rect_default,
            font_factor=self._clock_factor_default,
        )
        self.add(self._clock)

        def station_line() -> str:
            cur = station_service.current()
            if cur is not None and cur.name:
                return cur.name
            return mpd_service.status.station or _t("station.unknown")

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
            return (_t("button.play") if mpd_service.status.state == "pause"
                    else _t("button.pause"))

        half = canvas_w // 2
        self.add(Button(
            Rect(0, bot_y, half, action_h),
            label_src=play_label,
            on_press=lambda: mpd_service.command("toggle"),
            font_factor=0.32,
        ))
        self.add(Button(
            Rect(half, bot_y, canvas_w - half, action_h),
            label_src=lambda: _t("button.stations"),
            on_press=lambda: compositor.set_overlay("station_list"),
            font_factor=0.32,
        ))
        _add_transport_footer(self, mpd_service, station_service,
                              canvas_w, canvas_h)

        # "Updating bg" indicator — paints last so it sits on top.
        self.add(_rendering_indicator(self, canvas_w, canvas_h))

    def on_tap(self, cx: float, cy: float) -> bool:
        """Same empty-area-tap-to-Apps as IdleScene so the gesture is
        the same in both home modes (clock & radio)."""
        self._compositor.set_overlay("launcher")
        return True

    def _apply_clock_layout(self) -> None:
        if _scene_bg_is_globe(self):
            self._clock.rect = self._clock_rect_globe
            self._clock.font_factor = self._clock_factor_globe
        else:
            self._clock.rect = self._clock_rect_default
            self._clock.font_factor = self._clock_factor_default

    def render(self) -> Image.Image:
        self._apply_clock_layout()
        return super().render()


def _add_bt_transport_footer(scene: "Scene", bluetooth_service,
                             mpd_service, canvas_w: int, canvas_h: int,
                             *, x_offset: int = 0,
                             frac: float = 0.10) -> None:
    """Variant of `_add_transport_footer` for the BluetoothPlayingScene.
    Same 4-zone strip, but the leftmost button is DISCONNECT (drops the
    phone link, leaves the pairing intact) instead of PLAY/STOP. Volume
    keeps working — bluealsa-aplay shares the ALSA mixer with MPD, so
    the same VOL−/+ buttons adjust the phone audio as they would adjust
    the radio."""
    foot_h = int(canvas_h * frac)
    foot_y = canvas_h - foot_h
    inner_w = canvas_w - x_offset
    play_w = int(inner_w * 0.30)   # wider — "DISCONNECT" needs the room
    minus_w = int(inner_w * 0.24)
    readout_w = int(inner_w * 0.20)
    plus_w = inner_w - play_w - minus_w - readout_w

    def _disconnect() -> None:
        # Find the streaming phone's MAC by walking the device list
        # for the first connected paired non-speaker (mirrors the
        # logic in BluetoothScene._connected_mac). Falls back to a
        # plain `set_discoverable(False)` if nothing matches — which
        # is itself a useful safety net (closes the door at minimum).
        s = bluetooth_service.status
        for d in s.devices:
            if d.connected and d.paired and not d.is_audio \
                    and d.mac != s.paired_mac:
                bluetooth_service.disconnect(d.mac)
                return
        bluetooth_service.set_discoverable(False)

    scene.add(Button(
        Rect(x_offset, foot_y, play_w, foot_h),
        label_src=lambda: _t("bluetooth.button.disconnect"),
        on_press=_disconnect,
        font_factor=0.36,
        color_role="fg_bright",
    ))
    scene.add(Button(
        Rect(x_offset + play_w, foot_y, minus_w, foot_h),
        label_src=lambda: _t("button.vol_down"),
        on_press=lambda: mpd_service.command("vol_down"),
        font_factor=0.42,
    ))
    scene.add(TextWidget(
        Rect(x_offset + play_w + minus_w, foot_y, readout_w, foot_h),
        text_src=lambda: f"{mpd_service.status.volume}",
        font_factor=0.55,
        color_role="fg_dim",
    ))
    scene.add(Button(
        Rect(x_offset + play_w + minus_w + readout_w,
             foot_y, plus_w, foot_h),
        label_src=lambda: _t("button.vol_up"),
        on_press=lambda: mpd_service.command("vol_up"),
        font_factor=0.42,
    ))


class BluetoothPlayingScene(Scene):
    """Home scene shown automatically while a phone is streaming to the
    radio over A2DP. Same shape as IdleScene/RadioScene (clock + body
    + footer) but the body announces the streaming source and the
    footer's primary action is DISCONNECT instead of PLAY/STOP.

    Selected by `pick_scene` in clockradio.main when
    `bluetooth.status.streaming_from` is non-empty. When the phone
    hangs up, streaming_from clears and the user lands back on
    IdleScene (or RadioScene if MPD auto-resumes its previous play)."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 alarm_service, mpd_service, bluetooth_service,
                 compositor):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._bt = bluetooth_service

        footer_h = int(canvas_h * 0.10)
        body_h = canvas_h - footer_h

        # Clock at the top — same alt-layout dance as IdleScene so
        # globe backgrounds get the corner-clock treatment that keeps
        # the daylit hemisphere clean.
        clock_h = int(canvas_h * 0.40)
        self._clock_rect_default = Rect(0, 0, canvas_w, clock_h)
        self._clock_factor_default = 0.62
        self._clock_rect_globe = Rect(
            int(canvas_w * 0.02), int(canvas_h * 0.02),
            int(canvas_w * 0.34), int(canvas_h * 0.18),
        )
        self._clock_factor_globe = 0.78
        self._clock = ClockWidget(
            self._clock_rect_default,
            font_factor=self._clock_factor_default,
        )
        self.add(self._clock)

        # Streaming-source band: subtitle + big phone name. Sits between
        # the clock and the action footer.
        np_y = clock_h
        np_h = body_h - clock_h
        sub_h = int(np_h * 0.30)
        name_h = int(np_h * 0.50)
        self.add(TextWidget(
            Rect(0, np_y, canvas_w, sub_h),
            text_src=lambda: _t("bluetooth.home.from_phone"),
            font_role="regular",
            font_factor=0.50,
            color_role="fg_subtle",
        ))
        self.add(TextWidget(
            Rect(0, np_y + sub_h, canvas_w, name_h),
            text_src=lambda: (self._bt.status.streaming_from
                              or self._bt.status.connected_phone
                              or _t("bluetooth.unknown_name")),
            font_factor=0.65,
            color_role="fg_accent",
        ))

        # Footer: alarm pill (same as IdleScene) + BT transport
        # (DISCONNECT + VOL controls).
        alarm_w = int(canvas_w * 0.30)
        foot_y = canvas_h - footer_h
        bell_size = int(footer_h * 0.55)
        bell_x = int(canvas_w * 0.025)
        self.add(Button(
            Rect(bell_x, foot_y, alarm_w - bell_x, footer_h),
            label_src=lambda: _format_footer_alarm(alarm_service),
            on_press=lambda: compositor.set_overlay("alarm_list"),
            outline_width=0,
            color_role="fg_bright",
            font_factor=0.52,
            halo=True,
        ))
        self.add(BellIconWidget(
            Rect(bell_x, foot_y + (footer_h - bell_size) // 2,
                 bell_size, bell_size),
            is_visible_src=lambda: _alarm_armed(alarm_service),
            color_role="fg_bright",
        ))
        _add_bt_transport_footer(self, bluetooth_service, mpd_service,
                                 canvas_w, canvas_h, x_offset=alarm_w)

        self.add(_rendering_indicator(self, canvas_w, canvas_h))

    def on_tap(self, cx: float, cy: float) -> bool:
        """Empty-area tap opens the launcher — matches IdleScene /
        RadioScene so the gesture is the same on every home variant."""
        self._compositor.set_overlay("launcher")
        return True

    def _apply_clock_layout(self) -> None:
        if _scene_bg_is_globe(self):
            self._clock.rect = self._clock_rect_globe
            self._clock.font_factor = self._clock_factor_globe
        else:
            self._clock.rect = self._clock_rect_default
            self._clock.font_factor = self._clock_factor_default

    def render(self) -> Image.Image:
        self._apply_clock_layout()
        return super().render()


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
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.launcher.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))

        def stub():
            return None

        # (i18n-key, on_press, icon-name). The label_src closes over the
        # key so a language switch repaints with the new text without
        # rebuilding the scene.
        cells = [
            ("launcher.tile.radio",
             lambda: compositor.set_overlay("station_list"), "radio"),
            ("launcher.tile.alarms",
             lambda: compositor.set_overlay("alarm_list"), "clock"),
            ("launcher.tile.weather",
             lambda: compositor.set_overlay("weather"), "partly_cloudy"),
            ("launcher.tile.verse",
             lambda: compositor.set_overlay("verse"), "book"),
            ("launcher.tile.camera", stub, "camera"),
            ("launcher.tile.settings",
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
        for i, (key, action, icon_name) in enumerate(cells):
            col = i % cols
            row = i // cols
            tile_x = margin_x + int(col * cell_w) + pad
            tile_y = grid_top + int(row * cell_h) + pad
            tile_w = int(cell_w) - 2 * pad
            tile_h = int(cell_h) - 2 * pad
            self.add(AppTile(
                Rect(tile_x, tile_y, tile_w, tile_h),
                label_src=(lambda k=key: _t(k)),
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
            station = self._mpd.status.station or _t("quick.radio")
            return station
        nf = self._alarms.next_to_fire()
        if nf is not None:
            a, _ = nf
            skip = (f"  ({_t('alarm.skip_marker')})"
                    if a.skip_next else "")
            return _t("quick.next_label",
                      time=f"{a.hour:02d}:{a.minute:02d}",
                      days=days_label(a.days)) + skip
        return _t("alarm.no_alarm")

    def _live_actions(self):
        """List of (label_callable, action) for buttons that make
        sense right now. Labels are callables so a language switch
        live-updates the label text without rebuilding."""
        comp = self._compositor

        def wrap(fn):
            def _():
                comp.clear_overlay()
                fn()
            return _

        actions: list[tuple] = []
        if self._mpd.status.active:
            actions.append((lambda: _t("quick.stop_radio"),
                            wrap(lambda: self._mpd.command(
                                ("stop_alarm",)))))
        nf = self._alarms.next_to_fire()
        if nf is not None:
            skip_now = nf[0].skip_next
            actions.append(
                ((lambda s=skip_now: _t("quick.unskip_next") if s
                  else _t("quick.skip_next_alarm")),
                 wrap(lambda: self._alarms.toggle_skip_next())))
        actions.append((lambda: _t("button.close"),
                        lambda: comp.clear_overlay()))
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
        for i, (label_fn, fn) in enumerate(actions):
            btn = Button(
                Rect(int(self.canvas_w * 0.06),
                     body_top + i * cell_h,
                     int(self.canvas_w * 0.88),
                     cell_h - 8),
                label_src=label_fn,
                on_press=fn,
                font_factor=0.36,
            )
            self.widgets.append(btn)
            self._action_buttons.append(btn)

    # --- Scene overrides ---------------------------------------------

    def state_key(self) -> tuple:
        # Build keys off the current live action set so a state change
        # (e.g. radio starts playing while panel is open) refreshes layout.
        # Label callables are eagerly evaluated for the cache key so a
        # language switch (which just changes their output) invalidates
        # the cache via _lang_version() in Scene.state_key already.
        labels = tuple(lbl() for lbl, _ in self._live_actions())
        return (self._header_text(), labels)

    def render(self) -> Image.Image:
        # Rebuild the action buttons each render so they reflect live state.
        self._rebuild_actions()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        # Make sure hit-test sees the freshly-built buttons.
        self._rebuild_actions()
        return super().hit(cx, cy)


def _adapt_settings_icon(fn):
    """Wrap a 3-arg SETTINGS_ICONS drawer (draw, rect, col) for AppTile's
    4-arg signature (draw, rect, theme, col). The settings glyphs were
    originally written for IconRow (3-arg) and don't need theme — this
    keeps both callers happy without touching every icon definition.
    Returns None unchanged so the AppTile no-icon branch still fires."""
    if fn is None:
        return None
    return lambda draw, rect, theme, col, _f=fn: _f(draw, rect, col)


def _settings_tile_grid(scene: "Scene", canvas_w: int, canvas_h: int,
                        head_h: int, tiles: list,
                        cols: int, rows: int) -> None:
    """Lay tiles out in a cols×rows grid below the header. Used by every
    settings page (top-level + group sub-pages) so paging visuals stay
    identical regardless of how many tiles a page hosts.

    `tiles` is a list of (label_src, on_press, icon_drawer). Cells past
    len(tiles) are left empty — the grid still reserves their slots so
    a 3-tile page lays out with the same per-tile size as a 6-tile one
    and the user's eye doesn't have to recalibrate between pages."""
    margin_x = int(canvas_w * 0.04)
    margin_y = int(canvas_h * 0.03)
    grid_top = head_h + margin_y
    grid_w = canvas_w - 2 * margin_x
    grid_h = canvas_h - grid_top - margin_y
    cell_w = grid_w / cols
    cell_h = grid_h / rows
    pad = int(min(cell_w, cell_h) * 0.06)
    for i, (label_src, action, icon_drawer) in enumerate(tiles):
        col = i % cols
        row = i // cols
        tile_x = margin_x + int(col * cell_w) + pad
        tile_y = grid_top + int(row * cell_h) + pad
        tile_w = int(cell_w) - 2 * pad
        tile_h = int(cell_h) - 2 * pad
        scene.add(AppTile(
            Rect(tile_x, tile_y, tile_w, tile_h),
            label_src=label_src,
            on_press=action,
            icon_drawer=icon_drawer,
        ))


class SettingsScene(Scene):
    """Overlay: top-level settings, presented as a 3×2 tile grid.

    Six groups: WIFI · AUDIO · DISPLAY / LANGUAGE · DEMO · BLUETOOTH.
    BLUETOOTH was promoted out of the old AUDIO sub-page because phone
    pairing is the most-tapped audio-related feature and deserved a
    direct path; the AUDIO tile now opens the output picker straight
    away (the only thing left under it). DISPLAY remains a sub-page
    (theme/background/brightness — three leaves worth grouping). ABOUT
    is reachable via a small ⓘ icon button in the header — kept for
    diagnostics without occupying a full tile."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor):
        super().__init__(theme, canvas_w, canvas_h)
        head_h = int(canvas_h * 0.12)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.60), head_h),
            text_src=lambda: _t("scene.settings.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        # ⓘ icon button on the right of the header → AboutScene. Sized
        # to match the BACK / HOME chrome on the left so the header
        # row reads as a balanced trio of affordances.
        info_h = int(head_h * 0.80)
        info_w = info_h
        right_pad = int(canvas_w * 0.025)
        self.add(IconButton(
            Rect(canvas_w - right_pad - info_w,
                 int(head_h * 0.10), info_w, info_h),
            on_press=lambda: compositor.set_overlay("about"),
            icon_drawer=SETTINGS_ICONS["info"],
            color_role="fg_dim",
            outline_width=2,
            icon_factor=0.65,
        ))
        tiles = [
            ((lambda: _t("settings.row.wifi")),
             lambda: compositor.set_overlay("wifi"),
             _adapt_settings_icon(SETTINGS_ICONS.get("wifi"))),
            ((lambda: _t("settings.row.audio")),
             lambda: compositor.set_overlay("audio_output"),
             _adapt_settings_icon(SETTINGS_ICONS.get("speaker"))),
            ((lambda: _t("settings.row.display")),
             lambda: compositor.set_overlay("display_settings"),
             _adapt_settings_icon(SETTINGS_ICONS.get("monitor"))),
            ((lambda: _t("settings.row.language")),
             lambda: compositor.set_overlay("language"),
             _adapt_settings_icon(SETTINGS_ICONS.get("language"))),
            ((lambda: _t("settings.row.demo")),
             lambda: compositor.set_overlay("demo_intro"),
             _adapt_settings_icon(SETTINGS_ICONS.get("play"))),
            ((lambda: _t("settings.row.bluetooth")),
             lambda: compositor.set_overlay("bluetooth"),
             _adapt_settings_icon(SETTINGS_ICONS.get("bluetooth"))),
        ]
        _settings_tile_grid(self, canvas_w, canvas_h, head_h,
                            tiles, cols=3, rows=2)


class DisplaySettingsScene(Scene):
    """Overlay: DISPLAY group page. Three leaf tiles — THEME, BACKGROUND,
    BRIGHTNESS. Reached from Settings → DISPLAY; back returns to
    Settings. Single row of three tiles keeps each tile tall and easy
    to read."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor):
        super().__init__(theme, canvas_w, canvas_h)
        head_h = int(canvas_h * 0.12)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.display_settings.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        tiles = [
            ((lambda: _t("settings.row.theme")),
             lambda: compositor.set_overlay("theme"),
             _adapt_settings_icon(SETTINGS_ICONS.get("palette"))),
            ((lambda: _t("settings.row.background")),
             lambda: compositor.set_overlay("background"),
             _adapt_settings_icon(SETTINGS_ICONS.get("globe"))),
            ((lambda: _t("settings.row.brightness")),
             lambda: compositor.set_overlay("brightness"),
             _adapt_settings_icon(SETTINGS_ICONS.get("brightness"))),
        ]
        _settings_tile_grid(self, canvas_w, canvas_h, head_h,
                            tiles, cols=3, rows=1)


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
            on_press=lambda: compositor.set_overlay("display_settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.theme.title"),
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
                font_factor=0.55,
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
    new secured networks step 2 will hand off to a password scene.

    Long lists page in MAX_ROWS-sized chunks via ▲/▼ buttons in the
    header right; page indicator (e.g. "2/3") sits just left of them
    and is hidden when the whole list fits on one page."""

    MAX_ROWS = 6

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, wifi_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._wifi = wifi_service
        self._page = 0
        head_h = int(canvas_h * 0.12)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.22), head_h),
            text_src=lambda: _t("scene.wifi.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        # RESCAN moved leftward to make room for the page indicator
        # and ▲/▼ paging buttons on the right edge.
        self.add(Button(
            Rect(int(canvas_w * 0.44), int(head_h * 0.10),
                 int(canvas_w * 0.20), int(head_h * 0.80)),
            label_src=lambda: _t("button.rescan"),
            on_press=lambda: wifi_service.rescan(),
            font_factor=0.42,
        ))
        # Paging affordances: same right-anchored pattern as
        # StationListScene / BackgroundScene for visual consistency.
        btn_h = int(head_h * 0.80)
        btn_w = btn_h
        right_pad = int(canvas_w * 0.025)
        gap = int(canvas_w * 0.012)
        down_x = canvas_w - right_pad - btn_w
        up_x = down_x - btn_w - gap
        self.add(TextWidget(
            Rect(int(canvas_w * 0.66), 0,
                 up_x - int(canvas_w * 0.66) - gap, head_h),
            text_src=self._page_label,
            font_factor=0.40,
            color_role="fg_dim",
        ))
        self.add(IconButton(
            Rect(up_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_up,
            icon_drawer=_icon_chevron_up,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        self.add(IconButton(
            Rect(down_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_down,
            icon_drawer=_icon_chevron_down,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
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
            return _t("wifi.connecting")
        if s.last_error:
            return _t("wifi.error", message=s.last_error)
        if s.ssid:
            return _t("wifi.connected",
                      ssid=s.ssid, signal=s.signal, ip=s.ip)
        return _t("wifi.not_connected", state=s.state)

    def _max_page(self) -> int:
        n = len(self._wifi.status.networks)
        if n <= self.MAX_ROWS:
            return 0
        return (n - 1) // self.MAX_ROWS

    def _page_label(self) -> str:
        n = len(self._wifi.status.networks)
        if n <= self.MAX_ROWS:
            return ""
        total = self._max_page() + 1
        page = min(self._page, total - 1)
        return f"{page + 1}/{total}"

    def _page_up(self) -> None:
        if self._page > 0:
            self._page -= 1

    def _page_down(self) -> None:
        if self._page < self._max_page():
            self._page += 1

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
        # Clamp page if a fresh scan returned fewer networks than before.
        max_p = self._max_page()
        if self._page > max_p:
            self._page = max_p
        if not nets:
            empty = TextWidget(
                Rect(0, body_top, self.canvas_w, body_h),
                text_src=lambda: _t("wifi.empty_list"),
                font_factor=0.05,
                color_role="fg_dim",
                font_role="regular",
            )
            self.widgets.append(empty)
            self._row_widgets.append(empty)
            return
        start = self._page * self.MAX_ROWS
        page_nets = nets[start:start + self.MAX_ROWS]
        cell_h = body_h // self.MAX_ROWS
        for i, n in enumerate(page_nets):
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
            self._page,
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
        self.add(_home_button(cw, head_h, self._compositor))
        self.add(TextWidget(
            Rect(int(cw * 0.20), 0, int(cw * 0.78), head_h),
            text_src=_t("scene.wifi_password.title", ssid=self._ssid),
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
            text_src=(display or _t("wifi.password_hint")),
            font_factor=0.55,
            color_role=("fg_bright" if self._password else "fg_dim"),
        ))
        self.add(Button(
            Rect(int(cw * 0.68), entry_y + int(entry_h * 0.10),
                 int(cw * 0.14), int(entry_h * 0.80)),
            label_src=(_t("button.hide") if self._show
                       else _t("button.show")),
            on_press=self._toggle_show,
            font_factor=0.42,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(cw * 0.84), entry_y + int(entry_h * 0.10),
                 int(cw * 0.14), int(entry_h * 0.80)),
            label_src=_t("button.ok"),
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
            label_src=_t("button.shift"),
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
            label_src=_t("button.del"),
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
            label_src=_t("button.space"),
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


class BluetoothScene(Scene):
    """Overlay: make this radio act as a Bluetooth speaker (sink mode).

    Most users only ever do one Bluetooth operation: pair a phone so it
    can stream music to the radio. That's the headline of this scene —
    a state-machine UI that walks the user through the full lifecycle:

        OFF              hero CTA  →  open the door for 5 minutes
        DISCOVERABLE     show the controller name + countdown
        CONNECTED        confirm the pair, prompt them to hit play
        STREAMING        "now playing from <phone>" + disconnect/forget

    Connecting *to* an external Bluetooth speaker (the inverse —
    radio → speaker) is rare enough that it lives behind a small
    "Connect external speaker ›" footer link, which navigates to a
    dedicated BluetoothSpeakerScene with the scan/pair/forget UI.

    All widgets are rebuilt per-frame from the current snapshot so the
    layout transitions cleanly between the four states without bespoke
    per-state widget caches.
    """

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, bluetooth_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._bt = bluetooth_service
        head_h = int(canvas_h * 0.12)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.60), head_h),
            text_src=lambda: _t("scene.bluetooth.title"),
            font_factor=0.45,
            color_role="fg_dim",
        ))
        # Status line under the header carries transient feedback only
        # ("Disconnected", "Forgotten", error text, etc.) — the primary
        # state shows up in the body hero. Kept reserved here so the
        # body geometry never shifts when an action message arrives.
        stat_h = int(canvas_h * 0.08)
        self._stat_h = stat_h
        self.add(TextWidget(
            Rect(int(canvas_w * 0.04), head_h,
                 int(canvas_w * 0.92), stat_h),
            text_src=self._status_line,
            font_factor=0.40,
            font_role="regular",
            color_role="fg_subtle",
        ))
        self._dynamic: list[Widget] = []

    # --- helpers -------------------------------------------------------

    def _status_line(self) -> str:
        s = self._bt.status
        if not s.available:
            return _t("bluetooth.unavailable")
        if s.busy:
            return _t("bluetooth.busy")
        if s.last_error:
            return _t("bluetooth.error", message=s.last_error)
        if s.last_action:
            return s.last_action
        return ""

    def _ui_state(self) -> str:
        s = self._bt.status
        if not s.available:
            return "unavailable"
        if s.streaming_from:
            return "streaming"
        if s.connected_phone:
            return "connected"
        if s.discoverable:
            return "discoverable"
        return "off"

    @staticmethod
    def _fmt_mmss(secs: int) -> str:
        secs = max(0, int(secs))
        mm, ss = divmod(secs, 60)
        return f"{mm}:{ss:02d}"

    def _on_open(self) -> None:
        if self._bt.status.available:
            self._bt.set_discoverable(True)

    def _on_stop(self) -> None:
        # Closes the discoverable window. Existing connections survive
        # (matches the bluez behaviour); the user has to disconnect
        # explicitly from the connected/streaming view.
        self._bt.set_discoverable(False)

    def _connected_mac(self) -> str:
        """MAC of the phone we're showing in the connected/streaming
        body. Walks the device list for the first connected paired
        non-speaker — same logic the service uses to pick
        connected_phone — so the FORGET / DISCONNECT buttons act on
        the right device. Returns "" if nothing matches (rare race:
        UI built off a snapshot the moment a phone disconnected)."""
        s = self._bt.status
        for d in s.devices:
            if d.connected and d.paired and not d.is_audio \
                    and d.mac != s.paired_mac:
                return d.mac
        return ""

    def _on_disconnect(self) -> None:
        mac = self._connected_mac()
        if mac:
            self._bt.disconnect(mac)

    def _on_forget_phone(self) -> None:
        mac = self._connected_mac()
        if mac:
            self._bt.forget(mac)

    def _go_speaker(self) -> None:
        self._compositor.set_overlay("bluetooth_speaker")

    # --- body builder --------------------------------------------------

    def _rebuild(self) -> None:
        for w in self._dynamic:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._dynamic.clear()

        body_top = self._head_h + self._stat_h + int(self.canvas_h * 0.02)
        body_bot = self.canvas_h - int(self.canvas_h * 0.02)
        margin_x = int(self.canvas_w * 0.04)
        inner_w = self.canvas_w - 2 * margin_x

        # Footer link is the same in every state — small, dim, lives at
        # the bottom of the body. Reserves vertical space at the bottom
        # so the body lays out above it.
        link_h = int(self.canvas_h * 0.08)
        link_w = int(self.canvas_w * 0.42)
        link_y = body_bot - link_h
        link_x = self.canvas_w // 2 - link_w // 2
        link_btn = Button(
            Rect(link_x, link_y, link_w, link_h),
            label_src=lambda: _t("bluetooth.link.speaker"),
            on_press=self._go_speaker,
            font_factor=0.36,
            color_role="fg_dim",
            outline_width=1,
        )
        self.widgets.append(link_btn)
        self._dynamic.append(link_btn)

        avail_top = body_top
        avail_bot = link_y - int(self.canvas_h * 0.02)

        state = self._ui_state()
        if state == "unavailable":
            return  # status line already says it; nothing else to draw
        if state == "off":
            self._build_off(avail_top, avail_bot, margin_x, inner_w)
        elif state == "discoverable":
            self._build_discoverable(avail_top, avail_bot,
                                     margin_x, inner_w)
        elif state == "connected":
            self._build_connected(avail_top, avail_bot,
                                  margin_x, inner_w, streaming=False)
        elif state == "streaming":
            self._build_connected(avail_top, avail_bot,
                                  margin_x, inner_w, streaming=True)

    def _add(self, w: Widget) -> None:
        self.widgets.append(w)
        self._dynamic.append(w)

    def _build_off(self, top: int, bot: int,
                   margin_x: int, inner_w: int) -> None:
        # Hero block + big primary button. Vertically centered in the
        # available area so the pair-once-a-month action looks
        # deliberate and easy to find at bedside distance.
        avail_h = bot - top
        title_h = int(avail_h * 0.18)
        sub_h = int(avail_h * 0.22)
        btn_h = int(avail_h * 0.22)
        gap = int(avail_h * 0.04)
        block_h = title_h + sub_h + gap + btn_h
        y = top + (avail_h - block_h) // 2
        self._add(TextWidget(
            Rect(margin_x, y, inner_w, title_h),
            text_src=lambda: _t("bluetooth.headline.idle"),
            font_factor=0.70,
            color_role="fg_bright",
        ))
        y += title_h
        self._add(WrappedTextWidget(
            Rect(margin_x, y, inner_w, sub_h),
            text_src=lambda: _t("bluetooth.subline.idle"),
            font_size=max(18, int(self.canvas_h * 0.038)),
            color_role="fg_subtle",
            line_spacing=1.20,
        ))
        y += sub_h + gap
        big_w = int(inner_w * 0.78)
        big_x = self.canvas_w // 2 - big_w // 2
        self._add(Button(
            Rect(big_x, y, big_w, btn_h),
            label_src=lambda: _t("bluetooth.cta.open"),
            on_press=self._on_open,
            font_factor=0.46,
            color_role="fg_accent",
            outline_width=3,
        ))

    def _build_discoverable(self, top: int, bot: int,
                            margin_x: int, inner_w: int) -> None:
        # "Pair your phone with «name»" with the controller name as the
        # visual centerpiece, then a big M:SS countdown, then STOP.
        s = self._bt.status
        avail_h = bot - top
        prompt_h = int(avail_h * 0.13)
        name_h = int(avail_h * 0.22)
        sub_h = int(avail_h * 0.16)
        count_h = int(avail_h * 0.18)
        btn_h = int(avail_h * 0.16)
        gap = int(avail_h * 0.025)
        block_h = (prompt_h + name_h + sub_h + count_h + btn_h + 4 * gap)
        y = top + max(0, (avail_h - block_h) // 2)
        self._add(TextWidget(
            Rect(margin_x, y, inner_w, prompt_h),
            text_src=lambda: _t("bluetooth.headline.discoverable"),
            font_factor=0.62,
            color_role="fg_bright",
        ))
        y += prompt_h + gap
        # Controller name in big accent type. Falls back to a neutral
        # placeholder if the alias hasn't been read yet (first second
        # after enabling sink mode); the next 1Hz poll fills it in.
        self._add(TextWidget(
            Rect(margin_x, y, inner_w, name_h),
            text_src=lambda: (s.controller_name
                              or _t("bluetooth.unknown_name")),
            font_factor=0.78,
            color_role="fg_accent",
        ))
        y += name_h + gap
        self._add(WrappedTextWidget(
            Rect(margin_x, y, inner_w, sub_h),
            text_src=lambda: _t("bluetooth.subline.discoverable"),
            font_size=max(16, int(self.canvas_h * 0.034)),
            color_role="fg_subtle",
            line_spacing=1.20,
        ))
        y += sub_h + gap
        self._add(TextWidget(
            Rect(margin_x, y, inner_w, count_h),
            text_src=lambda: _t(
                "bluetooth.countdown_remaining",
                time=self._fmt_mmss(s.discoverable_seconds_left)),
            font_factor=0.70,
            color_role="fg_bright",
        ))
        y += count_h + gap
        big_w = int(inner_w * 0.50)
        big_x = self.canvas_w // 2 - big_w // 2
        self._add(Button(
            Rect(big_x, y, big_w, btn_h),
            label_src=lambda: _t("bluetooth.button.stop"),
            on_press=self._on_stop,
            font_factor=0.50,
            color_role="fg_dim",
            outline_width=2,
        ))

    def _build_connected(self, top: int, bot: int,
                         margin_x: int, inner_w: int, *,
                         streaming: bool) -> None:
        # Same skeleton for "connected" and "streaming"; differs only in
        # the headline + subline strings and the headline colour role
        # (accent for live audio, bright for paired-but-idle).
        s = self._bt.status
        phone = s.streaming_from or s.connected_phone or ""
        avail_h = bot - top
        head_h = int(avail_h * 0.16)
        name_h = int(avail_h * 0.22)
        sub_h = int(avail_h * 0.20)
        btn_h = int(avail_h * 0.16)
        gap = int(avail_h * 0.04)
        # Optional small countdown row (only while still discoverable).
        show_count = bool(s.discoverable and s.discoverable_seconds_left > 0)
        count_h = int(avail_h * 0.08) if show_count else 0
        block_h = head_h + name_h + sub_h + count_h + btn_h + 4 * gap
        y = top + max(0, (avail_h - block_h) // 2)
        if streaming:
            head_key = "bluetooth.headline.streaming"
            sub_key = "bluetooth.subline.streaming"
            head_color = "fg_accent"
        else:
            head_key = "bluetooth.headline.connected"
            sub_key = "bluetooth.subline.connected"
            head_color = "fg_bright"
        self._add(TextWidget(
            Rect(margin_x, y, inner_w, head_h),
            text_src=lambda: _t(head_key),
            font_factor=0.62,
            color_role=head_color,
        ))
        y += head_h + gap
        self._add(TextWidget(
            Rect(margin_x, y, inner_w, name_h),
            text_src=lambda: phone,
            font_factor=0.70,
            color_role="fg_bright",
        ))
        y += name_h + gap
        self._add(WrappedTextWidget(
            Rect(margin_x, y, inner_w, sub_h),
            text_src=lambda: _t(sub_key),
            font_size=max(16, int(self.canvas_h * 0.036)),
            color_role="fg_subtle",
            line_spacing=1.20,
        ))
        y += sub_h + gap
        if show_count:
            self._add(TextWidget(
                Rect(margin_x, y, inner_w, count_h),
                text_src=lambda: _t(
                    "bluetooth.countdown_remaining",
                    time=self._fmt_mmss(s.discoverable_seconds_left)),
                font_factor=0.70,
                color_role="fg_dim",
            ))
            y += count_h + gap
        # Two buttons side by side: DISCONNECT (keeps pairing) +
        # FORGET PHONE (removes pairing entirely). Disconnect is the
        # everyday action — placed first; FORGET reads as the
        # "remove this phone forever" affordance, dimmed.
        btn_gap = int(self.canvas_w * 0.02)
        btn_w = (inner_w - btn_gap) // 2
        self._add(Button(
            Rect(margin_x, y, btn_w, btn_h),
            label_src=lambda: _t("bluetooth.button.disconnect"),
            on_press=self._on_disconnect,
            font_factor=0.42,
            color_role="fg_bright",
            outline_width=2,
        ))
        self._add(Button(
            Rect(margin_x + btn_w + btn_gap, y, btn_w, btn_h),
            label_src=lambda: _t("bluetooth.button.forget_phone"),
            on_press=self._on_forget_phone,
            font_factor=0.42,
            color_role="fg_dim",
            outline_width=2,
        ))

    # --- scene API -----------------------------------------------------

    def state_key(self) -> tuple:
        s = self._bt.status
        return (
            s.available, s.busy, s.last_error, s.last_action,
            s.discoverable, s.discoverable_seconds_left,
            s.streaming_from, s.connected_phone, s.controller_name,
            self._ui_state(),
        )

    def render(self) -> Image.Image:
        self._rebuild()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild()
        return super().hit(cx, cy)


class BluetoothSpeakerScene(Scene):
    """Overlay: pair an external Bluetooth speaker as the radio's
    audio output. Less prominent than sink mode (most users won't open
    this) — reached via the "Connect external speaker ›" link inside
    BluetoothScene.

    UX is the discover-and-pair list lifted from the previous combined
    scene, with phones filtered out (anything paired and not flagged
    is_audio is presumed to be a sink-mode phone and belongs over in
    BluetoothScene's lifecycle UI). Currently-paired speaker pinned at
    the top with FORGET; everything else paginated."""

    MAX_ROWS = 6

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, bluetooth_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._bt = bluetooth_service
        self._page = 0
        head_h = int(canvas_h * 0.12)
        self._head_h = head_h
        # BACK returns to the BluetoothScene (the speaker scene is a
        # leaf under it). HOME is a one-tap escape to idle as usual.
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("bluetooth"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.30), head_h),
            text_src=lambda: _t("scene.bluetooth_speaker.title"),
            font_factor=0.40,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.52), int(head_h * 0.10),
                 int(canvas_w * 0.20), int(head_h * 0.80)),
            label_src=lambda: _t("button.rescan"),
            on_press=lambda: self._bt.scan(),
            font_factor=0.42,
        ))
        btn_h = int(head_h * 0.80)
        btn_w = btn_h
        right_pad = int(canvas_w * 0.025)
        gap = int(canvas_w * 0.012)
        down_x = canvas_w - right_pad - btn_w
        up_x = down_x - btn_w - gap
        self.add(TextWidget(
            Rect(int(canvas_w * 0.74), 0,
                 up_x - int(canvas_w * 0.74) - gap, head_h),
            text_src=self._page_label,
            font_factor=0.38,
            color_role="fg_dim",
        ))
        self.add(IconButton(
            Rect(up_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_up,
            icon_drawer=_icon_chevron_up,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        self.add(IconButton(
            Rect(down_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_down,
            icon_drawer=_icon_chevron_down,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        stat_h = int(canvas_h * 0.08)
        self._stat_h = stat_h
        self.add(TextWidget(
            Rect(int(canvas_w * 0.04), head_h,
                 int(canvas_w * 0.92), stat_h),
            text_src=self._status_line,
            font_factor=0.40,
            font_role="regular",
            color_role="fg_subtle",
        ))
        self._row_widgets: list[Widget] = []

    # --- status / paging ----------------------------------------------

    def _status_line(self) -> str:
        s = self._bt.status
        if not s.available:
            return _t("bluetooth.unavailable")
        if s.busy:
            return _t("bluetooth.busy")
        if s.discovering:
            return _t("bluetooth.scanning")
        if s.last_error:
            return _t("bluetooth.error", message=s.last_error)
        if s.last_action:
            return s.last_action
        if s.paired_mac:
            name = next(
                (d.name for d in s.devices if d.mac == s.paired_mac),
                s.paired_mac)
            return _t("bluetooth.connected", name=name)
        return _t("bluetooth.idle")

    def _list_devices(self) -> list:
        """Devices to render in the scrollable list. Hides:
          • the currently-paired speaker (pinned above the list)
          • paired non-audio devices (phones from sink-mode pairings —
            those belong in the BluetoothScene lifecycle UI, not here)."""
        s = self._bt.status
        out = []
        for d in s.devices:
            if d.mac == s.paired_mac:
                continue
            if d.paired and not d.is_audio:
                continue
            out.append(d)
        return out

    def _max_page(self) -> int:
        n = len(self._list_devices())
        if n <= self.MAX_ROWS:
            return 0
        return (n - 1) // self.MAX_ROWS

    def _page_label(self) -> str:
        n = len(self._list_devices())
        if n <= self.MAX_ROWS:
            return ""
        total = self._max_page() + 1
        page = min(self._page, total - 1)
        return f"{page + 1}/{total}"

    def _page_up(self) -> None:
        if self._page > 0:
            self._page -= 1

    def _page_down(self) -> None:
        if self._page < self._max_page():
            self._page += 1

    def _on_pick(self, mac: str, name: str) -> None:
        s = self._bt.status
        if mac == s.paired_mac:
            return
        self._bt.pair(mac, name)

    def _on_forget(self, mac: str) -> None:
        self._bt.forget(mac)

    def on_show(self) -> None:
        if self._bt.status.available and not self._bt.status.busy:
            self._bt.scan()

    # --- layout --------------------------------------------------------

    def _rebuild_rows(self) -> None:
        for w in self._row_widgets:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._row_widgets.clear()
        s = self._bt.status

        body_top = self._head_h + self._stat_h + int(self.canvas_h * 0.02)
        body_bot = self.canvas_h - int(self.canvas_h * 0.02)
        margin_x = int(self.canvas_w * 0.04)
        inner_w = self.canvas_w - 2 * margin_x
        y = body_top

        if s.paired_mac:
            paired_dev = next(
                (d for d in s.devices if d.mac == s.paired_mac), None)
            paired_name = paired_dev.name if paired_dev else s.paired_mac
            row_h = int(self.canvas_h * 0.10)
            forget_w = int(self.canvas_w * 0.22)
            label_w = inner_w - forget_w - int(self.canvas_w * 0.02)
            connected = bool(paired_dev and paired_dev.connected)
            mark = "▶ " if connected else "  "
            label = f"{mark}{paired_name}"
            label_widget = TextWidget(
                Rect(margin_x, y, label_w, row_h),
                text_src=label,
                font_factor=0.50,
                color_role="fg_bright",
            )
            self.widgets.append(label_widget)
            self._row_widgets.append(label_widget)
            forget_btn = Button(
                Rect(margin_x + label_w + int(self.canvas_w * 0.02),
                     y + int(row_h * 0.10),
                     forget_w, int(row_h * 0.80)),
                label_src=lambda: _t("button.forget"),
                on_press=lambda mac=s.paired_mac: self._on_forget(mac),
                font_factor=0.42,
                color_role="fg_dim",
            )
            self.widgets.append(forget_btn)
            self._row_widgets.append(forget_btn)
            y += row_h + int(self.canvas_h * 0.01)

        others = self._list_devices()
        max_p = self._max_page()
        if self._page > max_p:
            self._page = max_p
        if not others:
            empty = TextWidget(
                Rect(0, y, self.canvas_w, body_bot - y),
                text_src=lambda: _t("bluetooth.empty_list"),
                font_factor=0.05,
                color_role="fg_dim",
                font_role="regular",
            )
            self.widgets.append(empty)
            self._row_widgets.append(empty)
            return
        start = self._page * self.MAX_ROWS
        page_devs = others[start:start + self.MAX_ROWS]
        cell_h = max(40, (body_bot - y) // self.MAX_ROWS)
        for i, d in enumerate(page_devs):
            tags = []
            if d.is_audio:
                tags.append(_t("bluetooth.tag.audio"))
            if d.connected:
                tags.append(_t("bluetooth.tag.connected"))
            elif d.paired:
                tags.append(_t("bluetooth.tag.paired"))
            tag_str = ("   " + "  ".join(tags)) if tags else ""
            label = f"  {d.name}{tag_str}"
            row_y = y + i * cell_h
            row_h = cell_h - 8
            btn = Button(
                Rect(margin_x, row_y, inner_w, row_h),
                label_src=label,
                on_press=lambda mac=d.mac, name=d.name:
                    self._on_pick(mac, name),
                font_factor=0.32,
                color_role="fg_bright",
            )
            self.widgets.append(btn)
            self._row_widgets.append(btn)

    def state_key(self) -> tuple:
        s = self._bt.status
        return (
            s.available, s.busy, s.discovering, s.paired_mac,
            s.last_error, s.last_action,
            tuple((d.mac, d.name, d.connected, d.paired, d.is_audio)
                  for d in s.devices),
            self._page,
        )

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
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
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.40), head_h),
            text_src=lambda: _t("scene.verse.title"),
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
            label_src=lambda: _t("button.refresh"),
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
            return _t("verse.loading")
        if s.last_error and not s.reference:
            return _t("wifi.error", message=s.last_error)
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
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.34), head_h),
            text_src=lambda: _t("scene.weather.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.74), int(head_h * 0.10),
                 int(canvas_w * 0.24), int(head_h * 0.80)),
            label_src=lambda: _t("button.refresh"),
            on_press=lambda: weather_service.refresh(),
            font_factor=0.42,
        ))
        # Current conditions block
        cur_y = head_h
        cur_h = int(canvas_h * 0.40)
        self._cur_y = cur_y
        self._cur_h = cur_h
        # Two stacked lines at the top of the block:
        #   1. Weekday + numeric date — always shown, locale-aware.
        #   2. Location — small, dim, *tappable* (opens the picker).
        # The location row is a Button so a tap anywhere on it routes
        # to WeatherLocationScene; outline_width=0 keeps it looking
        # like a label, not a chunky widget. Adding a trailing "›"
        # is the only affordance hinting at tappability.
        date_h = int(cur_h * 0.16)
        loc_h = int(cur_h * 0.16)
        self.add(TextWidget(
            Rect(0, cur_y, canvas_w, date_h),
            text_src=self._date_line,
            font_factor=0.55,
            color_role="fg_bright",
        ))
        self.add(Button(
            Rect(0, cur_y + date_h, canvas_w, loc_h),
            label_src=self._loc_line,
            on_press=lambda: compositor.set_overlay("weather_location"),
            font_factor=0.45,
            font_role="regular",
            color_role="fg_dim",
            outline_width=0,
        ))
        # Below location: icon (left) + temp/condition (right).
        body_y = cur_y + date_h + loc_h
        body_h = cur_h - date_h - loc_h
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
            return _t("weather.locating")
        if s.last_error:
            return _t("wifi.error", message=s.last_error)
        # Append a chevron so the row reads as tappable. Uses the
        # i18n-managed location string when nothing has been set yet
        # (e.g. very first boot before the IP-geo lookup completes).
        loc = s.location or _t("weather.tap_to_set")
        return f"{loc}  ›"

    def _date_line(self) -> str:
        """Today's weekday + numeric date, e.g. "Tuesday · 9 May".
        Uses the localised long weekday names from translations and
        the system's `%-d %b` for the date (numeric day is universal;
        month abbreviation falls back to the locale shipped with
        the OS image, English on a stock Pi)."""
        from datetime import date
        try:
            today = date.today()
        except Exception:
            return ""
        wkey = ("day.long.mon", "day.long.tue", "day.long.wed",
                "day.long.thu", "day.long.fri", "day.long.sat",
                "day.long.sun")[today.weekday()]
        weekday = _t(wkey)
        # `%-d` is POSIX-only (Linux); on Windows we'd need %#d. Pi is
        # Linux so this is safe. %b gives a 3-letter month abbreviation.
        try:
            datestr = today.strftime("%-d %b")
        except ValueError:
            datestr = today.strftime("%d %b").lstrip("0")
        return f"{weekday}  ·  {datestr}"

    def _temp_line(self) -> str:
        s = self._weather.status
        if s.cur_temp_c is None:
            return _t("misc.dash")
        return f"{round(s.cur_temp_c)}°C"

    def _cond_line(self) -> str:
        s = self._weather.status
        if s.cur_temp_c is None:
            return ""
        # Resolve the WMO code to a localised label at render time so a
        # language switch updates the readout without a fresh fetch.
        from weather_service import label_for_code
        return _t("weather.cond_line",
                  label=label_for_code(s.cur_code),
                  wind=round(s.cur_wind_kmh))

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
            wd = date.fromisoformat(iso).weekday()
        except Exception:
            return iso
        # Look up the localised short day name (Mon=0..Sun=6).
        return _t(("day.short.mon", "day.short.tue", "day.short.wed",
                   "day.short.thu", "day.short.fri", "day.short.sat",
                   "day.short.sun")[wd])

    def state_key(self) -> tuple:
        s = self._weather.status
        # Fold today's ISO date into the key so the weekday + numeric
        # date readout repaints automatically when the clock crosses
        # midnight. Cheap (one date.today() call per state_key probe).
        from datetime import date
        today_iso = date.today().isoformat()
        return (
            today_iso, s.location, s.busy, s.last_error,
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


class WeatherLocationScene(Scene):
    """Overlay: pick a city/place name and persist it as the weather
    location. Reached by tapping the location row inside WeatherScene.

    Layout (top to bottom):
      • header — BACK / HOME / title
      • entry  — the in-progress query, plus CLEAR + SEARCH on the right
      • keyboard — compact 3-row QWERTY + space/backspace
      • results — up to MAX_RESULTS tappable rows from Open-Meteo geocoding

    Search is asynchronous: tapping SEARCH enqueues a service command,
    the worker thread does the HTTP call, and the next render shows
    `search_busy` / results / errors via the WeatherStatus snapshot.
    Tapping a result row persists the lat/lon, navigates back, and the
    weather scene's snapshot refreshes within ~1 s."""

    MAX_RESULTS = 5

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, weather_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._weather = weather_service
        self._query: str = ""

    # --- mutations -----------------------------------------------------

    def _add_char(self, c: str) -> None:
        if len(self._query) >= 40:
            return
        self._query += c

    def _backspace(self) -> None:
        self._query = self._query[:-1]

    def _clear(self) -> None:
        self._query = ""

    def _do_search(self) -> None:
        q = self._query.strip()
        if not q:
            return
        self._weather.search_locations(q)

    def _on_pick(self, sug) -> None:
        self._weather.set_location(sug.lat, sug.lon, sug.label)
        # Hop back immediately — the user sees the new label populate
        # in the WeatherScene as soon as the worker thread persists.
        self._compositor.set_overlay("weather")

    def on_show(self) -> None:
        # Reset the scratchpad each time the picker opens, so a stale
        # result list from a previous visit doesn't mislead the user.
        self._query = ""
        self._weather.search_locations("")

    # --- layout --------------------------------------------------------

    def _build(self) -> None:
        self.widgets.clear()
        cw, ch = self.canvas_w, self.canvas_h

        head_h = int(ch * 0.10)
        self.add(_back_button(
            cw, head_h,
            on_press=lambda: self._compositor.set_overlay("weather"),
        ))
        self.add(_home_button(cw, head_h, self._compositor))
        self.add(TextWidget(
            Rect(int(cw * 0.20), 0, int(cw * 0.60), head_h),
            text_src=lambda: _t("scene.weather_location.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))

        # Entry row: query text on the left, CLEAR + SEARCH on the right.
        entry_y = head_h + int(ch * 0.01)
        entry_h = int(ch * 0.10)
        self.add(TextWidget(
            Rect(int(cw * 0.04), entry_y, int(cw * 0.50), entry_h),
            text_src=(self._query
                      if self._query else _t("weather.search_placeholder")),
            font_factor=0.55,
            color_role=("fg_bright" if self._query else "fg_dim"),
            font_role="regular",
        ))
        self.add(Button(
            Rect(int(cw * 0.56), entry_y + int(entry_h * 0.10),
                 int(cw * 0.18), int(entry_h * 0.80)),
            label_src=lambda: _t("button.clear"),
            on_press=self._clear,
            font_factor=0.42,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(cw * 0.76), entry_y + int(entry_h * 0.10),
                 int(cw * 0.20), int(entry_h * 0.80)),
            label_src=lambda: _t("button.search"),
            on_press=self._do_search,
            font_factor=0.50,
            color_role=("fg_bright" if self._query.strip() else "fg_dim"),
        ))

        # Compact 3-row QWERTY (no shift, no symbols — city names are
        # forgiving and Open-Meteo's matcher is fuzzy enough).
        kb_top = entry_y + entry_h + int(ch * 0.015)
        kb_h = int(ch * 0.36)
        row_h = kb_h // 3
        cell_w = cw // 10
        self._row(kb_top, row_h, cell_w, "qwertyuiop", offset=0)
        self._row(kb_top + row_h, row_h, cell_w, "asdfghjkl",
                  offset=cell_w // 2)
        # Bottom row: BKSP (1.5w) + 7 letters + SPACE (rest).
        row_y = kb_top + 2 * row_h
        bw = int(cell_w * 1.5)
        self.add(Button(
            Rect(0, row_y, bw, row_h),
            label_src=lambda: _t("button.del"),
            on_press=self._backspace,
            font_factor=0.32,
            color_role="fg_dim",
        ))
        for i, c in enumerate("zxcvbnm"):
            x = bw + i * cell_w
            self.add(Button(
                Rect(x, row_y, cell_w, row_h),
                label_src=c,
                on_press=lambda ch=c: self._add_char(ch),
                font_factor=0.55,
            ))
        sp_x = bw + 7 * cell_w
        self.add(Button(
            Rect(sp_x, row_y, cw - sp_x, row_h),
            label_src=lambda: _t("button.space"),
            on_press=lambda: self._add_char(" "),
            font_factor=0.36,
            color_role="fg_dim",
        ))

        # Results list (or status string when nothing to show).
        res_y = kb_top + kb_h + int(ch * 0.015)
        res_h = ch - res_y - int(ch * 0.02)
        s = self._weather.status
        if s.search_busy:
            self.add(TextWidget(
                Rect(0, res_y, cw, res_h),
                text_src=lambda: _t("weather.search_busy"),
                font_factor=0.05,
                color_role="fg_dim",
                font_role="regular",
            ))
            return
        if s.search_error:
            self.add(TextWidget(
                Rect(0, res_y, cw, res_h),
                text_src=lambda: _t("weather.search_error",
                                    message=s.search_error),
                font_factor=0.04,
                color_role="fg_dim",
                font_role="regular",
            ))
            return
        results = list(s.search_results)[:self.MAX_RESULTS]
        if not results:
            # Two phases of "nothing to show": a placeholder when the
            # user hasn't pressed SEARCH yet (search_query empty), and
            # an explicit "no matches" once a query came back empty.
            key = ("weather.search_no_results" if s.search_query
                   else "weather.search_hint")
            self.add(TextWidget(
                Rect(0, res_y, cw, res_h),
                text_src=lambda k=key: _t(k),
                font_factor=0.05,
                color_role="fg_dim",
                font_role="regular",
            ))
            return
        cell_h = res_h // self.MAX_RESULTS
        for i, sug in enumerate(results):
            self.add(Button(
                Rect(int(cw * 0.04), res_y + i * cell_h,
                     int(cw * 0.92), cell_h - 6),
                label_src=sug.label,
                on_press=lambda s=sug: self._on_pick(s),
                font_factor=0.36,
                color_role="fg_bright",
            ))

    def _row(self, y: int, h: int, cell_w: int, chars: str,
             *, offset: int) -> None:
        for i, c in enumerate(chars):
            x = offset + i * cell_w
            self.add(Button(
                Rect(x, y, cell_w, h),
                label_src=c,
                on_press=lambda ch=c: self._add_char(ch),
                font_factor=0.55,
            ))

    # --- Scene overrides ----------------------------------------------

    def state_key(self) -> tuple:
        s = self._weather.status
        return (self._query, s.search_busy, s.search_query,
                s.search_error,
                tuple((r.label, r.lat, r.lon) for r in s.search_results))

    def render(self) -> Image.Image:
        self._build()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._build()
        return super().hit(cx, cy)


class StationListScene(Scene):
    """Overlay: pick a station to play. Tap a row → play + close.

    Long lists page in MAX_ROWS-sized chunks via ▲/▼ buttons in the
    header right; page indicator (e.g. "2/3") sits just left of them.
    Page count is hidden when the whole list fits — keeps the header
    visually quiet for the common short-list case."""

    MAX_ROWS = 6

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, station_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._stations = station_service
        self._page = 0
        head_h = int(canvas_h * 0.14)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("launcher"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        # Title trimmed to leave the right end of the header free for
        # the page indicator + paging buttons.
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.50), head_h),
            text_src=lambda: _t("scene.station_list.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        # Paging affordances. Two square IconButtons anchored to the
        # right edge with the same gap pattern back/home use on the
        # left, plus a small page-count text just to their left.
        btn_h = int(head_h * 0.80)
        btn_w = btn_h
        right_pad = int(canvas_w * 0.025)
        gap = int(canvas_w * 0.012)
        down_x = canvas_w - right_pad - btn_w
        up_x = down_x - btn_w - gap
        self.add(TextWidget(
            Rect(int(canvas_w * 0.58), 0,
                 up_x - int(canvas_w * 0.58) - gap, head_h),
            text_src=self._page_label,
            font_factor=0.40,
            color_role="fg_dim",
        ))
        self.add(IconButton(
            Rect(up_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_up,
            icon_drawer=_icon_chevron_up,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        self.add(IconButton(
            Rect(down_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_down,
            icon_drawer=_icon_chevron_down,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        self._row_widgets: list[Widget] = []

    def _max_page(self) -> int:
        n = len(self._stations.stations)
        if n <= self.MAX_ROWS:
            return 0
        return (n - 1) // self.MAX_ROWS

    def _page_label(self) -> str:
        n = len(self._stations.stations)
        if n <= self.MAX_ROWS:
            return ""
        total = self._max_page() + 1
        page = min(self._page, total - 1)
        return f"{page + 1}/{total}"

    def _page_up(self) -> None:
        if self._page > 0:
            self._page -= 1

    def _page_down(self) -> None:
        if self._page < self._max_page():
            self._page += 1

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
        # Clamp page if the list shrunk under us.
        max_p = self._max_page()
        if self._page > max_p:
            self._page = max_p
        body_top = self._head_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        if not sts:
            empty = TextWidget(
                Rect(0, body_top, self.canvas_w, body_h),
                text_src=lambda: _t("station.empty_list"),
                font_factor=0.06,
                color_role="fg_dim",
                font_role="regular",
            )
            self.widgets.append(empty)
            self._row_widgets.append(empty)
            return
        start = self._page * self.MAX_ROWS
        page_sts = sts[start:start + self.MAX_ROWS]
        cell_h = body_h // self.MAX_ROWS
        cur = self._stations.current_id
        for i, s in enumerate(page_sts):
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
            self._page,
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
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.44), head_h),
            text_src=lambda: _t("scene.alarm_list.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(canvas_w * 0.74), int(head_h * 0.10),
                 int(canvas_w * 0.24), int(head_h * 0.80)),
            label_src=lambda: _t("button.add"),
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
        on = _t("alarm.on_prefix") if a.enabled else _t("alarm.off_prefix")
        skip = (f"  ({_t('alarm.skip_marker')})"
                if a.skip_next else "")
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
                text_src=lambda: _t("alarm.no_alarms_hint"),
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
                label_src=(lambda a=row_a: self._row_label(a)),
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

    # Source-of-truth day-letter keys; the picker label resolves through
    # i18n at render time so a language switch updates without rebuild.
    DAY_LETTER_KEYS = (
        "day.letter.mon", "day.letter.tue", "day.letter.wed",
        "day.letter.thu", "day.letter.fri", "day.letter.sat",
        "day.letter.sun",
    )

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
        self.add(_home_button(cw, head_h, self._compositor))
        self.add(TextWidget(
            Rect(int(cw * 0.20), 0, int(cw * 0.34), head_h),
            text_src=(_t("scene.alarm_edit.title.new") if self._is_new
                      else _t("scene.alarm_edit.title.edit")),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(Button(
            Rect(int(cw * 0.68), int(head_h * 0.10),
                 int(cw * 0.30), int(head_h * 0.80)),
            label_src=(_t("alarm.enabled") if d.enabled
                       else _t("alarm.disabled")),
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
        for i, key in enumerate(self.DAY_LETTER_KEYS):
            on = bool(d.days & (1 << i))
            self.add(Button(
                Rect(i * cell_w, days_y, cell_w, days_h),
                label_src=_t(key),
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
                label_src=_t("button.cancel"), on_press=self._cancel,
                font_factor=0.40,
            ))
            self.add(Button(
                Rect(half, act_y, cw - half, act_h),
                label_src=_t("button.save"), on_press=self._save,
                font_factor=0.40,
                color_role="fg_bright",
            ))
        else:
            third = cw // 3
            self.add(Button(
                Rect(0, act_y, third, act_h),
                label_src=_t("button.cancel"), on_press=self._cancel,
                font_factor=0.40,
            ))
            self.add(Button(
                Rect(third, act_y, third, act_h),
                label_src=_t("button.delete"), on_press=self._delete,
                font_factor=0.40,
                color_role="fg_dim",
            ))
            self.add(Button(
                Rect(2 * third, act_y, cw - 2 * third, act_h),
                label_src=_t("button.save"), on_press=self._save,
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
    """Alarm-firing: world-map background + halo'd clock + bell-and-time
    label + a single big STOP button at the bottom. No snooze — the
    user explicitly wanted the firing screen to be unambiguous: tap to
    stop, end. The hard cap in AlarmService still auto-stops after 30
    min so an unattended panel doesn't play all day."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 alarm_service):
        super().__init__(theme, canvas_w, canvas_h)

        # Bottom band carries the STOP button; clock + bell label
        # share the upper area, vertically centred.
        btn_h = int(canvas_h * 0.22)
        body_h = canvas_h - btn_h

        # Big clock in the upper portion, halo'd so it pops cleanly
        # over the map.
        clock_h = int(canvas_h * 0.46)
        self.add(ClockWidget(
            Rect(0, int(body_h * 0.10), canvas_w, clock_h),
            font_factor=0.55,
        ))

        def alarm_label() -> str:
            a = alarm_service.firing_alarm
            if a is None:
                return ""
            return f"{a.hour:02d}:{a.minute:02d}"

        # Bell glyph + label sit on the same row, just below the clock.
        # Bell is drawn from PIL primitives so emoji-less fonts work.
        label_y = int(body_h * 0.10) + clock_h + int(body_h * 0.02)
        label_h = body_h - (label_y - 0) - int(body_h * 0.04)
        bell_size = int(label_h * 0.65)
        # Centre bell + text together: pre-measure text, lay them out
        # as a left-anchored unit roughly centred on canvas.
        bell_x = int(canvas_w * 0.36)
        self.add(BellIconWidget(
            Rect(bell_x, label_y + (label_h - bell_size) // 2,
                 bell_size, bell_size),
            color_role="fg_accent",
        ))
        self.add(TextWidget(
            Rect(bell_x + bell_size, label_y,
                 canvas_w - bell_x - bell_size, label_h),
            text_src=alarm_label,
            font_factor=0.70,
            color_role="fg_accent",
            halo=True,
        ))

        # Single STOP button — full footer width, large but not absurd
        # font (font_factor on min(w, h) of a 1280×158 button — height
        # is the limiter, so 0.40 gives ~63px text).
        btn_y = canvas_h - btn_h
        self.add(Button(
            Rect(0, btn_y, canvas_w, btn_h),
            label_src=lambda: _t("button.stop"),
            on_press=lambda: alarm_service.stop_firing(),
            font_factor=0.40,
            color_role="fg_bright",
            halo=True,
        ))

        # "Updating bg" indicator — paints last so it sits on top.
        self.add(_rendering_indicator(self, canvas_w, canvas_h))


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
                 station_service, mpd_service, i18n_service):
        super().__init__(theme, canvas_w, canvas_h)
        head_h = int(canvas_h * 0.14)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.about.title"),
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
            kernel = _t("misc.dash")

        rows: list = [
            (lambda h=host: _t("about.row.host", hostname=h)),
            (lambda k=kernel: _t("about.row.kernel", release=k)),
            (lambda: _t("about.row.ip", ip=_local_ip())),
            (lambda: _t("about.row.theme",
                        name=theme_service.current.name)),
            (lambda: _t("about.row.language",
                        name=i18n_service.native_name())),
            (lambda: _t("about.row.alarms",
                        count=len(alarm_service.alarms))),
            (lambda: _t("about.row.stations",
                        count=len(station_service.stations))),
            (lambda: _t("about.row.mpd",
                        state=mpd_service.status.state)),
        ]
        # Reserve the bottom slice of the body for a "RUN GUIDED TOUR"
        # button — info rows live above it.
        body_top = head_h + int(canvas_h * 0.04)
        full_body_h = canvas_h - body_top - int(canvas_h * 0.04)
        tour_btn_h = int(canvas_h * 0.10)
        tour_gap = int(canvas_h * 0.03)
        body_h = full_body_h - tour_btn_h - tour_gap
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
        # Tour launcher — a second entry point for the demo, placed
        # here because About is where someone evaluating "what is this
        # thing?" naturally lands.
        tour_w = int(canvas_w * 0.66)
        tour_x = (canvas_w - tour_w) // 2
        tour_y = body_top + full_body_h - tour_btn_h
        self.add(Button(
            Rect(tour_x, tour_y, tour_w, tour_btn_h),
            label_src=lambda: _t("button.run_tour"),
            on_press=lambda: compositor.set_overlay("demo_intro"),
            font_factor=0.40,
            color_role="fg_accent",
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
            on_press=lambda: compositor.set_overlay("display_settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.brightness.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))

        body_top = head_h + int(canvas_h * 0.04)
        body_h = canvas_h - body_top - int(canvas_h * 0.04)
        # Two settings: each gets a label + −/value/+ control row, with
        # vertical breathing room above and below. Three vertical bands.
        band_h = body_h // 3
        self._build_setting(
            label_key="brightness.active",
            band_y=body_top,
            band_h=band_h,
            value_fn=lambda: f"{self._svc.config.active_pct}%",
            minus=lambda: self._svc.step_active(-1),
            plus=lambda: self._svc.step_active(+1),
        )
        self._build_setting(
            label_key="brightness.idle_dim",
            band_y=body_top + band_h + int(body_h * 0.04),
            band_h=band_h,
            value_fn=lambda: f"{self._svc.config.dim_pct}%",
            minus=lambda: self._svc.step_dim(-1),
            plus=lambda: self._svc.step_dim(+1),
        )
        # Bedside "night red" toggle. Tints the rendered image toward
        # deep red so the panel emits as little blue/green as possible
        # — preserves dark adaptation and minimises melatonin
        # disruption when the panel is glanced at in the dark.
        nr_y = body_top + 2 * band_h + int(body_h * 0.08)
        nr_h = band_h - int(body_h * 0.04)
        self.add(CheckboxRow(
            Rect(int(canvas_w * 0.10), nr_y,
                 int(canvas_w * 0.80), nr_h),
            label_src=lambda: _t("brightness.night_red"),
            is_on_src=lambda: self._svc.config.night_red,
            on_press=lambda: self._svc.toggle_night_red(),
            font_factor=0.40,
        ))

    def _build_setting(self, *, label_key: str, band_y: int, band_h: int,
                       value_fn, minus, plus) -> None:
        cw = self.canvas_w
        # Label takes the top half, control row the bottom half.
        lbl_h = int(band_h * 0.40)
        ctl_h = band_h - lbl_h
        self.add(TextWidget(
            Rect(int(cw * 0.06), band_y, int(cw * 0.88), lbl_h),
            text_src=(lambda k=label_key: _t(k)),
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
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.audio_output.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.06), head_h + int(canvas_h * 0.06),
                 int(canvas_w * 0.88), int(canvas_h * 0.12)),
            text_src=lambda: ("" if self._mpd.status.outputs
                              else _t("audio.no_outputs")),
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
                device = kv.get("device", _t("misc.dash"))
                d_x = margin_x + int(cw * 0.04)
                d_w = inner_w - int(cw * 0.04)
                line_h = detail_h // 4
                self.widgets.append(TextWidget(
                    Rect(d_x, y, d_w, line_h),
                    text_src=_t("audio.row.plugin",
                                plugin=(o.plugin or _t("misc.dash"))),
                    font_role="regular",
                    font_factor=0.55,
                    color_role="fg_subtle",
                ))
                self._rows.append(self.widgets[-1])
                self.widgets.append(TextWidget(
                    Rect(d_x, y + line_h, d_w, line_h),
                    text_src=_t("audio.row.device", device=device),
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
                        text_src=_t("audio.active"),
                        font_factor=0.55,
                        color_role="fg_accent",
                    ))
                    self._rows.append(self.widgets[-1])
                else:
                    use_btn = Button(
                        Rect(d_x, btn_y, d_w, btn_h),
                        label_src=_t("button.use_output"),
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

    # (mode, i18n-key) — label resolved at render via _t().
    STYLES = (
        ("none", "background.style.none"),
        ("world_map_slate", "background.style.slate"),
        ("world_map_atlas", "background.style.atlas"),
        ("world_map_vintage", "background.style.vintage"),
        ("world_map_blueprint", "background.style.blueprint"),
        ("world_map_globe", "background.style.globe"),
        ("world_map_starmap", "background.style.starmap"),
    )
    OVERLAYS = (
        ("city_lights", "background.overlay.city_lights"),
        ("water", "background.overlay.water"),
        ("political", "background.overlay.political"),
        ("annotations", "background.overlay.annotations"),
    )

    # MAX_ROWS keeps each cell big enough that font_factor=0.55 reads
    # comfortably at bedside distance. Total items (6 styles + 4
    # overlays + 1 centre = 11) splits into 2 pages (6 + 5).
    MAX_ROWS = 6

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, background_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._bg = background_service
        self._page = 0
        head_h = int(canvas_h * 0.10)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("display_settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        # Title trimmed so the page indicator + chevron buttons fit on
        # the right side of the header (matches StationListScene).
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.50), head_h),
            text_src=lambda: _t("scene.background.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        btn_h = int(head_h * 0.80)
        btn_w = btn_h
        right_pad = int(canvas_w * 0.025)
        gap = int(canvas_w * 0.012)
        down_x = canvas_w - right_pad - btn_w
        up_x = down_x - btn_w - gap
        self.add(TextWidget(
            Rect(int(canvas_w * 0.58), 0,
                 up_x - int(canvas_w * 0.58) - gap, head_h),
            text_src=self._page_label,
            font_factor=0.40,
            color_role="fg_dim",
        ))
        self.add(IconButton(
            Rect(up_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_up,
            icon_drawer=_icon_chevron_up,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        self.add(IconButton(
            Rect(down_x, int(head_h * 0.10), btn_w, btn_h),
            on_press=self._page_down,
            icon_drawer=_icon_chevron_down,
            color_role="fg_accent",
            outline_width=2,
            icon_factor=0.65,
        ))
        self._rows: list = []

    def _items(self) -> list[tuple]:
        """Flat ordered list of selectable rows. Style radios first
        (page 1), then overlays + Centre (page 2). Tagged tuples so
        _rebuild_rows can dispatch to the right widget per kind. The
        third tuple slot carries the i18n key — resolved to a label
        at render time so language switches are live."""
        items: list[tuple] = []
        for mode, key in self.STYLES:
            items.append(("style", mode, key))
        for name, key in self.OVERLAYS:
            items.append(("overlay", name, key))
        items.append(("centre",))
        return items

    def _max_page(self) -> int:
        n = len(self._items())
        if n <= self.MAX_ROWS:
            return 0
        return (n - 1) // self.MAX_ROWS

    def _page_label(self) -> str:
        n = len(self._items())
        if n <= self.MAX_ROWS:
            return ""
        total = self._max_page() + 1
        page = min(self._page, total - 1)
        return f"{page + 1}/{total}"

    def _page_up(self) -> None:
        if self._page > 0:
            self._page -= 1

    def _page_down(self) -> None:
        if self._page < self._max_page():
            self._page += 1

    def _set_mode(self, mode: str) -> None:
        self._bg.set_mode(mode)

    def _toggle_overlay(self, name: str) -> None:
        self._bg.toggle_overlay(name)

    def _rebuild_rows(self) -> None:
        for w in self._rows:
            try:
                self.widgets.remove(w)
            except ValueError:
                pass
        self._rows.clear()
        # Pagination layout: each page shows up to MAX_ROWS items at a
        # readable cell height. Section labels are dropped — the row
        # widget shape (radio dot vs checkbox vs button outline) plus
        # the natural page break between styles and overlays carries
        # the grouping clearly enough.
        items = self._items()
        max_p = self._max_page()
        if self._page > max_p:
            self._page = max_p
        body_top = self._head_h + int(self.canvas_h * 0.02)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.02)
        cell_h = body_h // self.MAX_ROWS
        margin_x = int(self.canvas_w * 0.05)
        content_w = self.canvas_w - 2 * margin_x
        start = self._page * self.MAX_ROWS
        page_items = items[start:start + self.MAX_ROWS]
        for i, item in enumerate(page_items):
            row_y = body_top + i * cell_h
            row_h = cell_h - 8
            kind = item[0]
            if kind == "style":
                _, mode, key = item
                row = CheckboxRow(
                    Rect(margin_x, row_y, content_w, row_h),
                    label_src=(lambda k=key: _t(k)),
                    on_press=(lambda m=mode: self._set_mode(m)),
                    is_on_src=(lambda m=mode: self._bg.mode == m),
                    shape="radio",
                    font_factor=0.55,
                )
            elif kind == "overlay":
                _, name, key = item
                row = CheckboxRow(
                    Rect(margin_x, row_y, content_w, row_h),
                    label_src=(lambda k=key: _t(k)),
                    on_press=(lambda n=name: self._toggle_overlay(n)),
                    is_on_src=(lambda n=name: self._bg.is_overlay(n)),
                    shape="check",
                    font_factor=0.55,
                    disabled_src=(lambda: self._bg.mode == "none"),
                )
            else:                                    # "centre"
                row = Button(
                    Rect(margin_x, row_y, content_w, row_h),
                    label_src=lambda: _t(
                        "background.center_button",
                        location=_format_centre_lon(self._bg.center_lon),
                    ),
                    on_press=lambda: self._compositor.set_overlay(
                        "map_center"),
                    font_factor=0.50,
                    color_role=("fg_dim" if self._bg.mode == "none"
                                else "fg_bright"),
                    outline_width=1,
                )
            self.widgets.append(row)
            self._rows.append(row)

    def state_key(self) -> tuple:
        return (self._bg.mode, self._bg.active_overlays(),
                int(round(self._bg.center_lon)), self._page)

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
# (longitude, i18n-key) — labels resolved at render time.
MAP_CENTERS = (
    (0.0, "map_center.greenwich"),
    (35.2, "map_center.jerusalem"),
    (39.8, "map_center.mecca"),
    (-74.0, "map_center.new_york"),
    (-87.6, "map_center.chicago"),
    (-122.3, "map_center.seattle"),
    (-157.9, "map_center.honolulu"),
    (139.7, "map_center.tokyo"),
    (116.4, "map_center.beijing"),
    (151.2, "map_center.sydney"),
    (18.4, "map_center.cape_town"),
    (-58.4, "map_center.buenos_aires"),
)


def _format_centre_lon(lon: float) -> str:
    """Closest predefined centre name, falling back to decimal degrees."""
    best_key = None
    best_dist = 360.0
    for clon, key in MAP_CENTERS:
        d = abs(((lon - clon + 180) % 360) - 180)
        if d < best_dist:
            best_dist = d
            best_key = key
    if best_key is not None and best_dist < 0.6:
        return _t(best_key)
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
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.map_center.title"),
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
        for clon, key in MAP_CENTERS:
            row = CheckboxRow(
                Rect(margin_x, y, content_w, row_h),
                label_src=(lambda k=key: _t(k)),
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


class DemoIntroScene(Scene):
    """Pre-tour configuration screen. Two questions are asked up front
    (length + whether to include the wifi setup walkthrough); START
    hands the chosen options to DemoService and the rest of the tour
    runs unattended."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, demo_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._demo = demo_service
        self._full = True
        self._include_wifi = True
        head_h = int(canvas_h * 0.14)
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.demo_intro.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))

        body_top = head_h + int(canvas_h * 0.03)
        action_band_h = int(canvas_h * 0.16)
        action_band_y = canvas_h - action_band_h - int(canvas_h * 0.03)
        body_h = action_band_y - body_top
        desc_h = int(body_h * 0.26)
        self.add(WrappedTextWidget(
            Rect(int(canvas_w * 0.06), body_top,
                 int(canvas_w * 0.88), desc_h),
            text_src=lambda: _t("demo_intro.description"),
            font_size=max(18, int(canvas_h * 0.026)),
            color_role="fg_dim",
        ))

        opts_top = body_top + desc_h + int(body_h * 0.04)
        opts_avail_h = body_h - desc_h - int(body_h * 0.04)
        row_h = int(opts_avail_h * 0.28)
        gap = int(opts_avail_h * 0.04)
        margin_x = int(canvas_w * 0.10)
        row_w = canvas_w - 2 * margin_x
        y = opts_top
        self.add(CheckboxRow(
            Rect(margin_x, y, row_w, row_h),
            label_src=lambda: _t("demo_intro.option.full"),
            is_on_src=lambda: self._full,
            on_press=lambda: self._set_full(True),
            shape="radio",
            font_factor=0.42,
        ))
        y += row_h + gap
        self.add(CheckboxRow(
            Rect(margin_x, y, row_w, row_h),
            label_src=lambda: _t("demo_intro.option.short"),
            is_on_src=lambda: not self._full,
            on_press=lambda: self._set_full(False),
            shape="radio",
            font_factor=0.42,
        ))
        y += row_h + gap
        self.add(CheckboxRow(
            Rect(margin_x, y, row_w, row_h),
            label_src=lambda: _t("demo_intro.option.wifi"),
            is_on_src=lambda: self._include_wifi,
            on_press=self._toggle_wifi,
            shape="check",
            font_factor=0.42,
        ))

        btn_pad = int(canvas_w * 0.04)
        btn_w = (canvas_w - 3 * btn_pad) // 2
        self.add(Button(
            Rect(btn_pad, action_band_y, btn_w, action_band_h),
            label_src=lambda: _t("button.cancel"),
            on_press=lambda: compositor.set_overlay("settings"),
            font_factor=0.42,
        ))
        self.add(Button(
            Rect(canvas_w - btn_pad - btn_w, action_band_y,
                 btn_w, action_band_h),
            label_src=lambda: _t("button.start"),
            on_press=self._start,
            font_factor=0.42,
            color_role="fg_bright",
        ))

    def _set_full(self, v: bool) -> None:
        self._full = v

    def _toggle_wifi(self) -> None:
        self._include_wifi = not self._include_wifi

    def _start(self) -> None:
        self._demo.start(
            length="full" if self._full else "short",
            include_wifi=self._include_wifi,
        )

    def state_key(self) -> tuple:
        return super().state_key() + (self._full, self._include_wifi)


class LanguageScene(Scene):
    """Overlay: pick UI language. Same pattern as ThemeScene — tap a
    row to apply + persist immediately, with the selected row drawn
    bright and the rest dim. Languages list themselves in their native
    name (English / Espanol / Norsk) so they're recognisable even
    when the user has accidentally selected one they can't read."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 compositor, i18n_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._compositor = compositor
        self._i18n = i18n_service
        head_h = int(canvas_h * 0.14)
        self._head_h = head_h
        self.add(_back_button(
            canvas_w, head_h,
            on_press=lambda: compositor.set_overlay("settings"),
        ))
        self.add(_home_button(canvas_w, head_h, compositor))
        self.add(TextWidget(
            Rect(int(canvas_w * 0.20), 0,
                 int(canvas_w * 0.66), head_h),
            text_src=lambda: _t("scene.language.title"),
            font_factor=0.55,
            color_role="fg_dim",
        ))
        self._rows: list[Button] = []

    def _apply(self, code: str) -> None:
        self._i18n.set(code)

    def _rebuild_rows(self) -> None:
        for b in self._rows:
            try:
                self.widgets.remove(b)
            except ValueError:
                pass
        self._rows.clear()
        langs = self._i18n.languages
        cur = self._i18n.lang
        body_top = self._head_h + int(self.canvas_h * 0.04)
        body_h = self.canvas_h - body_top - int(self.canvas_h * 0.04)
        # Reserve cell height for up to 5 rows so layout is stable
        # even if we add more languages later.
        slots = max(len(langs), 5)
        cell_h = body_h // slots
        margin_x = int(self.canvas_w * 0.06)
        btn_w = self.canvas_w - 2 * margin_x
        for i, (code, name) in enumerate(langs):
            selected = code == cur
            mark = "▶ " if selected else "  "
            label = f"{mark}{name}"
            color_role = "fg_bright" if selected else "fg_dim"
            row_y = body_top + i * cell_h
            row_h = cell_h - 8
            btn = Button(
                Rect(margin_x, row_y, btn_w, row_h),
                label_src=label,
                on_press=lambda c=code: self._apply(c),
                font_factor=0.55,
                color_role=color_role,
            )
            self.widgets.append(btn)
            self._rows.append(btn)

    def state_key(self) -> tuple:
        return (self._i18n.lang,
                tuple(c for c, _ in self._i18n.languages))

    def render(self) -> Image.Image:
        self._rebuild_rows()
        return super().render()

    def hit(self, cx: float, cy: float) -> Button | None:
        self._rebuild_rows()
        return super().hit(cx, cy)


class DemoSplashScene(Scene):
    """Full-bleed centred large-text splash, used by demo splash steps
    (intro greetings + closing slide). One scene serves all splash
    captions — the text is sourced from DemoService each render so a
    step transition just changes which message appears.

    iMac-unboxing aesthetic: solid theme-bg behind big bold centred
    text, no header, no footer, no chrome. Brightness override in the
    main loop pins the panel to 60% during the demo so the splash
    really lands rather than barely glowing at the user's bedside
    setting."""

    def __init__(self, theme: Theme, canvas_w: int, canvas_h: int, *,
                 demo_service):
        super().__init__(theme, canvas_w, canvas_h)
        self._demo = demo_service
        # Vertical band ~80% of canvas, padded sides. WrappedTextWidget
        # auto-wraps long captions onto multiple lines and centres
        # them — short single-word splashes ("Hello.") sit on one line
        # at the chosen font size; longer ones break naturally.
        self.add(WrappedTextWidget(
            Rect(int(canvas_w * 0.06), int(canvas_h * 0.10),
                 int(canvas_w * 0.88), int(canvas_h * 0.80)),
            text_src=lambda: self._demo.caption,
            font_role="bold",
            font_size=max(56, int(canvas_h * 0.075)),
            color_role="fg_bright",
            line_spacing=1.18,
        ))

    def state_key(self) -> tuple:
        return (self._demo.caption,)
