"""Central configuration. Everything tunable lives here or in scoring/weights.yaml."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshot"
HUBS_FILE = DATA_DIR / "hubs.json"
WEIGHTS_FILE = ROOT / "app" / "scoring" / "weights.yaml"
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "app.db"))

# Climatology window used for scoring: the last 5 full calendar years.
CLIMATE_START = date(2021, 1, 1)
CLIMATE_END = date(2025, 12, 31)

# LLM
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "low")  # evals: 13/13 at low, ~25% faster than medium
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))

# Optional shared token protecting /api/chat on a public deployment (empty = open).
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

HTTP_TIMEOUT = 30.0
USER_AGENT = "weather-risk-agent/1.0 (take-home demo)"
