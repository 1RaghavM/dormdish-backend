"""
Gemini context cache management for dining hall menus.

Creates, looks up, and cleans up Gemini cached content objects.
Caches are created during the daily cron after menu refresh and
referenced by food scan and meal plan agents to reduce token costs.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton Gemini client (shared with agents)
# ---------------------------------------------------------------------------
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set")
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Cache creation
# ---------------------------------------------------------------------------

async def create_menu_cache(
    dining_hall: str,
    university: str,
    date: str,
    use_case: str,
    system_instruction: str,
    model: str,
) -> Optional[str]:
    """Create a Gemini cached content object and store handle in Supabase.

    Args:
        dining_hall: Hall slug (e.g. "busch-dining-hall")
        university: University key (e.g. "rutgers")
        date: Date string YYYY-MM-DD
        use_case: "food_scan" or "meal_plan"
        system_instruction: The full static system prompt to cache
        model: Gemini model name (e.g. "gemini-2.5-flash")

    Returns:
        Cache name string if successful, None on failure.
    """
    from app import supabase  # lazy import to avoid circular deps

    cache_id = f"{use_case}_{university}_{dining_hall}_{date}"
    display_name = f"{use_case}_{dining_hall}_{date}"
    ttl = os.getenv("GEMINI_CACHE_TTL", "86400s")

    try:
        def _create():
            return _get_client().caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    display_name=display_name,
                    ttl=ttl,
                ),
            )

        cached_content = await asyncio.to_thread(_create)
        cache_name = cached_content.name

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl.rstrip("s")))).isoformat()

        def _store():
            supabase.table("gemini_caches").upsert({
                "id": cache_id,
                "cache_name": cache_name,
                "model": model,
                "university": university,
                "dining_hall": dining_hall,
                "date": date,
                "use_case": use_case,
                "expires_at": expires_at,
            }).execute()

        await asyncio.to_thread(_store)

        logger.info("Created Gemini cache: %s -> %s", cache_id, cache_name)
        return cache_name

    except Exception as exc:
        logger.error("Failed to create Gemini cache %s: %s", cache_id, exc)
        return None


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------

async def get_cache_name(
    dining_hall: str,
    university: str,
    date: str,
    use_case: str,
) -> Optional[str]:
    """Look up a Gemini cache handle from Supabase.

    Returns cache_name if found and not expired, None otherwise.
    Never raises — returns None on any failure.
    """
    from app import supabase

    cache_id = f"{use_case}_{university}_{dining_hall}_{date}"

    try:
        def _fetch():
            now_iso = datetime.now(timezone.utc).isoformat()
            resp = (
                supabase.table("gemini_caches")
                .select("cache_name")
                .eq("id", cache_id)
                .gt("expires_at", now_iso)
                .execute()
            )
            if resp.data and len(resp.data) > 0:
                return resp.data[0]["cache_name"]
            return None

        cache_name = await asyncio.to_thread(_fetch)
        if cache_name:
            logger.info("Cache hit: %s -> %s", cache_id, cache_name)
        else:
            logger.info("Cache miss: %s", cache_id)
        return cache_name

    except Exception as exc:
        logger.warning("Cache lookup failed for %s: %s", cache_id, exc)
        return None


# ---------------------------------------------------------------------------
# Cache cleanup
# ---------------------------------------------------------------------------

async def cleanup_stale_caches() -> int:
    """Delete expired rows from gemini_caches table. Returns count deleted."""
    from app import supabase

    try:
        def _cleanup():
            now_iso = datetime.now(timezone.utc).isoformat()
            resp = (
                supabase.table("gemini_caches")
                .delete()
                .lt("expires_at", now_iso)
                .execute()
            )
            return len(resp.data) if resp.data else 0

        count = await asyncio.to_thread(_cleanup)
        if count > 0:
            logger.info("Cleaned up %d stale cache rows", count)
        return count

    except Exception as exc:
        logger.warning("Cache cleanup failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Bulk cache creation (called from cron)
# ---------------------------------------------------------------------------

async def create_caches_for_date(date: str, halls: list) -> dict:
    """Create Gemini caches for all dining halls for a given date.

    Args:
        date: Date string YYYY-MM-DD
        halls: List of (university, hall_slug) tuples that were successfully refreshed

    Returns:
        dict with "created", "failed", and "cleaned_up" counts
    """
    from app import get_menu_from_supabase
    from food_scan_prompts import build_system_prompt
    from food_scan_menu import _extract_nutrition_items
    import json

    # Clean up stale rows first
    cleaned = await cleanup_stale_caches()

    foodscan_model = os.getenv("FOODSCAN_MODEL", "gemini-2.5-flash")
    mealplan_model = os.getenv("MEALPLAN_MODEL", "gemini-2.5-flash")

    async def _create_for_hall(university: str, hall_slug: str):
        """Create both food_scan and meal_plan caches for one hall."""
        results = {"hall": hall_slug, "food_scan": None, "meal_plan": None}

        # Fetch menu from Supabase (use university slug, NOT display name —
        # get_menu_from_supabase builds menu_id as "{university}_{dining_hall}_{date}")
        menu_data = await get_menu_from_supabase(university, hall_slug, date)
        if not menu_data:
            logger.warning("No menu data for %s/%s on %s, skipping cache creation", university, hall_slug, date)
            results["food_scan"] = "no_menu"
            results["meal_plan"] = "no_menu"
            return results

        # Extract nutrition items
        items = _extract_nutrition_items(menu_data)
        if not items:
            logger.warning("Empty menu items for %s/%s on %s", university, hall_slug, date)
            results["food_scan"] = "empty_menu"
            results["meal_plan"] = "empty_menu"
            return results

        # --- Food scan cache ---
        menu_json = json.dumps(items, indent=2, ensure_ascii=False)
        food_scan_system = build_system_prompt(
            dining_hall=hall_slug,
            date=date,
            menu_json=menu_json,
        )
        fs_name = await create_menu_cache(
            dining_hall=hall_slug,
            university=university,
            date=date,
            use_case="food_scan",
            system_instruction=food_scan_system,
            model=foodscan_model,
        )
        results["food_scan"] = "ok" if fs_name else "failed"

        # --- Meal plan cache ---
        from meal_plan_agent import STATIC_MEAL_PLAN_INSTRUCTIONS, _render_allowed_items
        menu_items_text = _render_allowed_items(items)
        meal_plan_system = (
            STATIC_MEAL_PLAN_INSTRUCTIONS
            + "\n\nMENU (name|cal|prot|fat|carb|serving)\n"
            + menu_items_text
        )
        mp_name = await create_menu_cache(
            dining_hall=hall_slug,
            university=university,
            date=date,
            use_case="meal_plan",
            system_instruction=meal_plan_system,
            model=mealplan_model,
        )
        results["meal_plan"] = "ok" if mp_name else "failed"

        return results

    # Run all halls in parallel
    tasks = [_create_for_hall(univ, hall) for univ, hall in halls]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    created = 0
    failed = 0
    details = []
    for r in all_results:
        if isinstance(r, Exception):
            failed += 2  # both use cases failed
            details.append({"error": str(r)})
        else:
            details.append(r)
            for use_case in ("food_scan", "meal_plan"):
                if r.get(use_case) == "ok":
                    created += 1
                elif r.get(use_case) == "failed":
                    failed += 1

    logger.info("Cache creation complete: %d created, %d failed, %d stale cleaned", created, failed, cleaned)
    return {"created": created, "failed": failed, "cleaned_up": cleaned, "details": details}
