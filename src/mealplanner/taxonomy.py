"""Domain taxonomy and policy configuration for menu items."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set

from .schemas import DietaryPreference, ItemPortionConstraints, MealSlot, MenuItem, Unit


class Category(str, Enum):
    PROTEIN_MAIN = "protein_main"
    GRAIN = "grain"
    LEGUME = "legume"
    VEG = "veg"
    SAUCE = "sauce"


@dataclass(frozen=True)
class PortionRule:
    unit: Unit
    step: float
    max_quantity: float
    per_meal_max: float

    def discrete_steps(self) -> Sequence[float]:
        steps = max(1, int(round(self.per_meal_max / self.step)))
        return [round(self.step * i, 3) for i in range(1, steps + 1)]


@dataclass(frozen=True)
class ItemPolicy:
    name: str
    category: Category
    portion: PortionRule
    meal_slots: Set[MealSlot]
    diet_tags: Set[str]
    is_main: bool = False
    is_sauce: bool = False

    def allows_preference(self, preference: DietaryPreference) -> bool:
        if preference is DietaryPreference.NONE:
            return True
        tags = {t.lower() for t in self.diet_tags}
        if preference is DietaryPreference.VEGAN:
            return "vegan" in tags
        if preference is DietaryPreference.VEGETARIAN:
            return "vegetarian" in tags or "vegan" in tags
        if preference is DietaryPreference.PESCETARIAN:
            # Pescetarian allows fish and plant-based options.
            # Accept items explicitly tagged pescetarian, and also vegetarian/vegan items.
            return "pescetarian" in tags or "vegetarian" in tags or "vegan" in tags
        return True

    def allows_meal(self, slot: MealSlot) -> bool:
        return slot in self.meal_slots


_DEFAULT_ALIAS_MAP: Dict[str, str] = {
    "grilled chicken": "FRESH CHICKEN STRIPS",
    "chicken breast": "FRESH CHICKEN STRIPS",
    "brown rice": "WHOLE GRAIN RICE",
    "steamed broccoli": "STEAMED BROCCOLI",
    "black beans": "SEASONED BLACK BEANS",
    "avocado salsa": "AVOCADO SALSA",
}


_DEFAULT_POLICIES: Dict[str, ItemPolicy] = {}


def _register_default(policy: ItemPolicy) -> None:
    _DEFAULT_POLICIES[policy.name] = policy


# Seed default policies for the fixture dataset.
_register_default(
    ItemPolicy(
        name="FRESH CHICKEN STRIPS",
        category=Category.PROTEIN_MAIN,
        portion=PortionRule(unit=Unit.OZ, step=2.0, max_quantity=12.0, per_meal_max=8.0),
        meal_slots={MealSlot.LUNCH, MealSlot.DINNER},
        diet_tags={"none"},
        is_main=True,
    )
)
_register_default(
    ItemPolicy(
    name="WHOLE GRAIN RICE",
        category=Category.GRAIN,
        portion=PortionRule(unit=Unit.OZ, step=2.0, max_quantity=16.0, per_meal_max=10.0),
        meal_slots={MealSlot.LUNCH, MealSlot.DINNER},
        diet_tags={"vegan", "vegetarian", "none"},
    )
)
_register_default(
    ItemPolicy(
        name="STEAMED BROCCOLI",
        category=Category.VEG,
        portion=PortionRule(unit=Unit.OZ, step=2.0, max_quantity=12.0, per_meal_max=8.0),
        meal_slots={MealSlot.LUNCH, MealSlot.DINNER},
        diet_tags={"vegan", "vegetarian", "none"},
    )
)
_register_default(
    ItemPolicy(
        name="SEASONED BLACK BEANS",
        category=Category.LEGUME,
        portion=PortionRule(unit=Unit.OZ, step=2.0, max_quantity=12.0, per_meal_max=8.0),
        meal_slots={MealSlot.LUNCH, MealSlot.DINNER},
        diet_tags={"vegan", "vegetarian", "none"},
        is_main=True,
    )
)
_register_default(
    ItemPolicy(
    name="OVERNIGHT OATS",
    category=Category.GRAIN,
    portion=PortionRule(unit=Unit.EACH, step=1.0, max_quantity=2.0, per_meal_max=1.0),
        meal_slots={MealSlot.BREAKFAST},
        diet_tags={"vegan", "vegetarian", "none"},
    )
)
_register_default(
    ItemPolicy(
        name="SCRAMBLED EGG WHITES",
        category=Category.PROTEIN_MAIN,
        portion=PortionRule(unit=Unit.OZ, step=2.0, max_quantity=10.0, per_meal_max=6.0),
        meal_slots={MealSlot.BREAKFAST},
        diet_tags={"vegetarian", "none"},
        is_main=True,
    )
)
_register_default(
    ItemPolicy(
        name="AVOCADO SALSA",
        category=Category.SAUCE,
        portion=PortionRule(unit=Unit.OZ, step=1.0, max_quantity=4.0, per_meal_max=2.0),
        meal_slots={MealSlot.BREAKFAST, MealSlot.LUNCH, MealSlot.DINNER},
        diet_tags={"vegan", "vegetarian", "none"},
        is_sauce=True,
    )
)


class Taxonomy:
    """Immutable view of item metadata and constraints."""

    def __init__(self, items: Mapping[str, ItemPolicy], alias_map: Mapping[str, str]):
        self._items = dict(items)
        self._alias_map = {k.lower(): v for k, v in alias_map.items()}

    @classmethod
    def from_menu(
        cls,
        menu: Sequence[MenuItem],
        policy_overrides: Optional[Mapping[str, Mapping[str, object]]] = None,
        alias_overrides: Optional[Mapping[str, str]] = None,
    ) -> "Taxonomy":
        items: Dict[str, ItemPolicy] = dict(_DEFAULT_POLICIES)
        alias_map: Dict[str, str] = {k.lower(): v for k, v in _DEFAULT_ALIAS_MAP.items()}

        if alias_overrides:
            alias_map.update({k.lower(): v.strip().upper() for k, v in alias_overrides.items()})

        if policy_overrides:
            for key, override in policy_overrides.items():
                canonical = cls.canonical_name(key, alias_map)
                base = items.get(canonical)
                portion = override.get("portion") if override else None
                if portion:
                    portion_rule = PortionRule(
                        unit=Unit(portion.get("unit", base.portion.unit if base else Unit.OZ)),
                        step=float(portion.get("step", base.portion.step if base else 1.0)),
                        max_quantity=float(
                            portion.get("max_quantity", base.portion.max_quantity if base else 4.0)
                        ),
                        per_meal_max=float(
                            portion.get("per_meal_max", base.portion.per_meal_max if base else 2.0)
                        ),
                    )
                elif base:
                    portion_rule = base.portion
                else:
                    portion_rule = PortionRule(unit=Unit.OZ, step=1.0, max_quantity=4.0, per_meal_max=2.0)

                base_slots: Sequence[MealSlot | str]
                if base:
                    base_slots = [slot.value if isinstance(slot, MealSlot) else slot for slot in base.meal_slots]
                else:
                    base_slots = [MealSlot.BREAKFAST.value]

                slot_values = override.get("meal_slots", base_slots)
                meal_slots = {MealSlot(slot) if not isinstance(slot, MealSlot) else slot for slot in slot_values}
                if not meal_slots:
                    meal_slots = {MealSlot.BREAKFAST}

                base_tags = base.diet_tags if base else {"none"}
                override_tags = override.get("diet_tags", base_tags)
                diet_tags = {str(tag).lower() for tag in override_tags}

                base_category = base.category if base else Category.VEG
                category = Category(override.get("category", base_category))
                is_main = bool(override.get("is_main", base.is_main if base else False))
                is_sauce = bool(override.get("is_sauce", base.is_sauce if base else False))

                items[canonical] = ItemPolicy(
                    name=canonical,
                    category=category,
                    portion=portion_rule,
                    meal_slots=meal_slots,
                    diet_tags=diet_tags,
                    is_main=is_main,
                    is_sauce=is_sauce,
                )

        # Ensure every menu item has at least a default policy.
        for item in menu:
            canonical = cls.canonical_name(item.name, alias_map)
            if canonical not in items:
                portion_rule = PortionRule(
                    unit=item.serving_size.unit,
                    step=item.serving_size.amount,
                    max_quantity=item.serving_size.amount * 4,
                    per_meal_max=item.serving_size.amount * 2,
                )
                diet_tags = {tag.lower() for tag in item.diet_tags} or {"none"}
                meal_slots = (
                    {slot if isinstance(slot, MealSlot) else MealSlot(slot) for slot in item.meal_eligibility}
                    if item.meal_eligibility
                    else {
                        MealSlot.BREAKFAST,
                        MealSlot.LUNCH,
                        MealSlot.DINNER,
                    }
                )
                items[canonical] = ItemPolicy(
                    name=canonical,
                    category=Category.VEG,
                    portion=portion_rule,
                    meal_slots=meal_slots,
                    diet_tags=diet_tags,
                    is_main=False,
                    is_sauce=False,
                )
        return cls(items=items, alias_map=alias_map)

    @staticmethod
    def canonical_name(name: str, alias_map: Optional[Mapping[str, str]] = None) -> str:
        key = name.strip().lower()
        if alias_map and key in alias_map:
            return alias_map[key]
        return name.strip().upper()

    def resolve(self, name: str) -> ItemPolicy:
        canonical = self.canonical_name(name, self._alias_map)
        if canonical not in self._items:
            raise KeyError(f"Unknown menu item: {name}")
        return self._items[canonical]

    def items(self) -> Iterable[ItemPolicy]:
        return self._items.values()

    def allowed_items(
        self, preference: DietaryPreference, meal_slot: MealSlot | None = None
    ) -> Dict[str, ItemPolicy]:
        result: Dict[str, ItemPolicy] = {}
        for name, policy in self._items.items():
            if not policy.allows_preference(preference):
                continue
            if meal_slot and not policy.allows_meal(meal_slot):
                continue
            result[name] = policy
        return result

    def structured_constraints(self) -> Dict[str, ItemPortionConstraints]:
        constraints: Dict[str, ItemPortionConstraints] = {}
        for policy in self._items.values():
            constraints[policy.name] = ItemPortionConstraints(
                unit=policy.portion.unit,
                quantities=list(policy.portion.discrete_steps()),
            )
        return constraints
