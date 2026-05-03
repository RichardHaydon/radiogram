"""One-time build: rasterize Natural Earth land + geography polygons,
downsample the 50m shaded-relief raster, and downsample NASA's Black
Marble city-lights image into the bundled PNGs that the runtime
composites for the world-map background.

Run this whenever any source data changes. Output PNGs ship under
app/data/ and are bundled into /opt/clockradio/app/data/ by the
bootstrap script.

Usage:
    python scripts/build_world_overlays.py

Reads (downloads + caches under app/data/):
    ne_50m_land.geojson                            — coastline polygons
    ne_50m_geography_regions_polys.geojson          — desert + range polygons
    ne_50m_lakes.geojson                            — lake polygons
    ne_50m_rivers_lake_centerlines.geojson          — river centerlines
    ne_50m_admin_0_boundary_lines_land.geojson      — country borders
    SR_50M.zip                                      — NE shaded relief
    lights_orig.jpg                                 — NASA Black Marble
Writes:
    app/data/world_land.png       (720x360, 1-bit) — land mask
    app/data/world_deserts.png    (720x360, 1-bit) — desert polygons
    app/data/world_mountains.png  (720x360, 1-bit) — mountain ranges
    app/data/world_lakes.png      (720x360, 1-bit) — lake polygons
    app/data/world_rivers.png     (1440x720, 1-bit) — major rivers (lines)
    app/data/world_borders.png    (1440x720, 1-bit) — country borders (lines)
    app/data/world_relief.png     (1440x720, L)    — NW-lit shaded relief
    app/data/world_lights.png     (1440x720, RGB)  — NASA city lights
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None


# Polygon masks at 1440x720 — bumped from 720x360 because 0.5° per
# pixel makes small islands and tight coastlines (UK, Japan, Indonesia,
# Aegean) look chunky once the runtime resamples them up to canvas
# size. 0.25° per pixel is roughly the canvas pixel density at 1280x720
# so the runtime no longer has to invent edges.
MASK_W, MASK_H = 1440, 720
# Line features (rivers, borders) and texture rasters (relief, lights)
# now match polygon mask resolution. Kept as a separate constant so
# they can be bumped further independently if needed.
HI_W, HI_H = 1440, 720

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "app" / "data"

GEO_BASE = ("https://raw.githubusercontent.com/nvkelso/"
            "natural-earth-vector/master/geojson/")

LAND_GEOJSON = DATA / "ne_50m_land.geojson"
LAND_URL = GEO_BASE + "ne_50m_land.geojson"
REGIONS_GEOJSON = DATA / "ne_50m_geography_regions_polys.geojson"
REGIONS_URL = GEO_BASE + "ne_50m_geography_regions_polys.geojson"
LAKES_GEOJSON = DATA / "ne_50m_lakes.geojson"
LAKES_URL = GEO_BASE + "ne_50m_lakes.geojson"
RIVERS_GEOJSON = DATA / "ne_50m_rivers_lake_centerlines.geojson"
RIVERS_URL = GEO_BASE + "ne_50m_rivers_lake_centerlines.geojson"
BORDERS_GEOJSON = DATA / "ne_50m_admin_0_boundary_lines_land.geojson"
BORDERS_URL = GEO_BASE + "ne_50m_admin_0_boundary_lines_land.geojson"
RELIEF_ZIP = DATA / "SR_50M.zip"
RELIEF_URL = "https://naciscdn.org/naturalearth/50m/raster/SR_50M.zip"
LIGHTS_ORIG = DATA / "lights_orig.jpg"
LIGHTS_URL = ("https://eoimages.gsfc.nasa.gov/images/imagerecords/"
              "79000/79765/dnb_land_ocean_ice.2012.3600x1800.jpg")

# Mountain MIN_LABEL cutoff — see WorldMapService docs.
MOUNTAIN_MIN_LABEL = 5.0
# River scalerank cutoff — only rivers ranking <= this are drawn.
# Natural Earth's 50m river dataset has ranks 1-9; <=6 keeps Amazon,
# Nile, Mississippi, Yangtze, Volga, Congo, Ganges, Danube, Murray, etc.
# while excluding minor tributaries that just clutter at this scale.
RIVER_SCALERANK = 6


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    print(f"  downloading {dest.name} ...")
    DATA.mkdir(parents=True, exist_ok=True)
    urlretrieve(url, dest)


def lonlat_to_xy(lon: float, lat: float,
                 w: int, h: int) -> tuple[float, float]:
    return (lon + 180.0) / 360.0 * w, (90.0 - lat) / 180.0 * h


def rasterize_polygons(features, w: int, h: int) -> Image.Image:
    """1-bit mask from polygon outer rings."""
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        polys = (coords if gtype == "MultiPolygon"
                 else [coords] if gtype == "Polygon" else [])
        for poly in polys:
            ring = poly[0] if poly else []
            if len(ring) < 3:
                continue
            pts = [lonlat_to_xy(lon, lat, w, h) for lon, lat in ring]
            draw.polygon(pts, fill=255)
    return img.convert("1")


def rasterize_lines(features, w: int, h: int,
                    line_width: int = 1) -> Image.Image:
    """1-bit mask from LineString / MultiLineString features.
    Anti-meridian crossings are not split — a polyline that wraps from
    +179 to -179 will draw a long horizontal line across the canvas.
    Skip segments that span more than 180° to suppress that artifact."""
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        lines = (coords if gtype == "MultiLineString"
                 else [coords] if gtype == "LineString" else [])
        for line in lines:
            if len(line) < 2:
                continue
            # Split on anti-meridian crossings: any segment where lon
            # jumps by more than 180° starts a new sub-polyline.
            sub: list[tuple[float, float]] = []
            prev_lon = None
            for lon, lat in line:
                x, y = lonlat_to_xy(lon, lat, w, h)
                if prev_lon is not None and abs(lon - prev_lon) > 180:
                    if len(sub) >= 2:
                        draw.line(sub, fill=255, width=line_width)
                    sub = [(x, y)]
                else:
                    sub.append((x, y))
                prev_lon = lon
            if len(sub) >= 2:
                draw.line(sub, fill=255, width=line_width)
    return img.convert("1")


def build_land() -> None:
    print("building world_land.png ...")
    fetch(LAND_URL, LAND_GEOJSON)
    with LAND_GEOJSON.open(encoding="utf-8") as f:
        feats = json.load(f)["features"]
    print(f"  {len(feats)} land features")
    out = DATA / "world_land.png"
    rasterize_polygons(feats, MASK_W, MASK_H).save(out, optimize=True)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")


def build_terrain_masks() -> None:
    print("building deserts + mountains ...")
    fetch(REGIONS_URL, REGIONS_GEOJSON)
    with REGIONS_GEOJSON.open(encoding="utf-8") as f:
        feats = json.load(f)["features"]
    deserts = [f for f in feats
               if f["properties"].get("FEATURECLA") == "Desert"]
    ranges = [f for f in feats
              if f["properties"].get("FEATURECLA") == "Range/mtn"
              and (f["properties"].get("MIN_LABEL") or 99)
              <= MOUNTAIN_MIN_LABEL]
    print(f"  {len(deserts)} deserts, {len(ranges)} ranges")
    rasterize_polygons(deserts, MASK_W, MASK_H).save(
        DATA / "world_deserts.png", optimize=True)
    rasterize_polygons(ranges, MASK_W, MASK_H).save(
        DATA / "world_mountains.png", optimize=True)


def build_lakes() -> None:
    print("building world_lakes.png ...")
    fetch(LAKES_URL, LAKES_GEOJSON)
    with LAKES_GEOJSON.open(encoding="utf-8") as f:
        feats = json.load(f)["features"]
    print(f"  {len(feats)} lakes")
    out = DATA / "world_lakes.png"
    rasterize_polygons(feats, MASK_W, MASK_H).save(out, optimize=True)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")


def build_rivers() -> None:
    print("building world_rivers.png ...")
    fetch(RIVERS_URL, RIVERS_GEOJSON)
    with RIVERS_GEOJSON.open(encoding="utf-8") as f:
        feats = json.load(f)["features"]
    rivers = [f for f in feats
              if (f["properties"].get("scalerank") or 99)
              <= RIVER_SCALERANK]
    print(f"  {len(rivers)} rivers (scalerank <= {RIVER_SCALERANK})")
    out = DATA / "world_rivers.png"
    rasterize_lines(rivers, HI_W, HI_H, line_width=1).save(
        out, optimize=True)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")


def build_borders() -> None:
    print("building world_borders.png ...")
    fetch(BORDERS_URL, BORDERS_GEOJSON)
    with BORDERS_GEOJSON.open(encoding="utf-8") as f:
        feats = json.load(f)["features"]
    print(f"  {len(feats)} border segments")
    out = DATA / "world_borders.png"
    rasterize_lines(feats, HI_W, HI_H, line_width=1).save(
        out, optimize=True)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")


def build_relief() -> None:
    print("building world_relief.png ...")
    fetch(RELIEF_URL, RELIEF_ZIP)
    with zipfile.ZipFile(RELIEF_ZIP) as z:
        for member in z.namelist():
            if member.endswith(".tif"):
                z.extract(member, path=DATA)
                tif = DATA / member
                break
        else:
            raise RuntimeError("no TIFF inside SR_50M.zip")
    img = Image.open(tif).convert("L")
    out = DATA / "world_relief.png"
    img.resize((HI_W, HI_H), Image.LANCZOS).save(out, optimize=True)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")
    tif.unlink()


def build_lights() -> None:
    print("building world_lights.png ...")
    fetch(LIGHTS_URL, LIGHTS_ORIG)
    img = Image.open(LIGHTS_ORIG).convert("RGB")
    print(f"  source size: {img.size}")
    out = DATA / "world_lights.png"
    img.resize((HI_W, HI_H), Image.LANCZOS).save(out, optimize=True)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    build_land()
    build_terrain_masks()
    build_lakes()
    build_rivers()
    build_borders()
    build_relief()
    build_lights()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
