"""Hub registry: the fixed list of distribution hubs we score."""
import json
from functools import lru_cache

from app.config import HUBS_FILE

REGIONS = ["Midwest", "Northeast", "South", "West"]


@lru_cache
def load_hubs() -> list[dict]:
    return json.loads(HUBS_FILE.read_text(encoding="utf-8"))


def hub_ids() -> list[str]:
    return [h["id"] for h in load_hubs()]


def get_hub(hub_id_or_name: str) -> dict | None:
    """Look up a hub by id, city or name (case-insensitive)."""
    key = hub_id_or_name.strip().lower().replace("-", "_")
    for h in load_hubs():
        if key in (h["id"], h["city"].lower(), h["name"].lower(), h["city"].lower().replace(" ", "_")):
            return h
    return None


def hubs_in_region(region: str | None) -> list[dict]:
    if not region:
        return load_hubs()
    return [h for h in load_hubs() if h["region"].lower() == region.lower()]
