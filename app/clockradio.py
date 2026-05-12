#!/usr/bin/env python3
"""Clock radio — entry point and compositor.

Architecture
------------
    hardware:    Display, Backlight, TouchReader  (this file)
    services:    MPDService                       (mpd_service.py)
                 future: WeatherService, VerseService, CameraService...
    ui:          Theme + Widgets + Scenes         (theme.py / widgets.py /
                                                   scenes.py)
    compositor:  Compositor                       (this file)

Why direct fb0 (not pygame/SDL): pygame on SDL2/KMSDRM consistently breaks
the panel on this hardware (Pi 3 + new official 7" touchscreen B). With
DRM master grabbed, the panel goes dark; without master, SDL renders to
nothing visible. We bypass SDL and write raw pixel bytes directly to
/dev/fb0, reading touch via raw evdev.

Adding a feature
----------------
    1. (optional) Background data: subclass the MPDService pattern in a
       new <thing>_service.py.
    2. Rendering: subclass Widget in widgets.py with render() and
       state_key().
    3. Wiring: add the widget to a Scene in scenes.py, or build a new
       Scene and register it in main()'s scenes dict + pick_scene().
"""
from __future__ import annotations

import errno
import fcntl
import glob
import os
import signal
import struct
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from alarm import AlarmStore
from alarm_service import AlarmService
from background_service import BackgroundService
from bluetooth_service import BluetoothService
from brightness_service import BrightnessService
from i18n_service import I18nService
from light_service import LightService
from mpd_service import MPDService
from station_service import StationService
from stations import StationStore
from theme import THEMES
from theme_service import ThemeProxy, ThemeService
from verse_service import VerseService
from weather_service import WeatherService
from wifi_service import WifiService
from world_map_service import WorldMapService
from demo_service import CaptionOverlay, DemoService
import scenes as _scenes_mod
from scenes import (
    AboutScene, AlarmEditScene, AlarmFiringScene, AlarmListScene,
    AudioOutputScene, BackgroundScene, BluetoothPlayingScene,
    BluetoothScene, BluetoothSpeakerScene, BrightnessScene,
    DemoIntroScene, DemoSplashScene, DisplaySettingsScene,
    IdleScene, LanguageScene, LauncherScene, MapCenterScene,
    QuickPanelScene, RadioScene, SettingsScene, StationListScene,
    ThemeScene, VerseScene, WeatherLocationScene, WeatherScene,
    WifiPasswordScene, WifiScene,
)


# --- tty graphics-mode ioctl -----------------------------------------
KDSETMODE = 0x4B3A
KD_TEXT = 0
KD_GRAPHICS = 1

# --- evdev event layout (64-bit Linux) -------------------------------
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_KEY = 0x01
EV_ABS = 0x03
BTN_TOUCH = 0x14a
ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

FB_PATH = Path("/dev/fb0")
FB_VSIZE = Path("/sys/class/graphics/fb0/virtual_size")
FB_BPP = Path("/sys/class/graphics/fb0/bits_per_pixel")
FB_STRIDE = Path("/sys/class/graphics/fb0/stride")
TTY_PATH = Path("/dev/tty1")
TOUCH_NAME = "Goodix Capacitive TouchScreen"
BACKLIGHT_GLOB = "/sys/class/backlight/*/brightness"

# --- behaviour --------------------------------------------------------
IDLE_TIMEOUT_S = 30.0
# Auto-return from any settings/launcher overlay back to the home
# scene after this much idleness. Lower than IDLE_TIMEOUT_S so the
# user lands back on the clock *before* the panel starts dimming —
# leaving a half-finished settings overlay visible during the dim
# transition would look broken.
SETTINGS_TIMEOUT_S = 20.0
FRAME_RATE = 5
# Brightness pinning while the guided tour runs. The user's everyday
# setting is bedside-low (often 5%); a tour at that level is invisible
# from across the room and would also drift into idle dim halfway
# through. 60% lands far enough above the panel's hardware floor to
# look like a deliberate showcase mode and well below 100% so a
# bedside user isn't blasted if they trigger the demo at night.
DEMO_BRIGHTNESS_PCT = 60
# Active↔idle dim is a gentle background transition — long fade.
# User-driven brightness steps in BrightnessScene want immediate
# feedback, so a much shorter fade (~one frame at 5fps) effectively
# snaps to the new level without the chunky multi-frame animation
# that made stepping feel jerky.
TRANSITION_FADE_S = 1.0
STEP_FADE_S = 0.2
ROTATION_CCW = 90  # CCW canvas → fb. Inverse used for touch hit-test.

# Gesture thresholds (panel pixels, except SWIPE_MAX_S in seconds).
# Tap: release within TAP_MAX_PX of press. Swipe: release at least
# SWIPE_MIN_PX away within SWIPE_MAX_S — anything in between counts
# as a slow drag and is discarded so the user doesn't accidentally
# trigger something while resting a finger.
TAP_MAX_PX = 30
SWIPE_MIN_PX = 80
SWIPE_MAX_S = 0.8

ALARMS_PATH = Path("/var/lib/clockradio/alarms.json")
STATIONS_PATH = Path("/var/lib/clockradio/stations.json")
LOCATION_PATH = Path("/var/lib/clockradio/location.json")
VERSE_PATH = Path("/var/lib/clockradio/verse.json")
THEME_PATH = Path("/var/lib/clockradio/theme.json")
BRIGHTNESS_PATH = Path("/var/lib/clockradio/brightness.json")
# Rolling 24 h CSV of light-sensor samples — purely for off-line
# calibration analysis. LightService trims this hourly back to the
# retention window so it doesn't grow forever.
LIGHT_LOG_PATH = Path("/var/lib/clockradio/light-log.csv")
BACKGROUND_PATH = Path("/var/lib/clockradio/background.json")
LANGUAGE_PATH = Path("/var/lib/clockradio/language.json")
# Default alarm sound. Slice 3c will add chime fallback for offline.
ALARM_URL = "https://nwm.streamguys1.com/faith/playlist.m3u8"


