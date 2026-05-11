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
from pathlib import Path
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
# Reading is now wall-clock microseconds, not iteration count — the
# old busy-loop counter was dominated by Python scheduling jitter
# (kernel preemption during the charge cycle made counts come back
# small even at constant illumination). perf_counter_ns measures the
# real elapsed time, so a 5 ms kernel pause shows up as a 5 ms read,
# not as a "fast / bright" outlier. Existing CAL DIM / CAL BRIGHT
# values stay roughly meaningful — the busy-loop ran at ~1 us / iter
# on this Pi, so old "counts" and new microseconds are similar in
# magnitude — but tap the buttons again to re-anchor exactly.
TIMEOUT_COUNT = 200_000     # 200 ms upper bound (covers no-LDR case)
SAMPLES_PER_TICK = 5

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
# 30 % deadband (was 15 %): per-sample noise plus the asymmetric
# EMA's slow release would otherwise leak across the threshold and
# pull the panel off zero in steady ambient. Wider window + cleaner
# per-tick reads (median of N) keep the dim state genuinely stable.
DEADBAND_FRAC = 0.70

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

# Rolling 24 h sensor log. One row every LOG_INTERVAL_S — at 5 s
# that's 17 280 rows / ~1.4 MB per day, which we trim back to the
# retention window every LOG_PRUNE_INTERVAL_S. Used purely for off-
# line analysis (calibration tuning, drift inspection) — the running
# service never reads its own log back.
LOG_INTERVAL_S = 5.0
LOG_RETENTION_S = 24 * 3600
LOG_PRUNE_INTERVAL_S = 3600
LOG_HEADER = "ts,raw,smooth,gain,dim_ref,bright_ref\n"


@dataclass(frozen=True)
class LightStatus:
    available: bool = False
    raw_count: int = 0
    smooth_count: float = 0.0
    gain: float = 0.0


class LightService:
    def __init__(self,
                 get_dim_ref: Optional[Callable[[], int]] = None,
                 get_bright_ref: Optional[Callable[[], int]] = None,
                 log_path: Optional[Path] = None) -> None:
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
        # 24 h CSV of timestamped readings — passed in (or None to
        # disable). Header is written lazily on first append so a
        # missing-then-restored data dir self-heals.
        self._log_path = log_path
        self._last_log_t = 0.0
        self._last_prune_t = 0.0

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

            self._maybe_log(count, smooth, gain, now)

            elapsed = now - t0
            self._stop.wait(max(0.0, POLL_S - elapsed))

    @staticmethod
    def _read_one_us() -> int:
        """One discharge/charge cycle. Returns elapsed microseconds
        until the input crosses HIGH (or TIMEOUT_COUNT if it never
        does within the upper bound).

        Wall-clock timing rather than iteration count — Python's
        busy-loop runs at ~1 us / iter on this Pi but is preempted
        unpredictably by the kernel. perf_counter_ns measures the
        *real* charge interval, so a kernel pause no longer makes
        the reading look brighter than the actual illumination.
        """
        # Phase 1: drain the cap by driving the pin LOW as an output.
        GPIO.setup(PIN, GPIO.OUT)
        GPIO.output(PIN, GPIO.LOW)
        time.sleep(DISCHARGE_S)
        # Phase 2: high-Z input — cap charges through the LDR.
        GPIO.setup(PIN, GPIO.IN)
        # Phase 3: time the rise to HIGH using a wall-clock deadline.
        start_ns = time.perf_counter_ns()
        deadline_ns = start_ns + TIMEOUT_COUNT * 1000
        while GPIO.input(PIN) == GPIO.LOW:
            if time.perf_counter_ns() > deadline_ns:
                return TIMEOUT_COUNT
        return max(0, (time.perf_counter_ns() - start_ns) // 1000)

    def _maybe_log(self, raw: int, smooth: Optional[float],
                   gain: float, now_mono: float) -> None:
        if self._log_path is None:
            return
        if now_mono - self._last_log_t < LOG_INTERVAL_S:
            return
        self._last_log_t = now_mono
        ts = int(time.time())
        smooth_i = int(smooth) if smooth is not None else 0
        row = (f"{ts},{int(raw)},{smooth_i},{gain:.3f},"
               f"{int(self._get_dim_ref())},"
               f"{int(self._get_bright_ref())}\n")
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self._log_path.exists()
            with self._log_path.open("a") as f:
                if new_file:
                    f.write(LOG_HEADER)
                f.write(row)
        except OSError as exc:
            # Don't kill the polling thread over a disk hiccup —
            # logs are a side project, the sensor must keep working.
            print(f"light: log write failed: {exc}", flush=True)
            return
        if now_mono - self._last_prune_t >= LOG_PRUNE_INTERVAL_S:
            self._last_prune_t = now_mono
            self._prune_log()

    def _prune_log(self) -> None:
        if self._log_path is None or not self._log_path.exists():
            return
        cutoff = int(time.time()) - LOG_RETENTION_S
        try:
            with self._log_path.open() as f:
                lines = f.readlines()
        except OSError as exc:
            print(f"light: log read failed: {exc}", flush=True)
            return
        # Preserve the header (any line that doesn't parse as an int
        # timestamp), keep rows whose ts is within retention.
        kept: list[str] = []
        for line in lines:
            first = line.split(",", 1)[0].strip()
            if not first or not first.lstrip("-").isdigit():
                kept.append(line)
                continue
            if int(first) >= cutoff:
                kept.append(line)
        if len(kept) == len(lines):
            return
        try:
            tmp = self._log_path.with_suffix(".tmp")
            with tmp.open("w") as f:
                f.writelines(kept)
            tmp.replace(self._log_path)
        except OSError as exc:
            print(f"light: log prune failed: {exc}", flush=True)

    @classmethod
    def _read_count(cls) -> int:
        """Median of SAMPLES_PER_TICK back-to-back reads. The median
        rejects single-cycle outliers — kernel preemption during one
        cycle, a brief shadow over the LDR, ESD blips on the GPIO —
        without slowing the response, since all samples are taken in
        the same tick (~30 ms total at indoor light levels)."""
        samples = sorted(cls._read_one_us()
                         for _ in range(SAMPLES_PER_TICK))
        return samples[SAMPLES_PER_TICK // 2]

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
