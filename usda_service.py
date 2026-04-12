"""USDA FoodData Central integration for own-food nutrition lookups."""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

USDA_API_BASE = "https://api.nal.usda.gov/fdc/v1"

# Nutrient IDs we care about
_NUTRIENT_ENERGY = 1008   # Energy (kcal)
_NUTRIENT_PROTEIN = 1003  # Protein (g)
_NUTRIENT_CARBS = 1005    # Carbohydrates (g)
_NUTRIENT_FAT = 1004      # Total fat (g)

# Data type priority (lower index = higher priority)
_DATA_TYPE_PRIORITY = [
    "Survey (FNDDS)",
    "Foundation",
    "SR Legacy",
    "Branded",
]

# In-memory cache: query_lower -> (USDAResult, timestamp)
_cache: dict[str, tuple[USDAResult, float]] = {}
_CACHE_TTL = 86400  # 24 hours


@dataclass
class USDAResult:
    name: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float


def extract_nutrients(food: dict) -> USDAResult:
    """Extract macro nutrients from a USDA food item dict."""
    nutrient_map: dict[int, float] = {}
    for n in food.get("foodNutrients", []):
        nid = n.get("nutrientId") or n.get("nutrient", {}).get("id")
        if nid is not None:
            nutrient_map[nid] = n.get("value", 0.0) or 0.0

    return USDAResult(
        name=food.get("description", "Unknown"),
        calories=round(nutrient_map.get(_NUTRIENT_ENERGY, 0.0), 1),
        protein_g=round(nutrient_map.get(_NUTRIENT_PROTEIN, 0.0), 1),
        carbs_g=round(nutrient_map.get(_NUTRIENT_CARBS, 0.0), 1),
        fat_g=round(nutrient_map.get(_NUTRIENT_FAT, 0.0), 1),
    )


def _pick_best_match(foods: list[dict]) -> dict | None:
    """Pick the best food result by data type priority."""
    if not foods:
        return None

    def sort_key(f: dict) -> int:
        dt = f.get("dataType", "")
        try:
            return _DATA_TYPE_PRIORITY.index(dt)
        except ValueError:
            return len(_DATA_TYPE_PRIORITY)

    return min(foods, key=sort_key)


async def search_usda(query: str) -> USDAResult | None:
    """Search USDA FoodData Central for a food item.

    Returns the best match with extracted macros, or None if no match found.
    Uses an in-memory cache with 24h TTL.
    """
    api_key = os.getenv("USDA_API_KEY")
    if not api_key:
        logger.warning("USDA_API_KEY not set — skipping USDA lookup")
        return None

    cache_key = query.strip().lower()

    # Check cache
    if cache_key in _cache:
        result, ts = _cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return result
        del _cache[cache_key]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{USDA_API_BASE}/foods/search",
                params={"api_key": api_key},
                json={
                    "query": query,
                    "pageSize": 5,
                    "dataType": list(_DATA_TYPE_PRIORITY),
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("USDA API request failed for query=%r", query)
        return None

    foods = data.get("foods", [])
    best = _pick_best_match(foods)
    if best is None:
        return None

    result = extract_nutrients(best)
    _cache[cache_key] = (result, time.time())
    return result


async def enrich_items_with_usda(items: list[dict]) -> list[dict]:
    """Enrich a list of Gemini-identified food items with USDA nutrition data.

    For each item, searches USDA. If a match is found, replaces nutrition values
    and sets confidence to "medium". Otherwise keeps Gemini values with "low" confidence.
    """
    enriched = []
    for item in items:
        name = item.get("name", "")
        usda = await search_usda(name)

        if usda is not None:
            item["calories"] = usda.calories
            item["protein_g"] = usda.protein_g
            item["carbs_g"] = usda.carbs_g
            item["fat_g"] = usda.fat_g
            item["matched_menu_item"] = usda.name
            item["confidence"] = "medium"
        else:
            item["matched_menu_item"] = None
            item["confidence"] = "low"

        enriched.append(item)
    return enriched
