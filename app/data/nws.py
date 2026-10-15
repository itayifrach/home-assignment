"""National Weather Service active alerts. Live context only - NOT used in the risk score."""
import httpx

from app.config import HTTP_TIMEOUT, USER_AGENT


def active_alerts(lat: float, lon: float) -> list[dict]:
    r = httpx.get("https://api.weather.gov/alerts/active", params={"point": f"{lat},{lon}"},
                  headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
                  timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return [
        {"event": f["properties"]["event"], "severity": f["properties"]["severity"],
         "headline": f["properties"].get("headline"), "expires": f["properties"].get("expires")}
        for f in r.json().get("features", [])
    ]
