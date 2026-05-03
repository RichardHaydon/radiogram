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
from brightness_service import BrightnessService
from mpd_service import MPDService
from station_service import StationService
from stations import StationStore
from theme import THEMES
from theme_service import ThemeProxy, ThemeService
from verse_service import VerseService
from weather_service import WeatherService
from wifi_service import WifiService
from world_map_service import WorldMapService
from scenes import (
    AboutScene, AlarmEditScene, AlarmFiringScene, AlarmListScene,
    AudioOutputScene, BackgroundScene, BrightnessScene, IdleScene,
    LauncherScene, MapCenterScene, QuickPanelScene, RadioScene,
    SettingsScene, StationListScene, ThemeScene, VerseScene,
    WeatherScene, WifiPasswordScene, WifiScene,
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
FRAME_RATE = 5
FADE_DURATION_S = 1.0
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
BACKGROUND_PATH = Path("/var/lib/clockradio/background.json")
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

    def present(self, img: Image.Image) -> None:
        if self.rotation_ccw:
            img = img.rotate(self.rotation_ccw, expand=True)
        os.lseek(self._fb_fd, 0, os.SEEK_SET)
        os.write(self._fb_fd, self._pack(img))

    def _pack(self, img: Image.Image) -> bytes:
        expected_stride = self.fb_w * (self.fb_bpp // 8)
        if self.fb_bpp == 32:
            raw = img.convert("RGBA").tobytes("raw", "BGRA")
        elif self.fb_bpp == 16:
            # RGB888 -> RGB565 via numpy (vectorised; per-pixel python is ~5s/frame)
            arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
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

    # --- frame + dispatch ---------------------------------------------

    def tick(self) -> None:
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
        key = (id(scene), theme_v) + scene.state_key()
        if key != self._last_key:
            self._last_key = key
            self.display.present(scene.render())

    def _drive_hold_repeat(self) -> None:
        """Poll TouchReader for stable-hold state and fire on_press on
        repeatable buttons after the initial-delay threshold.

        The release path (dispatch_tap) checks `_held_did_repeat` to
        skip the trailing on_press — otherwise lifting the finger
        after a long hold would tack on one more action."""
        if self._touch is None:
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
            self.display.present(scene.render())
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

    weather = WeatherService(LOCATION_PATH)
    weather.start()

    verse = VerseService(VERSE_PATH)
    verse.start()

    alarm_store = AlarmStore(ALARMS_PATH)
    alarms = AlarmService(alarm_store, mpd, alarm_url=ALARM_URL)
    alarms.start()

    theme_service = ThemeService(THEME_PATH, THEMES)
    theme = ThemeProxy(theme_service)
    print(f"theme: {theme_service.current.name}", flush=True)

    brightness = BrightnessService(BRIGHTNESS_PATH)
    print(f"brightness: active={brightness.config.active_pct}% "
          f"dim={brightness.config.dim_pct}%", flush=True)

    background = BackgroundService(BACKGROUND_PATH)
    world_map = WorldMapService(display.canvas_w, display.canvas_h)
    world_map.start()
    print(f"background: {background.mode}", flush=True)

    # Idle/Radio opt in to a background; the provider returns None when
    # mode == "none" so the legacy solid-bg path is unchanged.
    class _BackgroundProvider:
        def __init__(self, bg_svc, wm_svc):
            self._bg = bg_svc
            self._wm = wm_svc

        def __call__(self, theme):
            style = self._bg.style_name()
            if style is not None:
                ovs = self._bg.active_overlays()
                cl = self._bg.center_lon
                return self._wm.current_image(
                    theme, style_name=style, overlays=ovs,
                    center_lon=cl)
            return None

        def state_key(self):
            style = self._bg.style_name()
            if style is not None:
                ovs = self._bg.active_overlays()
                cl = self._bg.center_lon
                return (("world_map", style)
                        + self._wm.state_key(style, ovs, cl))
            return ("none",)

    bg_provider = _BackgroundProvider(background, world_map)

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
    )
    # Idle + Radio opt in to backgrounds (the only scenes shown long
    # enough that an animated underlay is meaningful).
    scenes["idle"]._background_provider = bg_provider
    scenes["radio"]._background_provider = bg_provider
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
    scenes["wifi"] = WifiScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, wifi_service=wifi,
    )
    scenes["wifi_password"] = WifiPasswordScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, wifi_service=wifi,
    )
    scenes["weather"] = WeatherScene(
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
    scenes["brightness"] = BrightnessScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, brightness_service=brightness,
    )
    scenes["about"] = AboutScene(
        theme, display.canvas_w, display.canvas_h,
        compositor=compositor, theme_service=theme_service,
        alarm_service=alarms, station_service=stations,
        mpd_service=mpd,
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

    # Brightness preferences are stored in percent so the same config
    # makes sense across different panel max_brightness values; resolve
    # to sysfs levels here, refreshed each frame so user changes apply
    # without a service restart.
    def active_level() -> int:
        return max(1, int(backlight.maximum
                          * brightness.config.active_pct / 100))

    def idle_dim_level() -> int:
        return max(0, int(backlight.maximum
                          * brightness.config.dim_pct / 100))

    last_input_t = time.monotonic()
    current_b = float(active_level())
    target_b = active_level()
    fade_step = max(1.0, backlight.maximum / (FADE_DURATION_S * FRAME_RATE))

    running = True

    def shutdown(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while running:
            active = active_level()
            for ev in touch.poll():
                last_input_t = time.monotonic()
                target_b = active
                # Suppress actions if screen was dim — first contact only
                # wakes; user must touch a lit screen to act.
                was_dim = current_b < active * 0.5
                if was_dim:
                    continue
                if ev.kind == "tap":
                    compositor.dispatch_tap(ev.cx, ev.cy)
                elif ev.kind == "swipe":
                    compositor.dispatch_swipe(ev.direction, ev.cx, ev.cy)

            if time.monotonic() - last_input_t > IDLE_TIMEOUT_S:
                target_b = idle_dim_level()
            else:
                # Track the (possibly-just-edited) active level even when
                # there's no fresh touch — otherwise BrightnessScene
                # changes wouldn't apply until the next tap.
                target_b = active

            if abs(current_b - target_b) > 0.5:
                if current_b < target_b:
                    current_b = min(float(target_b), current_b + fade_step)
                else:
                    current_b = max(float(target_b), current_b - fade_step)
                backlight.write(int(round(current_b)))

            compositor.tick()
            time.sleep(1.0 / FRAME_RATE)

    finally:
        alarms.stop()
        wifi.stop()
        weather.stop()
        verse.stop()
        mpd.stop()
        world_map.stop()
        try:
            backlight.write(backlight.maximum)
        except Exception:
            pass
        touch.close()
        display.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
