"""Brightness preferences — active + idle dim, persisted as JSON.

Stored in percent (10..100 active, 0..50 dim) rather than raw sysfs values
so the same config still makes sense if the underlying max_brightness ever
changes (kernel update, hardware swap). Pure data — the main loop reads
.config every frame and computes the actual sysfs level itself.
"""
from __future__ import annotations

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
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return BrightnessConfig()

    def _save(self) -> None:
        d = {"active_pct": self._cfg.active_pct,
             "dim_pct": self._cfg.dim_pct,
             "night_red": self._cfg.night_red}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, self.path)

    @property
    def config(self) -> BrightnessConfig:
        return self._cfg

    def step_active(self, direction: int) -> None:
        new = _step(self._cfg.active_pct, direction, ACTIVE_LEVELS)
        if new != self._cfg.active_pct:
            self._cfg = BrightnessConfig(
                active_pct=new, dim_pct=self._cfg.dim_pct,
                night_red=self._cfg.night_red)
            self._save()

    def step_dim(self, direction: int) -> None:
        new = _step(self._cfg.dim_pct, direction, DIM_LEVELS)
        if new != self._cfg.dim_pct:
            self._cfg = BrightnessConfig(
                active_pct=self._cfg.active_pct, dim_pct=new,
                night_red=self._cfg.night_red)
            self._save()

    def toggle_night_red(self) -> bool:
        self._cfg = BrightnessConfig(
            active_pct=self._cfg.active_pct,
            dim_pct=self._cfg.dim_pct,
            night_red=not self._cfg.night_red,
        )
        self._save()
        return self._cfg.night_red


def _snap(v: int, levels: tuple[int, ...]) -> int:
    """Closest allowed level — used when loading config that may have
    been written under an older ladder."""
    return min(levels, key=lambda lv: abs(lv - v))


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
