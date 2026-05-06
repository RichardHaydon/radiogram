"""Offline night-sky render on the Pi for visual verification."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/clockradio/app")
from skymap_service import SkymapService

svc = SkymapService(location_path=Path("/var/lib/clockradio/location.json"))
# Mid-May Oslo: actual astronomical night barely happens (latitude is
# already in the "white nights" band — sun stays above -18° most of
# the night). Render at 02:00 local (00:00 UTC) — the darkest moment.
img = svc.render(
    720, 1280,
    now=datetime(2026, 5, 7, 0, 0, tzinfo=timezone.utc),
    draw_constellations=True,
    planet_labels=True,
)
img.save("/tmp/sky_oslo_night.png")
print(f"saved /tmp/sky_oslo_night.png {img.size}")

# Also render Jan deep-night (Oslo, 22:00 local = 21:00 UTC).
img2 = svc.render(
    720, 1280,
    now=datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc),
    draw_constellations=True,
    planet_labels=True,
)
img2.save("/tmp/sky_oslo_winter.png")
print(f"saved /tmp/sky_oslo_winter.png {img2.size}")
