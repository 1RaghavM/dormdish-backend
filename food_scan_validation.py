"""
Response parsing, validation, and totals calculation for the food photo scanner.

After Gemini 2.5 Flash returns its analysis of a food photo, this module:
1. Parses the raw JSON response into structured items
2. Validates each item has required fields and reasonable values
3. Calculates per-item adjusted values (calories * servings, etc.)
4. Calculates aggregate totals across all items
5. Assigns a scan_id
6. Returns a clean response dict

Following the "never trust LLM math" pattern from meal_plan_agent.py,
all nutrition totals are recalculated server-side rather than relying
on values the model may have computed.
"""

import uuid
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

VALID_CONFIDENCE_VALUES = {"high", "medium", "low"}
DEFAULT_CONFIDENCE = "medium"

# Per-serving sanity caps -- anything above these is likely hallucinated.
MAX_CALORIES_PER_SERVING = 2000.0
MAX_PROTEIN_PER_SERVING = 200.0
MAX_CARBS_PER_SERVING = 500.0
MAX_FAT_PER_SERVING = 200.0

MAX_SERVINGS = 10.0
DEFAULT_SERVINGS = 1.0

# Required numeric macro fields and their per-serving caps.
_MACRO_CAPS: dict[str, float] = {
    "calories": MAX_CALORIES_PER_SERVING,
    "protein_g": MAX_PROTEIN_PER_SERVING,
    "carbs_g": MAX_CARBS_PER_SERVING,
    "fat_g": MAX_FAT_PER_SERVING,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* into [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_and_parse_scan_response(
    raw_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and clean individual items from Gemini's response.

    * Ensures required fields exist (name, calories, protein_g, carbs_g,
      fat_g, servings).
    * Clamps values to reasonable ranges.
    * Normalises confidence to one of ``"high"``, ``"medium"``, ``"low"``.
    * Filters out items whose name is empty after stripping.
    * Returns a cleaned list of item dicts.
    """

    if not raw_items:
        logger.warning("[food_scan] validate_and_parse_scan_response called with empty/None items")
        return []

    logger.info("[food_scan] Validating %d raw item(s)", len(raw_items))

    cleaned: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            logger.warning("[food_scan] Item %d is not a dict -- skipping", idx)
            continue

        # -- name ----------------------------------------------------------
        name = str(raw.get("name", "")).strip()
        if not name:
            logger.warning("[food_scan] Item %d has empty name -- skipping", idx)
            continue

        # -- macro fields ---------------------------------------------------
        try:
            item: dict[str, Any] = {"name": name}

            for field, cap in _MACRO_CAPS.items():
                val = _safe_float(raw.get(field))
                val = _clamp(val, 0.0, cap)
                item[field] = round(val, 1)

            # -- servings ---------------------------------------------------
            servings = _safe_float(raw.get("servings"), default=DEFAULT_SERVINGS)
            if servings <= 0:
                servings = DEFAULT_SERVINGS
            servings = _clamp(servings, 0.0, MAX_SERVINGS)
            item["servings"] = round(servings, 1)

            # -- optional string fields -------------------------------------
            serving_size = raw.get("serving_size")
            if serving_size is not None:
                item["serving_size"] = str(serving_size).strip()
            else:
                item["serving_size"] = None

            matched_menu_item = raw.get("matched_menu_item")
            if matched_menu_item is not None:
                item["matched_menu_item"] = str(matched_menu_item).strip()
            else:
                item["matched_menu_item"] = None

            # -- confidence -------------------------------------------------
            confidence = str(raw.get("confidence", "")).strip().lower()
            if confidence not in VALID_CONFIDENCE_VALUES:
                confidence = DEFAULT_CONFIDENCE
            item["confidence"] = confidence

            cleaned.append(item)

        except Exception:
            logger.warning(
                "[food_scan] Item %d ('%s') failed validation -- skipping",
                idx,
                name,
                exc_info=True,
            )
            continue

    logger.info(
        "[food_scan] Validation complete: %d/%d items passed",
        len(cleaned),
        len(raw_items),
    )

    return cleaned


def calculate_totals(items: list[dict[str, Any]]) -> dict[str, float]:
    """Calculate aggregate nutrition totals from a list of food scan items.

    Each item's macros are **per serving**. The total for each macro is
    ``sum(value * servings)`` across all items.  All results are rounded
    to 1 decimal place to avoid floating-point artifacts.

    Returns::

        {"calories": float, "protein_g": float, "carbs_g": float, "fat_g": float}
    """

    if not items:
        return {
            "calories": 0.0,
            "protein_g": 0.0,
            "carbs_g": 0.0,
            "fat_g": 0.0,
        }

    total_calories = sum(item["calories"] * item["servings"] for item in items)
    total_protein = sum(item["protein_g"] * item["servings"] for item in items)
    total_carbs = sum(item["carbs_g"] * item["servings"] for item in items)
    total_fat = sum(item["fat_g"] * item["servings"] for item in items)

    return {
        "calories": round(total_calories, 1),
        "protein_g": round(total_protein, 1),
        "carbs_g": round(total_carbs, 1),
        "fat_g": round(total_fat, 1),
    }


def build_scan_response(
    items: list[dict[str, Any]],
    dining_hall: str,
    date: str,
) -> dict[str, Any]:
    """Build the final API response dict for the food scan analyse endpoint.

    * Generates a ``scan_id`` (UUID).
    * Validates and cleans *items* via :func:`validate_and_parse_scan_response`.
    * Recalculates totals server-side (never trusts LLM math).
    * Returns the complete response structure.

    If *items* is empty or ``None``, returns a response with
    ``"success": False`` and zeroed totals.
    """

    # Handle empty / None input early.
    if not items:
        logger.warning("[food_scan] build_scan_response called with empty/None items")
        return {
            "success": False,
            "scan_id": str(uuid.uuid4()),
            "items": [],
            "totals": {
                "calories": 0.0,
                "protein_g": 0.0,
                "carbs_g": 0.0,
                "fat_g": 0.0,
            },
            "dining_hall": dining_hall,
            "date": date,
        }

    validated_items = validate_and_parse_scan_response(items)

    if not validated_items:
        logger.warning("[food_scan] All items failed validation")
        return {
            "success": False,
            "scan_id": str(uuid.uuid4()),
            "items": [],
            "totals": {
                "calories": 0.0,
                "protein_g": 0.0,
                "carbs_g": 0.0,
                "fat_g": 0.0,
            },
            "dining_hall": dining_hall,
            "date": date,
        }

    # Recalculate totals server-side -- never trust LLM math.
    totals = calculate_totals(validated_items)

    logger.info(
        "[food_scan] Scan response built: %d items, %.1f cal, %.1fg protein, %.1fg carbs, %.1fg fat",
        len(validated_items),
        totals["calories"],
        totals["protein_g"],
        totals["carbs_g"],
        totals["fat_g"],
    )

    return {
        "success": True,
        "scan_id": str(uuid.uuid4()),
        "items": validated_items,
        "totals": totals,
        "dining_hall": dining_hall,
        "date": date,
    }
