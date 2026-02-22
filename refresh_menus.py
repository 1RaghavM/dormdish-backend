"""
Daily Menu Refresh Script

Fetches dining hall menus from FoodPro and caches them in Supabase.
Designed to run as a GitHub Actions cron job at 5 AM ET daily.

Usage:
    python refresh_menus.py [--date YYYY-MM-DD]
"""

import os
import re
import sys
import asyncio
import random
import time
import argparse
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from supabase import create_client

from extract import (
    build_menu_url as fp_build_menu_url,
    fetch as fp_fetch,
    parse_menu as fp_parse_menu,
)

# --- Configuration ---

UNIVERSITY_DISPLAY_NAMES = {
    "rutgers": "Rutgers University",
    "hopkins": "Johns Hopkins University",
}

ALL_DINING_HALLS = [
    ("rutgers", "busch-dining-hall", True),
    ("rutgers", "livingston-dining-commons", True),
    ("rutgers", "neilson-dining-hall", True),
    ("rutgers", "the-atrium", True),
]

ATRIUM_CATEGORY_TO_RESTAURANT = {
    "pi pizza": "PI Pizza",
    "pi pizza toppings": "PI Pizza",
    "mezze proteins": "Mezze",
    "mezze bases": "Mezze",
    "mezze toppings and sides": "Mezze",
    "scarlet ginger": "Scarlet Ginger",
    "scarlet ginger noodle bowl": "Scarlet Ginger",
    "kings hawaiian": "Kings Hawaiian",
    "three chilies": "Three Chilies",
    "salad bar": "Salad Bar",
    "soups": "Soups",
    "grab and go": "Grab and Go",
}

ATRIUM_RESTAURANTS = [
    "PI Pizza", "Mezze", "Scarlet Ginger", "Kings Hawaiian",
    "Three Chilies", "Salad Bar", "Soups", "Grab and Go",
]

LOCATION_MAP = {
    "livingston-dining-commons": {"locationNum": "03", "locationName": "Livingston Dining Commons"},
    "busch-dining-hall": {"locationNum": "04", "locationName": "Busch Dining Hall"},
    "neilson-dining-hall": {"locationNum": "05", "locationName": "Neilson Dining Hall"},
    "the-atrium": {"locationNum": "07", "locationName": "The Atrium"},
}

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# --- Helpers ---

def title_case_name(name: str | None) -> str:
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
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")


# --- HTTP fetching ---

async def _fetch_with_retries(client: httpx.AsyncClient, url: str, attempts: int = 3, base_delay: float = 0.6) -> str:
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
            print(f"  ⏳ Retry {attempt}/{attempts} for {url[:80]}... sleeping {sleep_for:.1f}s")
            await asyncio.sleep(sleep_for)
    raise last_exc  # type: ignore[misc]


# --- FoodPro scraping (ported from app.py) ---

def _set_meal_in_url(url: str, meal: str) -> str:
    try:
        u = urlparse(url)
        qs = parse_qs(u.query)
        qs["mealName"] = [meal.capitalize()]
        new_q = urlencode(qs, doseq=True)
        return urlunparse(u._replace(query=new_q))
    except Exception:
        return url


def _add_food_obj_from_new_schema(food: dict, items: List[dict], category: str = None) -> None:
    if not isinstance(food, dict):
        return
    name = food.get("name")
    if not name:
        return
    out_item: Dict[str, object] = {"name": title_case_name(name)}
    if category:
        out_item["category"] = category
    prot = food.get("g_protein") or food.get("protein_g")
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


