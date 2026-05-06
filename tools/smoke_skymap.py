"""Render a few skymap frames offline so I can eyeball them and catch
import / arithmetic / drawing bugs before deploy."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Run from anywhere — make sure we can import the app package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from skymap_service import SkymapService  # type: ignore  # noqa: E402


def main() -> int:
    svc = SkymapService(location_path=None)
    svc._lat = 51.5
    svc._lon = -0.1

    cases = [
        ("london_winter_night",
         datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)),
        ("london_summer_morning",
         datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)),
        ("london_now",
         datetime.now(timezone.utc)),
    ]
    out_dir = Path(__file__).resolve().parent / "smoke_out"
    out_dir.mkdir(exist_ok=True)
    for name, when in cases:
        img = svc.render(720, 800, now=when, draw_constellations=True,
                         planet_labels=True)
        out = out_dir / f"{name}.png"
        img.save(out)
        print(f"  {name}: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
