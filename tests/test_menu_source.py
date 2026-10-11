"""Independent offline ingestion/caller regressions for menu provenance PR #7."""
import importlib.util
import os
import socket
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

DAY = "2026-10-09"


@pytest.fixture(scope="module")
def api():
    # The override replays exactly these tests against the pre-fix app in a temporary file.
    source = Path(os.environ.get("MENU_SOURCE_APP_FILE", Path(__file__).parents[1] / "app.py"))
    spec = importlib.util.spec_from_file_location("menu_source_under_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv", return_value=False), patch.dict(os.environ, {}, clear=True):
        spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*_args, **_kwargs):
        raise AssertionError("Menu source tests must not access the network")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


@pytest.fixture
def database(api, monkeypatch):
    class Menus:
        def __init__(self):
            self.rows = {}
            self.selected = None

        def table(self, name):
            assert name == "menus"
            return self

        def upsert(self, row):
            self.rows[row["id"]] = deepcopy(row)
            self.selected = row["id"]
            return self

        def select(self, fields):
            assert fields == "*"
            return self

        def eq(self, key, value):
            assert key == "id"
            self.selected = value
            return self

        def execute(self):
            row = self.rows.get(self.selected)
            return SimpleNamespace(data=[deepcopy(row)] if row else [])

    menus = Menus()
    monkeypatch.setattr(api, "supabase", menus)
    return menus


def nutrislice_food(portion=1, **metadata):
    return {"food": {
        "name": "  HERBAL\n TEA ",
        "rounded_nutrition_info": {"calories": 0, "g_protein": 0, "g_fat": 0, "g_carbs": 0},
        "serving_size_info": {"serving_size_amount": portion, "serving_size_unit": "cup"},
        **metadata,
    }}


def install_nutrislice(api, monkeypatch, items, *, wrapped=False, key="menu_items"):
    requests = []

    async def get(url):
        requests.append(url)
        day = {"date": DAY, key: deepcopy(items)}
        days = [{"date": "2026-10-08", "menu_items": [{"name": "WRONG DAY"}]}, day]
        payload = {"week": {"days": days}} if wrapped else {"days": days}
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(api, "_get_http_client", lambda: SimpleNamespace(get=get))
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("university,hall,alias,lunch_slug", [
    ("rutgers", "brower-commons", "  College AVE ", "lunch-test"),
    ("hopkins", "nolans-on-33rd", "nolans-on-33rd", "nolans-dinner-2"),
])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_raw_nutrislice_to_per_meal_rows_and_cached_caller(api, database, monkeypatch, university, hall, alias, lunch_slug, wrapped):
    small = nutrislice_food(allergens=[], dietary_tags=["vegan"], nutrition_url="https://menu.example/tea")
    large = nutrislice_food(2, allergens=["milk"], dietary_tags=["vegetarian"], nutrition_url="https://menu.example/large")
    requests = install_nutrislice(api, monkeypatch, [small, large, small], wrapped=wrapped)
    before = datetime.now(timezone.utc)
    result = await api.fetch_and_cache_menu(university, hall, DAY, is_foodpro=False)
    after = datetime.now(timezone.utc)
    assert result == {"success": True, "hall": hall, "items": 6}
    assert len(requests) == 3 and any(f"/menu-type/{lunch_slug}/" in url for url in requests)
    assert all(url.endswith("/2026/10/09/") for url in requests)
    assert len(database.rows) == 4
    for meal in ("breakfast", "lunch", "dinner"):
        row = database.rows[f"{university}_{hall}_{DAY}_{meal}"]
        items = row["menu_data"]["items"]
        assert row["date"] == DAY and row["meal"] == meal
        assert row["menu_data"]["meal_sources"] == [meal]
        assert [(item["name"], item["meal"], item["serving_size"]) for item in items] == [
            ("Herbal Tea", meal, "1 cup"), ("Herbal Tea", meal, "2 cup")]
        assert items[0]["allergens"] == [] and items[1]["allergens"] == ["milk"]
        assert items[0]["dietary_tags"] == ["vegan"]
        assert items[1]["nutrition_url"] == "https://menu.example/large"
        assert all(item[key] == 0 for item in items for key in ("calories", "protein_g", "fat_g", "carbs_g"))
        for field in ("fetched_at", "created_at"):
            instant = datetime.fromisoformat(row[field])
            assert instant.utcoffset() is not None
            assert before <= instant <= after
    cached = await api.get_dining_hall_menu(alias, DAY)
    assert len(cached["items"]) == 6
    assert {item["meal"] for item in cached["items"]} == {"breakfast", "lunch", "dinner"}
    assert len(requests) == 3  # The user-facing cache read must not re-fetch upstream.