async def fetch_foodpro_menu(client: httpx.AsyncClient, hall_slug: str, date: str) -> dict:
    params = LOCATION_MAP.get(hall_slug)
    if not params:
        raise ValueError(f"Unsupported FoodPro hall: {hall_slug}")

    base_url = fp_build_menu_url(date, params["locationNum"], params["locationName"])

    try:
        weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
    except Exception:
        weekday = None
    if weekday is not None and weekday >= 5:
        meals_sequence = ("brunch", "dinner", "breakfast", "lunch")
    else:
        meals_sequence = ("breakfast", "brunch", "lunch", "dinner")

    async def _fetch_and_parse_meal(meal: str) -> tuple[str, List[dict], str | None]:
        meal_url = _set_meal_in_url(base_url, meal)
        try:
            html_text = await _fetch_with_retries(client, meal_url)
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
                    out_item["protein_g"] = it["protein_g"]
                if it.get("calories") is not None:
                    out_item["calories"] = it["calories"]
                if it.get("fat_g") is not None:
                    out_item["fat_g"] = it["fat_g"]
                if it.get("carbs_g") is not None:
                    out_item["carbs_g"] = it["carbs_g"]
                if it.get("serving_size"):
                    out_item["serving_size"] = it["serving_size"]
                if it.get("category"):
                    out_item["category"] = it["category"]
                items.append(out_item)
        else:
            if isinstance(parsed, dict):
                meal_map = parsed.get(meal) or {}
                blocks = meal_map.get(date) or [] if isinstance(meal_map, dict) else []
                if isinstance(blocks, list):
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        block_category = block.get("category")
                        foods = block.get("foods") or []
                        if isinstance(foods, list):
                            for food in foods:
                                _add_food_obj_from_new_schema(food, items, category=block_category)

        if items:
            return (meal, items, None)
        return (meal, [], f"No items found for {meal}")

    print(f"  🔄 Fetching {len(meals_sequence)} meals in parallel: {', '.join(meals_sequence)}")
    results = await asyncio.gather(*[_fetch_and_parse_meal(meal) for meal in meals_sequence])

    all_items: List[dict] = []
    meal_sources: List[str] = []
    warnings: List[str] = []

    for meal, items, warning in results:
        if warning:
            warnings.append(warning)
        if items:
            meal_sources.append(meal)
            all_items.extend(items)

    # Deduplicate by name
    seen = set()
    unique_items: List[dict] = []
    for it in all_items:
        nm = (it.get("name") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            unique_items.append(it)

    if not unique_items:
        raise RuntimeError(f"FoodPro returned zero items for {hall_slug}")

    # Map categories to restaurants for Atrium
    present_restaurants: List[str] = []
    if hall_slug == "the-atrium":
        seen_restaurants: set = set()
        for item in unique_items:
            cat = (item.get("category") or "").strip().lower()
            restaurant = ATRIUM_CATEGORY_TO_RESTAURANT.get(cat)
            if restaurant:
                item["restaurant"] = restaurant
                if restaurant not in seen_restaurants:
                    seen_restaurants.add(restaurant)
                    present_restaurants.append(restaurant)

    result = {
        "university": "Rutgers University",
        "hall": hall_slug,
        "hall_slug": hall_slug,
        "date": date,
        "fetched_at": datetime.now().isoformat(),
        "items": unique_items,
        "menu": {"items": unique_items, "meal_sources": meal_sources or ["foodpro"]},
        "meta": {"warnings": warnings, "notes": ["source: FoodPro"]},
        "error": None,
    }
    if present_restaurants:
        result["restaurants"] = present_restaurants
        result["menu"]["restaurants"] = present_restaurants
    return result


# --- Supabase persistence ---

def save_menu_to_supabase(supabase_client, university: str, dining_hall: str, date: str, menu_data: dict) -> bool:
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
            print(f"  ⚠️ Skipping save for empty menu {menu_id}")
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
        print(f"  💾 Saved {items_count} items to Supabase: {menu_id}")
        return True
    except Exception as exc:
        print(f"  ⚠️ Error saving to Supabase: {exc}")
        return False


# --- Orchestration ---

async def fetch_and_cache_menu(client: httpx.AsyncClient, supabase_client, university: str, hall_slug: str, date: str, is_foodpro: bool) -> dict:
    try:
        if is_foodpro:
            print(f"🌐 Fetching FoodPro menu for {university}/{hall_slug}")
            menu_data = await fetch_foodpro_menu(client, hall_slug, date)
        else:
            print(f"⚠️ Non-FoodPro halls not supported in standalone script")
            return {"success": False, "hall": hall_slug, "error": "Non-FoodPro not supported"}

        if not menu_data.get("items"):
            print(f"  ⚠️ No items for {hall_slug}, skipping cache")
            return {"success": False, "hall": hall_slug, "error": "No items returned"}

        save_menu_to_supabase(supabase_client, university, hall_slug, date, menu_data)
        item_count = len(menu_data.get("items", []))
        print(f"  ✅ Cached {item_count} items for {hall_slug}")
        return {"success": True, "hall": hall_slug, "items": item_count}
    except Exception as e:
        print(f"  ❌ Failed to fetch {hall_slug}: {e}")
        return {"success": False, "hall": hall_slug, "error": str(e)}


async def main(target_date: str | None = None) -> int:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")

    if not supabase_url or not supabase_key:
        print("❌ Supabase credentials not found in environment variables")
        print("   Set SUPABASE_URL and SUPABASE_SERVICE_KEY")
        return 1

    supabase_client = create_client(supabase_url, supabase_key)

    date = target_date or _rutgers_today_str()

    print(f"🕐 Starting daily menu refresh...")
    print(f"📅 Date: {date}")
    print(f"🏫 Halls: {len(ALL_DINING_HALLS)}")
    print("-" * 50)

    start_time = time.time()
    success_count = 0
    fail_count = 0

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for university, hall_slug, is_foodpro in ALL_DINING_HALLS:
            result = await fetch_and_cache_menu(client, supabase_client, university, hall_slug, date, is_foodpro)
            if result.get("success"):
                success_count += 1
            else:
                fail_count += 1

    elapsed = time.time() - start_time
    print("-" * 50)
    print(f"✅ Menu refresh complete: {success_count} success, {fail_count} failed in {elapsed:.1f}s")

    return 1 if fail_count == len(ALL_DINING_HALLS) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh dining hall menus and cache in Supabase")
    parser.add_argument("--date", type=str, help="Date in YYYY-MM-DD format (default: today ET)")
    args = parser.parse_args()

    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"❌ Invalid date format: {args.date}. Use YYYY-MM-DD.")
            sys.exit(1)

    sys.exit(asyncio.run(main(args.date)))
