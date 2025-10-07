"""High-level orchestration layer for the deterministic meal planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, MutableMapping, Sequence, Tuple

from . import nutrition_engine
from .schemas import (
    GoalType,
    Meal,
    MealItem,
    MealPlan,
    MealRequest,
    MealSlot,
    Meals,
    Totals,
    Unit,
    UserStats,
)
from .solver import MealPlanSolver, ShareWindow, SolverRequest, SolverStatus, build_solver_items
from .taxonomy import Category, ItemPolicy, Taxonomy


@dataclass(frozen=True)
class PlannerConfig:
    share_windows: Mapping[MealSlot, ShareWindow]
    protein_floor_per_meal: int = 30
    repair_iterations: int = 3


@dataclass(frozen=True)
class ItemMetadata:
    unit: Unit
    step_qty: float
    max_qty: float
    per_meal_max: float
    calories_per_step: float
    protein_per_step: float
    category: Category
    allowed_meals: Tuple[MealSlot, ...]
    max_steps: int
    per_meal_steps: int
    is_main: bool
    is_sauce: bool


DEFAULT_SHARE_WINDOWS: Mapping[MealSlot, ShareWindow] = {
    MealSlot.BREAKFAST: ShareWindow(target=0.30, tolerance=0.08),
    MealSlot.LUNCH: ShareWindow(target=0.40, tolerance=0.08),
    MealSlot.DINNER: ShareWindow(target=0.30, tolerance=0.08),
}


class MealPlanner:
    """Coordinates taxonomy filtering, solver execution, and verification."""

    def __init__(self, config: PlannerConfig | None = None) -> None:
        if config is None:
            config = PlannerConfig(share_windows=DEFAULT_SHARE_WINDOWS)
        self.config = config

    def solve(self, request: MealRequest) -> MealPlan:
        policy_cfg = request.policy or {}
        taxonomy = Taxonomy.from_menu(
            request.menu,
            policy_overrides=policy_cfg.get("items"),
            alias_overrides=policy_cfg.get("aliases"),
        )

        canonical_items: Dict[str, MealItem] = {}
        policy_map: Dict[str, ItemPolicy] = {}
        for menu_item in request.menu:
            policy = taxonomy.resolve(menu_item.name)
            if not policy.allows_preference(request.dietary_preference):
                continue
            canonical_name = policy.name
            canonical_items[canonical_name] = menu_item.model_copy(update={"name": canonical_name})
            policy_map[canonical_name] = policy

        if not canonical_items:
            raise ValueError("No menu items remain after applying dietary preference")

        canonical_menu = list(canonical_items.values())
        nutrition_engine.configure_menu(canonical_menu)
        metadata = self._build_metadata(canonical_menu, policy_map)

        solver_metadata = {
            name: {
                "unit": meta.unit.value,
                "step_qty": meta.step_qty,
                "max_qty": meta.max_qty,
                "per_meal_max": meta.per_meal_max,
                "calories_per_step": meta.calories_per_step,
                "protein_per_step": meta.protein_per_step,
                "category": meta.category,
            }
            for name, meta in metadata.items()
        }
        meal_map = {name: meta.allowed_meals for name, meta in metadata.items()}
        mains = [name for name, meta in metadata.items() if meta.is_main]
        sauces = [name for name, meta in metadata.items() if meta.is_sauce]

        solver_items = build_solver_items(
            metadata=solver_metadata,
            meal_map=meal_map,
            mains=mains,
            sauces=sauces,
        )
        if not solver_items:
            raise ValueError("No solver variables were generated; check policy configuration")

        solver_request = SolverRequest(
            calorie_target=request.goals.calories,
            protein_target=request.goals.protein_g,
            items=solver_items,
            share_windows=self.config.share_windows,
            protein_floor_per_meal=self.config.protein_floor_per_meal,
        )
        solver = MealPlanSolver(solver_request)
        solution = solver.solve()
        if solution.status is SolverStatus.INFEASIBLE:
            raise ValueError("Unable to find feasible plan under current constraints")

        portions: Dict[MealSlot, Dict[str, int]] = {
            slot: dict(solution.portions.get(slot, {})) for slot in MealSlot
        }

        plan, totals = self._plan_from_portions(portions, metadata, request)
        if self._within_tolerance(totals, request.goals):
            return plan

        for _ in range(self.config.repair_iterations):
            if not self._adjust_portions(portions, metadata, totals, request.goals):
                break
            plan, totals = self._plan_from_portions(portions, metadata, request)
            if self._within_tolerance(totals, request.goals):
                return plan

        explanation = plan.explanation + " Targets slightly outside tolerance after repairs."
        return plan.model_copy(update={"totals": totals, "explanation": explanation})

    def _build_metadata(
        self,
        menu: Sequence[MealItem],
        policy_map: Mapping[str, ItemPolicy],
    ) -> Dict[str, ItemMetadata]:
        metadata: Dict[str, ItemMetadata] = {}
        for item in menu:
            policy = policy_map[item.name]
            profile = nutrition_engine.get_item_profile(item.name)
            grams_per_unit = profile.grams_per_unit[item.serving_size.unit]
            grams_per_step = grams_per_unit * policy.portion.step
            calories_per_step = profile.calories_per_gram * grams_per_step
            protein_per_step = profile.protein_per_gram * grams_per_step
            max_steps = max(1, int(policy.portion.max_quantity / policy.portion.step))
            per_meal_steps = max(1, int(policy.portion.per_meal_max / policy.portion.step))
            metadata[item.name] = ItemMetadata(
                unit=item.serving_size.unit,
                step_qty=policy.portion.step,
                max_qty=policy.portion.max_quantity,
                per_meal_max=policy.portion.per_meal_max,
                calories_per_step=calories_per_step,
                protein_per_step=protein_per_step,
                category=policy.category,
                allowed_meals=tuple(policy.meal_slots),
                max_steps=max_steps,
                per_meal_steps=per_meal_steps,
                is_main=policy.is_main,
                is_sauce=policy.is_sauce,
            )
        return metadata

    def _plan_from_portions(
        self,
        portions: Mapping[MealSlot, Mapping[str, int]],
        metadata: Mapping[str, ItemMetadata],
        request: MealRequest,
    ) -> Tuple[MealPlan, Totals]:
        meal_payload: Dict[str, Meal] = {}
        for slot in MealSlot:
            items: list[MealItem] = []
            slot_portions = portions.get(slot, {})
            for name, steps in slot_portions.items():
                if steps <= 0:
                    continue
                meta = metadata[name]
                qty = round(steps * meta.step_qty, 2)
                items.append(MealItem(name=name, unit=meta.unit, qty=qty))
            description = self._describe_meal(slot, items, metadata, request.goals.goal_type)
            meal_payload[slot.value] = Meal(description=description, items=items)

        explanation = self._build_explanation(request, meal_payload)
        provisional = MealPlan(
            user_stats=UserStats(dietary_preference=request.dietary_preference),
            goals=request.goals,
            totals=Totals(calories=0, protein_g=0, net_weight_g=0),
            meals=Meals(**meal_payload),
            explanation=explanation,
        )
        totals = nutrition_engine.sum_plan(provisional)
        return provisional.model_copy(update={"totals": totals}), totals

    def _within_tolerance(self, totals: Totals, goals) -> bool:
        cal_tol = goals.calories * 0.05
        prot_tol = goals.protein_g * 0.05
        return (
            abs(totals.calories - goals.calories) <= cal_tol
            and abs(totals.protein_g - goals.protein_g) <= prot_tol
        )

    def _adjust_portions(
        self,
        portions: MutableMapping[MealSlot, MutableMapping[str, int]],
        metadata: Mapping[str, ItemMetadata],
        totals: Totals,
        goals,
    ) -> bool:
        diff_cal = goals.calories - totals.calories
        diff_protein = goals.protein_g - totals.protein_g
        cal_tol = goals.calories * 0.05
        prot_tol = goals.protein_g * 0.05
        day_counts = self._day_counts(portions)

        if diff_cal > cal_tol or diff_protein > prot_tol:
            for category in (Category.PROTEIN_MAIN, Category.GRAIN, Category.LEGUME):
                candidate = self._pick_candidate(True, category, portions, metadata, day_counts)
                if candidate:
                    slot, name = candidate
                    portions[slot][name] = portions[slot].get(name, 0) + 1
                    return True
        elif diff_cal < -cal_tol:
            for category in (Category.SAUCE, Category.GRAIN, Category.LEGUME, Category.PROTEIN_MAIN):
                candidate = self._pick_candidate(False, category, portions, metadata, day_counts)
                if candidate:
                    slot, name = candidate
                    portions[slot][name] -= 1
                    if portions[slot][name] <= 0:
                        portions[slot].pop(name)
                    return True
        return False

    def _pick_candidate(
        self,
        increase: bool,
        category: Category,
        portions: Mapping[MealSlot, Mapping[str, int]],
        metadata: Mapping[str, ItemMetadata],
        day_counts: Mapping[str, int],
    ) -> Tuple[MealSlot, str] | None:
        meal_priority = (
            [MealSlot.LUNCH, MealSlot.DINNER, MealSlot.BREAKFAST]
            if increase
            else [MealSlot.DINNER, MealSlot.LUNCH, MealSlot.BREAKFAST]
        )
        best: Tuple[MealSlot, str] | None = None
        best_score = float("-inf") if increase else float("inf")
        for slot in meal_priority:
            slot_portions = portions.get(slot, {})
            for name, meta in metadata.items():
                if meta.category is not category:
                    continue
                if slot not in meta.allowed_meals:
                    continue
                current = slot_portions.get(name, 0)
                total = day_counts.get(name, 0)
                if increase:
                    if total >= meta.max_steps or current >= meta.per_meal_steps:
                        continue
                    score = meta.protein_per_step if category is Category.PROTEIN_MAIN else meta.calories_per_step
                    if score > best_score:
                        best = (slot, name)
                        best_score = score
                else:
                    if current <= 0:
                        continue
                    score = meta.calories_per_step
                    if score < best_score:
                        best = (slot, name)
                        best_score = score
        return best

    def _day_counts(self, portions: Mapping[MealSlot, Mapping[str, int]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for slot in MealSlot:
            for name, steps in portions.get(slot, {}).items():
                counts[name] = counts.get(name, 0) + steps
        return counts

    def _describe_meal(
        self,
        slot: MealSlot,
        items: Sequence[MealItem],
        metadata: Mapping[str, ItemMetadata],
        goal: GoalType,
    ) -> str:
        if not items:
            return "Macro-friendly reset meal to balance the day."
        categories = {
            metadata[item.name].category for item in items if item.name in metadata
        }
        category_text = ", ".join(sorted(cat.value.replace("_", " ") for cat in categories))
        if goal is GoalType.CUT:
            tone = "lean"
        elif goal is GoalType.BULK:
            tone = "protein-forward"
        else:
            tone = "balanced"
        return f"{slot.value.capitalize()} delivers a {tone} mix of {category_text}."

    def _build_explanation(self, request: MealRequest, meals: Mapping[str, Meal]) -> str:
        share_summary = ", ".join(
            f"{slot}: {len(meals[slot].items)} items" for slot in ["breakfast", "lunch", "dinner"]
        )
        return (
            f"Goal {request.goals.goal_type.value} targeting {request.goals.calories} kcal/"
            f"{request.goals.protein_g} g protein ({share_summary})."
        )
