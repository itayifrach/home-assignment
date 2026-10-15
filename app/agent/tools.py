"""Tools the LLM can call. Every number the agent reports comes from one of these deterministic functions."""
import json
from datetime import date

from app.config import CLIMATE_END, CLIMATE_START
from app.data import nws, open_meteo, snapshot
from app.data.hubs import REGIONS, get_hub, hub_ids, hubs_in_region
from app.scoring.metrics import DAY_METRICS, count_days, totals
from app.scoring.scoring import get_scores, load_weights, rank_hubs
from app.storage import db

HAZARD_KEYS = ["winter", "hurricane", "flood", "severe_storm", "heat"]


class ToolError(Exception):
    """An error the model should see and can recover from (bad hub, bad date...)."""


def _hub_or_error(hub_id: str) -> dict:
    hub = get_hub(hub_id)
    if not hub:
        raise ToolError(f"Unknown hub '{hub_id}'. Valid hub ids: {', '.join(hub_ids())}")
    return hub


# --- tool implementations ---------------------------------------------------------
def t_list_hubs(region: str | None) -> dict:
    return {"hubs": [{k: h[k] for k in ("id", "name", "state", "region")} for h in hubs_in_region(region)]}


def t_rank_hubs(hazard: str, region: str | None, top_n: int | None) -> dict:
    ranking = rank_hubs(hazard, region, top_n)
    if not ranking:
        raise ToolError(f"No hubs in region '{region}'. Valid regions: {REGIONS}")
    return {"hazard": hazard, "region": region or "all", "ranking": ranking,
            "note": "Scores are 0-100 exposure indices (higher = more exposed), not loss predictions."}


def t_get_hub_risk(hub_id: str) -> dict:
    hub = _hub_or_error(hub_id)
    s = get_scores()["hubs"][hub["id"]]
    ranks = {hz: next(r["rank"] for r in rank_hubs(hz) if r["hub_id"] == hub["id"])
             for hz in ["overall", *HAZARD_KEYS]}
    return {**s, "rank_among_all_hubs": ranks, "total_hubs": len(hub_ids())}


def t_compare_hubs(hub_ids_: list[str], hazards: list[str]) -> dict:
    hazards = hazards or HAZARD_KEYS
    out = []
    for hid in hub_ids_:
        s = get_scores()["hubs"][_hub_or_error(hid)["id"]]
        out.append({
            "hub_id": s["hub_id"], "name": s["name"],
            "overall_score": s["overall_score"], "overall_tier": s["overall_tier"],
            "hazards": {hz: s["hazards"][hz] for hz in hazards},
        })
    return {"comparison": out}


def _daily_for(location: str, start: date, end: date) -> tuple[dict, str, str]:
    """Daily weather for a hub (from snapshot when possible) or any US place (live, cached)."""
    hub = get_hub(location)
    if hub and CLIMATE_START <= start and end <= CLIMATE_END:
        return snapshot.load_weather(hub["id"]), hub["name"], "snapshot (Open-Meteo ERA5)"
    if hub:
        lat, lon, label = hub["lat"], hub["lon"], hub["name"]
    else:
        geo = db.cache_get(f"geo:{location.lower()}", ttl_seconds=30 * 86400) or open_meteo.geocode_us(location)
        if not geo:
            raise ToolError(f"Could not find a US location named '{location}'. This agent covers the US only.")
        db.cache_put(f"geo:{location.lower()}", geo)
        lat, lon, label = geo["lat"], geo["lon"], f"{geo['name']}, {geo['state']} (not one of our hubs)"
    key = f"wx:{lat:.2f},{lon:.2f}:{start}:{end}"
    daily = db.cache_get(key, ttl_seconds=86400)
    if daily is None:
        daily = open_meteo.fetch_daily(lat, lon, start, end)
        db.cache_put(key, daily)
    return daily, label, "live Open-Meteo archive (ERA5)"


def t_weather_stat(location: str, metric: str, start_date: str, end_date: str) -> dict:
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        raise ToolError("Dates must be YYYY-MM-DD")
    if end < start:
        raise ToolError("end_date is before start_date")
    if start < date(1950, 1, 1) or end > date.today():
        raise ToolError(f"Dates must be between 1950-01-01 and today ({date.today()})")
    daily, label, src = _daily_for(location, start, end)
    thresholds = load_weights()["thresholds"]
    result = count_days(daily, metric, thresholds, start, end)
    return {"location": label, "data_source": src, **result, "period_totals": totals(daily, start, end),
            "note": "ERA5 reanalysis is a ~25 km gridded estimate, not a station record; "
                    "small amounts may differ from local airport observations."}


def t_get_active_alerts(hub_id: str) -> dict:
    hub = _hub_or_error(hub_id)
    key = f"alerts:{hub['id']}"
    alerts = db.cache_get(key, ttl_seconds=600)
    if alerts is None:
        alerts = nws.active_alerts(hub["lat"], hub["lon"])
        db.cache_put(key, alerts)
    return {"hub_id": hub["id"], "active_alerts": alerts,
            "note": "Live NWS alerts are context only; they do not change the long-term risk score."}


