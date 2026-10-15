"""Step 1 KPIs: turn daily weather arrays into disruption-day counts. Pure functions, no I/O."""
from datetime import date

# metric name -> (daily variable, comparison, threshold key, human description)
DAY_METRICS = {
    "snow_day":         ("snowfall_sum",       ">=", "any_snow_cm",    "days with any snowfall (>= {t} cm)"),
    "heavy_snow_day":   ("snowfall_sum",       ">=", "snow_day_cm",    "days with >= {t} cm snowfall"),
    "hard_freeze_day":  ("temperature_2m_min", "<=", "hard_freeze_c",  "days with low <= {t} °C"),
    "heavy_rain_day":   ("precipitation_sum",  ">=", "heavy_rain_mm",  "days with >= {t} mm precipitation"),
    "high_wind_day":    ("wind_gusts_10m_max", ">=", "high_wind_kmh",  "days with gusts >= {t} km/h"),
    "extreme_heat_day": ("temperature_2m_max", ">=", "extreme_heat_c", "days with high >= {t} °C"),
}

# Annualized climatology metrics used by the scoring model -> underlying day metric
CLIMATE_METRICS = {
    "snow_days_per_year": "heavy_snow_day",
    "hard_freeze_days_per_year": "hard_freeze_day",
    "heavy_rain_days_per_year": "heavy_rain_day",
    "high_wind_days_per_year": "high_wind_day",
    "extreme_heat_days_per_year": "extreme_heat_day",
}


def _slice(daily: dict, start: date | None, end: date | None) -> dict:
    if start is None and end is None:
        return daily
    times = daily["time"]
    lo, hi = (start.isoformat() if start else times[0]), (end.isoformat() if end else times[-1])
    idx = [i for i, t in enumerate(times) if lo <= t <= hi]
    return {k: [v[i] for i in idx] for k, v in daily.items()}


def count_days(daily: dict, metric: str, thresholds: dict,
               start: date | None = None, end: date | None = None) -> dict:
    """Count days meeting a metric's threshold. Days with missing data are excluded, and reported."""
    var, op, tkey, desc = DAY_METRICS[metric]
    t = thresholds[tkey]
    d = _slice(daily, start, end)
    values = d[var]
    valid = [v for v in values if v is not None]
    hits = sum(1 for v in valid if (v >= t if op == ">=" else v <= t))
    return {
        "metric": metric,
        "definition": desc.format(t=t),
        "start": d["time"][0] if d["time"] else None,
        "end": d["time"][-1] if d["time"] else None,
        "days_in_period": len(values),
        "days_with_data": len(valid),
        "days_meeting_threshold": hits,
        "percent_of_days": round(100 * hits / len(valid), 1) if valid else None,
    }


def climatology(daily: dict, thresholds: dict) -> dict[str, float]:
    """Average number of disruption days per year over the whole series."""
    n_days = sum(1 for v in daily["temperature_2m_max"] if v is not None)
    years = n_days / 365.25
    return {name: round(count_days(daily, dm, thresholds)["days_meeting_threshold"] / years, 1)
            for name, dm in CLIMATE_METRICS.items()}


def totals(daily: dict, start: date | None = None, end: date | None = None) -> dict:
    d = _slice(daily, start, end)
    return {
        "total_snowfall_cm": round(sum(v for v in d["snowfall_sum"] if v), 1),
        "total_precip_mm": round(sum(v for v in d["precipitation_sum"] if v), 1),
        "min_temp_c": min((v for v in d["temperature_2m_min"] if v is not None), default=None),
        "max_temp_c": max((v for v in d["temperature_2m_max"] if v is not None), default=None),
        "max_gust_kmh": max((v for v in d["wind_gusts_10m_max"] if v is not None), default=None),
    }
