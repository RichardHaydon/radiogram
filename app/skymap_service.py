"""Realistic local sky renderer — bright stars, constellations, Sun,
Moon, and the five naked-eye planets, projected onto a horizon dome.

Projection
----------
Equidistant azimuthal: zenith at the disc centre, horizon at the limb.
Radius ρ = (90° - alt) / 90°. North up, east at 90° clockwise from
north (so an observer "looking down at the sky from outside" matches
the printed star map convention).

Ephemeris
---------
Schlyter's (Paul Schlyter, stjarnhimlen.se) low-precision Keplerian
formulas. ~0.1° accuracy for the planets and Sun, ~0.5° for the Moon —
plenty at 720x800 where 1° ≈ 4 px.

Star data
---------
Bundled CSVs in data/:
    stars.csv          mag <= 5.5 plus every star referenced by a
                       constellation polyline (HYG v4.2 sourced).
    constellations.csv polyline vertex sequences keyed by HIP id
                       (Stellarium western skyculture).

Caller integration
------------------
WorldMapService dispatches to render() when the active style has
is_starmap=True. The same minute-bucket cache wraps it so a starmap
background only renders once per minute.
"""
from __future__ import annotations

import csv
import math
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


DATA_DIR = Path(__file__).resolve().parent / "data"
STARS_CSV = DATA_DIR / "stars.csv"
CONST_CSV = DATA_DIR / "constellations.csv"


# --- time helpers ---------------------------------------------------

