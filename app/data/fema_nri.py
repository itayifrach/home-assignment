"""FEMA National Risk Index (county level) via FEMA's public ArcGIS feature service.

We deliberately use the *Expected Annual Loss Rate national percentile* (`<HAZ>_ALR_NPCTL`)
rather than the headline Risk score (`<HAZ>_RISKS`). The Risk score scales with how many people
and buildings a county has, so every big-city county looks "very high" on everything. A hub is a
single facility; the loss *rate* (loss per $ exposed) reflects how harsh the hazard is per asset.
"""
import httpx

from app.config import HTTP_TIMEOUT

NRI_URL = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
           "National_Risk_Index_Counties/FeatureServer/0/query")

# NRI hazard prefix -> our name
HAZARDS = {
    "WNTW": "winter_weather",
    "ISTM": "ice_storm",
    "HRCN": "hurricane",
    "IFLD": "inland_flood",  # FEMA renamed "Riverine Flooding" to "Inland Flooding" in NRI v1.20
    "CFLD": "coastal_flood",
    "TRND": "tornado",
    "HAIL": "hail",
    "HWAV": "heat_wave",
    "SWND": "strong_wind",
}


def _fields() -> list[str]:
    out = ["STCOFIPS", "COUNTY", "STATEABBRV", "RISK_SCORE", "RISK_RATNG"]
    for p in HAZARDS:
        out += [f"{p}_ALR_NPCTL", f"{p}_AFREQ", f"{p}_RISKR"]
    return out


def fetch_counties(fips_codes: list[str]) -> dict[str, dict]:
    """Return {fips: {hazard: {"loss_rate_pctl", "annual_freq", "fema_rating"}, ...}}."""
    where = "STCOFIPS IN (" + ",".join(f"'{f}'" for f in fips_codes) + ")"
    r = httpx.get(NRI_URL, params={"where": where, "outFields": ",".join(_fields()),
                                   "returnGeometry": "false", "f": "json"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for feat in r.json()["features"]:
        a = feat["attributes"]
        rec = {"county": a["COUNTY"], "state": a["STATEABBRV"],
               "composite_risk_score": a["RISK_SCORE"], "composite_risk_rating": a["RISK_RATNG"],
               "hazards": {}}
        for p, name in HAZARDS.items():
            rec["hazards"][name] = {
                # None means FEMA says the hazard is not applicable (e.g. coastal flood inland) -> 0
                "loss_rate_pctl": round(a.get(f"{p}_ALR_NPCTL") or 0.0, 2),
                "annual_freq": round(a.get(f"{p}_AFREQ") or 0.0, 3),
                "fema_rating": a.get(f"{p}_RISKR"),
            }
        out[a["STCOFIPS"]] = rec
    return out
