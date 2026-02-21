"""
Daily Menu Refresh Script

This script fetches dining hall menus from FoodPro and caches them in Supabase.
It's designed to run automatically via GitHub Actions but can also be run manually.

Usage:
    python refresh_menus.py [--date YYYY-MM-DD] [--dry-run]

Options:
    --date: Specific date to fetch menus for (default: today in Eastern Time)
    --dry-run: Show what would be fetched without saving to Supabase
"""

import os
import sys
import asyncio
import re
import argparse
import time
import random
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from supabase import create_client

from extract import build_menu_url as fp_build_menu_url, parse_menu as fp_parse_menu

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_DINING_HALLS = [
    ("rutgers", "busch-dining-hall", True),
    ("rutgers", "livingston-dining-commons", True),
    ("rutgers", "neilson-dining-hall", True),
]

UNIVERSITY_DISPLAY_NAMES = {
    "rutgers": "Rutgers University",
    "hopkins": "Johns Hopkins University",
}

LOCATION_MAP = {
    "livingston-dining-commons": {"locationNum": "03", "locationName": "Livingston Dining Commons"},
    "busch-dining-hall": {"locationNum": "04", "locationName": "Busch Dining Hall"},
    "neilson-dining-hall": {"locationNum": "05", "locationName": "Neilson Dining Hall"},
    "the-atrium": {"locationNum": "07", "locationName": "The Atrium"},
}

# ---------------------------------------------------------------------------
# HTTP client (module-level, lazy-initialized)
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
HTTP_LIMITS = httpx.Limits(max_connections=40, max_keepalive_connections=20)
HTTP_HEADERS = {
    "User-Agent": "DormDishBackend/1.0 (+https://dormdish.com)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS, headers=HTTP_HEADERS
        )
    return _http_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def title_case_name(name: str | None) -> str:
    """Convert menu item name to proper title case."""
    if not name:
        return ""
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return ""
    if name.isupper():
        name = name.title()
        name = re.sub(r'\bAnd\b', 'and', name, flags=re.IGNORECASE)
        name = re.sub(r'\bOr\b', 'or', name, flags=re.IGNORECASE)
        name = re.sub(r'\bOf\b', 'of', name, flags=re.IGNORECASE)
        name = re.sub(r'\bThe\b', 'the', name, flags=re.IGNORECASE)
        name = re.sub(r'\bA\b', 'a', name, flags=re.IGNORECASE)
        if name:
            name = name[0].upper() + name[1:]
    else:
        if name:
            name = name[0].upper() + name[1:]
    return name


def _rutgers_today_str() -> str:
    """Return today's date string in Eastern Time."""
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        from datetime import timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _fetch_with_retries(url: str, attempts: int = 3, base_delay: float = 0.6) -> str:
    """Fetch a URL with retries and exponential backoff."""
    client = _get_http_client()
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            sleep_for = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            print(f"  Retry {attempt}/{attempts} for {url}, waiting {sleep_for:.1f}s")
            await asyncio.sleep(sleep_for)
    if last_exc:
        raise last_exc
    raise RuntimeError("Failed to fetch URL without specific exception")


# ---------------------------------------------------------------------------
# FoodPro menu fetcher (ported from app.py)
# ---------------------------------------------------------------------------

