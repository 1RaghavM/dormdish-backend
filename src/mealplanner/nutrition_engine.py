"""Deterministic nutrition math utilities for the meal planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, MutableMapping, Sequence

from .schemas import MealPlan, MealSlot, MenuItem, Totals, Unit

__all__ = [
    "configure_menu",
    "portion_to_grams",
    "sum_plan",
    "get_item_profile",
    "ItemNutrition",
]

OZ_TO_GRAMS = 28.349523125


@dataclass(frozen=True)
class ItemNutrition:
    """Canonical nutrition data anchored to a menu item."""

    calories_per_gram: float
    protein_per_gram: float
    grams_per_unit: Mapping[Unit, float]


_ITEM_PROFILES: MutableMapping[str, ItemNutrition] = {}


def configure_menu(menu: Sequence[MenuItem]) -> None:
    """Register menu items that can participate in deterministic math.

    The planner is expected to pass menu entries whose names are already
    canonicalised. The resulting cache powers all subsequent calculations.
    """

    _ITEM_PROFILES.clear()
    for item in menu:
        grams_per_serving = _canonical_serving_grams(item)
        calories = _require_numeric(item, "calories")
        protein = _require_numeric(item, "g_protein")
        calories_per_gram = calories / grams_per_serving
        protein_per_gram = protein / grams_per_serving

        grams_per_unit: Dict[Unit, float] = {
            item.serving_size.unit: _grams_per_unit(item, item.serving_size.unit)
        }
        key = item.name.strip().upper()
        _ITEM_PROFILES[key] = ItemNutrition(
            calories_per_gram=calories_per_gram,
            protein_per_gram=protein_per_gram,
            grams_per_unit=grams_per_unit,
        )


def portion_to_grams(item: MenuItem, qty: float, unit: Unit | str) -> float:
    """Convert a portion expressed in units to grams.

    Parameters
    ----------
    item:
        The menu entry describing the canonical serving size.
    qty:
        Quantity of the item the solver selected for a given meal.
    unit:
        Declared unit of the quantity.
    """

    if qty <= 0:
        return 0.0

    normalized_unit = Unit(unit)
    if normalized_unit is not item.serving_size.unit:
        raise ValueError(
            f"Requested unit {normalized_unit.value} does not match canonical serving unit {item.serving_size.unit.value}"
        )
    grams_per_unit = _grams_per_unit(item, normalized_unit)
    return grams_per_unit * qty


def sum_plan(meal_plan: MealPlan) -> Totals:
    """Compute deterministic totals for a meal plan."""

    calories = 0.0
    protein = 0.0
    weight = 0.0

    for slot in MealSlot:
        meal = getattr(meal_plan.meals, slot.value)
        for meal_item in meal.items:
            profile = _lookup_profile(meal_item.name)
            unit = Unit(meal_item.unit)
            grams_per_unit = profile.grams_per_unit.get(unit)
            if grams_per_unit is None:
                raise KeyError(f"Unit {unit.value} not registered for {meal_item.name}")
            grams = grams_per_unit * meal_item.qty
            calories += grams * profile.calories_per_gram
            protein += grams * profile.protein_per_gram
            weight += grams

    return Totals(
        calories=round(calories),
        protein_g=round(protein),
        net_weight_g=round(weight),
    )


def _lookup_profile(name: str) -> ItemNutrition:
    canonical = name.strip().upper()
    if canonical not in _ITEM_PROFILES:
        raise KeyError(
            f"Item '{name}' was not registered in nutrition engine. "
            "Call configure_menu() with the canonical menu before summing."
        )
    return _ITEM_PROFILES[canonical]


def _canonical_serving_grams(item: MenuItem) -> float:
    unit = item.serving_size.unit
    qty = item.serving_size.amount
    if unit is Unit.OZ:
        return qty * OZ_TO_GRAMS
    if unit is Unit.EACH:
        if item.serving_size.gram_weight is None:
            raise ValueError(f"gram_weight is required for 'each' serving: {item.name}")
        return item.serving_size.gram_weight
    raise ValueError(f"Unsupported unit: {unit}")


def _grams_per_unit(item: MenuItem, unit: Unit) -> float:
    if unit is Unit.OZ:
        return OZ_TO_GRAMS
    if unit is Unit.EACH:
        gram_weight = item.serving_size.gram_weight
        if gram_weight is None:
            raise ValueError(f"gram_weight is required to compute 'each' portions: {item.name}")
        return gram_weight / item.serving_size.amount
    raise ValueError(f"Unsupported unit: {unit}")


def _require_numeric(item: MenuItem, attribute: str) -> float:
    value = getattr(item, attribute)
    if value is None:
        raise ValueError(f"Menu item '{item.name}' is missing {attribute}")
    return float(value)


def get_item_profile(name: str) -> ItemNutrition:
    """Expose immutable nutrition profile for solver coefficients."""

    return _lookup_profile(name)
