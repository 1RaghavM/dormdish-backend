"""Pydantic models and JSON schema helpers for the meal planner."""

from __future__ import annotations

from enum import Enum
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from pydantic import BaseModel, Field, RootModel, ValidationError, field_validator, model_validator


class Unit(str, Enum):
    """Supported measurement units for menu items."""

    OZ = "oz"
    EACH = "each"


class MealSlot(str, Enum):
    """Enumeration of supported meal slots in the day."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"


class DietaryPreference(str, Enum):
    """Dietary preference filter applied to the menu."""

    NONE = "none"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"


class GoalType(str, Enum):
    """High level goal descriptor."""

    CUT = "cut"
    MAINTAIN = "maintain"
    BULK = "bulk"


class ServingSize(BaseModel):
    amount: float = Field(..., gt=0, description="Reference serving amount expressed in the declared unit")
    unit: Unit
    gram_weight: float | None = Field(
        default=None,
        gt=0,
        description="Optional canonical gram weight for one serving (required when unit == 'each')",
    )

    @field_validator("gram_weight", mode="before")
    @classmethod
    def _coerce_float(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return float(value)

    @model_validator(mode="after")
    def _ensure_each_has_weight(self) -> "ServingSize":
        if self.unit is Unit.EACH and self.gram_weight is None:
            raise ValueError("ServingSize.gram_weight is required for 'each' units")
        return self


class MenuItem(BaseModel):
    """Representation of a menu row supplied by the caller."""

    name: str
    serving_size: ServingSize
    calories: float | None = Field(None, ge=0)
    g_protein: float | None = Field(None, ge=0)
    diet_tags: List[str] = Field(default_factory=list)
    meal_eligibility: List[MealSlot] | None = None
    fdc_id: Optional[int] = Field(default=None, description="Optional USDA FoodData Central identifier")

    @field_validator("diet_tags", mode="after")
    @classmethod
    def _lowercase_tags(cls, tags: List[str]) -> List[str]:
        return [t.lower() for t in tags]


class Goals(BaseModel):
    goal_type: GoalType
    calories: int = Field(..., gt=0)
    protein_g: int = Field(..., gt=0)


class UserStats(BaseModel):
    dietary_preference: DietaryPreference


class Totals(BaseModel):
    calories: int
    protein_g: int
    net_weight_g: int


class MealItem(BaseModel):
    name: str
    unit: Unit
    qty: float = Field(..., ge=0)


class Meal(BaseModel):
    description: str = Field(default="", description="Narrative summary for personalization")
    items: List[MealItem]

    @field_validator("items", mode="after")
    @classmethod
    def _dedupe_names(cls, items: List[MealItem]) -> List[MealItem]:
        seen: set[str] = set()
        deduped: list[MealItem] = []
        for entry in items:
            if entry.name not in seen:
                deduped.append(entry)
                seen.add(entry.name)
        return deduped


class Meals(BaseModel):
    breakfast: Meal
    lunch: Meal
    dinner: Meal


class MealPlan(BaseModel):
    user_stats: UserStats
    goals: Goals
    totals: Totals
    meals: Meals
    explanation: str = Field(default="")


class MealRequest(BaseModel):
    menu: List[MenuItem]
    goals: Goals
    dietary_preference: DietaryPreference = DietaryPreference.NONE
    policy: Optional[dict] = Field(default=None, description="Optional policy overrides")


class TotalsRequest(BaseModel):
    menu: List[MenuItem]
    meal_plan: MealPlan
    policy: Optional[dict] = None


class ItemPortionConstraints(BaseModel):
    """Discrete options exposed to LLM structured decoding for a menu item."""

    unit: Unit
    quantities: List[float]

    @field_validator("quantities")
    @classmethod
    def _validate_qty(cls, values: List[float]) -> List[float]:
        if not values:
            raise ValueError("At least one quantity is required")
        if any(v <= 0 for v in values):
            raise ValueError("Quantities must be positive")
        sorted_vals = sorted(set(round(v, 6) for v in values))
        return sorted_vals


class StructuredSchema(RootModel[dict]):
    """Wrapper root model used when exposing the JSON schema via FastAPI."""


def build_structured_output_schema(
    item_constraints: Mapping[str, ItemPortionConstraints],
) -> dict:
    """Return a JSON schema for LM Studio structured output with hard enums.

    Parameters
    ----------
    item_constraints:
        Mapping of canonical menu item name to the discrete options available in
        the solver. Each entry drives the enum for the item name as well as the
        allowed set / unit for qty.
    """

    base_schema = MealPlan.model_json_schema()
    defs: MutableMapping[str, dict] = base_schema.setdefault("$defs", {})

    meal_item_schema = defs.get("MealItem")
    if meal_item_schema is None:
        raise RuntimeError("MealItem schema missing from generated JSON schema")

    name_enum = sorted(item_constraints.keys())
    meal_item_schema.setdefault("properties", {}).setdefault("name", {})["enum"] = name_enum
    meal_item_schema.setdefault("properties", {}).setdefault("unit", {})["enum"] = [
        Unit.OZ.value,
        Unit.EACH.value,
    ]

    conditional_rules: List[dict] = []
    for name, constraints in item_constraints.items():
        conditional_rules.append(
            {
                "if": {
                    "properties": {
                        "name": {"const": name}
                    }
                },
                "then": {
                    "properties": {
                        "qty": {
                            "enum": constraints.quantities,
                        },
                        "unit": {
                            "const": constraints.unit.value,
                        },
                    },
                    "required": ["qty", "unit"],
                },
            }
        )

    if conditional_rules:
        meal_item_schema.setdefault("allOf", []).extend(conditional_rules)

    return base_schema