async def fetch_foodpro_menu_rutgers(dining_hall: str, hall_slug: str, date: str) -> dict:
    """Fetch menu from Rutgers FoodPro pickmenu pages and format like Nutrislice output.
    Aggregates breakfast, lunch, and dinner by invoking the scraper per meal.
    """
    if not (fp_build_menu_url and fp_parse_menu):
        raise RuntimeError("FoodPro scraper not available")

    params = LOCATION_MAP.get(hall_slug)
    if not params:
        raise ValueError(f"Unsupported Rutgers FoodPro hall: {hall_slug}")

    def _set_meal_in_url(url: str, meal: str) -> str:
        try:
            u = urlparse(url)
            qs = parse_qs(u.query)
            qs["mealName"] = [meal.capitalize()]
            new_q = urlencode(qs, doseq=True)
            return urlunparse(u._replace(query=new_q))
        except Exception:
            return url

    def _add_food_obj_from_new_schema(food: dict, items: List[dict]):
        if not isinstance(food, dict):
            return
        name = food.get("name")
        if not name:
            return
        out_item: Dict[str, object] = {"name": title_case_name(name)}
        prot = food.get("g_protein")
        if prot is None:
            prot = food.get("protein_g")
        if prot is not None:
            out_item["protein_g"] = prot
        fat = food.get("g_fat") or food.get("fat_g")
        if fat is not None:
            out_item["fat_g"] = fat
        carbs = food.get("g_carbs") or food.get("carbs_g") or food.get("carbohydrates")
        if carbs is not None:
            out_item["carbs_g"] = carbs
        cals = food.get("calories")
        if cals is not None:
            out_item["calories"] = cals
        sv = food.get("serving_size")
        if isinstance(sv, dict):
            amt = sv.get("serving_size_amount")
            unit = sv.get("serving_size_unit")
            parts = []
            if amt not in (None, ""):
                parts.append(str(amt))
            if unit not in (None, ""):
                parts.append(str(unit))
            if parts:
                out_item["serving_size"] = " ".join(parts)
        elif isinstance(sv, str) and sv:
            out_item["serving_size"] = sv
        items.append(out_item)

    async def _attempt_all_meals(location_num: str, location_name: str) -> dict:
        base_url = fp_build_menu_url(date, location_num, location_name)
        all_items: List[dict] = []
        meal_sources: List[str] = []
        warnings: List[str] = []

        # Weekend detection: prefer brunch/dinner order on Sat/Sun
        try:
            weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
        except Exception:
            weekday = None
        if weekday is not None and weekday >= 5:
            meals_sequence = ("brunch", "dinner", "breakfast", "lunch")
        else:
            meals_sequence = ("breakfast", "brunch", "lunch", "dinner")

        async def _fetch_and_parse_meal(meal: str) -> tuple:
            """Fetch and parse a single meal, returns (meal_name, items, warning_or_none)"""
            meal_url = _set_meal_in_url(base_url, meal)
            try:
                html_text = await _fetch_with_retries(meal_url)
            except Exception as exc:
                return (meal, [], f"Failed to fetch {meal}: {exc}")
            try:
                parsed = await asyncio.to_thread(fp_parse_menu, html_text, meal, date, meal_url)
            except Exception as exc:
                return (meal, [], f"Failed to parse {meal}: {exc}")

            items: List[dict] = []
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                for it in parsed.get("items", []):
                    if not isinstance(it, dict):
                        continue
                    name = it.get("name")
                    if not name:
                        continue
                    out_item: Dict[str, object] = {"name": title_case_name(name)}
                    if it.get("protein_g") is not None:
                        out_item["protein_g"] = it.get("protein_g")
                    if it.get("calories") is not None:
                        out_item["calories"] = it.get("calories")
                    if it.get("fat_g") is not None:
                        out_item["fat_g"] = it.get("fat_g")
                    if it.get("carbs_g") is not None:
                        out_item["carbs_g"] = it.get("carbs_g")
                    if it.get("serving_size"):
                        out_item["serving_size"] = it.get("serving_size")
                    items.append(out_item)
            else:
                if isinstance(parsed, dict):
                    meal_map = parsed.get(meal) or {}
                    blocks = meal_map.get(date) or [] if isinstance(meal_map, dict) else []
                    if isinstance(blocks, list):
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue
                            foods = block.get("foods") or []
                            if isinstance(foods, list):
                                for food in foods:
                                    _add_food_obj_from_new_schema(food, items)

            if items:
                return (meal, items, None)
            else:
                return (meal, [], f"No items found for {meal}")

        # Fetch all meals in parallel using asyncio.gather
        print(f"  Fetching {len(meals_sequence)} meals in parallel: {meals_sequence}")
        results = await asyncio.gather(*[_fetch_and_parse_meal(meal) for meal in meals_sequence])

        # Process results and maintain meal order
        for meal, items, warning in results:
            if warning:
                warnings.append(warning)
            if items:
                meal_sources.append(meal)
                all_items.extend(items)

        return {"items": all_items, "meal_sources": meal_sources, "warnings": warnings, "url": base_url}

    attempt = await _attempt_all_meals(params["locationNum"], params["locationName"])

    # Atrium fallback: try alternative location names if no items found
    if hall_slug == "the-atrium" and not attempt["items"]:
        for alt_name in [
            "Atrium",
            "The Atrium at Livingston",
            "The Atrium at Livingston Campus",
        ]:
            try:
                print(f"  Atrium retry with alt_name={alt_name}")
                attempt = await _attempt_all_meals(params["locationNum"], alt_name)
                if attempt["items"]:
                    break
            except Exception as exc:
                print(f"  Atrium retry failed alt_name={alt_name} error={exc}")

    # Deduplicate items by name
    all_items = attempt["items"]
    seen = set()
    unique_items: List[dict] = []
    for it in all_items:
        nm = (it.get("name") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            unique_items.append(it)

    warnings: List[str] = attempt.get("warnings", [])
    if not unique_items:
        warnings.append("No items parsed from FoodPro")
        raise RuntimeError("FoodPro returned zero items")

    meal_sources = attempt.get("meal_sources", [])

    return {
        "university": "Rutgers University",
        "hall": dining_hall,
        "hall_slug": hall_slug,
        "date": date,
        "fetched_at": datetime.now().isoformat(),
        "items": unique_items,
        "menu": {"items": unique_items, "meal_sources": meal_sources or ["foodpro"]},
        "meta": {"warnings": warnings, "notes": ["source: FoodPro"]},
        "error": None,
    }


# ---------------------------------------------------------------------------
# Supabase persistence (synchronous)
# ---------------------------------------------------------------------------

def save_menu_to_supabase(supabase_client, university: str, dining_hall: str, date: str, menu_data: dict) -> bool:
    """Persist menu data to Supabase."""
    try:
        menu_id = f"{university}_{dining_hall}_{date}"
        hall_slug = menu_data.get("hall_slug", dining_hall)
        fetched_at = menu_data.get("fetched_at", datetime.now().isoformat())

        items_count = 0
        if "items" in menu_data:
            items_count = len(menu_data["items"])
        elif "menu" in menu_data and "items" in menu_data["menu"]:
            items_count = len(menu_data["menu"]["items"])

        if items_count == 0:
            print(f"  Skipping empty menu {menu_id}")
            return False

        stored_menu_data = menu_data.get("menu", menu_data)
        data_to_insert = {
            "id": menu_id,
            "university": UNIVERSITY_DISPLAY_NAMES.get(university, university),
            "dining_hall": dining_hall,
            "hall_slug": hall_slug,
            "date": date,
            "fetched_at": fetched_at,
            "menu_data": stored_menu_data,
            "created_at": datetime.now().isoformat(),
        }
        supabase_client.table("menus").upsert(data_to_insert).execute()
        print(f"  Saved {items_count} items for {dining_hall}")
        return True
    except Exception as exc:
        print(f"  Error saving to Supabase: {exc}")
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def fetch_and_cache_menu(supabase_client, university: str, hall_slug: str, date: str, is_foodpro: bool) -> dict:
    """Fetch menu data and save it to Supabase."""
    try:
        if is_foodpro:
            menu_data = await fetch_foodpro_menu_rutgers(hall_slug, hall_slug, date)
        else:
            raise ValueError(f"Non-FoodPro halls not yet supported in standalone script")

        if not menu_data.get("items"):
            return {"success": False, "hall": hall_slug, "error": "No items returned"}

        save_result = save_menu_to_supabase(supabase_client, university, hall_slug, date, menu_data)
        item_count = len(menu_data.get("items", []))
        return {"success": True, "hall": hall_slug, "items": item_count}
    except Exception as e:
        return {"success": False, "hall": hall_slug, "error": str(e)}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Fetch and cache dining hall menus in Supabase")
    parser.add_argument("--date", type=str, help="Date in YYYY-MM-DD format (default: today ET)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without saving")
    args = parser.parse_args()

    load_dotenv()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        print("Error: SUPABASE_URL and SUPABASE_KEY/SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)

    supabase_client = create_client(supabase_url, supabase_key)
    date = args.date or _rutgers_today_str()

    print(f"Menu Refresh Script")
    print(f"Date: {date}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"Halls: {len(ALL_DINING_HALLS)}")
    print("-" * 50)

    start_time = time.time()
    success_count = 0
    fail_count = 0

    try:
        for university, hall_slug, is_foodpro in ALL_DINING_HALLS:
            print(f"\nFetching {hall_slug}...")
            if args.dry_run:
                print(f"  [DRY RUN] Would fetch {university}/{hall_slug} for {date}")
                success_count += 1
                continue

            result = await fetch_and_cache_menu(supabase_client, university, hall_slug, date, is_foodpro)
            if result.get("success"):
                success_count += 1
                print(f"  OK - {result.get('items', 0)} items")
            else:
                fail_count += 1
                print(f"  FAILED - {result.get('error', 'unknown')}")
    finally:
        if _http_client is not None:
            await _http_client.aclose()

    elapsed = time.time() - start_time
    print(f"\n{'=' * 50}")
    print(f"Done: {success_count} success, {fail_count} failed in {elapsed:.1f}s")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
