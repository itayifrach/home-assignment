# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide **which US distribution hubs are most exposed to weather disruption, and why**, so resilience investment goes where it matters.

- **Deterministic scoring:** a config-driven KPI built from FEMA's National Risk Index and 5 years of observed weather (Open-Meteo ERA5) produces every number.
- **Claude agent:** picks tools, explains the results, and handles follow-up questions. Its final answer must match a JSON schema and is re-validated in code.
- **Chat UI:** a chat page plus a live risk leaderboard. Every answer shows key drivers, assumptions, caveats, sources, confidence and the tool trace.
- **Evaluation:** offline unit tests for the KPI, plus a 13-case agent eval with deterministic grading.

📐 **Design and architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · 🤖 **AI session transcript:** [docs/ai-session/](docs/ai-session/)

---

## Quick start (local)

Requirements: Python 3.11+ and an [Anthropic API key](https://console.anthropic.com).

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then put your ANTHROPIC_API_KEY in .env
uvicorn app.main:app --reload
```

Open http://localhost:8000 and try:
- *Which hubs in the Midwest are most exposed to winter disruption?*
- *Compare Miami and Houston in terms of hurricane and flood exposure.*
- *What percentage of days in Denver last year had snowfall?*
- *Why is the Dallas hub's weather disruption risk high?* → then follow up: *What would most reduce it?*

With Docker:
```bash
docker build -t weather-risk-agent .
docker run -p 8000:8000 --env-file .env weather-risk-agent
```

## Tests and evaluation
```bash
pytest -q                       # KPI unit tests (offline, no API key needed)
python -m evals.run_evals       # 13 agent cases, deterministic grading (needs API key, ~3-5 min)
python -m evals.run_evals -k denver   # a subset
```
The eval prints a pass/fail table and writes `evals/results/<timestamp>.json`. Expected rankings and numbers are computed from the scoring code at run time, so the eval stays valid after a data refresh.

## Refreshing the data
The scoring inputs are committed in `data/snapshot/`, so the app runs without depending on the external APIs. To rebuild:
```bash
python -m scripts.refresh_data            # skips hubs already fetched; add --force to refetch all
```

## API
| Endpoint | Description |
|---|---|
| `POST /api/chat` `{message, session_id?}` | Ask the agent. Returns the validated answer JSON plus `session_id`, `tool_trace`, `validation_warnings`, `latency_ms` |
| `GET /api/scores?hazard=overall\|winter\|flood\|hurricane\|severe_storm\|heat&region=` | Deterministic ranking |
| `GET /api/hubs/{hub_id}` | Full score breakdown for one hub |
| `GET /api/methodology` | Thresholds, weights, tiers, data provenance |
| `GET /health` | Liveness, model, weights version |

Interactive docs: http://localhost:8000/docs

## Deploy (Render)
1. Push this repo to GitHub.
2. In Render: **New → Blueprint** → select the repo (it uses `render.yaml` and the Dockerfile).
3. Set `ANTHROPIC_API_KEY`, plus `ACCESS_TOKEN` so the public URL doesn't spend your key; the UI asks visitors for the token.
4. The free tier sleeps after ~15 min idle (the first request then takes ~30–60 s). Open the URL a few minutes before a demo.

## How the score works (short version)
| Hazard | Formula (all inputs 0–100) |
|---|---|
| Winter | 50% FEMA (winter weather, ice storm) + 25% snow days + 25% hard-freeze days |
| Hurricane | 100% FEMA hurricane |
| Flood | 60% FEMA max(inland, coastal flood) + 40% heavy-rain days |
| Severe storm | 70% FEMA (tornado, hail) + 30% high-wind days |
| Extreme heat | 40% FEMA heat wave + 60% extreme-heat days |

**Weather Disruption Risk Score** = 25% winter + 25% flood + 20% hurricane + 15% severe storm + 15% heat.
- FEMA inputs are the county's Expected Annual Loss *Rate* national percentile.
- Observed inputs are disruption days per year, scaled to 100 at a saturation level.
- Everything is tunable in [`app/scoring/weights.yaml`](app/scoring/weights.yaml).
- The full rationale is in [ARCHITECTURE.md §3](docs/ARCHITECTURE.md#3-the-kpi-weather-disruption-risk-score).

## Assumptions, uncertainty and scope
- **Scope:** 22 fixed US hubs, 5 hazard families, historical exposure (not forecasts or dollar losses). Live NWS alerts are context only.
- **Assumptions:**
  - Hub location is the city center, and its hazard data is its county's.
  - Regions are US Census regions.
  - "Last year" means the last full calendar year.
- **Uncertainty:**
  - ERA5 is a ~25 km gridded reanalysis, not station data.
  - 5 years under-samples rare events (which is why FEMA carries most of the weight for those).
  - The weights are expert judgment, not yet calibrated against our own closure and delay history. That calibration is the recommended next step.

## Configuration (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Claude API key |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model id |
| `ANTHROPIC_EFFORT` | `low` | Reasoning effort. `low` passed 13/13 evals and is ~25% faster than `medium` |
| `ACCESS_TOKEN` | (empty) | If set, `/api/chat` requires the `X-Access-Token` header |