def _days_since_2000(now: datetime) -> float:
    """Schlyter's 'd' parameter — days since 2000 Jan 0.0 UT
    (= JD 2451543.5)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    # Calendar -> JD via Meeus eq 7.1.
    y, m = now.year, now.month
    d = (now.day + now.hour / 24.0
         + now.minute / 1440.0 + now.second / 86400.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = (math.floor(365.25 * (y + 4716))
          + math.floor(30.6001 * (m + 1))
          + d + b - 1524.5)
    return jd - 2451543.5


def _gmst_deg(d: float) -> float:
    """Greenwich mean sidereal time, degrees, 0..360. Schlyter:
    GMST0 (h) = (Sun_mean_long + 180) / 15, then add UT hours."""
    # Mean longitude of Sun (Schlyter's L = M + w where for Sun
    # M = 356.0470 + 0.9856002585*d and w = 282.9404 + 4.70935e-5*d).
    L_sun = 356.0470 + 0.9856002585 * d + 282.9404 + 4.70935e-5 * d
    # Hours since 2000 Jan 0.0 UT, mod 24.
    ut_hours = (d - math.floor(d)) * 24.0
    gmst_h = (L_sun + 180.0) / 15.0 + ut_hours
    return (gmst_h * 15.0) % 360.0


def _lst_deg(d: float, observer_lon_deg: float) -> float:
    """Local mean sidereal time, degrees."""
    return (_gmst_deg(d) + observer_lon_deg) % 360.0


# --- Kepler / orbital -----------------------------------------------

def _solve_kepler(M_deg: float, e: float) -> float:
    """Solve M = E - e*sin(E) for E. Newton iteration; converges in
    3-4 steps for our eccentricities."""
    M = math.radians(M_deg % 360.0)
    E = M + e * math.sin(M) * (1.0 + e * math.cos(M))
    for _ in range(6):
        delta = (E - e * math.sin(E) - M) / (1.0 - e * math.cos(E))
        E -= delta
        if abs(delta) < 1e-9:
            break
    return E


def _heliocentric_ecliptic(elem: dict, d: float) -> tuple[float, float, float, float]:
    """Apply Schlyter's element formulas, solve Kepler, return
    (xh, yh, zh, r) in the body's own distance unit (AU for planets,
    Earth radii for Moon)."""
    N = math.radians(elem["N"](d))
    i = math.radians(elem["i"](d))
    w = elem["w"](d)
    a = elem["a"]
    e = elem["e"](d) if callable(elem["e"]) else elem["e"]
    M = elem["M"](d)
    E = _solve_kepler(M, e)
    xv = a * (math.cos(E) - e)
    yv = a * math.sqrt(1 - e * e) * math.sin(E)
    r = math.hypot(xv, yv)
    v = math.atan2(yv, xv)
    vw = v + math.radians(w)
    xh = r * (math.cos(N) * math.cos(vw)
              - math.sin(N) * math.sin(vw) * math.cos(i))
    yh = r * (math.sin(N) * math.cos(vw)
              + math.cos(N) * math.sin(vw) * math.cos(i))
    zh = r * math.sin(vw) * math.sin(i)
    return xh, yh, zh, r


# Schlyter element tables. Each lambda takes d and returns the element
# value at that moment (deg or AU). 'a' is constant for our precision.
SUN = {
    "N": lambda d: 0.0,
    "i": lambda d: 0.0,
    "w": lambda d: 282.9404 + 4.70935e-5 * d,
    "a": 1.0,
    "e": lambda d: 0.016709 - 1.151e-9 * d,
    "M": lambda d: 356.0470 + 0.9856002585 * d,
}
MOON = {
    "N": lambda d: 125.1228 - 0.0529538083 * d,
    "i": lambda d: 5.1454,
    "w": lambda d: 318.0634 + 0.1643573223 * d,
    "a": 60.2666,           # Earth radii — converted to AU only for
                            # geocentric subtraction (we don't need
                            # absolute distance for plotting).
    "e": 0.054900,
    "M": lambda d: 115.3654 + 13.0649929509 * d,
}
MERCURY = {
    "N": lambda d: 48.3313 + 3.24587e-5 * d,
    "i": lambda d: 7.0047 + 5.00e-8 * d,
    "w": lambda d: 29.1241 + 1.01444e-5 * d,
    "a": 0.387098,
    "e": lambda d: 0.205635 + 5.59e-10 * d,
    "M": lambda d: 168.6562 + 4.0923344368 * d,
}
VENUS = {
    "N": lambda d: 76.6799 + 2.46590e-5 * d,
    "i": lambda d: 3.3946 + 2.75e-8 * d,
    "w": lambda d: 54.8910 + 1.38374e-5 * d,
    "a": 0.723330,
    "e": lambda d: 0.006773 - 1.302e-9 * d,
    "M": lambda d: 48.0052 + 1.6021302244 * d,
}
MARS = {
    "N": lambda d: 49.5574 + 2.11081e-5 * d,
    "i": lambda d: 1.8497 - 1.78e-8 * d,
    "w": lambda d: 286.5016 + 2.92961e-5 * d,
    "a": 1.523688,
    "e": lambda d: 0.093405 + 2.516e-9 * d,
    "M": lambda d: 18.6021 + 0.5240207766 * d,
}
JUPITER = {
    "N": lambda d: 100.4542 + 2.76854e-5 * d,
    "i": lambda d: 1.3030 - 1.557e-7 * d,
    "w": lambda d: 273.8777 + 1.64505e-5 * d,
    "a": 5.20256,
    "e": lambda d: 0.048498 + 4.469e-9 * d,
    "M": lambda d: 19.8950 + 0.0830853001 * d,
}
SATURN = {
    "N": lambda d: 113.6634 + 2.38980e-5 * d,
    "i": lambda d: 2.4886 - 1.081e-7 * d,
    "w": lambda d: 339.3939 + 2.97661e-5 * d,
    "a": 9.55475,
    "e": lambda d: 0.055546 - 9.499e-9 * d,
    "M": lambda d: 316.9670 + 0.0334442282 * d,
}


# --- ephemeris ------------------------------------------------------

def _obliquity_deg(d: float) -> float:
    return 23.4393 - 3.563e-7 * d


def _ecl_to_eq(xg: float, yg: float, zg: float,
               ecl_deg: float) -> tuple[float, float]:
    """Geocentric ecliptic xyz -> RA, Dec (radians)."""
    ecl = math.radians(ecl_deg)
    xe = xg
    ye = yg * math.cos(ecl) - zg * math.sin(ecl)
    ze = yg * math.sin(ecl) + zg * math.cos(ecl)
    ra = math.atan2(ye, xe) % (2 * math.pi)
    dec = math.atan2(ze, math.hypot(xe, ye))
    return ra, dec


def _sun_radec(d: float) -> tuple[float, float, tuple[float, float, float]]:
    """Sun geocentric RA, Dec (radians) and ecliptic xyz (AU). The
    ecliptic xyz is the heliocentric Earth → Sun vector, used for
    converting other planets' heliocentric positions to geocentric."""
    xh, yh, zh, _ = _heliocentric_ecliptic(SUN, d)
    # Sun's geocentric ecliptic = its heliocentric (since heliocentric
    # SUN is the Sun-Earth vector — flipped sign convention in
    # Schlyter where Sun's "elements" describe the Sun as seen from
    # Earth, not Earth's orbit).
    ra, dec = _ecl_to_eq(xh, yh, zh, _obliquity_deg(d))
    return ra, dec, (xh, yh, zh)


