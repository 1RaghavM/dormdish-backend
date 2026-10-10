"""Source adapters preserve meal membership and measured zero nutrition offline."""
import importlib.util
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def api():
    spec = importlib.util.spec_from_file_location("menu_source_app", Path(__file__).parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv", return_value=False), patch.dict(os.environ, {}, clear=True):
        spec.loader.exec_module(module)
    return module


def food():
    return {"name": "Rice", "calories": 100, "protein_g": 0, "fat_g": 0,
            "carbs_g": 25, "serving_size": "1 cup"}


def test_flat_zero_is_not_missing_or_replaced_by_alias(api):
    item = api.process_menu_item({"name": "Water", "g_protein": 0, "protein": 9,
        "calories": 0, "kcal": 9, "g_fat": 0, "fat": 9, "g_carbs": 0,
        "carbs": 9, "serving_size": "1 cup"})
    assert [item[key] for key in ("protein_g", "calories", "fat_g", "carbs_g")] == [0] * 4
    assert "allergens" not in item and "dietary_tags" not in item


@pytest.mark.parametrize("nested", [False, True])
def test_explicit_metadata_survives_without_inference(api, nested):
    source = {"name": "Rice", "allergens": None, "dietary_tags": ["vegan"],
              "nutrition_url": "https://dining.example/rice"}
    result = api.process_menu_item({"food": source} if nested else source)
    assert {key: result[key] for key in source} == source


@pytest.mark.asyncio
@pytest.mark.parametrize("university,hall", [("rutgers", "busch-dining-hall"), ("hopkins", "nolans-on-33rd")])
async def test_nutrislice_retains_food_in_each_meal_and_maps_special_slugs(api, monkeypatch, university, hall):
    async def fetch(*_):
        return [food(), food()]
    monkeypatch.setattr(api, "fetch_meal_items", fetch)
    menu = await api.fetch_nutrislice_menu(university, hall, hall, "2026-10-10")
    assert [(item["name"], item["meal"]) for item in menu["items"]] == [
        ("Rice", "breakfast"), ("Rice", "lunch"), ("Rice", "dinner")]
    assert datetime.fromisoformat(menu["fetched_at"]).utcoffset() == timedelta(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True])
async def test_foodpro_preserves_each_meal_metadata_and_zero_macros(api, monkeypatch, flat):
    import extract
    item = {**food(), "g_fat": 0, "g_carbs": 0, "carbs_g": 0,
            "allergens": [], "dietary_tags": ["vegan"], "nutrition_url": "https://dining.example/rice"}
    def parse(_html, meal, day, _url):
        return {"items": [item, item]} if flat else {meal: {day: [{"foods": [item, item]}]}}
    monkeypatch.setattr(extract, "parse_menu", parse)
    monkeypatch.setattr(api, "_fetch_with_retries", AsyncMock(return_value="synthetic"))
    menu = await api.fetch_foodpro_menu_rutgers("Busch", "busch-dining-hall", "2026-10-09")
    assert {item["meal"] for item in menu["items"]} == {"breakfast", "brunch", "lunch", "dinner"}
    assert len(menu["items"]) == 4
    assert all(item["fat_g"] == item["carbs_g"] == 0 for item in menu["items"])
    assert all(item["allergens"] == [] and item["dietary_tags"] == ["vegan"] for item in menu["items"])
    assert datetime.fromisoformat(menu["fetched_at"]).utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_storage_preserves_fetch_time_and_writes_aware_creation_time(api, monkeypatch):
    database = MagicMock()
    database.table.return_value.upsert.return_value.execute.return_value.data = [{}]
    monkeypatch.setattr(api, "supabase", database)
    fetched = "2026-10-10T09:00:00+00:00"
    assert await api.save_menu_to_supabase("rutgers", "busch-dining-hall", "2026-10-10",
                                         {"items": [food()], "fetched_at": fetched}, meal="dinner")
    row = database.table.return_value.upsert.call_args.args[0]
    assert row["fetched_at"] == fetched
    assert row["meal"] == "dinner" and row["date"] == "2026-10-10"
    assert datetime.fromisoformat(row["created_at"]).utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_same_name_with_different_portions_remains_selectable(api, monkeypatch):
    async def fetch(*_):
        return [food(), {**food(), "serving_size": "2 cups", "calories": 200}, food()]
    monkeypatch.setattr(api, "fetch_meal_items", fetch)
    menu = await api.fetch_nutrislice_menu("rutgers", "busch-dining-hall", "Busch", "2026-10-10")
    assert len(menu["items"]) == 6
    assert [item["calories"] for item in menu["items"] if item["meal"] == "dinner"] == [100, 200]
