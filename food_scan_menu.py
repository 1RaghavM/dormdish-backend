"""
food_scan_menu.py
-----------------
Helper module for the food-photo-scanner feature.

Fetches the dining-hall menu from the Supabase cache (via the main app's
`get_dining_hall_menu`) and reformats it into a compact JSON payload suitable
for inclusion in a Gemini AI prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# -- Nutrition fields we keep in the prompt payload --------------------------
_NUTRITION_FIELDS = ("name", "calories", "protein_g", "carbs_g", "fat_g", "serving_size")


# -- Public helpers ----------------------------------------------------------

def resolve_dining_hall(dining_hall: str) -> tuple[str, str]:
    """Return (university, hall_slug) for a dining-hall name or alias.

    Uses the canonical ``DINING_HALL_MAPPING`` dict defined in *app.py*.

    Raises ``ValueError`` if the hall name is unrecognised.
    """
    from app import DINING_HALL_MAPPING  # lazy import to avoid circular deps

    dh_norm = dining_hall.strip().lower()
    # Match the same typo corrections applied inside get_dining_hall_menu
    if dh_norm == "nielson":
        dh_norm = "neilson"
    if dh_norm in {"nielson-dining-hall", "nielson dining hall"}:
        dh_norm = "neilson"

    mapping = DINING_HALL_MAPPING.get(dh_norm)
    if not mapping:
        raise ValueError(
            f"Unknown dining hall: '{dining_hall}'. "
            "Please provide a recognised hall name (e.g. 'busch', 'livingston', 'neilson', 'brower')."
        )
    return mapping


def _extract_nutrition_items(menu_data: dict) -> List[Dict[str, Any]]:
    """Pull the flat list of menu items out of whatever shape the cache returns.

    The Supabase cache stores ``menu_data.get("menu", menu_data)`` at write
    time, so at read time we may receive either:

    * ``{"items": [...], "meal_sources": [...]}``        (the ``menu`` sub-dict)
    * the full original dict with a top-level ``items`` key

    In both cases the items live under ``"items"``.
    """
    items: Optional[list] = None

    # Prefer top-level "items" key (most common after cache round-trip)
    if isinstance(menu_data.get("items"), list):
        items = menu_data["items"]
    # Fallback: nested under "menu" -> "items"
    elif isinstance(menu_data.get("menu"), dict):
        nested = menu_data["menu"]
        if isinstance(nested.get("items"), list):
            items = nested["items"]

    if not items:
        return []

    cleaned: List[Dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        name = raw_item.get("name")
        if not name or not isinstance(name, str):
            continue

        entry: Dict[str, Any] = {"name": name.strip()}
        for field in _NUTRITION_FIELDS:
            if field == "name":
                continue
            value = raw_item.get(field)
            if value is not None:
                entry[field] = value

        cleaned.append(entry)

    return cleaned


async def prepare_menu_for_food_scan(dining_hall: str, date: str) -> dict:
    """Fetch the dining-hall menu and format it for the Gemini food-scan prompt.

    Parameters
    ----------
    dining_hall:
        Human-friendly hall name or slug (e.g. ``"busch"``, ``"livingston-dining-commons"``).
    date:
        ISO-8601 date string (``"YYYY-MM-DD"``) or ``"today"``.

    Returns
    -------
    dict
        ``menu_json``   -- compact JSON string of menu items for the prompt.
        ``item_count``  -- number of items on the menu.
        ``dining_hall`` -- normalised dining-hall slug.
        ``university``  -- university the hall belongs to.

    Raises
    ------
    ValueError
        If the dining hall is unknown, the menu is not cached yet, or the
        cached menu contains zero items.
    """
    # Lazy import keeps this module free of import-time side effects from the
    # monolithic app module.
    from app import get_dining_hall_menu  # noqa: E402

    # -- 1. Resolve hall name ------------------------------------------------
    university, hall_slug = resolve_dining_hall(dining_hall)
    logger.info(
        "food_scan_menu: resolved %r -> university=%s hall_slug=%s",
        dining_hall, university, hall_slug,
    )

    # -- 2. Fetch cached menu ------------------------------------------------
    menu_data = await get_dining_hall_menu(dining_hall, date)

    if not isinstance(menu_data, dict):
        raise ValueError(
            f"Unexpected response when fetching menu for '{dining_hall}' on {date}."
        )

    # get_dining_hall_menu returns {"error": "..."} when something goes wrong
    if "error" in menu_data and menu_data["error"] is not None:
        error_msg = menu_data["error"]
        retry_hint = ""
        if menu_data.get("retry_later"):
            retry_hint = (
                " Menus are refreshed daily at 5 AM ET -- please try again later."
            )
        raise ValueError(f"Menu not available: {error_msg}{retry_hint}")

    # -- 3. Extract & clean nutrition items ----------------------------------
    items = _extract_nutrition_items(menu_data)

    if not items:
        raise ValueError(
            f"The menu for '{hall_slug}' on {date} was found but contains no items. "
            "It may not have been fully scraped yet."
        )

    # -- 4. Format as compact JSON for the prompt ----------------------------
    menu_json = json.dumps(items, indent=2, ensure_ascii=False)

    logger.info(
        "food_scan_menu: prepared %d items for %s/%s",
        len(items), university, hall_slug,
    )

    return {
        "menu_json": menu_json,
        "item_count": len(items),
        "dining_hall": hall_slug,
        "university": university,
    }
