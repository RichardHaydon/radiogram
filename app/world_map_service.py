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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    # When True, the renderer dispatches to the orthographic-globe path
    # instead of the equirectangular world map. Subsolar-centred sphere
    # in the disc, atmospheric rim + starfield outside.
    is_globe: bool = False
    # When True, dispatch to the realistic local-sky renderer
    # (skymap_service). Disc = horizon dome with bright stars,
    # constellation lines, Sun/Moon/planets at their actual positions.
    # is_globe and is_starmap are mutually exclusive.
    is_starmap: bool = False
    # Globe-only knobs. atmosphere is the cyan rim colour; rim_frac is
    # the rim's width as a fraction of the disc radius. specular sets
    # peak ocean sun-glint brightness (added on top of the day fill);
    # specular_power tightens the glint into a small bright spot at
    # high values.
    atmosphere: RGB = (90, 140, 200)
    rim_frac: float = 0.045
    specular: float = 70.0
    specular_power: float = 60.0


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


GLOBE = MapStyle(
    name="globe",
    # Orthographic sphere centred on the subsolar point — what you see
    # is the daylit hemisphere from a viewpoint at the Sun. Palette is
    # atlas-like but with a deeper ocean to read against the dark space
    # backdrop, and a brighter coast so continents pop at the limb.
    bg=(4, 6, 14),                     # space backdrop
    ocean=(28, 70, 122),
    land=(95, 145, 100),
    coast=(160, 195, 140),
    night_floor=1.0,                   # disc is fully day-lit by definition
    desert=(205, 180, 115),
    mountain=(115, 90, 70),
    terrain_blend=0.50,
    ice=None,
    relief_strength=0.55,
    river_color=(80, 130, 180),
    border_color=(50, 35, 25),
    border_alpha=0.55,
    day_lift=0.18,
    is_globe=True,
    atmosphere=(105, 165, 220),
    rim_frac=0.045,
    specular=75.0,
    specular_power=55.0,
)


STARMAP = MapStyle(
    name="starmap",
    # Realistic dark-sky horizon view. Disc = local zenith dome (north
    # up). Outside disc = ground colour. The sky/star colours are
    # generated entirely inside skymap_service — these palette fields
    # only matter for the cache-key bookkeeping below.
    bg=(8, 12, 24),
    ocean=(8, 12, 24),
    land=(8, 12, 24),
    coast=(8, 12, 24),
    night_floor=1.0,
    is_starmap=True,
)


