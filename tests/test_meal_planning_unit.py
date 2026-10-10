from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from dormdish_agent.models import Nutrition, PlanningContext
from services.meal_planning import build_menu_snapshot, remaining_targets, validate_meal

NOW = datetime(2026, 10, 10, 16, tzinfo=timezone.utc)
CONTEXT = PlanningContext(dining_hall="busch", date="2026-10-10", meal_type="dinner")
ROW = {"university": "Rutgers", "dining_hall": "busch", "date": "2026-10-10",
       "fetched_at": NOW.isoformat(), "menu_data": {"items": [
           {"name": "Rice", "meal": "dinner", "serving_size": "1 cup", "calories": 200,
            "protein_g": 4, "fat_g": 1, "carbs_g": 42, "allergens": [], "dietary_tags": ["vegan"]},
           {"name": "Chicken", "meal": "dinner", "serving_size": "1 piece", "calories": 250,
            "protein_g": 45, "fat_g": 5, "carbs_g": 0},
       ]}}


def snapshot(row=None):
    return build_menu_snapshot(row or ROW, university="Rutgers", context=CONTEXT,
                               now=NOW, max_age=timedelta(hours=6))


def validate(menu, choices, **kwargs):
    return validate_meal(menu, choices, university="Rutgers", context=CONTEXT, now=NOW, **kwargs)


def test_fractional_nutrition_and_substitution_use_final_selection():
    menu = snapshot()
    rice, chicken = [item["menu_item_id"] for item in menu.items]
    original = validate(menu, [{"menu_item_id": chicken, "servings": 2.0}])
    replacement = validate(menu, [{"menu_item_id": rice, "servings": 1.5}])
    assert original.totals.calories == 500
    assert replacement.totals.model_dump() == dict(calories=300, protein_g=6, fat_g=1.5, carbs_g=63)
    assert replacement.date == CONTEXT.date
    assert snapshot().items == menu.items


@pytest.mark.parametrize("servings", [0, -1, 21, float("nan"), float("inf"), True, "1"])
def test_invalid_portions_fail(servings):
    menu = snapshot()
    with pytest.raises(ValueError):
        validate(menu, [{"menu_item_id": menu.items[0]["menu_item_id"], "servings": servings}])


def test_reject_whole_unknown_or_duplicate_proposal():
    menu = snapshot()
    choice = {"menu_item_id": menu.items[0]["menu_item_id"], "servings": 1.0}
    for choices in ([choice, choice], [choice, {**choice, "menu_item_id": "Rice"}]):
        with pytest.raises(ValueError):
            validate(menu, choices)


@pytest.mark.parametrize("change", [
    {"date": "2026-10-09"}, {"dining_hall": "livingston"}, {"university": "Other"},
    {"fetched_at": None}, {"fetched_at": "2026-10-10T16:00:00"},
    {"fetched_at": (NOW - timedelta(days=1)).isoformat()},
    {"fetched_at": (NOW + timedelta(seconds=1)).isoformat()}, {"menu_data": {"items": []}},
])
def test_invalid_snapshot_context_or_freshness(change):
    with pytest.raises(ValueError):
        snapshot({**ROW, **change})


def test_hard_restrictions_need_explicit_metadata():
    menu = snapshot()
    rice, chicken = [{"menu_item_id": item["menu_item_id"], "servings": 1.0} for item in menu.items]
    assert validate(menu, [rice], restrictions=["vegan"], allergens=["milk"]).totals.calories == 200
    for kwargs in ({"restrictions": ["vegan"]}, {"allergens": ["milk"]}):
        with pytest.raises(ValueError):
            validate(menu, [chicken], **kwargs)
    row = deepcopy(ROW)
    row["menu_data"]["items"][0]["allergens"] = ["milk"]
    menu = snapshot(row)
    with pytest.raises(ValueError):
        validate(menu, [{**rice, "menu_item_id": menu.items[0]["menu_item_id"]}], allergens=["milk"])


def test_intake_uses_evidence_and_deduplicates_across_sources_without_multiplying_servings():
    targets = Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=250)
    log = {"id": "lunch", "user_id": "owner", "date": "2026-10-10", "servings": 2.0,
           "calories": 600, "protein_g": 40, "fat_g": 20, "carbs_g": 70}
    completion = {"user_id": "owner", "date": "2026-10-10", "completed": True,
                  "source_id": "food_log:lunch", "totals": {key: log[key] for key in Nutrition.model_fields}}
    planned = {**completion, "source_id": "dinner", "completed": False}
    result = remaining_targets(targets, [log, log], [completion, planned], user_id="owner", requested_date=CONTEXT.date)
    assert result.model_dump() == dict(calories=1400, protein_g=60, fat_g=50, carbs_g=180)
    with pytest.raises(ValueError):
        remaining_targets(targets, [log], [], user_id="other", requested_date=CONTEXT.date)
    with pytest.raises(ValueError):
        remaining_targets(targets, [log, {**log, "calories": 1}], [], user_id="owner", requested_date=CONTEXT.date)