def t_get_methodology() -> dict:
    w = load_weights()
    return {"weights_version": w["version"], "thresholds": w["thresholds"],
            "saturation_days_per_year": w["saturation_days_per_year"], "overall_tiers": w["overall_tiers"], "hazards": w["hazards"],
            "overall_weights": w["overall_weights"], "tiers": w["tiers"], "meta": get_scores()["meta"],
            "normalization": {
                "nri": "FEMA NRI Expected Annual Loss Rate national percentile (0-100, vs all US counties). "
                       "Loss RATE, not total loss, so big-city exposure does not inflate a single facility's risk.",
                "climate": "Observed disruption days/yr (Open-Meteo, last 5 full years) scaled linearly: 0 -> 0, "
                           "saturation level -> 100 (see saturation_days_per_year).",
            }}


# --- tool schemas (strict) ----------------------------------------------------------
# Spell out each metric's threshold in the tool description so the model picks the right one
# (an eval caught it using snow_day (>=0.1 cm) for a ">=1 cm" question because the names alone were ambiguous).
_METRIC_DOCS = "; ".join(f"{m} = " + d[3].format(t=load_weights()["thresholds"][d[2]])
                         for m, d in DAY_METRICS.items())
_nullable_region = {"anyOf": [{"type": "string", "enum": REGIONS}, {"type": "null"}],
                    "description": "US Census region filter, or null for all hubs"}

TOOLS = [
    {
        "name": "list_hubs",
        "description": "List our distribution hubs (id, name, state, Census region). Use to resolve which hubs exist.",
        "input_schema": {"type": "object", "properties": {"region": _nullable_region},
                         "required": ["region"], "additionalProperties": False},
    },
    {
        "name": "rank_hubs",
        "description": "Rank hubs by the overall Weather Disruption Risk Score or by one hazard score (0-100, "
                       "higher = more exposed). Use for 'which hubs are most exposed to X' questions.",
        "input_schema": {"type": "object", "properties": {
            "hazard": {"type": "string", "enum": ["overall", *HAZARD_KEYS]},
            "region": _nullable_region,
            "top_n": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Limit results, or null for all"},
        }, "required": ["hazard", "region", "top_n"], "additionalProperties": False},
    },
    {
        "name": "get_hub_risk",
        "description": "Full risk breakdown for one hub: overall score, each hazard score, every input's raw value, "
                       "normalized value, weight and contribution, plus rank among all hubs. Use for 'why' questions.",
        "input_schema": {"type": "object", "properties": {
            "hub_id": {"type": "string", "enum": hub_ids()},
        }, "required": ["hub_id"], "additionalProperties": False},
    },
    {
        "name": "compare_hubs",
        "description": "Side-by-side hazard scores (with breakdowns) for 2+ hubs. Pass hazards=[] for all hazards.",
        "input_schema": {"type": "object", "properties": {
            "hub_ids": {"type": "array", "items": {"type": "string", "enum": hub_ids()}},
            "hazards": {"type": "array", "items": {"type": "string", "enum": HAZARD_KEYS}},
        }, "required": ["hub_ids", "hazards"], "additionalProperties": False},
    },
    {
        "name": "weather_stat",
        "description": "Count days meeting a weather condition at a hub or any US city over a date range, and the "
                       "percentage of days. Also returns period totals (snowfall, precipitation, extremes). "
                       "Use for factual historical weather questions. Metrics: " + _METRIC_DOCS,
        "input_schema": {"type": "object", "properties": {
            "location": {"type": "string", "description": "Hub id (preferred) or a US city name"},
            "metric": {"type": "string", "enum": list(DAY_METRICS)},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        }, "required": ["location", "metric", "start_date", "end_date"], "additionalProperties": False},
    },
    {
        "name": "get_active_alerts",
        "description": "Current National Weather Service alerts at a hub (live; context only, not part of the score).",
        "input_schema": {"type": "object", "properties": {
            "hub_id": {"type": "string", "enum": hub_ids()},
        }, "required": ["hub_id"], "additionalProperties": False},
    },
    {
        "name": "get_methodology",
        "description": "The scoring methodology: thresholds, hazard formulas, weights, tiers, data sources and dates.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
]
for _t in TOOLS:
    _t["strict"] = True

_DISPATCH = {
    "list_hubs": lambda a: t_list_hubs(a.get("region")),
    "rank_hubs": lambda a: t_rank_hubs(a["hazard"], a.get("region"), a.get("top_n")),
    "get_hub_risk": lambda a: t_get_hub_risk(a["hub_id"]),
    "compare_hubs": lambda a: t_compare_hubs(a["hub_ids"], a.get("hazards") or []),
    "weather_stat": lambda a: t_weather_stat(a["location"], a["metric"], a["start_date"], a["end_date"]),
    "get_active_alerts": lambda a: t_get_active_alerts(a["hub_id"]),
    "get_methodology": lambda a: t_get_methodology(),
}


def run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool. Returns (json_text, is_error)."""
    try:
        if name not in _DISPATCH:
            raise ToolError(f"Unknown tool {name}")
        return json.dumps(_DISPATCH[name](args), default=str), False
    except ToolError as e:
        return json.dumps({"error": str(e)}), True
    except Exception as e:  # upstream API failure etc. - let the model explain it rather than crash
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
