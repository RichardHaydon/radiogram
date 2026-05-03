"""World map renderer — equirectangular continents + day/night terminator.

Outputs a canvas-sized PIL.Image. Recomputes once per minute (sun moves
<0.25°/min — sub-minute refresh would just burn CPU for no visible
change). Multi-style: pick a MapStyle and the renderer paints a wall-map
atlas, antique parchment, blueprint drawing, or the original slate look.
Each style is theme-independent — the map owns its own palette so the
foreground UI keeps predictable contrast regardless of theme choice.

The land mask is a bundled 1-bit PNG (data/world_land.png) rasterised
from Natural Earth's ne_110m_land. Resized once to canvas size at init
and held in memory as a bool numpy array.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


DATA_DIR = Path(__file__).resolve().parent / "data"
LAND_PNG = DATA_DIR / "world_land.png"
DESERT_PNG = DATA_DIR / "world_deserts.png"
MOUNTAIN_PNG = DATA_DIR / "world_mountains.png"
RELIEF_PNG = DATA_DIR / "world_relief.png"
LAKES_PNG = DATA_DIR / "world_lakes.png"
RIVERS_PNG = DATA_DIR / "world_rivers.png"
BORDERS_PNG = DATA_DIR / "world_borders.png"
LIGHTS_PNG = DATA_DIR / "world_lights.png"


# --- styles ---------------------------------------------------------

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class MapStyle:
    """Visual recipe for one map flavor.

    bg is the canvas backdrop colour (also the colour the night side
    fades toward). ocean/land are the day-side fills. coast is a
    slightly lifted shade applied to land pixels that touch ocean —
    gives the map a drawn-coastline feel rather than a flat fill.
    desert/mountain, if set, tint land pixels that fall inside the
    bundled desert + mountain-range masks (Natural Earth 50m polygons)
    by `terrain_blend` toward those colours, giving the map a "subtle
    relief" feel without an actual elevation raster. None = don't paint
    that layer for this style.
    night_floor (0..1) is the minimum brightness in the dark hemisphere
    so coastlines stay legible past sunset. sun_glow (0..1) adds a
    warm radial lift centered on the subsolar point — subtle "where the
    sun is right now" cue. graticule, if set, draws faint 30° lat/lon
    lines on top in this colour for the engineering-drawing look.
    """
    name: str
    bg: RGB
    ocean: RGB
    land: RGB
    coast: RGB
    night_floor: float
    sun_glow: float = 0.0
    graticule: RGB | None = None
    desert: RGB | None = None
    mountain: RGB | None = None
    terrain_blend: float = 0.55  # peak strength of desert/mountain tint
    # Polar caps. ice colour is the "snow/ice" tint; ice_strength is a
    # multiplier on the latitude-based alpha (0 = no caps, 1 = full).
    ice: RGB | None = None
    ice_strength: float = 1.0
    # Shaded-relief intensity. 0 = flat colours; 1 = full multiply
    # blend (highlights brighten 2x, shadows go to black). Real wall
    # maps live around 0.3-0.5 — visible 3D form without flattening
    # the chosen palette.
    relief_strength: float = 0.40
    # Overlay tints. Each is the colour the overlay paints in this
    # style; None falls back to a sensible default. border_alpha is
    # 0..1 — political borders tend to look loud at full opacity, so
    # most styles use 0.4-0.6.
    river_color: RGB | None = None
    border_color: RGB | None = None
    border_alpha: float = 0.55
    # Cartographic annotation overlay — colour for tropics, polar
    # circles, equator, and the day/night terminator. None = fall back
    # to the graticule colour or a softened version of `coast`.
    annotation_color: RGB | None = None
    # Day-side brightness lift — multiplicative gain applied where the
    # sun is up so the lit hemisphere reads as actual daytime rather
    # than a slightly-less-dark version of the night side. 0 = no lift,
    # 0.20 ≈ "noon glare" peak. Falls off through the twilight band the
    # same way `day` does, so the boost dies smoothly at the terminator.
    day_lift: float = 0.0


SLATE = MapStyle(
    name="slate",
    bg=(8, 12, 22),
    ocean=(24, 40, 70),
    land=(70, 90, 120),
    coast=(115, 135, 165),
    night_floor=0.15,
    desert=(125, 125, 145),
    mountain=(110, 130, 160),
    terrain_blend=0.25,
    # Polar caps disabled — Greenland/Antarctica looked like a stuck
    # white blob. The Arctic Circle line in the annotations overlay
    # carries the high-latitude reference now.
    ice=None,
    relief_strength=0.45,
    river_color=(95, 130, 175),
    border_color=(35, 50, 80),
    border_alpha=0.55,
    day_lift=0.22,
)

ATLAS = MapStyle(
    name="atlas",
    # Classic wall-map blue ocean + sage-green land, dialed down for
    # bedside use. Saturated daytime atlas colors fight every clock
    # theme at night, so this is "atlas after dusk" rather than noon.
    bg=(10, 20, 38),
    ocean=(35, 78, 130),
    land=(90, 142, 95),
    coast=(150, 185, 135),
    night_floor=0.22,
    sun_glow=0.20,
    desert=(205, 180, 115),
    mountain=(120, 95, 70),
    terrain_blend=0.50,
    ice=None,
    relief_strength=0.50,
    river_color=(80, 130, 180),
    border_color=(50, 35, 25),
    border_alpha=0.55,
    day_lift=0.20,
)

VINTAGE = MapStyle(
    name="vintage",
    # Antique parchment: warm sepia ocean, tobacco-brown land. Pairs
    # especially well with Red LED + Daylight themes. Higher night
    # floor preserves the warm tone past the terminator.
    bg=(26, 20, 14),
    ocean=(72, 58, 42),
    land=(125, 98, 62),
    coast=(175, 145, 95),
    night_floor=0.30,
    sun_glow=0.10,
    desert=(180, 145, 85),
    mountain=(70, 50, 30),
    terrain_blend=0.40,
    ice=None,
    relief_strength=0.55,
    river_color=(125, 100, 70),
    border_color=(55, 35, 20),
    border_alpha=0.50,
    day_lift=0.18,
)

BLUEPRINT = MapStyle(
    name="blueprint",
    # Engineering-drawing aesthetic: deep navy bg, ocean barely
    # distinguishable from bg, land in pale chalk-blue. Faint 30°
    # graticule completes the technical-drawing feel.
    bg=(8, 16, 36),
    ocean=(16, 30, 58),
    land=(165, 200, 232),
    coast=(215, 232, 250),
    night_floor=0.12,
    graticule=(70, 110, 160),
    desert=(140, 185, 225),
    mountain=(210, 230, 250),
    terrain_blend=0.40,
    ice=None,
    relief_strength=0.30,
    river_color=(140, 195, 235),
    border_color=(70, 110, 160),
    border_alpha=0.45,
    day_lift=0.16,
)


STYLES: dict[str, MapStyle] = {
    s.name: s for s in (SLATE, ATLAS, VINTAGE, BLUEPRINT)
}


# --- solar geometry --------------------------------------------------

def _solar_position(now: datetime | None = None) -> tuple[float, float]:
    """Approximate sun position — declination + subsolar longitude in
    degrees. Cooper's formula for declination (~1° accurate, plenty for
    a clock-face terminator). Subsolar lon moves −15°/h from 0° at noon
    UTC."""
    now = now or datetime.now(timezone.utc)
    doy = now.timetuple().tm_yday
    decl = 23.44 * math.sin(math.radians(360 * (doy - 80) / 365.25))
    secs = now.hour * 3600 + now.minute * 60 + now.second
    subsolar_lon = -((secs / 3600.0) - 12.0) * 15.0
    if subsolar_lon > 180:
        subsolar_lon -= 360
    elif subsolar_lon <= -180:
        subsolar_lon += 360
    return decl, subsolar_lon


def _sun_elevation(w: int, h: int,
                   decl_deg: float,
                   subsolar_lon_deg: float,
                   center_lon: float = 0.0) -> np.ndarray:
    """HxW float32 array: sun elevation in degrees at every pixel.
    Negative = below horizon (night), positive = above (day). Used both
    for the terminator and for the sun-glow lift.

    `center_lon` is the longitude shown at the horizontal centre of
    the canvas — the per-column lon array spans
    [center_lon-180, center_lon+180), wrapping naturally because the
    sun-elevation formula is periodic in longitude.
    """
    lons = np.linspace(center_lon - 180, center_lon + 180,
                       w, endpoint=False, dtype=np.float32)
    lats = np.linspace(90, -90, h, endpoint=False, dtype=np.float32)
    lon_g, lat_g = np.meshgrid(lons, lats)
    decl = math.radians(decl_deg)
    lat_r = np.deg2rad(lat_g)
    lon_r = np.deg2rad(lon_g - subsolar_lon_deg)
    sin_h = (np.sin(lat_r) * math.sin(decl)
             + np.cos(lat_r) * math.cos(decl) * np.cos(lon_r))
    return np.rad2deg(np.arcsin(np.clip(sin_h, -1, 1))).astype(np.float32)


def _day_mask_from_elev(elev_deg: np.ndarray) -> np.ndarray:
    """Smooth day/night blend through the −18°..0° twilight band so the
    terminator reads as a soft gradient rather than a hard edge."""
    return np.clip((elev_deg + 18.0) / 18.0, 0.0, 1.0).astype(np.float32)


# --- service --------------------------------------------------------

class WorldMapService:
    """Per-canvas, per-style cache of the current day/night world map.

    Cache invalidates when the UTC minute bucket OR the requested style
    changes — single-slot cache, so flipping back-and-forth between two
    styles in a settings picker recomputes each time. That's fine for
    settings-rate clicks; the map is a once-per-minute background, not a
    hot path."""

    # Gaussian-blur radius for the terrain alpha masks, in canvas pixels.
    # ~0.8% of the canvas height gives a feathered transition zone of
    # ~12-15 pixels at 720x800 — Sahara fades into Sahel rather than
    # ending at a polygon edge, like a real wall map. Smaller than first
    # try since aggressive blur dilutes thin polygons (mountain ranges)
    # to invisibility even after normalization.
    TERRAIN_BLUR_FRAC = 0.008
    # Latitude band where polar caps fade in (degrees). |lat| < START
    # = no ice; |lat| > FULL = full ice. Smooth ramp between.
    ICE_LAT_START = 58.0
    ICE_LAT_FULL = 72.0
    # Power curve applied to terrain alpha — values below 1 lift the
    # mid-range (a 0.5 alpha pixel becomes ~0.66), so feathered edges
    # stay visible after normalization. 1.0 = linear.
    TERRAIN_ALPHA_GAMMA = 0.65

    def __init__(self, canvas_w: int, canvas_h: int):
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self._cached_bucket: int = -1
        self._cached_style: str = ""
        self._cached_overlays: tuple[str, ...] = ()
        self._cached_center_lon: int = 0
        self._cached_image: Image.Image | None = None
        self._land_mask = self._load_binary_mask(LAND_PNG)
        self._coast_mask = self._compute_coast_mask(self._land_mask)
        # Terrain alphas: blurred to soften polygon boundaries, then
        # multiplied by the binary land mask so blur that bled into the
        # ocean doesn't tint the sea. Power-curved so feathered edges
        # don't fade into invisibility after normalization.
        blur_r = max(2.0, canvas_h * self.TERRAIN_BLUR_FRAC)
        self._desert_alpha = self._load_soft_mask(DESERT_PNG, blur_r)
        self._mountain_alpha = self._load_soft_mask(MOUNTAIN_PNG, blur_r)
        if self._land_mask is not None:
            land_f = self._land_mask.astype(np.float32)
            if self._desert_alpha is not None:
                self._desert_alpha = (
                    self._desert_alpha ** self.TERRAIN_ALPHA_GAMMA
                ) * land_f
            if self._mountain_alpha is not None:
                self._mountain_alpha = (
                    self._mountain_alpha ** self.TERRAIN_ALPHA_GAMMA
                ) * land_f
        # Polar-cap alpha: smooth ramp from 0 at |lat|=ICE_LAT_START to
        # 1 at |lat|=ICE_LAT_FULL. Time-invariant, so cache once.
        self._ice_alpha = self._compute_ice_alpha()
        # Pre-rendered shaded relief — Natural Earth 50m, NW-lit. Loaded
        # as float32 in [0,1] so it can multiply into the colour layer.
        # 0.5 is "flat ground"; >0.5 is sun-facing, <0.5 is shadow.
        self._relief = self._load_grayscale(RELIEF_PNG, Image.LANCZOS)
        # Optional overlays — each loaded lazily and held as float
        # arrays so the renderer can stack them without re-decoding.
        self._lakes = self._load_binary_mask(LAKES_PNG)
        self._rivers = self._load_grayscale(RIVERS_PNG, Image.BILINEAR)
        self._borders = self._load_grayscale(BORDERS_PNG, Image.BILINEAR)
        self._lights = self._load_rgb(LIGHTS_PNG)

    def _load_grayscale(self, path: Path,
                        resample) -> np.ndarray | None:
        try:
            img = Image.open(path).convert("L")
            img = img.resize((self.canvas_w, self.canvas_h), resample)
            return np.array(img, dtype=np.float32) / 255.0
        except (OSError, FileNotFoundError) as exc:
            print(f"{path.name} missing: {exc}",
                  file=sys.stderr, flush=True)
            return None

    def _load_rgb(self, path: Path) -> np.ndarray | None:
        try:
            img = Image.open(path).convert("RGB")
            img = img.resize((self.canvas_w, self.canvas_h),
                             Image.LANCZOS)
            return np.array(img, dtype=np.float32) / 255.0
        except (OSError, FileNotFoundError) as exc:
            print(f"{path.name} missing: {exc}",
                  file=sys.stderr, flush=True)
            return None

    def _compute_ice_alpha(self) -> np.ndarray:
        lats = np.linspace(90, -90, self.canvas_h,
                           endpoint=False, dtype=np.float32)
        abslat = np.abs(lats)
        ramp = np.clip(
            (abslat - self.ICE_LAT_START)
            / (self.ICE_LAT_FULL - self.ICE_LAT_START),
            0.0, 1.0,
        )
        return np.broadcast_to(ramp[:, None],
                               (self.canvas_h, self.canvas_w)).copy()

    def _load_binary_mask(self, path: Path) -> np.ndarray | None:
        """Load + resize a bundled 1-bit mask to canvas size. Returns
        a HxW bool array (True = mask present) or None if missing.

        Resamples through L (8-bit) with BILINEAR rather than 1-bit
        NEAREST: the bilinear pass anti-aliases the edge, and the >127
        threshold then decides each pixel based on sub-pixel polygon
        coverage. The net effect is coastlines positioned within half
        a pixel of the true polygon edge instead of snapped to the
        nearest source cell — a noticeable upgrade on small islands
        (UK, Japan) where one source cell is the whole feature."""
        try:
            img = Image.open(path).convert("L")
            img = img.resize((self.canvas_w, self.canvas_h),
                             Image.BILINEAR)
            return (np.array(img, dtype=np.uint8) > 127)
        except (OSError, FileNotFoundError) as exc:
            print(f"{path.name} missing: {exc}",
                  file=sys.stderr, flush=True)
            return None

    def _load_soft_mask(self, path: Path,
                        blur_radius: float) -> np.ndarray | None:
        """Load a 1-bit mask, resize, then Gaussian-blur into a
        continuous alpha in [0, 1]. The blur softens polygon edges so
        the desert/mountain tints feather into the surrounding land
        instead of stopping at a hard line — matches how real wall
        maps render these regions. Normalised so the densest pixel
        reaches 1.0; otherwise thin polygons (mountain ranges) get
        diluted by the blur and look weaker than wide ones (deserts),
        which makes `terrain_blend` mean different things per layer."""
        try:
            img = Image.open(path).convert("L")
            img = img.resize((self.canvas_w, self.canvas_h),
                             Image.BILINEAR)
            img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
            arr = np.array(img, dtype=np.float32) / 255.0
            peak = float(arr.max())
            if peak > 0.05:           # guard against an empty mask
                arr = arr / peak
            return arr
        except (OSError, FileNotFoundError) as exc:
            print(f"{path.name} missing: {exc}",
                  file=sys.stderr, flush=True)
            return None

    @staticmethod
    def _compute_coast_mask(land: np.ndarray | None) -> np.ndarray | None:
        """Coastal pixels: land cells with at least one ocean neighbor
        (4-connectivity). Computed once at init since the land mask
        doesn't change. Used to lift those pixels toward `coast` colour
        — gives the map a drawn coastline rather than a hard fill edge."""
        if land is None:
            return None
        ocean = ~land
        # any-of-4-neighbours-is-ocean, AND we are land
        neigh = (np.roll(ocean, 1, axis=0) | np.roll(ocean, -1, axis=0)
                 | np.roll(ocean, 1, axis=1) | np.roll(ocean, -1, axis=1))
        return land & neigh

    @staticmethod
    def _bucket_now() -> int:
        n = datetime.now(timezone.utc)
        return ((n.year * 366 + n.timetuple().tm_yday) * 24
                + n.hour) * 60 + n.minute

    def state_key(self, style_name: str = "slate",
                  overlays: tuple[str, ...] = (),
                  center_lon: float = 0.0) -> tuple:
        # Bucket center_lon to whole degrees so a click on the picker
        # invalidates but tiny float drift doesn't churn the cache.
        return (self._bucket_now(), style_name, tuple(overlays),
                int(round(center_lon)))

    def current_image(self, theme,
                      style_name: str = "slate",
                      overlays: tuple[str, ...] = (),
                      center_lon: float = 0.0,
                      ) -> Image.Image:
        # `theme` accepted for API symmetry but unused — map palettes
        # are intentionally theme-independent.
        del theme
        style = STYLES.get(style_name, SLATE)
        bucket = self._bucket_now()
        cl_key = int(round(center_lon))
        cache_key = (bucket, style.name, tuple(overlays), cl_key)
        if (cache_key == (self._cached_bucket, self._cached_style,
                          self._cached_overlays, self._cached_center_lon)
                and self._cached_image is not None):
            return self._cached_image
        try:
            img = self._render(style, set(overlays), float(center_lon))
        except Exception as exc:
            print(f"world map render failed: {exc}",
                  file=sys.stderr, flush=True)
            img = Image.new("RGB", (self.canvas_w, self.canvas_h),
                            color=style.bg)
        self._cached_bucket = bucket
        self._cached_style = style.name
        self._cached_overlays = tuple(overlays)
        self._cached_center_lon = cl_key
        self._cached_image = img
        return img

    def _shift_for(self, center_lon: float) -> int:
        """Column offset to apply to bundled equirectangular masks so a
        map originally centred on lon=0 displays with `center_lon` at
        the horizontal centre.

        Original col 0 → lon −180. Output col 0 → lon center_lon−180.
        We need original col `center_lon/360*w` to land at output col 0,
        which is exactly `np.roll(arr, -center_lon/360*w, axis=1)`.
        """
        if abs(center_lon) < 1e-3:
            return 0
        return -int(round(center_lon / 360.0 * self.canvas_w))

    @staticmethod
    def _rolled(arr, shift: int):
        if arr is None or shift == 0:
            return arr
        return np.roll(arr, shift, axis=1)

    def _render(self, style: MapStyle,
                overlays: set[str],
                center_lon: float = 0.0) -> Image.Image:
        w, h = self.canvas_w, self.canvas_h
        bg = np.array(style.bg, dtype=np.float32)
        ocean = np.array(style.ocean, dtype=np.float32)
        land = np.array(style.land, dtype=np.float32)
        coast = np.array(style.coast, dtype=np.float32)

        decl, sub_lon = _solar_position()
        elev = _sun_elevation(w, h, decl, sub_lon, center_lon)
        day = _day_mask_from_elev(elev)

        # Apply the longitude offset to every bundled raster/mask so
        # the equirectangular content lines up with the new lon axis
        # used by the day/night calculation. Ice cap is purely
        # latitude-based and can stay unrolled.
        shift = self._shift_for(center_lon)
        land_mask = self._rolled(self._land_mask, shift)
        coast_mask = self._rolled(self._coast_mask, shift)
        desert_alpha = self._rolled(self._desert_alpha, shift)
        mountain_alpha = self._rolled(self._mountain_alpha, shift)
        relief = self._rolled(self._relief, shift)
        lakes_mask = self._rolled(self._lakes, shift)
        rivers_mask = self._rolled(self._rivers, shift)
        borders_mask = self._rolled(self._borders, shift)
        lights_mask = self._rolled(self._lights, shift)

        # Day/night blend: night side is lifted by night_floor so the
        # dark hemisphere doesn't go pitch-black; day side reaches full
        # tint plus an optional sun-glow lift at the subsolar point.
        weight = style.night_floor + (1.0 - style.night_floor) * day
        if style.sun_glow > 0:
            # Above-horizon contribution only; fades to zero at the
            # terminator so the glow sits inside the day side.
            glow = np.clip(elev / 90.0, 0.0, 1.0)
            weight = np.clip(weight + style.sun_glow * glow, 0.0, 1.1)

        # Per-pixel base colour built bottom-up:
        #   1. ocean everywhere
        #   2. land where land mask is True
        #   3. desert tint feathered in by desert alpha (soft edges)
        #   4. mountain tint feathered in by mountain alpha
        #   5. coast colour wins on coastal pixels (defines the shape)
        # Terrain alphas are continuous [0,1] from a Gaussian blur, so
        # tints fade smoothly — no hard polygon edges.
        if land_mask is not None:
            base = np.where(
                land_mask[..., None],
                land[None, None, :],
                ocean[None, None, :],
            ).astype(np.float32)
            blend = style.terrain_blend
            if (style.desert is not None
                    and desert_alpha is not None):
                desert_c = np.array(style.desert, dtype=np.float32)
                a = (desert_alpha * blend)[..., None]
                base = base * (1 - a) + desert_c[None, None, :] * a
            if (style.mountain is not None
                    and mountain_alpha is not None):
                mtn_c = np.array(style.mountain, dtype=np.float32)
                a = (mountain_alpha * blend)[..., None]
                base = base * (1 - a) + mtn_c[None, None, :] * a
            if coast_mask is not None:
                base = np.where(
                    coast_mask[..., None],
                    coast[None, None, :],
                    base,
                ).astype(np.float32)
            # Shaded relief: multiply blend on land. relief value 0.5
            # is "flat ground" (no change); >0.5 brightens (sun-facing
            # slopes); <0.5 darkens (shadow). This is what gives the
            # map its 3D feel — Andes ridges cast shadows, Tibetan
            # plateau lifts, coastal mountains read as bumps.
            if (relief is not None
                    and style.relief_strength > 0):
                factor = (1.0 + style.relief_strength
                          * (relief * 2.0 - 1.0))[..., None]
                lit = np.clip(base * factor, 0.0, 255.0)
                base = np.where(
                    land_mask[..., None], lit, base)

            # Water overlay: lakes + rivers. Lakes paint as ocean
            # colour (so the Caspian/Great-Lakes/Victoria look like
            # water inside continents). Rivers paint as a slightly
            # lighter water tint, alpha-blended by line intensity.
            if "water" in overlays:
                if lakes_mask is not None:
                    base = np.where(
                        lakes_mask[..., None],
                        ocean[None, None, :],
                        base,
                    ).astype(np.float32)
                if (rivers_mask is not None
                        and style.river_color is not None):
                    river_c = np.array(style.river_color,
                                       dtype=np.float32)
                    a = rivers_mask[..., None]
                    base = base * (1 - a) + river_c[None, None, :] * a

            # Political borders: thin dark lines where borders_mask is
            # high. border_alpha tones them down — full-opacity
            # borders look loud at this resolution.
            if ("political" in overlays
                    and borders_mask is not None
                    and style.border_color is not None):
                border_c = np.array(style.border_color, dtype=np.float32)
                a = (borders_mask * style.border_alpha)[..., None]
                base = base * (1 - a) + border_c[None, None, :] * a
        else:
            base = np.broadcast_to(ocean[None, None, :], (h, w, 3)).copy()

        # Polar caps last — paints over everything (land/ocean/coast)
        # in the cap zone, so Greenland + Antarctica look like solid
        # white blobs, the way real wall maps render the poles.
        if style.ice is not None and self._ice_alpha is not None:
            ice_c = np.array(style.ice, dtype=np.float32)
            a = (self._ice_alpha * style.ice_strength)[..., None]
            base = base * (1 - a) + ice_c[None, None, :] * a

        # Blend toward bg by the (1-weight) factor.
        delta = base - bg[None, None, :]
        rgb = bg[None, None, :] + delta * weight[..., None]

        # Day-side brightness lift. Multiplicative gain that scales
        # with `day` (0 at night, 1 at noon) so the lit hemisphere
        # actually reads as daytime — the night side and the bg fade
        # are untouched, and the boost dies smoothly through the
        # twilight band. Clip after to avoid uint8 wrap.
        if style.day_lift > 0:
            factor = 1.0 + style.day_lift * day[..., None]
            rgb = rgb * factor

        # City lights: NASA Black Marble, additive over night side.
        # weight ~ 1 on day (no lights), low on night (full lights).
        # Multiply by (1 - day) to expose them only past dusk, with
        # a soft falloff through the twilight band.
        if ("city_lights" in overlays
                and lights_mask is not None):
            night_alpha = (1.0 - day)[..., None]
            # 0.85 strength + 255 scale gives bright cities without
            # blowing out. Pixels outside cities are near-zero so they
            # don't lift the night sky.
            additive = lights_mask * 255.0 * 0.85 * night_alpha
            rgb = np.clip(rgb + additive, 0.0, 255.0)

        # Clip before uint8 conversion — np.astype(uint8) WRAPS modulo
        # 256, so a sun-glow lift over 255 silently becomes near-black
        # (magenta/yellow garbage in the Arctic). Always clip first.
        img = Image.fromarray(
            np.clip(rgb, 0, 255).astype(np.uint8), "RGB")

        if style.graticule is not None:
            img = self._stamp_graticule(img, style.graticule)
        if "annotations" in overlays:
            img = self._stamp_annotations(img, style, decl, sub_lon,
                                          center_lon)
        return img

    def _stamp_annotations(self, img: Image.Image, style: MapStyle,
                           decl_deg: float,
                           subsolar_lon_deg: float,
                           center_lon: float = 0.0) -> Image.Image:
        """Cartographic annotation overlay: equator, tropics, polar
        circles, and the day/night terminator. Drawn on an RGBA layer
        so each line blends into the map palette instead of replacing
        pixels. The terminator is a dashed curve following sun
        elevation = 0; lat lines are solid with tiny right-edge labels.
        """
        w, h = self.canvas_w, self.canvas_h
        # Pick a colour: explicit annotation_color > graticule > a
        # softened version of coast > white as a final fallback.
        base = (style.annotation_color
                or style.graticule
                or style.coast
                or (240, 240, 240))
        # Tropics and polar circles are subdued reference lines —
        # the equator is the only one that should read at a glance.
        line_rgba = base + (85,)           # tropics + polar circles, soft
        eq_rgba = base + (210,)            # equator stays prominent
        term_rgba = base + (180,)          # terminator dashes
        eq_label_rgba = base + (220,)
        line_label_rgba = base + (110,)    # subdued labels match lines
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        # Latitude lines (degrees) and short labels for the right edge.
        TROPIC = 23.4366
        ARCTIC = 66.5634
        lines = (
            (ARCTIC, "Arctic Circle"),
            (TROPIC, "Tropic of Cancer"),
            (0.0, "Equator"),
            (-TROPIC, "Tropic of Capricorn"),
            (-ARCTIC, "Antarctic Circle"),
        )
        label_size = max(9, int(h * 0.018))
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                label_size)
        except OSError:
            font = ImageFont.load_default()
        for lat, label in lines:
            y = int(round((90 - lat) / 180.0 * h))
            if not (0 <= y < h):
                continue
            rgba = eq_rgba if lat == 0 else line_rgba
            line_w = 2 if lat == 0 else 1
            d.line([(0, y), (w - 1, y)], fill=rgba, width=line_w)
            if font is not None:
                # Right-edge label, lifted just above the line so it
                # doesn't sit on top of the stroke.
                tx = w - int(w * 0.02)
                ty = y - label_size - 1
                if ty < 0:
                    ty = y + 2
                bbox = d.textbbox((0, 0), label, font=font)
                tw_ = bbox[2] - bbox[0]
                fill = (eq_label_rgba if lat == 0
                        else line_label_rgba)
                d.text((tx - tw_, ty), label, font=font, fill=fill)
        # Day/night terminator: trace where sun elevation crosses 0°.
        # Equation: tan(lat) = -cos(lon - subsolar_lon) / tan(decl).
        # Degenerates near equinox (decl ≈ 0); guard with a small
        # epsilon so we don't divide by zero.
        decl_r = math.radians(decl_deg)
        # Per-column lon: spans [center_lon-180, center_lon+180). The
        # x-axis-to-lon mapping below uses this same range so the
        # terminator stays correctly placed when the user picks a
        # non-Greenwich centre.
        lon_left = center_lon - 180.0

        def _lon_to_x(lon: float) -> int:
            # Wrap into [-180, 180) of the centred axis, then convert
            # to a pixel column.
            rel = lon - center_lon
            while rel <= -180.0:
                rel += 360.0
            while rel > 180.0:
                rel -= 360.0
            return int(round((rel + 180.0) / 360.0 * w))

        if abs(math.tan(decl_r)) < 1e-3:
            # At equinox the terminator is the meridian 90° from the
            # subsolar point — draw it as two vertical dashed lines.
            for sign in (1, -1):
                lon = subsolar_lon_deg + sign * 90.0
                x = _lon_to_x(lon)
                if 0 <= x < w:
                    self._draw_dashed_line(
                        d, [(x, 0), (x, h - 1)], term_rgba, 2)
            return Image.alpha_composite(
                img.convert("RGBA"), overlay).convert("RGB")
        pts: list[tuple[int, int]] = []
        # Sample every column; skip points where the formula falls off
        # (|tan lat| > tan 90° guard, but with decl bounded this just
        # means lat outside [-90, 90] which we clip on draw).
        for px in range(w):
            lon = lon_left + px * 360.0 / w
            dlon = math.radians(lon - subsolar_lon_deg)
            tan_lat = -math.cos(dlon) / math.tan(decl_r)
            lat = math.degrees(math.atan(tan_lat))
            py = int(round((90 - lat) / 180.0 * h))
            if 0 <= py < h:
                pts.append((px, py))
        if len(pts) >= 2:
            # Split into runs whose y stays "close" so a wrap-around
            # in latitude doesn't draw a long diagonal across the map.
            run: list[tuple[int, int]] = [pts[0]]
            runs: list[list[tuple[int, int]]] = []
            for p in pts[1:]:
                if abs(p[1] - run[-1][1]) > h * 0.25:
                    runs.append(run)
                    run = [p]
                else:
                    run.append(p)
            runs.append(run)
            for r in runs:
                self._draw_dashed_polyline(d, r, term_rgba, 2)
        return Image.alpha_composite(
            img.convert("RGBA"), overlay).convert("RGB")

    @staticmethod
    def _draw_dashed_line(d: ImageDraw.ImageDraw,
                          endpoints: list[tuple[int, int]],
                          color, width: int,
                          dash: int = 6, gap: int = 5) -> None:
        (x0, y0), (x1, y1) = endpoints
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1:
            return
        steps = int(length // (dash + gap)) + 1
        ux, uy = dx / length, dy / length
        for i in range(steps):
            s = i * (dash + gap)
            e = min(s + dash, length)
            d.line([(x0 + ux * s, y0 + uy * s),
                    (x0 + ux * e, y0 + uy * e)],
                   fill=color, width=width)

    @staticmethod
    def _draw_dashed_polyline(d: ImageDraw.ImageDraw,
                              pts: list[tuple[int, int]],
                              color, width: int,
                              dash: int = 6, gap: int = 5) -> None:
        # Walk the polyline by arc length, alternating dash / gap.
        on = True
        carry = 0.0
        target = dash
        seg_start = pts[0]
        for px, py in pts[1:]:
            dx, dy = px - seg_start[0], py - seg_start[1]
            seg_len = math.hypot(dx, dy)
            if seg_len <= 0:
                seg_start = (px, py)
                continue
            ux, uy = dx / seg_len, dy / seg_len
            cursor = 0.0
            while cursor < seg_len:
                remain = target - carry
                step = min(remain, seg_len - cursor)
                x0 = seg_start[0] + ux * cursor
                y0 = seg_start[1] + uy * cursor
                x1 = seg_start[0] + ux * (cursor + step)
                y1 = seg_start[1] + uy * (cursor + step)
                if on:
                    d.line([(x0, y0), (x1, y1)],
                           fill=color, width=width)
                cursor += step
                carry += step
                if carry >= target - 1e-6:
                    on = not on
                    target = dash if on else gap
                    carry = 0.0
            seg_start = (px, py)

    def _stamp_graticule(self, img: Image.Image,
                         color: RGB) -> Image.Image:
        """Faint 30° lat/lon grid — drawn on an RGBA overlay so the
        line color blends into the map rather than overwriting pixels.
        Only used by the Blueprint style."""
        w, h = self.canvas_w, self.canvas_h
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        line_rgba = color + (70,)        # ~27% alpha — present, not loud
        equator_rgba = color + (110,)    # equator a hair stronger
        # Verticals every 30° lon
        for lon in range(-180, 181, 30):
            x = int(round((lon + 180) / 360.0 * w))
            if 0 <= x < w:
                d.line([(x, 0), (x, h - 1)], fill=line_rgba, width=1)
        # Horizontals every 30° lat, equator highlighted
        for lat in range(-90, 91, 30):
            y = int(round((90 - lat) / 180.0 * h))
            if 0 <= y < h:
                rgba = equator_rgba if lat == 0 else line_rgba
                d.line([(0, y), (w - 1, y)], fill=rgba, width=1)
        return Image.alpha_composite(
            img.convert("RGBA"), overlay).convert("RGB")
