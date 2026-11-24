"""Static design samples retained until the workspace feature is backed by PostgreSQL."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def preview_data():
    path = Path(__file__).parent / "data" / "analytics.json"
    return json.loads(path.read_text(encoding="utf-8"))
