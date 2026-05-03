"""Render every world-map style to a PNG so we can inspect them
without deploying to the Pi. Used as a tight feedback loop for tuning
palettes, blur, and blend factors.

Usage: python scripts/render_map_styles.py
Outputs: tmp/map_previews/{slate,atlas,vintage,blueprint}.png
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from world_map_service import STYLES, WorldMapService  # noqa: E402


CANVAS_W, CANVAS_H = 720, 800
OUT = ROOT / "tmp" / "map_previews"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    svc = WorldMapService(CANVAS_W, CANVAS_H)
    print(f"land mask: {svc._land_mask is not None}")
    if svc._desert_alpha is not None:
        print(f"desert alpha: max={float(svc._desert_alpha.max()):.3f} "
              f"mean(land)={float(svc._desert_alpha[svc._land_mask].mean()):.3f}")
    if svc._mountain_alpha is not None:
        print(f"mountain alpha: max={float(svc._mountain_alpha.max()):.3f} "
              f"mean(land)={float(svc._mountain_alpha[svc._land_mask].mean()):.3f}")
    for name in STYLES:
        img = svc.current_image(theme=None, style_name=name)
        p = OUT / f"{name}.png"
        img.save(p)
        print(f"wrote {p}  ({p.stat().st_size} bytes)")
    # Atlas with each overlay individually + all together — for tuning.
    overlay_combos = [
        ("city_lights",),
        ("water",),
        ("political",),
        ("timezones",),
        ("city_lights", "water", "political", "timezones"),
    ]
    for combo in overlay_combos:
        tag = "atlas_" + "+".join(combo).replace("_", "")
        img = svc.current_image(theme=None, style_name="atlas",
                                overlays=combo)
        p = OUT / f"{tag}.png"
        img.save(p)
        print(f"wrote {p}  ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
