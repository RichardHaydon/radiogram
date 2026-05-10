"""Brightness preferences — active + idle dim, persisted as JSON.

Stored in percent (10..100 active, 0..50 dim) rather than raw sysfs values
so the same config still makes sense if the underlying max_brightness ever
changes (kernel update, hardware swap). Pure data — the main loop reads
.config every frame and computes the actual sysfs level itself.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path


# Ordered ladders rather than a fixed step. Dim crowds levels near zero
# because the useful bedside range (just-visible glow at night) lives in
# the bottom 5% — a uniform 10% step skips right over it. Active gets
# the same low entries (1, 2, 3, 5) so the user can dial active mode
# right down for late-night interaction without flipping into idle dim.
# Levels below the panel's hardware backlight floor (~3% on Pi 7") rely
# on the software RGB multiplier in clockradio.Display to actually
# achieve the perceived intensity — without it 1/2/3 would all just
# round down to backlight=0 (off).
ACTIVE_LEVELS: tuple[int, ...] = (
    1, 2, 3, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100,
)
DIM_LEVELS: tuple[int, ...] = (0, 1, 2, 3, 5, 8, 12, 18, 25, 35, 50)

# Sensible bounds when the user calibrates the light sensor. A captured
# count below 10 would clamp the boost ramp to a near-zero range and
# make every read look "bright"; above 200000 is the LDR/photodiode
# timeout sentinel and would flatline the gain at zero.
LIGHT_DIM_REF_MIN = 10
LIGHT_DIM_REF_MAX = 199000
LIGHT_DIM_REF_DEFAULT = 800
LIGHT_BRIGHT_REF_MIN = 1
LIGHT_BRIGHT_REF_MAX = 199000
LIGHT_BRIGHT_REF_DEFAULT = 50


@dataclass(frozen=True)
class BrightnessConfig:
    active_pct: int = 100
    dim_pct: int = 5
    # Bedside "night red" mode. When True, the rendered image is
    # tinted toward deep red before being written to the framebuffer:
    # red preserved, green strongly suppressed, blue near-zero.
    # Preserves dark adaptation and minimises melatonin disruption
    # the way an astronomer's red filter does.
    night_red: bool = False
    # Auto-brightness from the LDR on GPIO17. The user's percent
    # settings represent comfort in a *dim* room; when ambient is
    # brighter, the LightService gain lifts the panel toward 100%.
    # Default ON — the feature is the whole point of the sensor.
    auto_ambient: bool = True
    # Sensor calibration anchors. Both are user-settable from
    # BrightnessScene: tap CAL DIM in the dim ambient you want
    # honoured (no boost above this count), tap CAL BRIGHT under
    # full lighting where the panel should be at 100 %. The user
    # sees the live values on the buttons so it's obvious what was
    # captured and what's currently in effect.
    light_dim_ref: int = LIGHT_DIM_REF_DEFAULT
    light_bright_ref: int = LIGHT_BRIGHT_REF_DEFAULT


class BrightnessService:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cfg = self._load()

    def _load(self) -> BrightnessConfig:
        try:
            d = json.loads(self.path.read_text())
            return BrightnessConfig(
                active_pct=_snap(int(d.get("active_pct", 100)),
                                 ACTIVE_LEVELS),
                dim_pct=_snap(int(d.get("dim_pct", 5)), DIM_LEVELS),
                night_red=bool(d.get("night_red", False)),
                auto_ambient=bool(d.get("auto_ambient", True)),
                light_dim_ref=_clamp_dim_ref(
                    int(d.get("light_dim_ref", LIGHT_DIM_REF_DEFAULT))),
                light_bright_ref=_clamp_bright_ref(
                    int(d.get("light_bright_ref",
                              LIGHT_BRIGHT_REF_DEFAULT))),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return BrightnessConfig()

    def _save(self) -> None:
        d = {"active_pct": self._cfg.active_pct,
             "dim_pct": self._cfg.dim_pct,
             "night_red": self._cfg.night_red,
             "auto_ambient": self._cfg.auto_ambient,
             "light_dim_ref": self._cfg.light_dim_ref,
             "light_bright_ref": self._cfg.light_bright_ref}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, self.path)

    @property
    def config(self) -> BrightnessConfig:
        return self._cfg

    def step_active(self, direction: int) -> None:
        new = _step(self._cfg.active_pct, direction, ACTIVE_LEVELS)
        if new != self._cfg.active_pct:
            self._cfg = dataclasses.replace(self._cfg, active_pct=new)
            self._save()

    def step_dim(self, direction: int) -> None:
        new = _step(self._cfg.dim_pct, direction, DIM_LEVELS)
        if new != self._cfg.dim_pct:
            self._cfg = dataclasses.replace(self._cfg, dim_pct=new)
            self._save()

    def toggle_night_red(self) -> bool:
        self._cfg = dataclasses.replace(
            self._cfg, night_red=not self._cfg.night_red)
        self._save()
        return self._cfg.night_red

    def toggle_auto_ambient(self) -> bool:
        self._cfg = dataclasses.replace(
            self._cfg, auto_ambient=not self._cfg.auto_ambient)
        self._save()
        return self._cfg.auto_ambient

    def set_light_dim_ref(self, value: int) -> int:
        """Capture a calibration sample as the new dim-room anchor.

        Caller should pass the current smoothed sensor count (from
        LightService.status.smooth_count). Out-of-range or zero values
        are ignored — a zero often just means the sensor hasn't
        produced a usable reading yet."""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return self._cfg.light_dim_ref
        if v <= 0:
            return self._cfg.light_dim_ref
        v = _clamp_dim_ref(v)
        if v != self._cfg.light_dim_ref:
            self._cfg = dataclasses.replace(self._cfg, light_dim_ref=v)
            self._save()
        return self._cfg.light_dim_ref

    def set_light_bright_ref(self, value: int) -> int:
        """Capture a calibration sample as the bright-room anchor (full
        boost below this count). Same validation as the dim setter."""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return self._cfg.light_bright_ref
        if v <= 0:
            return self._cfg.light_bright_ref
        v = _clamp_bright_ref(v)
        if v != self._cfg.light_bright_ref:
            self._cfg = dataclasses.replace(self._cfg, light_bright_ref=v)
            self._save()
        return self._cfg.light_bright_ref


def _snap(v: int, levels: tuple[int, ...]) -> int:
    """Closest allowed level — used when loading config that may have
    been written under an older ladder."""
    return min(levels, key=lambda lv: abs(lv - v))


def _clamp_dim_ref(v: int) -> int:
    return max(LIGHT_DIM_REF_MIN, min(LIGHT_DIM_REF_MAX, int(v)))


def _clamp_bright_ref(v: int) -> int:
    return max(LIGHT_BRIGHT_REF_MIN, min(LIGHT_BRIGHT_REF_MAX, int(v)))


def _step(current: int, direction: int, levels: tuple[int, ...]) -> int:
    """Move one position along `levels` in `direction` (+1 up, −1 down).
    If `current` isn't in the list, snap then step."""
    if current not in levels:
        current = _snap(current, levels)
    idx = levels.index(current)
    if direction > 0:
        return levels[min(idx + 1, len(levels) - 1)]
    if direction < 0:
        return levels[max(idx - 1, 0)]
    return current