def _planet_radec(elem: dict, d: float,
                  earth_xyz: tuple[float, float, float]
                  ) -> tuple[float, float]:
    xh, yh, zh, _ = _heliocentric_ecliptic(elem, d)
    # Geocentric = heliocentric - Earth's heliocentric. In Schlyter's
    # frame, the Sun's vector IS the Earth->Sun vector, so the Earth's
    # heliocentric is -that.
    xg = xh - earth_xyz[0]
    yg = yh - earth_xyz[1]
    zg = zh - earth_xyz[2]
    return _ecl_to_eq(xg, yg, zg, _obliquity_deg(d))


def _moon_radec(d: float) -> tuple[float, float, float]:
    """Moon geocentric RA, Dec (radians) plus phase angle in radians.
    Phase angle 0 = new moon, π = full moon."""
    xh, yh, zh, _ = _heliocentric_ecliptic(MOON, d)
    # MOON's elements are already geocentric; xh/yh/zh are Earth-centred
    # ecliptic coords in Earth radii. Plotting only needs the unit
    # vector, so distance scale is irrelevant.
    ra, dec = _ecl_to_eq(xh, yh, zh, _obliquity_deg(d))
    # Phase angle: angular separation Sun-Earth-Moon. Approximated as
    # the angular distance Moon ↔ Sun in ecliptic longitude — accurate
    # enough for shading the lit fraction.
    sun_ra, sun_dec, _ = _sun_radec(d)
    cos_sep = (math.sin(dec) * math.sin(sun_dec)
               + math.cos(dec) * math.cos(sun_dec)
               * math.cos(ra - sun_ra))
    phase_angle = math.acos(max(-1.0, min(1.0, cos_sep)))
    return ra, dec, phase_angle


# --- altaz transform ------------------------------------------------