STYLES: dict[str, MapStyle] = {
    s.name: s for s in (SLATE, ATLAS, VINTAGE, BLUEPRINT, GLOBE, STARMAP)
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

    # Background pre-warmer: when the wall-clock is within this many
    # seconds of the next minute boundary, the warmer renders the
    # next-minute image so the main thread doesn't take the
    # rendering hit when the bucket rolls over. 12s lead-time covers
    # a worst-case ~600ms render with margin even if the warmer was
    # already mid-cycle when the threshold was crossed.
    WARMER_LEAD_S = 12.0
    WARMER_TICK_S = 4.0

    def __init__(self, canvas_w: int, canvas_h: int,
                 *, location_path: Path | None = None):
        # Render at full display resolution — the warmer thread covers
        # the per-minute rendering hit so we don't need to trade off
        # crispness for speed on the main thread.
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.display_w = canvas_w
        self.display_h = canvas_h
        # Skymap is built lazily — only construct (and load 100KB of
        # bundled CSV) on first render of a starmap style. Keeps the
        # cold-start cost off the main path for users who never pick
        # the star-chart background.
        self._location_path = location_path
        self._skymap = None
        self._cached_bucket: int = -1
        self._cached_style: str = ""
        self._cached_overlays: tuple[str, ...] = ()
        self._cached_center_lon: int = 0
        self._cached_image: Image.Image | None = None
        # Pre-warm slot: holds the next-minute image computed by the
        # warmer thread. Promoted into _cached_* on the first
        # current_image call after the bucket rolls over.
        self._next_bucket: int = -1
        self._next_style: str = ""
        self._next_overlays: tuple[str, ...] = ()
        self._next_center_lon: int = 0
        self._next_image: Image.Image | None = None
        self._cache_lock = threading.Lock()
        self._warmer_stop = threading.Event()
        self._warmer_thread: threading.Thread | None = None
        # In-flight render coordination. When a settings scene fires
        # request_prewarm() for new params, _inflight_params tracks
        # those params so a parallel current_image() call doesn't
        # duplicate the work — it just waits on _inflight_done. Cleared
        # by the worker once it stashes the result (or the user changes
        # params again before the worker finishes; in that case the
        # old worker's result is silently discarded).
        self._inflight_params: tuple | None = None
        self._inflight_done = threading.Event()
        self._inflight_done.set()  # idle = "no render in flight"
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
        # Star field — sparse white pixels for the globe's space backdrop.
        # Built once at init with a fixed seed so the sky doesn't flicker
        # between renders. Float32 in [0,1] so it adds cleanly into rgb.
        self._starfield = self._build_starfield()
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
    def _bucket_for(n: datetime) -> int:
        return ((n.year * 366 + n.timetuple().tm_yday) * 24
                + n.hour) * 60 + n.minute

    @classmethod
    def _bucket_now(cls) -> int:
        return cls._bucket_for(datetime.now(timezone.utc))

    def state_key(self, style_name: str = "slate",
                  overlays: tuple[str, ...] = (),
                  center_lon: float = 0.0) -> tuple:
        # Bucket center_lon to whole degrees so a click on the picker
        # invalidates but tiny float drift doesn't churn the cache.
        return (self._bucket_now(), style_name, tuple(overlays),
                int(round(center_lon)))

    def is_rendering(self) -> bool:
        """True while an eager / pre-warm render is in flight. Scenes
        use this to show a small "updating" indicator over a stale
        background while the worker finishes."""
        with self._cache_lock:
            return self._inflight_params is not None

    def current_image_nonblocking(
            self, theme,
            style_name: str = "slate",
            overlays: tuple[str, ...] = (),
            center_lon: float = 0.0,
            ) -> tuple[Image.Image | None, bool]:
        """Like current_image() but never blocks the caller.

        Returns (img, is_stale). If the requested params are already
        cached the answer is fresh (is_stale=False). If they're not,
        the call kicks off an eager render and returns the most recent
        cached image — even if it's for a different bucket / style /
        params — with is_stale=True. The caller can paint the stale
        image and overlay an "updating" hint until the next render
        cycle picks up the fresh result.

        On first boot before any image has been rendered, returns
        (None, True). Callers should fall back to a solid bg in that
        case (Scene._make_canvas already handles None providers)."""
        del theme
        style = STYLES.get(style_name, SLATE)
        ovs_key = tuple(overlays)
        cl_key = int(round(center_lon))
        bucket = self._bucket_now()
        cache_key = (bucket, style.name, ovs_key, cl_key)
        with self._cache_lock:
            # Hot path — exact match in main slot.
            if (cache_key == (self._cached_bucket, self._cached_style,
                              self._cached_overlays,
                              self._cached_center_lon)
                    and self._cached_image is not None):
                return self._cached_image, False
            # Pre-warm slot match — promote and serve fresh.
            if (cache_key == (self._next_bucket, self._next_style,
                              self._next_overlays,
                              self._next_center_lon)
                    and self._next_image is not None):
                img = self._next_image
                self._cached_bucket = self._next_bucket
                self._cached_style = self._next_style
                self._cached_overlays = self._next_overlays
                self._cached_center_lon = self._next_center_lon
                self._cached_image = img
                self._next_image = None
                self._next_bucket = -1
                return img, False
            # Stale fallback: serve whatever is in the main cache slot
            # so the UI doesn't flash a solid bg colour. The caller
            # uses is_stale=True to overlay the updating hint.
            stale = self._cached_image
            need_render = self._inflight_params != cache_key
        if need_render:
            # Outside the lock — request_prewarm acquires it itself.
            self.request_prewarm(style_name, overlays, center_lon)
        return stale, True

    def current_image(self, theme,
                      style_name: str = "slate",
                      overlays: tuple[str, ...] = (),
                      center_lon: float = 0.0,
                      ) -> Image.Image:
        # `theme` accepted for API symmetry but unused — map palettes
        # are intentionally theme-independent.
        del theme
        style = STYLES.get(style_name, SLATE)
        ovs_key = tuple(overlays)
        cl_key = int(round(center_lon))
        bucket = self._bucket_now()
        cache_key = (bucket, style.name, ovs_key, cl_key)
        with self._cache_lock:
            # Hot path: current bucket already cached.
            if (cache_key == (self._cached_bucket, self._cached_style,
                              self._cached_overlays,
                              self._cached_center_lon)
                    and self._cached_image is not None):
                return self._cached_image
            # Pre-warm hit: the warmer rendered this exact bucket /
            # style / overlays / lon ahead of time. Promote it into
            # the main cache slot — that's the bucket-rollover fast
            # path that keeps the home screen from stuttering when
            # the user closes settings just after a minute change.
            if (cache_key == (self._next_bucket, self._next_style,
                              self._next_overlays,
                              self._next_center_lon)
                    and self._next_image is not None):
                img = self._next_image
                self._cached_bucket = self._next_bucket
                self._cached_style = self._next_style
                self._cached_overlays = self._next_overlays
                self._cached_center_lon = self._next_center_lon
                self._cached_image = img
                self._next_image = None
                self._next_bucket = -1
                return img
            # In-flight render with matching params? Wait for it
            # rather than start a duplicate. Settings scenes call
            # request_prewarm() the moment the user picks a new style,
            # so by the time the user navigates back to home this
            # branch usually trips and the wait is short (or zero).
            inflight_event = None
            if self._inflight_params == cache_key:
                inflight_event = self._inflight_done
            # Cache miss — fall through to a synchronous render below.
            # Must drop the lock first; _render is expensive and we
            # don't want to block the warmer. The masks + style data
            # it reads are immutable after __init__.
        if inflight_event is not None:
            # 10s ceiling guards against a worker that died silently —
            # we still want some image on screen, even if it means a
            # synchronous render after the timeout.
            if inflight_event.wait(timeout=10.0):
                with self._cache_lock:
                    if (cache_key == (self._cached_bucket,
                                      self._cached_style,
                                      self._cached_overlays,
                                      self._cached_center_lon)
                            and self._cached_image is not None):
                        return self._cached_image
        try:
            img = self._render(style, set(overlays), float(center_lon))
        except Exception as exc:
            print(f"world map render failed: {exc}",
                  file=sys.stderr, flush=True)
            img = Image.new("RGB", (self.canvas_w, self.canvas_h),
                            color=style.bg)
        with self._cache_lock:
            self._cached_bucket = bucket
            self._cached_style = style.name
            self._cached_overlays = ovs_key
            self._cached_center_lon = cl_key
            self._cached_image = img
            # Style/overlays/lon may have just changed — invalidate
            # any stale pre-warm so the warmer recomputes against
            # the new params on its next tick.
            if (self._next_style != style.name
                    or self._next_overlays != ovs_key
                    or self._next_center_lon != cl_key):
                self._next_image = None
                self._next_bucket = -1
        return img

    # --- background pre-warmer ---------------------------------------

    def start(self) -> None:
        """Spawn the daemon thread that pre-renders the next-minute
        image just before the bucket rolls over. Idempotent."""
        if self._warmer_thread is not None:
            return
        self._warmer_thread = threading.Thread(
            target=self._warmer_loop, daemon=True, name="map-warmer")
        self._warmer_thread.start()

    def stop(self) -> None:
        self._warmer_stop.set()
        if self._warmer_thread is not None:
            self._warmer_thread.join(timeout=2.0)
            self._warmer_thread = None

    def request_prewarm(self, style_name: str,
                        overlays: tuple[str, ...] = (),
                        center_lon: float = 0.0) -> None:
        """Eagerly start rendering this style+overlays+lon for the
        current minute on a daemon thread.

        Settings scenes call this the moment the user picks new params.
        By the time they navigate back to the home screen the result
        is usually already cached — eliminating the perceived lag of a
        synchronous render on the main thread.

        No-op if the requested params are already cached or already in
        flight. Calling with new params while a previous prewarm is
        running is fine: the running worker keeps going but its result
        will be discarded once it finishes (params no longer match)."""
        style = STYLES.get(style_name, SLATE)
        ovs_key = tuple(overlays)
        cl_key = int(round(center_lon))
        bucket = self._bucket_now()
        target = (bucket, style.name, ovs_key, cl_key)
        with self._cache_lock:
            if (target == (self._cached_bucket, self._cached_style,
                           self._cached_overlays,
                           self._cached_center_lon)
                    and self._cached_image is not None):
                return
            if self._inflight_params == target:
                return
            self._inflight_params = target
            self._inflight_done.clear()
        threading.Thread(
            target=self._eager_render, args=(target,),
            daemon=True, name="map-eager").start()

    def _eager_render(self, target: tuple) -> None:
        """Worker for request_prewarm. Renders, then promotes into the
        main cache slot iff the user hasn't switched to different params
        meanwhile (which would mean _inflight_params got overwritten)."""
        bucket, style_name, ovs, cl_key = target
        style = STYLES.get(style_name, SLATE)
        try:
            img = self._render(style, set(ovs), float(cl_key))
        except Exception as exc:
            print(f"map eager render failed: {exc}",
                  file=sys.stderr, flush=True)
            with self._cache_lock:
                if self._inflight_params == target:
                    self._inflight_params = None
                    self._inflight_done.set()
            return
        with self._cache_lock:
            if self._inflight_params == target:
                self._cached_bucket = bucket
                self._cached_style = style_name
                self._cached_overlays = ovs
                self._cached_center_lon = cl_key
                self._cached_image = img
                # Stale pre-warm slot for these new params — let the
                # minute-boundary warmer recompute on its next tick.
                if (self._next_style != style_name
                        or self._next_overlays != ovs
                        or self._next_center_lon != cl_key):
                    self._next_image = None
                    self._next_bucket = -1
                self._inflight_params = None
                self._inflight_done.set()
            # Else: user switched params before we finished. A different
            # eager render is already in flight; our result is silently
            # discarded.

    def _warmer_loop(self) -> None:
        while not self._warmer_stop.wait(self.WARMER_TICK_S):
            try:
                self._maybe_prewarm()
            except Exception as exc:
                print(f"map warmer: {exc}",
                      file=sys.stderr, flush=True)

    def _maybe_prewarm(self) -> None:
        """If we're inside the pre-warm window before a minute boundary
        AND the next-minute slot isn't already filled, render the
        next-minute image off-thread and stash it.

        The "next bucket" is computed from the wall clock — NOT from
        _cached_bucket. The cache can lag arbitrarily behind real time
        (e.g. user sits in settings for 10 minutes; idle scene never
        runs current_image to advance _cached_bucket). What matters is
        what the bucket WILL BE when the user next looks at home.

        Reads the params (style/overlays/lon) from the currently-
        cached image so we follow whatever the user picked last."""
        now = datetime.now(timezone.utc)
        # Distance to the next minute boundary, in seconds.
        seconds_to_next = 60 - now.second
        if seconds_to_next > self.WARMER_LEAD_S:
            return
        # Render for the wall-clock's next minute boundary — this is
        # the bucket the main thread will ask for once we cross.
        next_moment = (now + timedelta(seconds=seconds_to_next)
                       ).replace(microsecond=0)
        target_bucket = self._bucket_for(next_moment)
        with self._cache_lock:
            if self._cached_image is None:
                return
            cur_style_name = self._cached_style
            cur_overlays = self._cached_overlays
            cur_lon_key = self._cached_center_lon
            # Already pre-warmed for THIS specific target bucket
            # against current params? Nothing to do.
            if (self._next_image is not None
                    and self._next_bucket == target_bucket
                    and self._next_style == cur_style_name
                    and self._next_overlays == cur_overlays
                    and self._next_center_lon == cur_lon_key):
                return
        style = STYLES.get(cur_style_name, SLATE)
        try:
            img = self._render(style, set(cur_overlays),
                               float(cur_lon_key), now=next_moment)
        except Exception as exc:
            print(f"map prewarm render failed: {exc}",
                  file=sys.stderr, flush=True)
            return
        with self._cache_lock:
            # Only stash if the user-controlled params haven't
            # changed under us during the render — otherwise we'd
            # be caching a stale prediction.
            if (self._cached_style == cur_style_name
                    and self._cached_overlays == cur_overlays
                    and self._cached_center_lon == cur_lon_key):
                self._next_bucket = target_bucket
                self._next_style = cur_style_name
                self._next_overlays = cur_overlays
                self._next_center_lon = cur_lon_key
                self._next_image = img

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
                center_lon: float = 0.0,
                *, now: datetime | None = None) -> Image.Image:
        if style.is_starmap:
            # Realistic dark-sky horizon view. Pulls observer lat/lon
            # from the location file the weather service maintains.
            # center_lon is ignored — the projection is local-zenith
            # centred. Overlays drive constellation lines + planet
            # labels.
            if self._skymap is None:
                from skymap_service import SkymapService
                self._skymap = SkymapService(
                    location_path=self._location_path)
            return self._skymap.render(
                self.canvas_w, self.canvas_h, now=now,
                draw_constellations=True,
                planet_labels=True,
            )
        if style.is_globe:
            # Globe path is a different projection entirely — share the
            # masks + solar geometry helpers but build the image with
            # its own routine. center_lon and overlays are ignored: the
            # globe is always subsolar-centred and overlays don't make
            # sense on a daylit hemisphere.
            return self._render_globe(style, now=now)
        t0 = time.monotonic()
        w, h = self.canvas_w, self.canvas_h
        bg = np.array(style.bg, dtype=np.float32)
        ocean = np.array(style.ocean, dtype=np.float32)
        land = np.array(style.land, dtype=np.float32)
        coast = np.array(style.coast, dtype=np.float32)

        # `now` is plumbed through so the background warmer can render
        # for the next-minute moment ahead of time and have the result
        # cached when the main thread asks for it.
        decl, sub_lon = _solar_position(now=now)
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
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        # Only log when the render was unusually slow OR ran on the
        # main thread (where it would surface as visible UI lag).
        # Silent on the fast warmer path so the journal stays quiet
        # in steady state.
        thread = threading.current_thread().name
        if elapsed_ms > 1500 or thread == "MainThread":
            print(f"map render [{thread}] {style.name} {elapsed_ms:.0f}ms",
                  file=sys.stderr, flush=True)
        return img

    # --- globe path ---------------------------------------------------

    def _build_starfield(self) -> np.ndarray:
        """Sparse starfield for the globe's space backdrop. Fixed seed
        so consecutive renders show the same sky — a moving starfield
        would flicker every minute when the cache invalidates. Returns
        an HxWx3 float32 array of additive star intensities (0 or a
        small lift per channel)."""
        rng = np.random.default_rng(0xC10C)
        h, w = self.canvas_h, self.canvas_w
        # ~1 star per 800 pixels keeps the sky sparse — a stuffed sky
        # competes with the globe for attention. Star brightness varies
        # so the field looks like depth, not a pixel-noise grid.
        n = max(20, int(h * w / 800))
        ys = rng.integers(0, h, size=n)
        xs = rng.integers(0, w, size=n)
        # Magnitude bias toward dim — most real stars are below the
        # naked-eye threshold; a few bright ones anchor the eye.
        mags = rng.beta(2.0, 5.0, size=n).astype(np.float32) * 180.0
        out = np.zeros((h, w, 3), dtype=np.float32)
        out[ys, xs, :] = mags[:, None]
        return out

    def _render_globe(self, style: MapStyle,
                      *, now: datetime | None = None) -> Image.Image:
        """Subsolar-centred orthographic projection of the Earth.

        The visible disc is the day-lit hemisphere (terminator coincides
        with the limb). Limb-darkening + atmospheric rim glow + ocean
        sun-glint give it a "view from the Sun" satellite feel. Uses the
        same equirectangular masks as `_render` — sampled by computed
        (lat, lon) per disc pixel via fancy indexing."""
        t0 = time.monotonic()
        w, h = self.canvas_w, self.canvas_h
        bg = np.array(style.bg, dtype=np.float32)
        ocean = np.array(style.ocean, dtype=np.float32)
        land = np.array(style.land, dtype=np.float32)
        coast = np.array(style.coast, dtype=np.float32)

        decl, sub_lon = _solar_position(now=now)
        decl_r = math.radians(decl)
        sub_lon_r = math.radians(sub_lon)
        cos_d = math.cos(decl_r)
        sin_d = math.sin(decl_r)

        # Disc geometry. Fit the sphere into the short axis with a
        # margin so the rim glow has room to bleed out without clipping.
        margin_frac = 0.07
        radius = (min(w, h) * 0.5) * (1.0 - margin_frac)
        cx = w * 0.5
        cy = h * 0.5

        # Per-pixel normalised disc coords. nx grows east (right), ny
        # grows north (up — note the y-flip vs image rows). The +0.5
        # samples pixel centres, which keeps the limb from looking like
        # a stair-step at low resolutions.
        ys = (np.arange(h, dtype=np.float32) + 0.5 - cy) / radius
        xs = (np.arange(w, dtype=np.float32) + 0.5 - cx) / radius
        ny, nx = np.meshgrid(-ys, xs, indexing="ij")  # ny flipped: up=+

        rho2 = nx * nx + ny * ny
        inside = rho2 <= 1.0
        # nz = depth into the screen, 1 at disc centre, 0 at the limb.
        # Clamp before sqrt — outside-disc pixels would otherwise NaN.
        nz = np.sqrt(np.maximum(0.0, 1.0 - rho2))

        # Inverse orthographic projection: for each disc pixel, recover
        # the (lat, lon) on the sphere given the centre at (decl, sub_lon).
        # Standard formulas with cos(c) = nz, sin(c)*x/ρ = nx, etc.
        sin_lat = np.clip(nz * sin_d + ny * cos_d, -1.0, 1.0)
        lat = np.arcsin(sin_lat)
        lon = sub_lon_r + np.arctan2(nx, nz * cos_d - ny * sin_d)

        # Equirect mask sampling: row from latitude (north→row 0),
        # column from longitude. Wrap longitude into [0, 2π) before
        # scaling so the col index lands inside the source array.
        src_h, src_w = self.canvas_h, self.canvas_w
        row = ((math.pi * 0.5 - lat) / math.pi * src_h).astype(np.int32)
        lon_wrap = np.mod(lon + math.pi, 2.0 * math.pi)
        col = (lon_wrap / (2.0 * math.pi) * src_w).astype(np.int32)
        np.clip(row, 0, src_h - 1, out=row)
        np.clip(col, 0, src_w - 1, out=col)

        # Sample land/ocean and build the base colour layer.
        if self._land_mask is not None:
            is_land = self._land_mask[row, col]
        else:
            is_land = np.zeros((h, w), dtype=bool)
        base = np.where(
            is_land[..., None],
            land[None, None, :],
            ocean[None, None, :],
        ).astype(np.float32)

        # Desert + mountain tints — same blend recipe as the equirect
        # path but sampled per disc pixel.
        blend = style.terrain_blend
        if (style.desert is not None
                and self._desert_alpha is not None):
            desert_c = np.array(style.desert, dtype=np.float32)
            a = (self._desert_alpha[row, col] * blend)[..., None]
            base = base * (1 - a) + desert_c[None, None, :] * a
        if (style.mountain is not None
                and self._mountain_alpha is not None):
            mtn_c = np.array(style.mountain, dtype=np.float32)
            a = (self._mountain_alpha[row, col] * blend)[..., None]
            base = base * (1 - a) + mtn_c[None, None, :] * a
        # Coast on top of land — defines the visible coastline shape
        # against the ocean blue.
        if self._coast_mask is not None:
            is_coast = self._coast_mask[row, col]
            base = np.where(
                is_coast[..., None],
                coast[None, None, :],
                base,
            ).astype(np.float32)
        # Shaded relief: multiply blend on land pixels only.
        if self._relief is not None and style.relief_strength > 0:
            rel = self._relief[row, col]
            factor = (1.0 + style.relief_strength
                      * (rel * 2.0 - 1.0))[..., None]
            lit = np.clip(base * factor, 0.0, 255.0)
            base = np.where(is_land[..., None], lit, base)

        # Day-side lift, scaled by nz (= sin of sun elevation, since
        # the disc IS the day-lit hemisphere). Drops to zero exactly
        # at the limb so the limb darkening reads naturally.
        if style.day_lift > 0:
            base = base * (1.0 + style.day_lift * nz[..., None])

        # Limb darkening — astronomical specification: brightness ∝
        # nz^k with k around 0.5–0.7 for an Earth-from-space look.
        # Real Earth's edge dims because the atmosphere viewed at a
        # grazing angle scatters more light. We just reuse the same
        # nz factor since it varies the right way.
        limb = np.power(np.maximum(nz, 0.0), 0.55)
        base = base * limb[..., None]

        # Specular ocean glint at the subsolar point — Phong-style spot,
        # only on ocean pixels so continents don't suddenly turn shiny
        # at the centre. Tight power keeps it a small bright disc rather
        # than a wash over the whole hemisphere.
        if style.specular > 0:
            glint = (np.power(np.maximum(nz, 0.0), style.specular_power)
                     * style.specular)
            ocean_mask = (~is_land) & inside
            base = np.where(
                ocean_mask[..., None],
                base + glint[..., None],
                base,
            )

        # Compose: disc pixels take the base colour, outside is space
        # (bg) plus the static starfield. Stars are masked so they only
        # appear in space, never on the globe.
        space = np.broadcast_to(
            bg[None, None, :], (h, w, 3)).astype(np.float32)
        outside = ~inside
        if self._starfield is not None:
            space = space + self._starfield * outside[..., None]
        rgb = np.where(inside[..., None], base, space)

        # Atmospheric rim glow — annulus just outside the disc fading
        # from the atmosphere colour to space. Inner edge sits exactly
        # at the limb; outer edge `rim_frac` * R further out.
        rim_outer = 1.0 + style.rim_frac
        rho = np.sqrt(rho2)
        if style.rim_frac > 0:
            t = np.clip((rim_outer - rho) / style.rim_frac, 0.0, 1.0)
            # Squared falloff makes the rim glow look softer / more
            # gaseous than a linear ramp.
            rim_alpha = np.where(
                (rho >= 1.0) & (rho <= rim_outer),
                t * t,
                0.0,
            )
            atmosphere = np.array(style.atmosphere, dtype=np.float32)
            rgb = (rgb * (1.0 - rim_alpha[..., None])
                   + atmosphere[None, None, :] * rim_alpha[..., None])

        # Inner-edge atmospheric haze — a faint blue lift just inside
        # the limb. Sells the "you're seeing the atmosphere from the
        # side" effect that real planet portraits show.
        haze_band = 0.10
        haze_t = np.clip((rho - (1.0 - haze_band)) / haze_band, 0.0, 1.0)
        haze_alpha = np.where(inside, haze_t * 0.25, 0.0)
        atmosphere = np.array(style.atmosphere, dtype=np.float32)
        rgb = rgb + atmosphere[None, None, :] * haze_alpha[..., None]

        img = Image.fromarray(
            np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        thread = threading.current_thread().name
        if elapsed_ms > 1500 or thread == "MainThread":
            print(f"globe render [{thread}] {elapsed_ms:.0f}ms",
                  file=sys.stderr, flush=True)
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
