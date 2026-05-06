"""One-off: filter HYG v4.2 to bright stars and extract constellation
polylines from Stellarium's western/index.json.

Outputs go into app/data/:
    stars.csv          hip,ra_deg,dec_deg,mag,spect_class
    constellations.csv polyline_id,hip   (one row per vertex; consecutive
                       same-id rows form a polyline)

We keep stars that are referenced by constellations (regardless of
magnitude) plus any other stars to mag <= 5.5. That way every polyline
endpoint is guaranteed to resolve, even if a constellation pattern names
a magnitude-6 star that would otherwise be cut.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
HYG_CSV = HERE / "hyg_v42.csv"
WESTERN_JSON = HERE / "western_index.json"
OUT_DIR = HERE.parent / "app" / "data"
STARS_OUT = OUT_DIR / "stars.csv"
CONST_OUT = OUT_DIR / "constellations.csv"

MAG_LIMIT = 5.5


def load_constellation_hips() -> tuple[set[int], list[list[int]]]:
    """Return (hip_set, polylines). hip_set is every star referenced by
    any line; polylines is a flat list of HIP polyline lists."""
    data = json.loads(WESTERN_JSON.read_text(encoding="utf-8"))
    hips: set[int] = set()
    polylines: list[list[int]] = []
    for con in data["constellations"]:
        for line in con.get("lines", []):
            # Some polylines have a leading "thin"/"thick" hint string —
            # we ignore it and just keep the integer HIP sequence.
            poly = [int(h) for h in line if isinstance(h, int)]
            if len(poly) < 2:
                continue
            polylines.append(poly)
            hips.update(poly)
    return hips, polylines


def main() -> int:
    if not HYG_CSV.exists() or not WESTERN_JSON.exists():
        print("source files missing", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    needed_hips, polylines = load_constellation_hips()
    print(f"constellation hips: {len(needed_hips)}, polylines: {len(polylines)}")

    kept: dict[int, tuple[float, float, float, str]] = {}
    with HYG_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            hip_s = row.get("hip") or ""
            if not hip_s:
                continue
            try:
                hip = int(hip_s)
            except ValueError:
                continue
            try:
                mag = float(row.get("mag") or "99")
                ra_h = float(row.get("ra") or "0")    # in hours
                dec = float(row.get("dec") or "0")    # degrees
            except ValueError:
                continue
            ra_deg = ra_h * 15.0
            spect = (row.get("spect") or "")[:1]
            if mag <= MAG_LIMIT or hip in needed_hips:
                kept[hip] = (ra_deg, dec, mag, spect)

    # Make sure every polyline endpoint is in the kept set. If a HIP is
    # missing (bad data / catalogue gap), drop the whole polyline rather
    # than render it as a stub.
    valid_polys: list[list[int]] = []
    for poly in polylines:
        if all(h in kept for h in poly):
            valid_polys.append(poly)
        else:
            missing = [h for h in poly if h not in kept]
            print(f"  dropping polyline with missing HIPs: {missing}",
                  file=sys.stderr)

    with STARS_OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hip", "ra_deg", "dec_deg", "mag", "spect"])
        for hip in sorted(kept):
            ra, dec, mag, spect = kept[hip]
            w.writerow([hip, f"{ra:.5f}", f"{dec:.5f}", f"{mag:.2f}", spect])

    with CONST_OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["polyline_id", "hip"])
        for pid, poly in enumerate(valid_polys):
            for h in poly:
                w.writerow([pid, h])

    print(f"wrote {STARS_OUT.name}: {len(kept)} stars")
    print(f"wrote {CONST_OUT.name}: {len(valid_polys)} polylines, "
          f"{sum(len(p) for p in valid_polys)} vertices")
    print(f"stars file size: {STARS_OUT.stat().st_size} bytes")
    print(f"const file size: {CONST_OUT.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