def _radec_to_altaz(ra_rad: np.ndarray, dec_rad: np.ndarray,
                    lst_deg: float, lat_deg: float
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised RA/Dec -> alt/az (radians). az is measured from
    north going clockwise (i.e. az=0 north, π/2 east)."""
    lat = math.radians(lat_deg)
    lst = math.radians(lst_deg)
    ha = lst - ra_rad
    sin_alt = (np.sin(dec_rad) * math.sin(lat)
               + np.cos(dec_rad) * math.cos(lat) * np.cos(ha))
    sin_alt = np.clip(sin_alt, -1.0, 1.0)
    alt = np.arcsin(sin_alt)
    # az measured from north toward east.
    az = np.arctan2(
        -np.cos(dec_rad) * np.sin(ha),
        np.sin(dec_rad) * math.cos(lat)
        - np.cos(dec_rad) * np.cos(ha) * math.sin(lat),
    )
    return alt, az


# --- catalogue loaders ----------------------------------------------

# Map spectral class first letter → tint applied to the white star core.
# Astronomically motivated (Wien-displacement-ish): hot O/B blue, cool
# K/M red. Empty/unknown → white.
SPECTRAL_TINT = {
    "O": (175, 200, 255),
    "B": (200, 220, 255),
    "A": (230, 235, 255),
    "F": (250, 250, 245),
    "G": (255, 240, 220),
    "K": (255, 215, 180),
    "M": (255, 195, 165),
}


def _load_stars() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[int, int]]:
    """Returns (ra_rad, dec_rad, mag, tint_rgb, hip_to_idx).

    tint_rgb is an N×3 uint8 array; hip_to_idx maps HIP -> index for
    the constellation line lookup."""
    if not STARS_CSV.exists():
        return (np.zeros(0), np.zeros(0), np.zeros(0),
                np.zeros((0, 3), dtype=np.uint8), {})
    ras: list[float] = []
    decs: list[float] = []
    mags: list[float] = []
    tints: list[tuple[int, int, int]] = []
    hip_to_idx: dict[int, int] = {}
    with STARS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                hip = int(row["hip"])
                ra = float(row["ra_deg"])
                dec = float(row["dec_deg"])
                mag = float(row["mag"])
            except (KeyError, ValueError):
                continue
            spect = (row.get("spect") or "")[:1].upper()
            tint = SPECTRAL_TINT.get(spect, (245, 245, 250))
            hip_to_idx[hip] = len(ras)
            ras.append(math.radians(ra))
            decs.append(math.radians(dec))
            mags.append(mag)
            tints.append(tint)
    return (
        np.array(ras, dtype=np.float64),
        np.array(decs, dtype=np.float64),
        np.array(mags, dtype=np.float32),
        np.array(tints, dtype=np.uint8),
        hip_to_idx,
    )


def _load_polylines(hip_to_idx: dict[int, int]) -> list[list[int]]:
    """Returns list of polylines, each a list of star indices into the
    star arrays. HIPs missing from the catalogue (shouldn't happen
    given the build script's guard) are silently dropped."""
    if not CONST_CSV.exists():
        return []
    by_id: dict[int, list[int]] = {}
    with CONST_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pid = int(row["polyline_id"])
                hip = int(row["hip"])
            except (KeyError, ValueError):
                continue
            idx = hip_to_idx.get(hip)
            if idx is None:
                continue
            by_id.setdefault(pid, []).append(idx)
    return [v for v in by_id.values() if len(v) >= 2]


# --- service --------------------------------------------------------

# Sky base colour at full astronomical night vs. midday.
NIGHT_BG = np.array([5, 8, 18], dtype=np.float32)
DAY_BG = np.array([100, 145, 200], dtype=np.float32)

# Default observer if no location is configured. London-ish — the
# weather service will normally have a real fix by the time the user
# selects this style, but we pick a temperate northern latitude as a
# reasonable starting point.
DEFAULT_LAT = 51.5
DEFAULT_LON = -0.1


class SkymapService:
    """Loads bundled star data once, renders horizon-view images on
    demand. Held by WorldMapService when a starmap style is active."""

    def __init__(self, location_path: Path | None = None):
        self._location_path = location_path
        self._lock = threading.Lock()
        self._lat = DEFAULT_LAT
        self._lon = DEFAULT_LON
        self._location_loaded_at: float = -1.0
        (self._ra, self._dec, self._mag,
         self._tint, hip_to_idx) = _load_stars()
        self._polylines = _load_polylines(hip_to_idx)
        if self._ra.size == 0:
            print("skymap: no stars loaded — data/stars.csv missing?",
                  file=sys.stderr, flush=True)
        else:
            print(f"skymap: {self._ra.size} stars, "
                  f"{len(self._polylines)} polylines",
                  file=sys.stderr, flush=True)

    def _maybe_refresh_location(self) -> None:
        """Re-read location.json on every render — cheap, and the
        weather service may have just produced a first fix."""
        if self._location_path is None:
            return
        try:
            import json
            data = json.loads(self._location_path.read_text())
            self._lat = float(data["lat"])
            self._lon = float(data["lon"])
        except (OSError, KeyError, ValueError):
            # Stick with whatever we had (default or previous read).
            pass

    def location(self) -> tuple[float, float]:
        return self._lat, self._lon

    def render(self, canvas_w: int, canvas_h: int,
               *, now: datetime | None = None,
               draw_constellations: bool = True,
               planet_labels: bool = False) -> Image.Image:
        self._maybe_refresh_location()
        lat = self._lat
        lon = self._lon
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        d = _days_since_2000(now)
        lst = _lst_deg(d, lon)

        # Sun first — its altitude controls the sky-base blend, and we
        # need its xyz to convert other bodies to geocentric.
        sun_ra, sun_dec, earth_xyz = _sun_radec(d)
        sun_alt, sun_az = _radec_to_altaz(
            np.array([sun_ra]), np.array([sun_dec]), lst, lat)
        sun_alt_deg = float(np.degrees(sun_alt[0]))
        # Day-blue blend factor, 0..1. Twilight band -6..+6 deg.
        day_factor = float(np.clip((sun_alt_deg + 6.0) / 12.0, 0.0, 1.0))

        # Compose sky base. Outside the disc is "ground" — the deeper
        # underground colour we paint when below the horizon. Inside,
        # blend night→day by sun altitude.
        sky_color = NIGHT_BG * (1 - day_factor) + DAY_BG * day_factor
        ground_color = np.array([18, 14, 10], dtype=np.float32)

        # Disc geometry: fit into short axis with a small margin so
        # constellation labels at the limb don't get clipped.
        margin_frac = 0.05
        radius = (min(canvas_w, canvas_h) * 0.5) * (1.0 - margin_frac)
        cx = canvas_w * 0.5
        cy = canvas_h * 0.5

        # Build base canvas: sky disc + dark ground outside.
        rgb = np.empty((canvas_h, canvas_w, 3), dtype=np.float32)
        ys = np.arange(canvas_h, dtype=np.float32) + 0.5 - cy
        xs = np.arange(canvas_w, dtype=np.float32) + 0.5 - cx
        yy, xx = np.meshgrid(ys, xs, indexing="ij")
        rho = np.sqrt(xx * xx + yy * yy) / radius
        inside = rho <= 1.0
        # Soft horizon haze — visible for the day case, but suppressed
        # at night so the stars near the horizon stay legible.
        haze_strength = 0.05 + 0.20 * day_factor
        horizon_lift = np.clip(rho, 0.0, 1.0) ** 2 * haze_strength
        sky_pixels = sky_color[None, None, :] * (1.0 + horizon_lift[..., None])
        rgb[...] = np.where(
            inside[..., None],
            sky_pixels,
            ground_color[None, None, :],
        )

        # Stars — vectorised altaz then a per-star scatter into the
        # numpy buffer. Skip everything below the horizon. Brightness
        # falls off with day_factor so sky-blue washes them out as the
        # day advances.
        if self._ra.size > 0:
            alt, az = _radec_to_altaz(self._ra, self._dec, lst, lat)
            visible = alt > 0
            self._scatter_stars(
                rgb, alt[visible], az[visible],
                self._mag[visible], self._tint[visible],
                cx, cy, radius, day_factor,
            )

        # Convert to PIL — RGBA so vector overlays can use alpha for
        # soft constellation lines and Sun haloes. Final flatten back
        # to RGB at the bottom of this function.
        rgba = Image.fromarray(
            np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

        if draw_constellations and self._polylines:
            rgba = self._draw_constellations(rgba, lst, lat, cx, cy, radius,
                                             day_factor)

        # Planets, sun, moon — small ImageDraw paths, drawn on top.
        rgba = self._draw_solar_system(rgba, d, lst, lat, cx, cy, radius,
                                       day_factor, planet_labels=planet_labels)

        # Horizon ring + cardinal labels.
        rgba = self._draw_horizon_ring(rgba, cx, cy, radius)

        return rgba.convert("RGB")

    # -- star scatter -------------------------------------------------

    @staticmethod
    def _scatter_stars(rgb: np.ndarray,
                       alt: np.ndarray, az: np.ndarray,
                       mag: np.ndarray, tint: np.ndarray,
                       cx: float, cy: float, radius: float,
                       day_factor: float) -> None:
        """Place stars as soft round discs sized by magnitude. Bright
        stars (mag<2) get a small bloom; mag-5 stars drop to a single
        pixel. Light stars are colour-tinted toward their spectral hue
        but kept near-white so the sky doesn't look like neon."""
        if alt.size == 0:
            return
        # Equidistant: rho_disc = (90° - alt) / 90° in [0, 1].
        rho_disc = (math.pi / 2.0 - alt) / (math.pi / 2.0)
        # az measured from north going east; canvas y points down so
        # north-up means y = cy - rho*cos(az), x = cx + rho*sin(az).
        sx = cx + rho_disc * np.sin(az) * radius
        sy = cy - rho_disc * np.cos(az) * radius
        h, w = rgb.shape[:2]
        # Brightness: scale by 2.512^(3.0 - mag) so mag-3 is the
        # reference. Bedside-display stylisation — a real dark sky has
        # a huge dynamic range; we punch up the dim end so faint stars
        # are still visible from across the room. Clip to keep mag-0
        # stars from blowing out into pure white.
        bright = np.clip(2.512 ** (3.0 - mag), 0.0, 14.0)
        # Day fade: stars vanish almost completely as the sky brightens.
        # Cubed so even bright stars are gone by mid-twilight, matching
        # what the eye actually sees from a city street.
        bright = bright * max(0.0, (1.0 - day_factor) ** 3)
        # Disc radius by magnitude bucket (px). Bright stars get a tiny
        # core + halo; faint stars are a single pixel so they don't
        # smear into one another.
        for low, high, kernel in _STAR_KERNELS:
            mask = (mag >= low) & (mag < high)
            if not np.any(mask):
                continue
            px = sx[mask].astype(np.int32)
            py = sy[mask].astype(np.int32)
            keep = (px >= 0) & (px < w) & (py >= 0) & (py < h)
            if not np.any(keep):
                continue
            px = px[keep]; py = py[keep]
            tints = tint[mask][keep].astype(np.float32) / 255.0
            br = bright[mask][keep][:, None]
            for dx, dy, weight in kernel:
                pyk = py + dy
                pxk = px + dx
                # Re-clip after the kernel offset.
                ok = ((pxk >= 0) & (pxk < w)
                      & (pyk >= 0) & (pyk < h))
                pyk = pyk[ok]; pxk = pxk[ok]
                add = (tints[ok] * br[ok] * weight * 255.0)
                # np.add.at avoids the racing-write bug of fancy
                # indexing when the same pixel appears twice in a
                # batch — fine here since we stay under 0.1ms anyway.
                np.add.at(rgb, (pyk, pxk), add)


# Bloom kernels per magnitude band. Each tuple is
# (mag_low, mag_high_exclusive, [(dx, dy, weight), ...]).
# Bright stars get a chunky disc + halo so they read as the eye-catching
# anchors of constellations; faint stars stay subdued so the sky doesn't
# read as a uniform fog of dots.
def _disc_kernel(radius: float, core: float = 1.0
                 ) -> list[tuple[int, int, float]]:
    """Round Gaussian-ish kernel out to ceil(radius) pixels. Weight at
    distance r drops as exp(-(r/sigma)^2) with sigma chosen so the rim
    pixel gets ~10% of core."""
    out: list[tuple[int, int, float]] = []
    sigma = max(0.5, radius / 1.5)
    rmax = int(math.ceil(radius)) + 1
    for dy in range(-rmax, rmax + 1):
        for dx in range(-rmax, rmax + 1):
            r = math.hypot(dx, dy)
            if r > radius + 0.5:
                continue
            w = math.exp(-(r * r) / (2.0 * sigma * sigma)) * core
            if w < 0.04:
                continue
            out.append((dx, dy, w))
    return out


_STAR_KERNELS: list[tuple[float, float, list[tuple[int, int, float]]]] = [
    (-2.0, 0.5, _disc_kernel(3.5, core=1.0)),
    (0.5, 1.5, _disc_kernel(2.5, core=0.9)),
    (1.5, 2.5, _disc_kernel(2.0, core=0.75)),
    (2.5, 3.5, _disc_kernel(1.5, core=0.55)),
    (3.5, 4.5, _disc_kernel(1.0, core=0.40)),
    (4.5, 99.0, [(0, 0, 0.25)]),
]


# --- vector overlays (constellations, planets, labels) --------------

def _altaz_to_xy(alt_deg: np.ndarray, az_rad: np.ndarray,
                 cx: float, cy: float, radius: float
                 ) -> tuple[np.ndarray, np.ndarray]:
    rho = (90.0 - alt_deg) / 90.0
    return (cx + rho * np.sin(az_rad) * radius,
            cy - rho * np.cos(az_rad) * radius)


def _radec_xy(ra_rad: float, dec_rad: float,
              lst_deg: float, lat_deg: float,
              cx: float, cy: float, radius: float
              ) -> tuple[float, float, float]:
    """Returns (x, y, alt_deg). Caller checks alt_deg > 0 for above-horizon."""
    alt, az = _radec_to_altaz(np.array([ra_rad]), np.array([dec_rad]),
                              lst_deg, lat_deg)
    alt_deg = float(np.degrees(alt[0]))
    az_rad = float(az[0])
    rho = (90.0 - alt_deg) / 90.0
    x = cx + rho * math.sin(az_rad) * radius
    y = cy - rho * math.cos(az_rad) * radius
    return x, y, alt_deg


# Add the methods that need access to the loaded data via SkymapService.
# Defining them as free functions keeps the class body short, but they
# need to be bound to the class — done at the bottom of the file.

def _draw_constellations(self, img: Image.Image,
                         lst: float, lat: float,
                         cx: float, cy: float, radius: float,
                         day_factor: float) -> Image.Image:
    """Faint polylines connecting catalogued star pairs. Drawn on an
    RGBA overlay so the line colour blends rather than overwrites
    pixels (so a bright star sitting on a line still reads brightly).
    Lines below horizon are dropped on a per-segment basis."""
    base_alpha = int(round(130 * (1.0 - day_factor) ** 2))
    if base_alpha < 8:
        return img
    alt, az = _radec_to_altaz(self._ra, self._dec, lst, lat)
    rho = (math.pi / 2.0 - alt) / (math.pi / 2.0)
    sx = cx + rho * np.sin(az) * radius
    sy = cy - rho * np.cos(az) * radius
    above = alt > math.radians(0.5)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    line_color = (170, 200, 240, base_alpha)
    for poly in self._polylines:
        for a, b in zip(poly, poly[1:]):
            if not (above[a] and above[b]):
                continue
            d.line([(float(sx[a]), float(sy[a])),
                    (float(sx[b]), float(sy[b]))],
                   fill=line_color, width=1)
    return Image.alpha_composite(img, overlay)


def _draw_solar_system(self, img: Image.Image,
                       d: float, lst: float, lat: float,
                       cx: float, cy: float, radius: float,
                       day_factor: float,
                       *, planet_labels: bool) -> Image.Image:
    """Sun, Moon, and the five naked-eye planets as labelled discs.
    Drawn on an RGBA overlay so the Sun halo can use alpha cleanly."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    sun_ra, sun_dec, earth_xyz = _sun_radec(d)
    moon_ra, moon_dec, phase = _moon_radec(d)
    bodies = [
        (sun_ra, sun_dec, (255, 230, 150), 9, "Sun"),
        (moon_ra, moon_dec, (235, 235, 220), 11, "Moon"),
    ]
    # Planet RA/Dec via Schlyter heliocentric subtraction. Earth's
    # heliocentric is the negation of Sun's (Schlyter convention).
    earth_helio = (-earth_xyz[0], -earth_xyz[1], -earth_xyz[2])
    for elem, name, color, sz in (
        (MERCURY, "Mercury", (210, 180, 150), 4),
        (VENUS, "Venus", (255, 240, 210), 6),
        (MARS, "Mars", (240, 130, 90), 5),
        (JUPITER, "Jupiter", (240, 220, 180), 7),
        (SATURN, "Saturn", (230, 210, 160), 6),
    ):
        ra, dec = _planet_radec(elem, d, earth_helio)
        bodies.append((ra, dec, color, sz, name))

    try:
        font_size = max(10, int(min(img.size) * 0.022))
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            font_size)
    except OSError:
        font = ImageFont.load_default()

    for ra, dec, color, sz, name in bodies:
        x, y, alt_deg = _radec_xy(float(ra), float(dec),
                                  lst, lat, cx, cy, radius)
        if alt_deg < 0:
            continue
        if name == "Sun":
            # Soft halo (only visible while it's above the horizon —
            # a daytime cue rather than a night-sky element).
            for r, alpha in ((sz + 12, 35), (sz + 6, 85)):
                draw.ellipse([x - r, y - r, x + r, y + r],
                             fill=(255, 220, 130, alpha))
            draw.ellipse([x - sz, y - sz, x + sz, y + sz],
                         fill=(255, 245, 220, 255))
        elif name == "Moon":
            _draw_moon_disc(draw, x, y, sz, phase)
        else:
            draw.ellipse([x - sz, y - sz, x + sz, y + sz],
                         fill=color + (255,),
                         outline=(20, 20, 30, 255))
        if planet_labels and name not in ("Sun", "Moon"):
            tx = x + sz + 3
            ty = y - font_size * 0.6
            draw.text((tx, ty), name, font=font,
                      fill=(220, 230, 255, 230))
    return Image.alpha_composite(img, overlay)


def _draw_moon_disc(draw: ImageDraw.ImageDraw,
                    x: float, y: float, r: int,
                    phase_angle_rad: float) -> None:
    """Phase-shaded Moon disc. phase_angle_rad ∈ [0, π]: 0 = new, π/2
    = quarter, π = full. We render the disc fully lit, then overlay a
    dark ellipse positioned to leave the correct lit fraction on the
    right side. East/west orientation is stylised — at our display
    size a scientifically-correct rotation would be sub-pixel."""
    full = (235, 235, 220, 255)
    dark = (40, 38, 50, 240)
    draw.ellipse([x - r, y - r, x + r, y + r],
                 fill=full, outline=(255, 255, 240, 255))
    if r < 4:
        return
    cos_p = math.cos(phase_angle_rad)
    if abs(cos_p) > 0.96:
        # Near full or near new — skip the terminator overlay.
        if cos_p > 0:
            # Near new: render mostly dark.
            draw.ellipse([x - r, y - r, x + r, y + r], fill=dark)
        return
    # Width of the terminator-ellipse semi-major axis: |r*cos(phase)|.
    # Less than half lit (waxing/waning crescent) when cos(phase) > 0;
    # more than half lit (gibbous) when cos(phase) < 0.
    w = abs(r * cos_p)
    if cos_p > 0:
        # Less than half lit. Cover everything except a thin crescent
        # on the right side.
        draw.ellipse([x - r, y - r, x + r, y + r], fill=dark)
        # Lit crescent: vertical ellipse half on the right side.
        draw.chord([x - w, y - r, x + w, y + r], 270, 90, fill=full)
        draw.chord([x - r, y - r, x + r, y + r], 270, 90, fill=full)
    else:
        # Gibbous — dark sliver on the left.
        draw.chord([x - r, y - r, x + r, y + r], 90, 270, fill=dark)
        draw.chord([x - w, y - r, x + w, y + r], 90, 270, fill=full)


def _draw_horizon_ring(self, img: Image.Image,
                       cx: float, cy: float, radius: float
                       ) -> Image.Image:
    """Faint circle marking the horizon plus N/E/S/W cardinal labels."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    ring_color = (130, 150, 175, 110)
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    d.ellipse(bbox, outline=ring_color, width=2)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            max(11, int(min(img.size) * 0.026)))
    except OSError:
        font = ImageFont.load_default()
    label_color = (200, 215, 235, 220)
    for label, ax, ay in (
        ("N", 0, -1), ("E", 1, 0), ("S", 0, 1), ("W", -1, 0),
    ):
        pad = max(8, int(radius * 0.04))
        bbox_text = d.textbbox((0, 0), label, font=font)
        tw = bbox_text[2] - bbox_text[0]
        th = bbox_text[3] - bbox_text[1]
        tx = cx + ax * (radius + pad) - tw / 2
        ty = cy + ay * (radius + pad) - th / 2
        d.text((tx, ty), label, font=font, fill=label_color)
    return Image.alpha_composite(img, overlay)


# Bind the free-function methods onto SkymapService.
SkymapService._draw_constellations = _draw_constellations
SkymapService._draw_solar_system = _draw_solar_system
SkymapService._draw_horizon_ring = _draw_horizon_ring
