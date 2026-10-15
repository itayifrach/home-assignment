"""Committed data snapshot: the scoring inputs, versioned in git so the demo never depends on live APIs.

Layout:
  data/snapshot/meta.json            {fetched_at, climate_start, climate_end, sources}
  data/snapshot/nri.json             {county_fips: {...}}
  data/snapshot/weather/<hub>.json   Open-Meteo daily arrays for the climatology window
"""
import json
from functools import lru_cache

from app.config import SNAPSHOT_DIR

WEATHER_DIR = SNAPSHOT_DIR / "weather"


def save_weather(hub_id: str, daily: dict) -> None:
    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    (WEATHER_DIR / f"{hub_id}.json").write_text(json.dumps(daily, separators=(",", ":")), encoding="utf-8")


def save_json(name: str, obj) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (SNAPSHOT_DIR / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")


@lru_cache
def load_weather(hub_id: str) -> dict:
    return json.loads((WEATHER_DIR / f"{hub_id}.json").read_text(encoding="utf-8"))


@lru_cache
def load_nri() -> dict:
    return json.loads((SNAPSHOT_DIR / "nri.json").read_text(encoding="utf-8"))


@lru_cache
def load_meta() -> dict:
    return json.loads((SNAPSHOT_DIR / "meta.json").read_text(encoding="utf-8"))


def clear_caches() -> None:
    load_weather.cache_clear()
    load_nri.cache_clear()
    load_meta.cache_clear()
