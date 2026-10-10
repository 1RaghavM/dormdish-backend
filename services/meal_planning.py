"""Deterministic meal validation; callers supply owner-scoped storage records."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from typing import Annotated
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError

from dormdish_agent.models import (
    Contract, MealItem, MealProposal, Name, Nutrition, PlanningContext, calendar_date,
)


class Selection(Contract):
    menu_item_id: Name
    servings: Annotated[float, Field(strict=True, gt=0, le=20, allow_inf_nan=False)]


_selections = TypeAdapter(Annotated[list[Selection], Field(min_length=1, max_length=30)])


@dataclass(frozen=True)
class MenuSnapshot:
    university: str
    context: PlanningContext
    fetched_at: datetime
    expires_at: datetime
    items: tuple[dict, ...]


def _aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Menu freshness requires timezone-aware timestamps")
    return value


def _labels(value):
    if not isinstance(value, (list, tuple, set, frozenset)) or any(
        not isinstance(label, str) or not label.strip() for label in value
    ):
        raise ValueError("Restrictions and safety metadata must be explicit label lists")
    return {label.strip().lower().replace("-", "_").replace(" ", "_") for label in value}


def build_menu_snapshot(row: dict, *, university: str, context: PlanningContext,
                        now: datetime, max_age: timedelta) -> MenuSnapshot:
    """Read a complete `menus` row, preserving date/meal/freshness provenance.

    Only items with complete trusted nutrition enter the selectable catalog.
    Missing dietary/allergen metadata stays unknown until validation requires it.
    """
    if not context.meal_type:
        raise ValueError("Select a meal before retrieving its menu")
    if (row.get("university") != university or row.get("dining_hall") != context.dining_hall
            or calendar_date(row.get("date")) != context.date or row.get("error")):
        raise ValueError("Menu does not match the requested university, hall, and date")
    now, fetched_at = _aware(now), _aware(row.get("fetched_at"))
    if max_age <= timedelta(0) or not fetched_at <= now < fetched_at + max_age:
        raise ValueError("Menu is stale or has a future fetch timestamp")
    raw_items = (row.get("menu_data") or {}).get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Menu has no items")
    digest = sha256(json.dumps(
        [university, context.model_dump(mode="json"), fetched_at.isoformat(), raw_items],
        sort_keys=True, allow_nan=False, separators=(",", ":"),
    ).encode()).hexdigest()
    items, source_ids = [], set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError("Malformed menu item")
        meal = raw.get("meal") or raw.get("meal_type") or row.get("meal")
        if meal != context.meal_type:
            continue
        if row.get("meal") not in (None, context.meal_type):
            raise ValueError("Menu row and item meal disagree")
        if raw.get("meal") and raw.get("meal_type") and raw["meal"] != raw["meal_type"]:
            raise ValueError("Menu item meal labels disagree")
        if raw.get("date") is not None and calendar_date(raw["date"]) != context.date:
            raise ValueError("Menu item date disagrees with its snapshot")
        source_id = raw.get("id")
        if source_id is not None:
            if str(source_id) in source_ids:
                raise ValueError("Duplicate source menu item ID")
            source_ids.add(str(source_id))
        nutrition = {
            field: raw.get(field, raw.get({"protein_g": "g_protein", "fat_g": "g_fat",
                                         "carbs_g": "g_carbs"}.get(field, field)))
            for field in Nutrition.model_fields
        }
        serving = raw.get("serving_size")
        if isinstance(serving, dict):
            serving = f"{serving.get('serving_size_amount', '')} {serving.get('serving_size_unit', '')}".strip()
        try:
            item = MealItem(menu_item_id=f"{digest}:{index}", name=raw.get("name"),
                            serving_size=serving, servings=1.0, **nutrition)
        except ValidationError:
            continue
        items.append({**item.model_dump(), "allergens": raw.get("allergens"),
                      "dietary_tags": raw.get("dietary_tags")})
    if not items:
        raise ValueError("No menu items have verified meal membership and complete nutrition")
    return MenuSnapshot(university, context.model_copy(deep=True), fetched_at,
                        fetched_at + max_age, tuple(items))


def validate_meal(snapshot: MenuSnapshot, selections: list, *, university: str,
                  context: PlanningContext, now: datetime, restrictions=(), allergens=(),
                  description: str = "") -> MealProposal:
    """Validate the entire final selection, including substitutions, before preview/save."""
    if snapshot.university != university or snapshot.context != context:
        raise ValueError("Menu snapshot does not match this request")
    if not snapshot.fetched_at <= _aware(now) < snapshot.expires_at:
        raise ValueError("Menu snapshot expired; retrieve and validate a fresh menu")
    choices = _selections.validate_python(selections)
    if len({choice.menu_item_id for choice in choices}) != len(choices):
        raise ValueError("Duplicate selection; combine servings first")
    required, excluded = _labels(restrictions), _labels(allergens)
    catalog = {item["menu_item_id"]: item for item in snapshot.items}
    items = []
    for choice in choices:
        raw = catalog.get(choice.menu_item_id)
        if raw is None:
            raise ValueError("Selected item is not in this menu snapshot")
        if required and (raw["dietary_tags"] is None or not required <= _labels(raw["dietary_tags"])):
            raise ValueError("Dietary restriction is unsupported or safety metadata is unknown")
        if excluded and (raw["allergens"] is None or excluded & _labels(raw["allergens"])):
            raise ValueError("Allergen restriction failed or allergen metadata is unknown")
        items.append(MealItem(
            menu_item_id=choice.menu_item_id, name=raw["name"], serving_size=raw["serving_size"],
            servings=choice.servings,
            **{field: raw[field] * choice.servings for field in Nutrition.model_fields},
        ))
    return MealProposal(
        id=uuid4(), version=1, **context.model_dump(), description=description, items=items,
        totals=Nutrition(**{field: sum(getattr(item, field) for item in items)
                            for field in Nutrition.model_fields}),
    )


def remaining_targets(targets: Nutrition, food_logs: list[dict], completions: list[dict],
                      *, user_id: str, requested_date: date) -> Nutrition:
    """Subtract actual intake once. Stored food-log amounts already include servings.

    Completion records must carry `completed: true`, `source_id`, and `totals`.
    Shared source IDs link a completion to a food log; conflicting duplicates fail.
    """
    requested_date = calendar_date(requested_date)
    consumed = {}
    for kind, rows in (("food_log", food_logs), ("completion", completions)):
        for row in rows:
            if str(row.get("user_id")) != user_id or calendar_date(row.get("date")) != requested_date:
                raise ValueError("Intake record does not belong to this user and date")
            if kind == "completion" and row.get("completed") is not True:
                continue
            identity = row.get("source_id") or (f"food_log:{row['id']}" if kind == "food_log" and row.get("id") else None)
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("Intake evidence requires a stable source identity")
            values = row if kind == "food_log" else row.get("totals", {})
            nutrition = Nutrition(**{field: values.get(field) for field in Nutrition.model_fields})
            if identity in consumed and consumed[identity] != nutrition:
                raise ValueError("Conflicting intake records for the same source")
            consumed[identity] = nutrition
    return Nutrition(**{
        field: max(0, getattr(targets, field) - sum(getattr(item, field) for item in consumed.values()))
        for field in Nutrition.model_fields
    })
