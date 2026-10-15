"""Steps 2-4: deterministic hazard scores and the Weather Disruption Risk Score.

`compute_scores` is a pure function (inputs in, scores out) so it is unit-testable with fixtures.
`get_scores` wires it to the committed snapshot and caches the result.
"""
from datetime import datetime, timezone
from functools import lru_cache

import yaml

from app.config import WEIGHTS_FILE
from app.data import snapshot
from app.data.hubs import load_hubs
from app.scoring.metrics import climatology

CLIMATE_UNITS = "days/yr"


def load_weights() -> dict:
    return yaml.safe_load(WEIGHTS_FILE.read_text(encoding="utf-8"))


def tier_for(score: float, tiers: list[dict]) -> str:
    for t in tiers:
        if score >= t["min"]:
            return t["label"]
    return tiers[-1]["label"]


def saturating_scale(days_per_year: float, saturation: float) -> float:
    """0 days -> 0, `saturation` days or more -> 100, linear in between.

    Absolute (not relative to other hubs): a hub's score does not move when the network changes, and a
    handful of trivial days is not inflated just because most hubs have none.
    """
    return round(min(100.0, 100.0 * max(days_per_year, 0.0) / saturation), 1)


def _nri_value(nri_hazards: dict, fields: list[str], agg: str) -> float:
    vals = [nri_hazards[f]["loss_rate_pctl"] for f in fields]
    return max(vals) if agg == "max" else sum(vals) / len(vals)


def compute_scores(hubs: list[dict], nri_by_fips: dict, climate_by_hub: dict, weights: dict) -> dict:
    """Return {hub_id: {...overall, tier, hazards{...components}}} with a full contribution breakdown."""
    tiers = weights["tiers"]
    saturation = weights["saturation_days_per_year"]

    results = {}
    for h in hubs:
        nri_h = nri_by_fips[h["county_fips"]]["hazards"]
        clim = climate_by_hub[h["id"]]
        hazards = {}
        for hz_key, hz in weights["hazards"].items():
            components = []
            for c in hz["components"]:
                if c["source"] == "nri":
                    raw = _nri_value(nri_h, c["fields"], c["agg"])
                    label = f"FEMA NRI {c['agg']}({', '.join(c['fields'])}) loss-rate percentile"
                    normalized, raw_unit = raw, "national percentile"
                else:
                    raw = clim[c["metric"]]
                    sat = saturation[c["metric"]]
                    label = f"{c['metric']} (observed, {sat} {CLIMATE_UNITS} = 100)"
                    normalized, raw_unit = saturating_scale(raw, sat), CLIMATE_UNITS
                components.append({
                    "input": label,
                    "source": c["source"],
                    "raw_value": round(raw, 1),
                    "raw_unit": raw_unit,
                    "normalized_0_100": round(normalized, 1),
                    "weight": c["weight"],
                    "contribution": round(c["weight"] * normalized, 1),
                })
            score = round(sum(c["weight"] * c["normalized_0_100"] for c in components), 1)
            hazards[hz_key] = {"label": hz["label"], "score": score, "tier": tier_for(score, tiers),
                               "components": components}

        overall_components = [
            {"hazard": k, "hazard_score": hazards[k]["score"], "weight": w,
             "contribution": round(w * hazards[k]["score"], 1)}
            for k, w in weights["overall_weights"].items()
        ]
        overall = round(sum(w * hazards[k]["score"] for k, w in weights["overall_weights"].items()), 1)
        results[h["id"]] = {
            "hub_id": h["id"], "name": h["name"], "city": h["city"], "state": h["state"],
            "region": h["region"], "county": h["county"],
            "overall_score": overall, "overall_tier": tier_for(overall, weights["overall_tiers"]),
            "top_hazard": max(hazards, key=lambda k: hazards[k]["score"]),
            "overall_components": sorted(overall_components, key=lambda c: -c["contribution"]),
            "hazards": hazards,
            "climatology": clim,
        }
    return results


@lru_cache
def get_scores() -> dict:
    """Scores for all hubs from the committed snapshot, plus provenance metadata."""
    weights = load_weights()
    hubs = load_hubs()
    climate = {h["id"]: climatology(snapshot.load_weather(h["id"]), weights["thresholds"]) for h in hubs}
    meta = snapshot.load_meta()
    return {
        "meta": {
            "weights_version": weights["version"],
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_fetched_at": meta["fetched_at"],
            "climatology_window": f"{meta['climate_start']} to {meta['climate_end']}",
            "sources": meta["sources"],
        },
        "hubs": compute_scores(hubs, snapshot.load_nri(), climate, weights),
    }


def rank_hubs(hazard: str = "overall", region: str | None = None, top_n: int | None = None) -> list[dict]:
    hubs = get_scores()["hubs"].values()
    if region:
        hubs = [h for h in hubs if h["region"].lower() == region.lower()]

    def score_of(h):
        return h["overall_score"] if hazard == "overall" else h["hazards"][hazard]["score"]

    ranked = sorted(hubs, key=lambda h: (-score_of(h), h["hub_id"]))
    out = [{"rank": i + 1, "hub_id": h["hub_id"], "name": h["name"], "region": h["region"],
            "score": score_of(h),
            "tier": h["overall_tier"] if hazard == "overall" else h["hazards"][hazard]["tier"]}
           for i, h in enumerate(ranked)]
    return out[:top_n] if top_n else out
