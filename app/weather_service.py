"""Weather service: Open-Meteo current + 6-day forecast, IP-geolocated.

No API key. No rate limit for non-commercial use. Location is resolved
once via ip-api.com on first run and cached at the location_path passed
in; edit that file (lat/lon/label) to override the IP-geo guess.

Standard service shape — thread + lock-protected snapshot + command
queue. Forecast is fetched every WEATHER_POLL_S; the WeatherScene also
nudges a refresh on entry via on_show().
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path


WEATHER_POLL_S = 900.0    # 15 minutes
HTTP_TIMEOUT_S = 6.0
GEO_URL = (
    "http://ip-api.com/json/"
    "?fields=status,message,country,city,lat,lon"
)
WX_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "clockradio/1.0 (https://github.com/RichardHaydon/radiogram)"


# WMO weather interpretation codes — i18n keys, resolved at render time
# so a language switch updates without re-fetching.
WMO_KEYS: dict[int, str] = {
    0: "weather.code.clear",
    1: "weather.code.mostly_clear",
    2: "weather.code.partly_cloudy",
    3: "weather.code.cloudy",
    45: "weather.code.fog", 48: "weather.code.fog",
    51: "weather.code.drizzle",
    53: "weather.code.drizzle",
    55: "weather.code.drizzle",
    56: "weather.code.frz_drizzle",
    57: "weather.code.frz_drizzle",
    61: "weather.code.rain",
    63: "weather.code.rain",
    65: "weather.code.heavy_rain",
    66: "weather.code.frz_rain",
    67: "weather.code.frz_rain",
    71: "weather.code.snow",
    73: "weather.code.snow",
    75: "weather.code.heavy_snow",
    77: "weather.code.snow",
    80: "weather.code.showers",
    81: "weather.code.showers",
    82: "weather.code.heavy_showers",
    85: "weather.code.snow_showers",
    86: "weather.code.snow_showers",
    95: "weather.code.storm",
    96: "weather.code.storm_hail",
    99: "weather.code.storm_hail",
}


def label_for_code(code: int) -> str:
    """Resolve a WMO code to a localised string at the moment of call.
    Importing i18n_service lazily breaks an otherwise-circular import
    (weather_service is constructed before main wires the service)."""
    try:
        from i18n_service import I18nService  # noqa: F401
        from scenes import _t                  # already wired in main
        key = WMO_KEYS.get(int(code))
        if key is not None:
            return _t(key)
        return _t("weather.code.unknown", code=code)
    except Exception:
        # Fallback to English if anything in the i18n layer is missing.
        from translations import EN
        key = WMO_KEYS.get(int(code))
        if key is not None:
            return EN.get(key, "")
        return f"Code {code}"


@dataclass(frozen=True)
class DayForecast:
    date: str            # ISO "YYYY-MM-DD"
    code: int
    label: str
    high_c: float
    low_c: float
    precip_pct: int


@dataclass(frozen=True)
class WeatherStatus:
    location: str = ""
    last_fetch_t: float = 0.0
    last_error: str = ""
    busy: bool = False
    cur_temp_c: float | None = None
    cur_code: int = 0
    cur_label: str = ""
    cur_wind_kmh: float = 0.0
    days: tuple[DayForecast, ...] = field(default_factory=tuple)


class WeatherService:
    def __init__(self, location_path: Path):
        self._location_path = Path(location_path)
        self._lock = threading.Lock()
        self._status = WeatherStatus()
        self._stop_evt = threading.Event()
        self._cmd_q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lat: float | None = None
        self._lon: float | None = None
        self._geo_label: str = ""

    @property
    def status(self) -> WeatherStatus:
        with self._lock:
            return self._status

    def refresh(self) -> None:
        self._cmd_q.put(("refresh",))

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="weather")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # --- thread internals ---------------------------------------------

    def _set(self, **kwargs) -> None:
        with self._lock:
            self._status = replace(self._status, **kwargs)

    def _run(self) -> None:
        self._safe_refresh()
        last = time.monotonic()
        while not self._stop_evt.is_set():
            try:
                cmd = self._cmd_q.get(timeout=0.5)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                self._safe_refresh()
                last = time.monotonic()
                continue
            if time.monotonic() - last >= WEATHER_POLL_S:
                self._safe_refresh()
                last = time.monotonic()

    def _safe_refresh(self) -> None:
        try:
            self._do_refresh()
        except Exception as exc:
            print(f"weather: {exc}", file=sys.stderr, flush=True)
            self._set(busy=False, last_error=str(exc))

    def _ensure_location(self) -> None:
        if self._lat is not None and self._lon is not None:
            return
        try:
            data = json.loads(self._location_path.read_text())
            self._lat = float(data["lat"])
            self._lon = float(data["lon"])
            self._geo_label = str(data.get("label", ""))
            return
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
        data = self._fetch_json(GEO_URL)
        if data.get("status") != "success":
            raise RuntimeError(
                f"geo: {data.get('message') or 'lookup failed'}")
        self._lat = float(data["lat"])
        self._lon = float(data["lon"])
        city = data.get("city", "")
        country = data.get("country", "")
        self._geo_label = ", ".join(p for p in (city, country) if p)
        try:
            self._location_path.parent.mkdir(parents=True, exist_ok=True)
            self._location_path.write_text(json.dumps({
                "lat": self._lat,
                "lon": self._lon,
                "label": self._geo_label,
            }, indent=2))
        except OSError as exc:
            print(f"location write: {exc}",
                  file=sys.stderr, flush=True)

    def _do_refresh(self) -> None:
        self._set(busy=True, last_error="")
        self._ensure_location()
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": ("weather_code,temperature_2m_max,"
                      "temperature_2m_min,precipitation_probability_max"),
            "timezone": "auto",
            "forecast_days": 6,
        }
        url = WX_URL + "?" + urllib.parse.urlencode(params)
        data = self._fetch_json(url)
        cur = data.get("current") or {}
        cur_code = int(cur.get("weather_code", 0))
        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        hi = daily.get("temperature_2m_max") or []
        lo = daily.get("temperature_2m_min") or []
        pp = daily.get("precipitation_probability_max") or []
        days: list[DayForecast] = []
        for i in range(min(len(dates), len(codes), len(hi), len(lo))):
            try:
                code = int(codes[i])
                pct = (int(pp[i]) if i < len(pp) and pp[i] is not None
                       else 0)
                days.append(DayForecast(
                    date=str(dates[i]),
                    code=code,
                    label=label_for_code(code),
                    high_c=float(hi[i]),
                    low_c=float(lo[i]),
                    precip_pct=pct,
                ))
            except (TypeError, ValueError):
                continue
        self._set(
            location=self._geo_label,
            last_fetch_t=time.monotonic(),
            last_error="",
            busy=False,
            cur_temp_c=float(cur.get("temperature_2m", 0.0)),
            cur_code=cur_code,
            # cur_label resolved at render time via label_for_code so a
            # language switch updates the displayed label without a new
            # fetch — kept as a snapshot here purely for back-compat
            # with anyone reading status.cur_label directly.
            cur_label=label_for_code(cur_code),
            cur_wind_kmh=float(cur.get("wind_speed_10m", 0.0)),
            days=tuple(days),
        )

    @staticmethod
    def _fetch_json(url: str) -> dict:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