@pytest.mark.asyncio
@pytest.mark.parametrize("group_key", ["menu_groups", "groups", "categories", "menuCategories", "stations"])
async def test_group_ingestion_does_not_infer_safety_from_outer_wrapper(api, monkeypatch, group_key):
    unknown = nutrislice_food()
    unknown.update(allergens=[], dietary_tags=["vegan"], nutrition_url="https://untrusted-wrapper.example")
    explicitly_unknown = nutrislice_food(allergens=None, dietary_tags=None, nutrition_url=None)
    install_nutrislice(api, monkeypatch, [{"items": [unknown, explicitly_unknown]}], key=group_key)
    items = await api.fetch_meal_items("https://menus.example", "hall", "dinner", DAY)
    assert len(items) == 2
    for key in ("allergens", "dietary_tags", "nutrition_url"):
        assert key not in items[0]
        assert key in items[1] and items[1][key] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["menu_items", "items", "menuItems"])
async def test_flat_ingestion_null_aliases_and_invalid_macros_are_not_fabricated(api, monkeypatch, key):
    raw = {"item_name": "SOUP AND SALAD", "g_protein": None, "protein": "0",
           "calories": None, "kcal": 0, "g_fat": "not measured", "fat": 19,
           "g_carbs": 0, "carbohydrates": 25, "portion_size": "1 bowl", "allergens": []}
    install_nutrislice(api, monkeypatch, [raw, {}, "not an item"], key=key)
    items = await api.fetch_meal_items("https://menus.example", "hall", "lunch", DAY)
    assert items == [{"name": "Soup and Salad", "protein_g": 0, "calories": 0,
                      "carbs_g": 0, "serving_size": "1 bowl", "allergens": []}]


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True])
async def test_foodpro_cron_keeps_station_portions_and_unknown_safety(api, database, monkeypatch, flat):
    import extract
    small = {"name": "PLAIN PASTA", "protein_g": 0, "calories": 0, "fat_g": 0, "carbs_g": 0,
             "serving_size": "1 oz", "category": "pi pizza", "allergens": ["wheat"],
             "dietary_tags": ["vegan"], "nutrition_url": "https://menu.example/pasta"}
    large = {**small, "serving_size": "4 oz", "calories": 200, "carbs_g": 40, "category": "mezze bases"}
    unknown = {key: value for key, value in small.items() if key not in ("allergens", "dietary_tags", "nutrition_url")}

    def parse(_html, meal, day, _url):
        foods = [small, large, unknown, small]
        if flat:
            return {"items": deepcopy(foods)}
        return {meal: {day: [{"category": food["category"], "foods": [deepcopy(food)]} for food in foods]}}

    monkeypatch.setattr(extract, "parse_menu", parse)
    monkeypatch.setattr(api, "_fetch_with_retries", AsyncMock(return_value="synthetic FoodPro document"))
    result = await api.fetch_and_cache_menu("rutgers", "the-atrium", DAY, is_foodpro=True)
    assert result["success"] and result["items"] == 12
    for meal in ("breakfast", "brunch", "lunch", "dinner"):
        row = database.rows[f"rutgers_the-atrium_{DAY}_{meal}"]
        items = row["menu_data"]["items"]
        assert len(items) == 3 and {item["meal"] for item in items} == {meal}
        assert [(item["serving_size"], item["restaurant"], item["calories"]) for item in items] == [
            ("1 oz", "PI Pizza", 0), ("4 oz", "Mezze", 200), ("1 oz", "PI Pizza", 0)]
        assert items[0]["allergens"] == ["wheat"] and items[1]["carbs_g"] == 40
        assert all(key not in items[2] for key in ("allergens", "dietary_tags", "nutrition_url"))
    cached = await api.get_dining_hall_menu("  ATRIUM ", DAY)
    assert len(cached["items"]) == 12


@pytest.mark.asyncio
async def test_no_matching_date_never_saves_another_days_menu(api, database, monkeypatch):
    install_nutrislice(api, monkeypatch, [nutrislice_food()])
    result = await api.fetch_and_cache_menu("rutgers", "brower-commons", "2026-10-11", is_foodpro=False)
    assert not result["success"] and database.rows == {}


@pytest.mark.asyncio
async def test_missing_macros_stay_missing_through_storage(api, database, monkeypatch):
    install_nutrislice(api, monkeypatch, [{"name": "Daily Special"}])
    result = await api.fetch_and_cache_menu("rutgers", "brower-commons", DAY, is_foodpro=False)
    assert result["success"]
    cached = await api.get_dining_hall_menu("brower", DAY)
    assert all(set(item) == {"name", "meal"} for item in cached["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fetched", [None, "2025-12-31T23:30:00-05:00"])
async def test_storage_freshness_does_not_replace_known_source_time(api, database, fetched):
    payload = {"items": [{"name": "Unmeasured Special"}]}
    if fetched is not None:
        payload["fetched_at"] = fetched
    before = datetime.now(timezone.utc)
    assert await api.save_menu_to_supabase("rutgers", "brower-commons", DAY, payload)
    after = datetime.now(timezone.utc)
    row = next(iter(database.rows.values()))
    observed = datetime.fromisoformat(row["fetched_at"])
    assert observed.utcoffset() is not None
    if fetched is None:
        assert before <= observed <= after
    else:
        assert row["fetched_at"] == fetched
    assert before <= datetime.fromisoformat(row["created_at"]) <= after
