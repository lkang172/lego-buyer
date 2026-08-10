"""Settings and on-disk locations."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"
CACHE_DIR = DATA_DIR / "cache"

# BrickLink serves prices converted to the viewer's display currency. We only
# understand USD, so responses are rejected if they come back as anything else.
EXPECTED_CURRENCY_PREFIX = "US $"

# Be a polite scraper: one request at a time, spaced out, and cache hard.
REQUEST_DELAY_SECONDS = 1.1
CACHE_TTL_SECONDS = 6 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# How many lots BrickLink returns per items-for-sale page.
LOTS_PER_PAGE = 500

_colors_cache: dict[int, str] | None = None


def color_names() -> dict[int, str]:
    """BrickLink color id -> name, vendored from catalogColors.asp."""
    global _colors_cache
    if _colors_cache is None:
        raw = json.loads((DATA_DIR / "bl_colors.json").read_text(encoding="utf-8"))
        _colors_cache = {int(k): v for k, v in raw.items()}
    return _colors_cache


def color_id_by_name() -> dict[str, int]:
    return {name.lower(): cid for cid, name in color_names().items()}
