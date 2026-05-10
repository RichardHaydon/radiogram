"""Ambient light service — LDR on GPIO17 via the RC charge-time technique.

Polls once a second, smooths with an EMA, and exposes a 0..1 boost gain.
The user's brightness setting represents the comfort point in a *dim*
room; brighter ambient lifts the panel above that toward 100%.

If the sensor isn't wired or the cap fails (5 consecutive timeouts) the
service flags itself unavailable and gain() returns 0 — the radio falls
back to the user's static brightness with no further intervention.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import RPi.GPIO as GPIO  # type: ignore
    _HAS_GPIO = True
    _IMPORT_ERR = ""
except Exception as _exc:
    GPIO = None  # type: ignore
    _HAS_GPIO = False
    _IMPORT_ERR = f"{type(_exc).__name__}: {_exc}"


PIN = 17
DISCHARGE_S = 0.005
TIMEOUT_COUNT = 200000

# Counts go *down* with brighter light. DIM_REF is the "no boost"
# anchor (matches the dim-room baseline). BRIGHT_REF is the "full
# boost" anchor (clamps to 100 %). Both come from BrightnessConfig
# at runtime via the callables passed to the service — the user
# tunes them via CAL DIM / CAL BRIGHT in BrightnessScene.
#
# DEADBAND_FRAC is the fraction of DIM_REF below which the boost
# ramp begins. Sensor noise typically swings counts by a few percent
# per sample; without a deadband, samples bouncing across DIM_REF
# would constantly nudge the gain off zero (and the asymmetric EMA
# release would hold each blip for several seconds). 0.85 leaves a
# 15 % margin: anything within 15 % of the dim anchor is honoured
# as "still dim" with gain pinned at exactly 0.
DIM_REF_COUNT = 800
BRIGHT_REF_COUNT = 50
DEADBAND_FRAC = 0.85

POLL_S = 0.3
# Asymmetric smoothing: panel should lift quickly when a room light
# flicks on (so the user isn't reading a dim screen in a bright room)
# but ease back gently — a hand-shadow passing over the LDR shouldn't
# plunge the panel to bedside dim mid-glance, and lights-off in a
# transitional moment should fade rather than snap.
ATTACK_TAU_S = 0.3    # count dropping (room getting brighter)
RELEASE_TAU_S = 8.0   # count rising  (room getting darker)
# 5 consecutive timeouts at 0.3 s poll = ~1.5 s before flagging the
# sensor unavailable, still well under any human-noticeable lag.
FAIL_STREAK_FOR_OUTAGE = 5


@dataclass(frozen=True)
class LightStatus:
    available: bool = False
    raw_count: int = 0
    smooth_count: float = 0.0
    gain: float = 0.0


class LightService:
    def __init__(self,
                 get_dim_ref: Optional[Callable[[], int]] = None,
                 get_bright_ref: Optional[Callable[[], int]] = None) -> None:
        self._lock = threading.Lock()
        self._status = LightStatus()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fail_streak = 0
        self._outage_logged = False
        # Callables so the calibration is hot — the user's CAL DIM /
        # CAL BRIGHT buttons write BrightnessConfig, and the next
        # sample picks up the new anchors without a service restart.
        self._get_dim_ref = get_dim_ref or (lambda: DIM_REF_COUNT)
        self._get_bright_ref = (
            get_bright_ref or (lambda: BRIGHT_REF_COUNT))

    @property
    def status(self) -> LightStatus:
        with self._lock:
            return self._status

    def gain(self) -> float:
        with self._lock:
            return self._status.gain if self._status.available else 0.0

    def start(self) -> None:
        if not _HAS_GPIO:
            print(f"light: RPi.GPIO unavailable ({_IMPORT_ERR}); "
                  "auto-brightness disabled", flush=True)
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
        except Exception as exc:
            print(f"light: GPIO setmode failed ({exc}); disabled",
                  flush=True)
            return
        self._thread = threading.Thread(target=self._run,
                                        name="light", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if _HAS_GPIO:
            try:
                GPIO.cleanup(PIN)
            except Exception:
                pass

    def _run(self) -> None:
        smooth: Optional[float] = None
        alpha_attack = 1.0 - math.exp(-POLL_S / ATTACK_TAU_S)
        alpha_release = 1.0 - math.exp(-POLL_S / RELEASE_TAU_S)
        last_heartbeat = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                count = self._read_count()
            except Exception as exc:
                print(f"light: read failed: {exc}", flush=True)
                count = TIMEOUT_COUNT

            timed_out = count >= TIMEOUT_COUNT
            if timed_out:
                self._fail_streak += 1
            else:
                self._fail_streak = 0

            available = self._fail_streak < FAIL_STREAK_FOR_OUTAGE
            if not available and not self._outage_logged:
                print("light: sensor unavailable (5 consecutive timeouts) "
                      "— falling back to user brightness", flush=True)
                self._outage_logged = True
            elif available and self._outage_logged:
                print("light: sensor recovered", flush=True)
                self._outage_logged = False

            if available and not timed_out:
                if smooth is None:
                    smooth = float(count)
                else:
                    a = alpha_attack if count < smooth else alpha_release
                    smooth += a * (count - smooth)
                gain = self._gain_for(
                    smooth, self._get_dim_ref(), self._get_bright_ref())
            else:
                smooth = None
                gain = 0.0

            with self._lock:
                self._status = LightStatus(
                    available=available,
                    raw_count=count,
                    smooth_count=smooth or 0.0,
                    gain=gain,
                )

            now = time.monotonic()
            if now - last_heartbeat >= 30.0:
                last_heartbeat = now
                print(f"light: count={count} smooth={int(smooth or 0)} "
                      f"gain={gain:.2f} avail={available}", flush=True)

            elapsed = now - t0
            self._stop.wait(max(0.0, POLL_S - elapsed))

    @staticmethod
    def _read_count() -> int:
        # Phase 1: drain the cap by driving the pin LOW as an output.
        GPIO.setup(PIN, GPIO.OUT)
        GPIO.output(PIN, GPIO.LOW)
        time.sleep(DISCHARGE_S)
        # Phase 2: high-Z input — cap charges through the LDR.
        GPIO.setup(PIN, GPIO.IN)
        # Phase 3: count loop iterations until the input crosses HIGH.
        n = 0
        while GPIO.input(PIN) == GPIO.LOW:
            n += 1
            if n >= TIMEOUT_COUNT:
                return TIMEOUT_COUNT
        return n

    @staticmethod
    def _gain_for(smooth_count: float,
                  dim_ref: int, bright_ref: int) -> float:
        # Linear in 1/count, which scales roughly with illuminance.
        # The dim-side ramp begins at DEADBAND_FRAC * dim_ref, not at
        # dim_ref itself — sensor noise around the threshold would
        # otherwise constantly nudge the gain just above zero and
        # cause visible level-stepping in steady ambient.
        if smooth_count <= 0:
            return 1.0
        dim_ref = max(1, int(dim_ref))
        bright_ref = max(1, int(bright_ref))
        if bright_ref >= dim_ref:
            # Invalid configuration (user set bright above dim); refuse
            # to boost rather than emit a nonsense ramp.
            return 0.0
        deadband_threshold = dim_ref * DEADBAND_FRAC
        if smooth_count >= deadband_threshold:
            return 0.0
        x = 1.0 / smooth_count
        x_dim = 1.0 / deadband_threshold
        x_brt = 1.0 / bright_ref
        if x_brt <= x_dim:
            return 0.0
        gain = (x - x_dim) / (x_brt - x_dim)
        return max(0.0, min(1.0, gain))
