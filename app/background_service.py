"""Background mode + overlay toggles — what (if anything) is composited
behind scene widgets. Persisted as JSON; default 'none' so the visual
stays unchanged until the user opts in via the picker.

Three independent dimensions:
- mode: a single base style (none / world_map_slate / atlas / vintage /
  blueprint). One choice at a time.
- overlays: zero or more layers painted on top of the base map (city
  lights, water features, political borders, clouds, annotations).
  Independent toggles, can stack.
- center_lon: the longitude (degrees) shown at the horizontal centre
  of the map. Defaults to 0 (Greenwich). Decoupled from system
  timezone — a user in Tokyo may still prefer a London-centred map.

Overlay + center_lon only apply when a world_map style is selected —
when mode == "none" the renderer ignores them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


VALID_MODES = (
    "none",
    "world_map_slate",
    "world_map_atlas",
    "world_map_vintage",
    "world_map_blueprint",
    "world_map_globe",
)

# Older configs wrote the bare "world_map" string before styles existed.
LEGACY_MODE_MAP = {
    "world_map": "world_map_slate",
}

# Overlays the user can stack on a world-map background. Listed in
# picker / render order — picker shows them top-to-bottom in this
# sequence, and the renderer applies them in this order too.
VALID_OVERLAYS = (
    "city_lights",
    "water",
    "political",
    "clouds",
    "annotations",
)

# Older configs may have saved overlays that have since been retired or
# renamed. On load we silently drop unknown keys; this keeps the file
# stable rather than rewriting users' choices behind their back.
RETIRED_OVERLAYS = ("timezones",)


class BackgroundService:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mode, self._overlays, self._center_lon = self._load()

    def _load(self) -> tuple[str, dict[str, bool], float]:
        mode = "none"
        overlays = {k: False for k in VALID_OVERLAYS}
        center_lon = 0.0
        try:
            d = json.loads(self.path.read_text())
            m = d.get("mode", "none")
            m = LEGACY_MODE_MAP.get(m, m)
            if m in VALID_MODES:
                mode = m
            saved = d.get("overlays") or {}
            for k in VALID_OVERLAYS:
                overlays[k] = bool(saved.get(k, False))
            cl = d.get("center_lon", 0.0)
            try:
                center_lon = _normalise_lon(float(cl))
            except (TypeError, ValueError):
                center_lon = 0.0
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return mode, overlays, center_lon

    def _save(self) -> None:
        d = {
            "mode": self._mode,
            "overlays": dict(self._overlays),
            "center_lon": self._center_lon,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, self.path)

    @property
    def mode(self) -> str:
        return self._mode

    def style_name(self) -> str | None:
        """The world-map style name (slate/atlas/vintage/blueprint), or
        None if the current mode isn't a world map."""
        if self._mode.startswith("world_map_"):
            return self._mode[len("world_map_"):]
        return None

    def set_mode(self, mode: str) -> None:
        if mode not in VALID_MODES or mode == self._mode:
            return
        self._mode = mode
        self._save()

    def is_overlay(self, name: str) -> bool:
        return self._overlays.get(name, False)

    def toggle_overlay(self, name: str) -> bool:
        """Flip a toggle. Returns the new value (False if invalid)."""
        if name not in VALID_OVERLAYS:
            return False
        self._overlays[name] = not self._overlays[name]
        self._save()
        return self._overlays[name]

    def active_overlays(self) -> tuple[str, ...]:
        """Sorted tuple of currently-enabled overlay names — used as a
        cache key by the renderer so a toggle-flip invalidates."""
        return tuple(k for k in VALID_OVERLAYS if self._overlays[k])

    @property
    def center_lon(self) -> float:
        return self._center_lon

    def set_center_lon(self, lon: float) -> None:
        n = _normalise_lon(float(lon))
        if abs(n - self._center_lon) < 1e-3:
            return
        self._center_lon = n
        self._save()


def _normalise_lon(lon: float) -> float:
    """Wrap to (-180, 180]."""
    while lon <= -180:
        lon += 360
    while lon > 180:
        lon -= 360
    return lon
