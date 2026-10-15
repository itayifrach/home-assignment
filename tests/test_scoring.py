"""Offline unit tests for the deterministic KPI logic (no network, no LLM)."""
import pytest

from app.scoring.metrics import climatology, count_days
from app.scoring.scoring import compute_scores, load_weights, saturating_scale, tier_for

W = load_weights()
T = W["thresholds"]


def _daily(snow, tmin=None, tmax=None, precip=None, gust=None):
    n = len(snow)
    return {
        "time": [f"2025-01-{i + 1:02d}" for i in range(n)],
        "snowfall_sum": snow,
        "temperature_2m_min": tmin or [0.0] * n,
        "temperature_2m_max": tmax or [10.0] * n,
        "precipitation_sum": precip or [0.0] * n,
        "wind_gusts_10m_max": gust or [10.0] * n,
    }


# --- weights file sanity -----------------------------------------------------------
def test_weights_sum_to_one():
    assert sum(W["overall_weights"].values()) == pytest.approx(1.0)
    for name, hz in W["hazards"].items():
        assert sum(c["weight"] for c in hz["components"]) == pytest.approx(1.0), name
    assert set(W["overall_weights"]) == set(W["hazards"])


def test_tiers():
    assert tier_for(80, W["tiers"]) == "Very High"
    assert tier_for(75, W["tiers"]) == "Very High"
    assert tier_for(50, W["tiers"]) == "High"
    assert tier_for(30, W["tiers"]) == "Moderate"
    assert tier_for(0, W["tiers"]) == "Low"


# --- metrics -------------------------------------------------------------------------
def test_snow_day_percentage():
    # 10 days: 3 with real snow, 1 trace (below 0.1 cm), 1 missing value
    d = _daily([0, 2.0, 0.05, 0.5, 0, 0, 1.2, 0, None, 0])
    r = count_days(d, "snow_day", T)
    assert r["days_in_period"] == 10
    assert r["days_with_data"] == 9
    assert r["days_meeting_threshold"] == 3
    assert r["percent_of_days"] == pytest.approx(33.3)


def test_date_slicing():
    d = _daily([1.0] * 10)
    from datetime import date
    r = count_days(d, "heavy_snow_day", T, date(2025, 1, 3), date(2025, 1, 5))
    assert r["days_in_period"] == 3 and r["days_meeting_threshold"] == 3


def test_freeze_uses_less_or_equal():
    d = _daily([0] * 3, tmin=[-12.0, -11.9, -20.0])
    assert count_days(d, "hard_freeze_day", T)["days_meeting_threshold"] == 2


def test_climatology_annualizes():
    # 730 days (2 "years") with 10 heavy-snow days -> ~5/yr
    d = _daily([2.0] * 10 + [0.0] * 720)
    d["time"] = [f"d{i}" for i in range(730)]
    assert climatology(d, T)["snow_days_per_year"] == pytest.approx(5.0, abs=0.1)


# --- normalization -----------------------------------------------------------------
def test_saturating_scale():
    assert saturating_scale(0, 30) == 0.0
    assert saturating_scale(15, 30) == 50.0
    assert saturating_scale(30, 30) == 100.0
    assert saturating_scale(90, 30) == 100.0    # capped
    assert saturating_scale(0.4, 60) < 1.0      # trivial exposure stays trivial


# --- end-to-end scoring on fixtures ---------------------------------------------------
def _nri(**pctls):
    names = ["winter_weather", "ice_storm", "hurricane", "inland_flood", "coastal_flood",
             "tornado", "hail", "heat_wave", "strong_wind"]
    return {"hazards": {n: {"loss_rate_pctl": pctls.get(n, 0.0)} for n in names}}


def _clim(**v):
    keys = ["snow_days_per_year", "hard_freeze_days_per_year", "heavy_rain_days_per_year",
            "high_wind_days_per_year", "extreme_heat_days_per_year"]
    return {k: v.get(k, 0.0) for k in keys}


HUBS = [{"id": i, "name": i, "city": i, "state": "XX", "region": "Midwest", "county": i, "county_fips": i}
        for i in ("cold", "coastal", "mild")]
NRI = {"cold": _nri(winter_weather=90, ice_storm=70),
       "coastal": _nri(hurricane=95, coastal_flood=80, inland_flood=40),
       "mild": _nri()}
CLIM = {"cold": _clim(snow_days_per_year=20, hard_freeze_days_per_year=15),
        "coastal": _clim(heavy_rain_days_per_year=12, high_wind_days_per_year=5),
        "mild": _clim()}


def test_scores_are_deterministic_and_correct():
    s1 = compute_scores(HUBS, NRI, CLIM, W)
    s2 = compute_scores(HUBS, NRI, CLIM, W)
    assert s1 == s2
    # winter(cold) = 0.5*mean(90,70) + 0.25*(20/30*100) + 0.25*(15/45*100) = 40 + 16.67 + 8.33 = 65
    assert s1["cold"]["hazards"]["winter"]["score"] == pytest.approx(65.0, abs=0.1)
    # flood(coastal) = 0.6*max(40,80) + 0.4*(12/25*100) = 48 + 19.2 = 67.2
    assert s1["coastal"]["hazards"]["flood"]["score"] == pytest.approx(67.2)
    # hurricane(coastal) = 1.0*95 (FEMA only)
    assert s1["coastal"]["hazards"]["hurricane"]["score"] == pytest.approx(95.0)
    # severe_storm(coastal) = 0.7*mean(0,0) + 0.3*(5/25*100) = 6
    assert s1["coastal"]["hazards"]["severe_storm"]["score"] == pytest.approx(6.0)
    # mild hub has zero everywhere
    assert s1["mild"]["overall_score"] == 0.0 and s1["mild"]["overall_tier"] == "Low"


def test_score_independent_of_other_hubs():
    """Absolute scaling: removing a hub from the network must not change another hub's score."""
    full = compute_scores(HUBS, NRI, CLIM, W)["cold"]
    alone = compute_scores([HUBS[0]], NRI, CLIM, W)["cold"]
    assert full == alone


def test_contributions_add_up():
    s = compute_scores(HUBS, NRI, CLIM, W)
    for hub in s.values():
        assert sum(c["contribution"] for c in hub["overall_components"]) == pytest.approx(hub["overall_score"], abs=0.3)
        for hz in hub["hazards"].values():
            assert sum(c["contribution"] for c in hz["components"]) == pytest.approx(hz["score"], abs=0.3)


def test_more_exposure_never_lowers_score():
    """Monotonicity: raising an input must not reduce the hazard score."""
    base = compute_scores(HUBS, NRI, CLIM, W)["cold"]["hazards"]["winter"]["score"]
    nri2 = {**NRI, "cold": _nri(winter_weather=99, ice_storm=70)}
    assert compute_scores(HUBS, nri2, CLIM, W)["cold"]["hazards"]["winter"]["score"] > base