# =====================================================================
# Display: framebuffer, KDSETMODE, rotation, RGB565 packing, present.
# =====================================================================
class Display:
    def __init__(self, rotation_ccw: int = 0):
        self.fb_w, self.fb_h, self.fb_bpp, self.fb_stride = self._fb_info()
        self.rotation_ccw = rotation_ccw
        if rotation_ccw in (90, 270):
            self.canvas_w, self.canvas_h = self.fb_h, self.fb_w
        else:
            self.canvas_w, self.canvas_h = self.fb_w, self.fb_h

        self._tty_fd = os.open(str(TTY_PATH), os.O_RDWR)
        try:
            fcntl.ioctl(self._tty_fd, KDSETMODE, KD_GRAPHICS)
        except OSError as exc:
            print(f"KDSETMODE GRAPHICS failed: {exc}", file=sys.stderr)
        self._fb_fd = os.open(str(FB_PATH), os.O_RDWR)

    @staticmethod
    def _fb_info() -> tuple[int, int, int, int]:
        w, h = (int(x) for x in FB_VSIZE.read_text().strip().split(","))
        bpp = int(FB_BPP.read_text().strip())
        try:
            stride = int(FB_STRIDE.read_text().strip())
        except OSError:
            stride = w * (bpp // 8)
        return w, h, bpp, stride

    def present(self, img: Image.Image,
                rgb_scale: tuple[float, float, float]
                = (1.0, 1.0, 1.0)) -> None:
        # `rgb_scale` is per-channel multipliers in 0..1 applied just
        # before the RGB565 pack. Used for two things:
        #   - software dim below the panel's hardware backlight floor
        #     (all three channels get the same factor),
        #   - night-red mode (red preserved, green/blue strongly
        #     suppressed) for bedside use.
        # Most frames pass (1, 1, 1) and the multiply path is skipped.
        if self.rotation_ccw:
            img = img.rotate(self.rotation_ccw, expand=True)
        os.lseek(self._fb_fd, 0, os.SEEK_SET)
        os.write(self._fb_fd, self._pack(img, rgb_scale))

    def _pack(self, img: Image.Image,
              rgb_scale: tuple[float, float, float]
              = (1.0, 1.0, 1.0)) -> bytes:
        expected_stride = self.fb_w * (self.fb_bpp // 8)
        rs, gs, bs = (max(0.0, min(1.0, float(v))) for v in rgb_scale)
        # Skip-fast path: every channel at 1.0 means the user is in
        # active mode with no night-red, so frame-cost stays exactly
        # what it was before the dim/tint pipeline existed.
        tint_active = rs < 0.999 or gs < 0.999 or bs < 0.999
        if self.fb_bpp == 32:
            if tint_active:
                # 32-bit path isn't used on the Pi 3 panel (bpp=16) but
                # keep parity for portability — per-channel multiply
                # via numpy, then back to a PIL image.
                arr = np.asarray(img.convert("RGBA"), dtype=np.uint16)
                arr[..., 0] = (arr[..., 0] * rs).astype(np.uint16)
                arr[..., 1] = (arr[..., 1] * gs).astype(np.uint16)
                arr[..., 2] = (arr[..., 2] * bs).astype(np.uint16)
                img = Image.fromarray(arr.astype(np.uint8), "RGBA")
            raw = img.convert("RGBA").tobytes("raw", "BGRA")
        elif self.fb_bpp == 16:
            # RGB888 -> RGB565 via numpy (vectorised; per-pixel python is ~5s/frame)
            arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
            if tint_active:
                arr[..., 0] = (arr[..., 0] * rs).astype(np.uint16)
                arr[..., 1] = (arr[..., 1] * gs).astype(np.uint16)
                arr[..., 2] = (arr[..., 2] * bs).astype(np.uint16)
            rgb565 = (
                ((arr[..., 0] & 0xF8) << 8)
                | ((arr[..., 1] & 0xFC) << 3)
                | (arr[..., 2] >> 3)
            ).astype("<u2", copy=False)
            raw = rgb565.tobytes()
        else:
            raise RuntimeError(f"unsupported fb bpp: {self.fb_bpp}")
        if self.fb_stride == expected_stride:
            return raw
        padding = b"\x00" * (self.fb_stride - expected_stride)
        h = len(raw) // expected_stride
        return b"".join(
            raw[i * expected_stride:(i + 1) * expected_stride] + padding
            for i in range(h))

    def panel_to_canvas(self, px: int, py: int) -> tuple[int, int]:
        """Inverse of the canvas → fb rotation, for hit-testing taps."""
        if self.rotation_ccw == 90:
            return self.canvas_w - 1 - py, px
        if self.rotation_ccw == 270:
            return py, self.canvas_h - 1 - px
        if self.rotation_ccw == 180:
            return self.canvas_w - 1 - px, self.canvas_h - 1 - py
        return px, py

    def close(self) -> None:
        try:
            fcntl.ioctl(self._tty_fd, KDSETMODE, KD_TEXT)
        except Exception:
            pass
        for fd in (self._fb_fd, self._tty_fd):
            try:
                os.close(fd)
            except Exception:
                pass


# =====================================================================
# Backlight: glob the sysfs path (it moves between kernels), write level.
# =====================================================================
class Backlight:
    def __init__(self):
        candidates = sorted(glob.glob(BACKLIGHT_GLOB))
        if not candidates:
            self.path: Path | None = None
            self.maximum = 31
            return
        self.path = Path(candidates[0])
        try:
            self.maximum = int(
                (self.path.parent / "max_brightness").read_text().strip())
        except OSError:
            self.maximum = 31

    @property
    def dim_level(self) -> int:
        return max(1, self.maximum // 30)

    def write(self, v: int) -> None:
        if self.path is None:
            return
        v = max(0, min(self.maximum, int(v)))
        try:
            self.path.write_text(str(v))
        except OSError as exc:
            print(f"backlight write {v} failed: {exc}", file=sys.stderr)


# =====================================================================
# TouchReader: drain evdev, classify wake events vs taps with canvas
# coords. Caller decides what to do with each kind.
# =====================================================================
class TouchEvent:
    __slots__ = ("kind", "cx", "cy", "direction")

    def __init__(self, kind: str, cx: int = 0, cy: int = 0,
                 direction: str = ""):
        self.kind = kind  # "wake" / "tap" / "swipe"
        self.cx = cx
        self.cy = cy
        # Direction is set on swipe events: "up"/"down"/"left"/"right"
        # in canvas space (matches what the user sees, not the panel).
        self.direction = direction


class TouchReader:
    def __init__(self, display: Display):
        self.display = display
        self.path = self._find()
        self._fd = -1
        if self.path is not None:
            try:
                self._fd = os.open(str(self.path),
                                   os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                print(f"open {self.path} failed: {exc}", file=sys.stderr)
        self._last_x: int | None = None
        self._last_y: int | None = None
        self._press_x: int | None = None
        self._press_y: int | None = None
        self._press_t: float = 0.0

    @staticmethod
    def _find() -> Path | None:
        try:
            text = Path("/proc/bus/input/devices").read_text()
        except OSError:
            return None
        in_block = False
        for line in text.splitlines():
            if line.startswith("N: "):
                in_block = TOUCH_NAME in line
            elif in_block and line.startswith("H: "):
                for tok in line[3:].split():
                    if tok.startswith("event"):
                        return Path("/dev/input") / tok
                in_block = False
        return None

    def poll(self) -> list[TouchEvent]:
        events: list[TouchEvent] = []
        if self._fd < 0:
            return events
        while True:
            try:
                data = os.read(self._fd, EVENT_SIZE * 32)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                raise
            if not data:
                break
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _s, _us, etype, ecode, evalue = struct.unpack_from(
                    EVENT_FORMAT, data, off)
                if etype == EV_ABS:
                    if ecode in (ABS_X, ABS_MT_POSITION_X):
                        self._last_x = evalue
                    elif ecode in (ABS_Y, ABS_MT_POSITION_Y):
                        self._last_y = evalue
                    elif ecode == ABS_MT_TRACKING_ID and evalue >= 0:
                        # Finger went down — wake the screen now.
                        events.append(TouchEvent("wake"))
                elif etype == EV_KEY and ecode == BTN_TOUCH:
                    if evalue == 1:
                        # Press: record start; classification waits for release.
                        self._press_x = self._last_x
                        self._press_y = self._last_y
                        self._press_t = time.monotonic()
                    elif evalue == 0:
                        # Release: classify as tap, swipe, or drag (discard).
                        if (self._press_x is None or self._press_y is None
                                or self._last_x is None
                                or self._last_y is None):
                            self._press_x = self._press_y = None
                            continue
                        dx = self._last_x - self._press_x
                        dy = self._last_y - self._press_y
                        dt = time.monotonic() - self._press_t
                        move = (dx * dx + dy * dy) ** 0.5
                        cx, cy = self.display.panel_to_canvas(
                            self._press_x, self._press_y)
                        if move <= TAP_MAX_PX:
                            events.append(TouchEvent("tap", cx, cy))
                        elif move >= SWIPE_MIN_PX and dt <= SWIPE_MAX_S:
                            end_cx, end_cy = self.display.panel_to_canvas(
                                self._last_x, self._last_y)
                            cdx = end_cx - cx
                            cdy = end_cy - cy
                            if abs(cdx) > abs(cdy):
                                direction = "right" if cdx > 0 else "left"
                            else:
                                direction = "down" if cdy > 0 else "up"
                            events.append(TouchEvent(
                                "swipe", cx, cy, direction))
                        # else: ambiguous slow drag — discard.
                        self._press_x = self._press_y = None
        return events

    def discard_press(self) -> None:
        """Drop the in-flight press tracking. Subsequent held_position()
        calls return None until the next finger-down, and the eventual
        BTN_TOUCH release skips emitting a tap/swipe (the classifier
        early-outs when _press_x is None). Used by the wake-from-dim
        path so a touch that only wakes the screen can't also act on
        whatever button it happened to land on."""
        self._press_x = None
        self._press_y = None

    def held_position(self) -> tuple[int, int, float] | None:
        """Stable-hold introspection: returns (cx, cy, held_seconds)
        if the finger is currently down and hasn't drifted beyond
        TAP_MAX_PX since the press started. Returns None on release
        OR once movement disqualifies the touch as a hold (it's now
        a drag/swipe). Compositor.tick() polls this each frame to
        drive auto-repeat on `repeatable=True` buttons."""
        if (self._press_x is None or self._press_y is None
                or self._last_x is None or self._last_y is None):
            return None
        dx = self._last_x - self._press_x
        dy = self._last_y - self._press_y
        if (dx * dx + dy * dy) ** 0.5 > TAP_MAX_PX:
            return None
        cx, cy = self.display.panel_to_canvas(
            self._press_x, self._press_y)
        return cx, cy, time.monotonic() - self._press_t

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except Exception:
                pass


# =====================================================================
# Compositor: pick active scene per frame, repaint on dirty, dispatch
# taps to scene.hit() → Button.on_press().
# =====================================================================
class Compositor:
    """Owns scene selection + overlay (system chrome) + repaint decisions.

    Scene selection model
    ---------------------
    - default_picker() returns the underlying scene name, driven by app state
      (idle / radio / alarm). The compositor renders this when no overlay is
      open.
    - set_overlay(name) shows an overlay scene on top. The same gesture that
      opened it (or any action button inside it) typically dismisses it via
      clear_overlay().

    System chrome
    -------------
    - A tap on empty area in IdleScene/RadioScene opens the "launcher"
      (Apps) overlay. Buttons inside those scenes still receive their
      own taps unchanged (alarm pill, transport row).
    - Swipe-up anywhere on the home screens (idle/radio) also opens
      the launcher — kept as a low-effort gesture for users who'd
      rather flick than tap. Blocked while an alarm is firing so a
      3 a.m. brush can't dismiss the alarm.
    - All other directions and any swipe over an open overlay are
      ignored — the back arrow at top-left of every overlay is the
      single navigation affordance.
    """

    # Window during which a second tap is ignored after a button
    # press. Covers the full visual feedback cycle (one repaint at
    # FRAME_RATE plus typical action+render time) so the user can't
    # queue a duplicate action while the screen is still updating.
    TAP_LOCKOUT_S = 0.35
    # Hold-to-repeat: how long the user must hold a `repeatable=True`
    # button before the first auto-repeat fires (matches OS-level
    # key-repeat conventions on most platforms).
    REPEAT_INITIAL_S = 0.45
    # Initial repeat interval and the floor it accelerates to. After
    # each repeat the interval shrinks by REPEAT_DECAY toward FLOOR.
    REPEAT_INTERVAL_S = 0.18
    REPEAT_FLOOR_S = 0.06
    REPEAT_DECAY = 0.85

    def __init__(self, display: Display, scenes: dict, default_picker,
                 *, theme_service=None, touch=None):
        self.display = display
        self.scenes = scenes
        self.default_picker = default_picker
        self._theme_service = theme_service
        self._touch = touch
        self._overlay: str | None = None
        self._last_key: tuple | None = None
        # Optional guided-tour wiring. When the demo is active the
        # compositor paints a translucent caption band over the picked
        # scene and re-routes taps so only EXIT / NEXT can fire.
        self._demo = None
        self._demo_overlay = None
        # Cached scene image + the last brightness quantum we presented
        # at. tick() re-renders the scene only when state_key changes,
        # but re-presents whenever brightness has shifted enough to be
        # visible — that lets the dim fade animate smoothly without
        # paying for a full scene re-render every frame.
        self._cached_image: Image.Image | None = None
        self._last_scale_q: tuple[int, int, int] = (-1, -1, -1)
        # Latest rgb-scale target tick() observed. dispatch_tap reads
        # it for the press-feedback frame so the flash respects dim
        # and the night-red tint.
        self._rgb_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
        self._tap_lockout_until: float = 0.0
        # Hold-state: the button currently under a stable press, when
        # we last fired an auto-repeat, and a flag the tap dispatcher
        # checks on release to decide whether the trailing tap should
        # also fire on_press.
        self._held_button = None
        self._held_scene = None
        self._held_did_repeat = False
        self._next_repeat_at: float = 0.0
        self._repeat_count: int = 0

    # --- demo wiring --------------------------------------------------

    def attach_demo(self, demo, demo_overlay) -> None:
        self._demo = demo
        self._demo_overlay = demo_overlay

    # --- overlay control ----------------------------------------------

    def set_overlay(self, name: str) -> bool:
        if name not in self.scenes:
            return False
        self._overlay = name
        scene = self.scenes[name]
        on_show = getattr(scene, "on_show", None)
        if callable(on_show):
            try:
                on_show()
            except Exception as exc:
                print(f"on_show {name}: {exc}",
                      file=sys.stderr, flush=True)
        return True

    def clear_overlay(self) -> None:
        self._overlay = None

    def toggle_overlay(self, name: str) -> bool:
        if self._overlay == name:
            self.clear_overlay()
            return False
        return self.set_overlay(name)

    def underlying_scene_name(self) -> str:
        return self.default_picker()

    def current_scene_name(self) -> str:
        return self._overlay or self.default_picker()

    @property
    def has_overlay(self) -> bool:
        return self._overlay is not None

    # --- frame + dispatch ---------------------------------------------

    def tick(self,
             rgb_scale: tuple[float, float, float]
             = (1.0, 1.0, 1.0)) -> None:
        # Drive auto-repeat from the touch state BEFORE rendering so
        # any state mutations (e.g. an alarm hour bumped while held)
        # land in this frame's repaint.
        self._drive_hold_repeat()
        scene = self.scenes[self.current_scene_name()]
        # id(scene) ensures a scene-switch always repaints, even if the
        # incoming widgets' keys happen to match the previous scene's.
        # Theme version forces a repaint when the user picks a new theme.
        theme_v = (self._theme_service.version
                   if self._theme_service is not None else 0)
        # Demo step index is part of the key so each tour step forces a
        # repaint — the underlying scene's state_key may not change
        # (e.g. three consecutive idle steps that just swap captions).
        demo_idx = (self._demo.step_index
                    if self._demo is not None and self._demo.is_active
                    else -1)
        key = (id(scene), theme_v, demo_idx) + scene.state_key()
        # Latest rgb-scale target — dispatch_tap reads this for the
        # press-flash frame so the flash respects dim + night-red.
        self._rgb_scale = rgb_scale
        scale_q = tuple(round(v * 256) for v in rgb_scale)
        if key != self._last_key:
            # Scene changed: render once, present once. Cache the image
            # so subsequent dim/tint fades can re-present it without
            # re-rendering the scene.
            self._last_key = key
            raw = scene.render()
            # Bake the demo caption into the cached image while the
            # tour is active. Doing it here (rather than on every
            # present()) lets fade-only re-presents reuse the cached
            # composite without paying for re-compositing per frame.
            if (self._demo is not None and self._demo.is_active
                    and self._demo_overlay is not None):
                try:
                    theme = (self._theme_service.current
                             if self._theme_service is not None else None)
                    self._demo_overlay.render(raw, theme)
                except Exception as exc:
                    print(f"demo overlay render: {exc}",
                          file=sys.stderr, flush=True)
            self._cached_image = raw
            self._last_scale_q = scale_q
            self.display.present(self._cached_image, rgb_scale)
            return
        # Scene unchanged. Re-present only if the rgb-scale has moved
        # by at least one quantum step on any channel — keeps the
        # framebuffer write off the hot path when the tint is steady.
        if (self._cached_image is not None
                and scale_q != self._last_scale_q):
            self._last_scale_q = scale_q
            self.display.present(self._cached_image, rgb_scale)

    def _drive_hold_repeat(self) -> None:
        """Poll TouchReader for stable-hold state and fire on_press on
        repeatable buttons after the initial-delay threshold.

        The release path (dispatch_tap) checks `_held_did_repeat` to
        skip the trailing on_press — otherwise lifting the finger
        after a long hold would tack on one more action."""
        if self._touch is None:
            return
        # Demo mode swallows underlying-scene buttons (only EXIT / NEXT
        # in the caption fire), so hold-to-repeat is irrelevant here
        # and leaving it on would arm a stuck-press visual on whatever
        # button the finger happened to land on.
        if self._demo is not None and self._demo.is_active:
            return
        held = self._touch.held_position()
        now = time.monotonic()
        if held is None:
            # Released or moved — clear hold state. _held_did_repeat
            # stays True until the trailing tap is dispatched and
            # checks it, then is reset on the next press.
            if self._held_button is not None:
                self._held_button._pressed = False
                self._last_key = None  # repaint to clear pressed visual
            self._held_button = None
            self._held_scene = None
            return
        cx, cy, held_for = held
        if self._held_button is None:
            # First sighting of this press — find the button under the
            # finger and stash it. Press visual + did-repeat flag are
            # set up here so the dispatcher pipeline stays consistent.
            scene = self.scenes[self.current_scene_name()]
            btn = scene.hit(cx, cy)
            if btn is None:
                return
            self._held_button = btn
            self._held_scene = scene
            self._held_did_repeat = False
            self._repeat_count = 0
            self._next_repeat_at = (time.monotonic()
                                    + self.REPEAT_INITIAL_S)
            # Mark the button pressed so this frame's render shows the
            # feedback even before any auto-repeat has fired.
            btn._pressed = True
            self._last_key = None
            return
        # Already tracking a press. Auto-repeat only on repeatable
        # buttons; non-repeatable held buttons just keep the pressed
        # visual and fire a single on_press on release as usual.
        btn = self._held_button
        if not getattr(btn, "repeatable", False):
            return
        if now < self._next_repeat_at:
            return
        try:
            btn.on_press()
        except Exception as exc:
            print(f"hold-repeat: {exc}",
                  file=sys.stderr, flush=True)
        self._held_did_repeat = True
        self._repeat_count += 1
        # Decay interval toward the floor so a long hold accelerates.
        interval = max(self.REPEAT_FLOOR_S,
                       self.REPEAT_INTERVAL_S
                       * (self.REPEAT_DECAY ** self._repeat_count))
        self._next_repeat_at = now + interval
        self._last_key = None  # force repaint of the bumped value

    def dispatch_swipe(self, direction: str, cx: float, cy: float) -> bool:
        """Swipe-up on idle/radio opens the Apps launcher. Every other
        direction is ignored — overlays are dismissed via their
        back-arrow button only, and we want zero ambiguity over the
        firing alarm screen."""
        # Demo runs the show — no swipe gestures while it's driving.
        if self._demo is not None and self._demo.is_active:
            return False
        if direction != "up":
            return False
        # Only accept swipe-up on the home screens — over an open
        # overlay it's almost certainly a fat-finger drag while reading.
        if self._overlay is not None:
            return False
        if self.underlying_scene_name() == "alarm":
            return False
        return self.set_overlay("launcher")

    def dispatch_tap(self, cx: float, cy: float) -> bool:
        # Lockout swallows queued double-taps. Heavy scenes (e.g. the
        # background picker re-rendering the world map) can lag a few
        # hundred ms behind the first tap; without this window the
        # second tap reaches whatever button now occupies the same
        # screen area in the freshly-presented scene.
        now = time.monotonic()
        if now < self._tap_lockout_until:
            return False
        # Demo intercepts the entire input plane: the only buttons
        # that fire are EXIT / NEXT inside the caption band. Every
        # other tap is swallowed so the tour isn't derailed by a
        # stray touch on an underlying-scene button.
        if (self._demo is not None and self._demo.is_active
                and self._demo_overlay is not None):
            self._tap_lockout_until = now + self.TAP_LOCKOUT_S
            self._last_key = None
            # Splash slides have no visible buttons, so a tap anywhere
            # advances to the next step (Apple-style "tap to continue"
            # on unboxing screens). EXIT isn't reachable during a
            # splash — the slides are short and EXIT comes back into
            # play as soon as the regular caption band returns.
            if self._demo.is_splash:
                self._demo.next_step()
                return True
            action = self._demo_overlay.hit(cx, cy)
            if action == "exit":
                self._demo.exit()
                return True
            if action == "next":
                self._demo.next_step()
                return True
            return False
        # If the hold path already auto-repeated during this press,
        # the user has already gotten N actions out of this gesture
        # — the trailing tap on lift-off should not fire one more.
        # Reset the flag now so the next press starts fresh.
        if self._held_did_repeat:
            self._held_did_repeat = False
            self._tap_lockout_until = now + self.TAP_LOCKOUT_S
            return False
        scene = self.scenes[self.current_scene_name()]
        btn = scene.hit(cx, cy)
        if btn is None:
            # No button — give the scene a chance to handle the tap
            # (IdleScene/RadioScene open the Apps overlay on empty-area
            # taps; everything else returns False and the tap is lost).
            try:
                handled = bool(scene.on_tap(cx, cy))
            except Exception as exc:
                print(f"scene on_tap: {exc}",
                      file=sys.stderr, flush=True)
                handled = False
            if handled:
                self._tap_lockout_until = now + self.TAP_LOCKOUT_S
                self._last_key = None
            return handled
        # Synchronously paint + present a "pressed" frame BEFORE
        # invoking the action. The user sees the press registered
        # within one render tick; the action's downstream render
        # follows on the next loop iteration. Caller resets _pressed
        # immediately so subsequent renders show the post-action
        # state, not a stuck-pressed visual.
        btn._pressed = True
        try:
            # Press-flash frame respects the latest rgb-scale so a tap
            # in dim mode (after the wake-only first contact) doesn't
            # blast a fully-bright flash onto the dark-adapted user,
            # and a tap with night-red on stays red.
            self.display.present(scene.render(), self._rgb_scale)
        except Exception as exc:
            print(f"press-flash render: {exc}",
                  file=sys.stderr, flush=True)
        finally:
            btn._pressed = False
        # Force a repaint on the next tick — the action below may
        # change scene/state and we need _last_key to differ.
        self._last_key = None
        self._tap_lockout_until = now + self.TAP_LOCKOUT_S
        try:
            btn.on_press()
        except Exception as exc:
            print(f"button on_press: {exc}", file=sys.stderr, flush=True)
        return True


# =====================================================================
# main
# =====================================================================
def main() -> int:
    display = Display(rotation_ccw=ROTATION_CCW)
    print(f"fb: {display.fb_w}x{display.fb_h} bpp={display.fb_bpp} "
          f"stride={display.fb_stride}", flush=True)

    backlight = Backlight()
    print(f"backlight: {backlight.path} max={backlight.maximum} "
          f"dim={backlight.dim_level}", flush=True)
    backlight.write(backlight.maximum)

    touch = TouchReader(display)
    print(f"touch: {touch.path}", flush=True)

    mpd = MPDService()
    mpd.start()

    station_store = StationStore(STATIONS_PATH)
    stations = StationService(station_store, mpd)

    wifi = WifiService()
    wifi.start()

    # BluetoothService takes a handle to MPDService so a successful
    # pair can route MPD to the new bluealsa output without the user
    # having to navigate to the audio-output picker. The privileged
    # helper that mutates /etc/mpd.conf lives at the path below — see
    # setup/01-bootstrap.sh + setup/bt-output-helper.sh.
    bluetooth = BluetoothService(mpd_service=mpd)
    bluetooth.start()

    weather = WeatherService(LOCATION_PATH)
    weather.start()

    verse = VerseService(VERSE_PATH)
    verse.start()

    alarm_store = AlarmStore(ALARMS_PATH)
    alarms = AlarmService(alarm_store, mpd, alarm_url=ALARM_URL)
    alarms.start()

    # i18n must come before any Scene is constructed — Scenes call into
    # scenes._t() at render time and that helper resolves through the
    # service we set here. Wired even when only English ships, so the
    # plumbing is identical whether or not other languages are loaded.
    i18n = I18nService(LANGUAGE_PATH)
    _scenes_mod.set_i18n(i18n)
    print(f"language: {i18n.lang} ({i18n.native_name()})", flush=True)

    theme_service = ThemeService(THEME_PATH, THEMES)
    theme = ThemeProxy(theme_service)
    print(f"theme: {theme_service.current.name}", flush=True)

    brightness = BrightnessService(BRIGHTNESS_PATH)
    print(f"brightness: active={brightness.config.active_pct}% "
          f"dim={brightness.config.dim_pct}% "
          f"auto_ambient={brightness.config.auto_ambient} "
          f"dim_ref={brightness.config.light_dim_ref} "
          f"bright_ref={brightness.config.light_bright_ref}",
          flush=True)

    light = LightService(
        get_dim_ref=lambda: brightness.config.light_dim_ref,
        get_bright_ref=lambda: brightness.config.light_bright_ref,
        log_path=LIGHT_LOG_PATH)
    light.start()

    background = BackgroundService(BACKGROUND_PATH)
    world_map = WorldMapService(display.canvas_w, display.canvas_h,
                                location_path=LOCATION_PATH)
    world_map.start()
    print(f"background: {background.mode}", flush=True)

    # Idle/Radio opt in to a background; the provider returns None when
    # mode == "none" so the legacy solid-bg path is unchanged.
    class _BackgroundProvider:
        # Styles that read poorly under the dim backlight (sparse star
        # dots get crushed past the visibility threshold once the
        # software RGB multiplier kicks in). When the panel goes idle
        # we suppress these and fall through to the solid theme bg —
        # other map styles have enough mid-tone area to survive dimming.
        _DIM_SUPPRESSED = frozenset({"starmap"})

        def __init__(self, bg_svc, wm_svc):
            self._bg = bg_svc
            self._wm = wm_svc
            self._was_stale = False
            # Set by the brightness loop on every active↔dim transition.
            # Folded into state_key so the compositor repaints the moment
            # we cross the threshold.
            self._dim = False

        def set_dim(self, dim: bool) -> None:
            self._dim = bool(dim)

        def __call__(self, theme):
            style = self._bg.style_name()
            if style is None:
                self._was_stale = False
                return None
            if self._dim and style in self._DIM_SUPPRESSED:
                # Caller falls back to solid theme bg — see Scene._make_canvas.
                self._was_stale = False
                return None
            ovs = self._bg.active_overlays()
            cl = self._bg.center_lon
            # Nonblocking path: returns the latest cached image (which
            # may be for the previous params) plus a stale flag. The
            # main render loop never blocks on a 2–4s map render —
            # instead the previous map stays visible until the eager
            # worker finishes and the next frame sees a fresh cache.
            img, stale = self._wm.current_image_nonblocking(
                theme, style_name=style, overlays=ovs, center_lon=cl)
            self._was_stale = stale
            return img

        def style_name(self):
            # Surface the current map style so scenes can adjust their
            # layout per-style — e.g. IdleScene tucks the clock into
            # the top-left when the globe is active so the daylit disc
            # stays unobstructed.
            return self._bg.style_name()

        def is_rendering(self):
            """True iff the bg image we last served was stale OR a
            worker is currently rendering. Either case means scenes
            should show the small 'updating' indicator."""
            if self._was_stale:
                return True
            return self._wm.is_rendering()

        def state_key(self):
            style = self._bg.style_name()
            if style is not None:
                # When the dim-suppression branch is active we don't
                # actually paint the map, so the cache key collapses to
                # "(suppressed)" — keeps the compositor from invalidating
                # on every map-warmer tick while we're dimmed.
                if self._dim and style in self._DIM_SUPPRESSED:
                    return ("world_map_dim_suppressed", style)
                ovs = self._bg.active_overlays()
                cl = self._bg.center_lon
                rendering = self._wm.is_rendering()
                return (("world_map", style, rendering)
                        + self._wm.state_key(style, ovs, cl))
            return ("none",)

    bg_provider = _BackgroundProvider(background, world_map)

    # Eager pre-warm: any time the user picks a new style / overlay /
    # center-longitude in settings, kick off the render of the new
    # params right away on the warmer thread. By the time the user
    # navigates back to the home screen the result is usually cached,
    # killing the synchronous render lag that otherwise showed up the
    # first time the home scene rebuilt.
    def _on_bg_changed():
        style = background.style_name()
        if style is None:
            return
        world_map.request_prewarm(
            style,
            background.active_overlays(),
            background.center_lon,
        )
    background.set_change_listener(_on_bg_changed)

    # IdleScene now needs compositor (alarm preview tap) + wifi_service
    # (header glyph), so it's built alongside the other compositor-aware
    # scenes below.
    scenes: dict = {
        "alarm": AlarmFiringScene(theme, display.canvas_w, display.canvas_h,
                                  alarm_service=alarms),
    }

    def pick_scene() -> str:
        if alarms.firing:
            return "alarm"
        # An active BT stream (or the phone's AVRCP reporting Playing
        # even if the A2DP route is still settling) takes precedence
        # over the radio
        # scene because the user paused MPD (or sink-mode did it for
        # them) and what's actually audible is the phone audio. Show
        # a home that reflects that — including a DISCONNECT button.
        if bluetooth.status.streaming_from or (
                bluetooth.status.connected_phone
                and bluetooth.status.media_status == "playing"):
            return "bt_playing"
        return "radio" if mpd.status.active else "idle"

    compositor = Compositor(display, scenes, pick_scene,
                            theme_service=theme_service,
                            touch=touch)

    # Scenes that need the compositor (for overlay control) are built
    # after compositor exists.
    scenes["idle"] = IdleScene(
        theme, display.canvas_w, display.canvas_h,
        alarm_service=alarms, mpd_service=mpd,
        station_service=stations, wifi_service=wifi,
        compositor=compositor,
    )
    scenes["radio"] = RadioScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, mpd_service=mpd, station_service=stations,
        alarm_service=alarms,
    )
    scenes["bt_playing"] = BluetoothPlayingScene(
        theme, display.canvas_w, display.canvas_h,
        alarm_service=alarms, mpd_service=mpd,
        bluetooth_service=bluetooth, compositor=compositor,
    )
    # Idle + Radio + the alarm-firing scene opt in to the world map
    # background. Alarm-firing inherits because the user wanted the
    # whole device to feel coherent at 7am — same map, halo'd clock,
    # then a single STOP button rather than a flat black emergency
    # screen. BT-playing inherits for the same reason — it IS a home
    # screen variant; switching backgrounds when the phone connects
    # would feel jarring.
    scenes["idle"]._background_provider = bg_provider
    scenes["radio"]._background_provider = bg_provider
    scenes["alarm"]._background_provider = bg_provider
    scenes["bt_playing"]._background_provider = bg_provider
    scenes["launcher"] = LauncherScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor,
    )
    scenes["quick"] = QuickPanelScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, mpd_service=mpd, alarm_service=alarms,
    )
    scenes["alarm_list"] = AlarmListScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, alarm_service=alarms,
    )
    scenes["alarm_edit"] = AlarmEditScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, alarm_service=alarms,
    )
    scenes["station_list"] = StationListScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, station_service=stations,
    )
    scenes["settings"] = SettingsScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor,
    )
    scenes["display_settings"] = DisplaySettingsScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor,
    )
    scenes["wifi"] = WifiScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, wifi_service=wifi,
    )
    scenes["wifi_password"] = WifiPasswordScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, wifi_service=wifi,
    )
    scenes["bluetooth"] = BluetoothScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, bluetooth_service=bluetooth,
    )
    scenes["bluetooth_speaker"] = BluetoothSpeakerScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, bluetooth_service=bluetooth,
    )
    scenes["weather"] = WeatherScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, weather_service=weather,
    )
    scenes["weather_location"] = WeatherLocationScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, weather_service=weather,
    )
    scenes["verse"] = VerseScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, verse_service=verse,
    )
    scenes["theme"] = ThemeScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, theme_service=theme_service,
    )
    scenes["language"] = LanguageScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, i18n_service=i18n,
    )
    scenes["brightness"] = BrightnessScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, brightness_service=brightness,
        light_service=light,
    )
    scenes["about"] = AboutScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, theme_service=theme_service,
        alarm_service=alarms, station_service=stations,
        mpd_service=mpd, i18n_service=i18n,
    )
    scenes["audio_output"] = AudioOutputScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, mpd_service=mpd,
    )
    scenes["background"] = BackgroundScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, background_service=background,
    )
    scenes["map_center"] = MapCenterScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, background_service=background,
    )

    # Guided demo — the service drives compositor.set_overlay() each
    # step; the caption overlay is composited onto the cached scene
    # image during tick(). Snapshots/restores background so the tour
    # never leaves the user's settings changed.
    demo = DemoService(background_service=background)
    demo.attach(compositor)
    demo_caption = CaptionOverlay(display.canvas_w, display.canvas_h)
    demo_caption.attach(demo)
    compositor.attach_demo(demo, demo_caption)
    scenes["demo_intro"] = DemoIntroScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, demo_service=demo,
    )
    scenes["demo_splash"] = DemoSplashScene(
        theme, display.canvas_w, display.canvas_h,
        demo_service=demo,
    )

    # Brightness preferences are stored in percent. Each frame we
    # resolve the target percent to a (backlight_level, software_factor)
    # pair: above the panel's hardware floor (1/max ≈ 3.2%) the
    # backlight does the dimming and software stays at 1.0; below it
    # the backlight pins at 1 and a software RGB multiplier takes
    # over so the user can dim further than the panel firmware allows.
    # That's the bedside-clock fix — the previous code just truncated
    # to backlight=0 below ~3% so 5% was the dimmest visible setting.
    HW_FLOOR = 1.0 / max(1, backlight.maximum)        # ≈ 0.032 on Pi 7"
    SW_FLOOR = 0.10                                   # never go pitch-black
    # Night-red weights: red kept full, green strongly suppressed,
    # blue near-zero — the astronomers' deep-red filter approximation.
    # Values picked so dim tones still read (e.g. fg_dim text doesn't
    # vanish) but the screen no longer emits the blue/green wavelengths
    # that disrupt melatonin and dark adaptation.
    NIGHT_R = 1.00
    NIGHT_G = 0.10
    NIGHT_B = 0.04

    def _resolve(pct: int) -> tuple[int, float]:
        """Map a percent target to (backlight_level, sw_factor) using
        the panel's hardware floor — shared by both active and dim
        target functions."""
        if pct <= 0:
            return 0, 1.0
        target = max(0.0, min(1.0, pct / 100.0))
        if target >= HW_FLOOR:
            return max(1, int(round(target * backlight.maximum))), 1.0
        return 1, max(SW_FLOOR, target / HW_FLOOR)

    def _apply_ambient(pct: int) -> int:
        # User percent is the dim-room baseline; LightService.gain
        # lifts it toward 100 when ambient gets brighter. No effect
        # when the toggle is off or the sensor is unavailable.
        if not brightness.config.auto_ambient or not light.status.available:
            return pct
        return int(round(pct + light.gain() * (100 - pct)))

    def active_target() -> tuple[int, tuple[float, float, float]]:
        # Active mode is never tinted — full colour even when night_red
        # is enabled. The red bias only kicks in once we've gone idle,
        # which is the bedside-glance scenario.
        bl, sw = _resolve(_apply_ambient(brightness.config.active_pct))
        return bl, (sw, sw, sw)

    def idle_dim_target() -> tuple[int, tuple[float, float, float]]:
        bl, sw = _resolve(_apply_ambient(brightness.config.dim_pct))
        # Night-red tint applies whenever the toggle is on and we're
        # in the idle/dim mode — independent of ambient brightness.
        # The deep red is restful at a glance regardless of room
        # light, so the previous ambient-suppression hysteresis has
        # been dropped.
        if brightness.config.night_red:
            return bl, (sw * NIGHT_R, sw * NIGHT_G, sw * NIGHT_B)
        return bl, (sw, sw, sw)

    last_input_t = time.monotonic()
    # Track previous alarm-firing edge so the very moment an alarm goes
    # off we treat it like a touch — reset last_input_t and snap out of
    # dim mode. Without this, a 06:30 alarm fired while the panel was
    # dim would render the AlarmFiringScene at bedside-low brightness
    # and the user couldn't see the STOP button until they touched the
    # screen first.
    prev_firing = bool(alarms.firing)
    init_b, init_rgb = active_target()
    current_b = float(init_b)
    target_b = init_b
    # Per-channel RGB scale. Faded toward target_rgb each frame so the
    # transition into / out of night-red animates over the same
    # duration as the backlight fade — no jarring colour pop.
    current_rgb: list[float] = list(init_rgb)
    target_rgb: tuple[float, float, float] = init_rgb
    # Mode tracking: "active" while the user is interacting, "dim" once
    # the idle timeout has elapsed. Mode *transitions* use the slow
    # fade (gentle bedside dim / wake); within-mode target changes
    # (BrightnessScene step, night-red toggle) use the fast fade.
    prev_mode = "active"
    slow_fade_until = 0.0

    running = True

    def shutdown(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while running:
            active_b, active_rgb = active_target()
            # Edge-trigger wake-on-fire: when alarms.firing flips True
            # we reset the idle stamp + snap brightness to active so the
            # firing scene comes up at full visibility regardless of
            # whether the panel was dim before. The firing-scene STOP
            # button is the only useful affordance at 06:30 — it has to
            # be readable immediately, not after a touch-to-wake gesture.
            firing_now = bool(alarms.firing)
            if firing_now and not prev_firing:
                last_input_t = time.monotonic()
                current_b = float(active_b)
                current_rgb = list(active_rgb)
                backlight.write(int(current_b))
            prev_firing = firing_now
            for ev in touch.poll():
                last_input_t = time.monotonic()
                target_b, target_rgb = active_b, active_rgb
                # Suppress actions if screen was dim — first contact only
                # wakes; user must touch a lit screen to act. The was_dim
                # check uses the backlight level + the red channel
                # (always == sw, never tinted) so it still trips
                # correctly when dim is achieved via software multiplier.
                was_dim = (current_b < max(active_b, 2) * 0.5
                           or current_rgb[0] < 0.5)
                if was_dim:
                    # Discard the in-flight press so neither the hold
                    # path (which would mark a button visually pressed
                    # during the fade-up) nor the trailing tap on
                    # release fires. Without this the user only got
                    # wake-only behaviour for very fast taps — a press
                    # held past ~half the fade let was_dim flip to
                    # False, and the release reached the underlying
                    # button.
                    touch.discard_press()
                    continue
                if ev.kind == "tap":
                    compositor.dispatch_tap(ev.cx, ev.cy)
                elif ev.kind == "swipe":
                    compositor.dispatch_swipe(ev.direction, ev.cx, ev.cy)

            # Auto-return from any settings/launcher overlay to the
            # home scene after SETTINGS_TIMEOUT_S of idleness. Demo
            # is exempted — the tour itself drives set_overlay() each
            # step and would fight a clear from underneath. Scenes
            # running a finite user-perceptible operation (BT pairing
            # countdown, mid-password typing, etc.) can opt out via
            # inhibit_auto_exit so they aren't dismissed mid-task.
            if (compositor.has_overlay
                    and not demo.is_active
                    and time.monotonic() - last_input_t > SETTINGS_TIMEOUT_S):
                cur = scenes.get(compositor.current_scene_name())
                if not (cur is not None
                        and getattr(cur, "inhibit_auto_exit", lambda: False)()):
                    compositor.clear_overlay()

            if demo.is_active:
                # Tour overrides everything: pin the panel to a
                # showcase level and out of the dim path. The mode
                # tag is its own value so entering and leaving the
                # tour both trigger the slow-fade window below
                # (gentle fade-up to the splash, gentle fade-back
                # to whatever the user's normal brightness is).
                bl, sw = _resolve(DEMO_BRIGHTNESS_PCT)
                target_b = bl
                target_rgb = (sw, sw, sw)
                target_mode = "demo"
            elif time.monotonic() - last_input_t > IDLE_TIMEOUT_S:
                target_b, target_rgb = idle_dim_target()
                target_mode = "dim"
            else:
                # Track the (possibly-just-edited) active level even when
                # there's no fresh touch — otherwise BrightnessScene
                # changes wouldn't apply until the next tap.
                target_b, target_rgb = active_b, active_rgb
                target_mode = "active"

            # On a mode flip, arm the slow fade for one full transition
            # window. While that window is open, fade gently; otherwise
            # snap (≈ one frame at 5fps) so user-driven step changes
            # feel responsive.
            now = time.monotonic()
            if target_mode != prev_mode:
                slow_fade_until = now + TRANSITION_FADE_S
                # Tell the bg provider whether the panel is dimmed so it
                # can suppress styles that read poorly under the dim
                # backlight (currently just starmap — sparse stars get
                # crushed past the visibility threshold).
                bg_provider.set_dim(target_mode == "dim")
            prev_mode = target_mode
            fade_s = TRANSITION_FADE_S if now < slow_fade_until else STEP_FADE_S
            fade_step = max(
                1.0, backlight.maximum / (fade_s * FRAME_RATE))
            fade_step_sw = 1.0 / max(1.0, fade_s * FRAME_RATE)

            if abs(current_b - target_b) > 0.5:
                if current_b < target_b:
                    current_b = min(float(target_b), current_b + fade_step)
                else:
                    current_b = max(float(target_b), current_b - fade_step)
                backlight.write(int(round(current_b)))

            # Fade each RGB channel toward its target independently —
            # going active→dim with night_red enabled is a non-uniform
            # transition (R stays high, G+B drop sharply) and per-channel
            # interpolation lets the colour shift animate smoothly.
            for i in range(3):
                if abs(current_rgb[i] - target_rgb[i]) > 0.005:
                    if current_rgb[i] < target_rgb[i]:
                        current_rgb[i] = min(float(target_rgb[i]),
                                             current_rgb[i] + fade_step_sw)
                    else:
                        current_rgb[i] = max(float(target_rgb[i]),
                                             current_rgb[i] - fade_step_sw)

            # Advance the guided tour before painting so a step
            # transition (which calls set_overlay) lands in this
            # frame's render rather than the next one.
            demo.tick()
            compositor.tick(rgb_scale=tuple(current_rgb))
            time.sleep(1.0 / FRAME_RATE)

    finally:
        alarms.stop()
        wifi.stop()
        bluetooth.stop()
        weather.stop()
        verse.stop()
        mpd.stop()
        world_map.stop()
        light.stop()
        try:
            backlight.write(backlight.maximum)
        except Exception:
            pass
        touch.close()
        display.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
