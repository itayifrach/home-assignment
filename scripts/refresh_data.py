"""Fetch all scoring inputs from public APIs and write the committed snapshot.

Usage:  python -m scripts.refresh_data
"""
import sys
import time
from datetime import datetime, timezone

from app.config import CLIMATE_END, CLIMATE_START
from app.data import fema_nri, open_meteo, snapshot
from app.data.hubs import load_hubs


def main(force: bool = False) -> None:
    hubs = load_hubs()

    print(f"FEMA NRI: fetching {len(hubs)} counties...")
    nri = fema_nri.fetch_counties([h["county_fips"] for h in hubs])
    missing = [h["id"] for h in hubs if h["county_fips"] not in nri]
    if missing:
        raise SystemExit(f"NRI missing counties for hubs: {missing}")
    snapshot.save_json("nri.json", nri)

    for h in hubs:
        if not force and (snapshot.WEATHER_DIR / f"{h['id']}.json").exists():
            print(f"Open-Meteo: {h['id']} already in snapshot, skipping (use --force to refetch)")
            continue
        print(f"Open-Meteo: {h['id']} {CLIMATE_START}..{CLIMATE_END}", flush=True)
        daily = open_meteo.fetch_daily(h["lat"], h["lon"], CLIMATE_START, CLIMATE_END)
        snapshot.save_weather(h["id"], daily)
        time.sleep(3)  # multi-year requests are "heavy" on the free tier

    snapshot.save_json("meta.json", {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "climate_start": CLIMATE_START.isoformat(),
        "climate_end": CLIMATE_END.isoformat(),
        "sources": {
            "weather": "Open-Meteo Historical Weather API (ERA5 reanalysis), archive-api.open-meteo.com",
            "hazard": "FEMA National Risk Index, county level (Expected Annual Loss Rate national percentile)",
        },
    })
    print("Snapshot written to data/snapshot/")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
