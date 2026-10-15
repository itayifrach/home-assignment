"""Open-Meteo clients: historical daily weather (ERA5 reanalysis) and geocoding. No API key needed."""
import time
from datetime import date

import httpx

from app.config import HTTP_TIMEOUT

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

DAILY_VARS = [
    "snowfall_sum",        # cm
    "precipitation_sum",   # mm
    "temperature_2m_min",  # °C
    "temperature_2m_max",  # °C
    "wind_gusts_10m_max",  # km/h
]


def _get_with_retry(url: str, params: dict, attempts: int = 5) -> httpx.Response:
    """The free tier rate-limits multi-year requests (429 or connection resets); back off and retry."""
    for i in range(attempts):
        try:
            r = httpx.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code != 429:
                r.raise_for_status()
                return r
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if i == attempts - 1 or (isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500):
                raise
        time.sleep(5 * 2 ** i)
    raise RuntimeError(f"Open-Meteo still rate-limited after {attempts} attempts")


def fetch_daily(lat: float, lon: float, start: date, end: date) -> dict:
    """Return {"time": [...], <var>: [...]} for each day in [start, end]."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_VARS),
        "timezone": "America/New_York",
    }
    r = _get_with_retry(ARCHIVE_URL, params)
    daily = r.json()["daily"]
    # Round to keep the committed snapshot small; precision beyond 0.1 is noise for ERA5.
    return {k: ([round(v, 1) if v is not None else None for v in vals] if k != "time" else vals)
            for k, vals in daily.items()}


def geocode_us(name: str) -> dict | None:
    """Resolve a US place name to {name, state, lat, lon}. Returns None if not found in the US."""
    r = _get_with_retry(GEOCODE_URL, {"name": name, "count": 10, "countryCode": "US"}, attempts=3)
    for res in r.json().get("results", []) or []:
        if res.get("country_code") == "US":
            return {"name": res["name"], "state": res.get("admin1", ""),
                    "lat": res["latitude"], "lon": res["longitude"]}
    return None
