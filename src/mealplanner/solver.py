"""CP-SAT powered solver for deterministic meal planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, MutableMapping, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

from .nutrition_engine import get_item_profile
from .schemas import MealSlot, Unit
from .taxonomy import Category

MACRO_SCALE = 100  # centi-units for kcal / grams
SHARE_SCALE = 1000  # thousandths for meal share ratios


class SolverStatus(str, Enum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"


@dataclass(frozen=True)
class ShareWindow:
    target: float
    tolerance: float

    def bounds(self, total: int) -> Tuple[int, int]:
        lower = max(0.0, self.target - self.tolerance)
        upper = min(1.0, self.target + self.tolerance)
        return (
            math.floor(total * lower),
            math.ceil(total * upper),
        )

    def relax(self, delta: float) -> "ShareWindow":
        return ShareWindow(target=self.target, tolerance=min(0.25, self.tolerance + delta))


@dataclass(frozen=True)
class SolverItem:
    name: str
    unit: Unit
    step_qty: float
    max_steps: int
    per_meal_max_steps: int
    calories_per_step: int
    protein_per_step: int
    category: Category
    allowed_meals: Sequence[MealSlot]
    is_main: bool
    is_sauce: bool


@dataclass
class SolverRequest:
    calorie_target: int
    protein_target: int
    items: Sequence[SolverItem]
    share_windows: Mapping[MealSlot, ShareWindow]
    protein_floor_per_meal: int = 25


@dataclass
class SolverSolution:
    status: SolverStatus
    portions: Mapping[MealSlot, Mapping[str, int]]
    calories: float
    protein: float
    objective: Optional[int] = None
    relaxation_applied: float = 0.0


class MealPlanSolver:
    """Wrapper around OR-Tools CP-SAT model construction and execution."""

    def __init__(self, request: SolverRequest):
        self.request = request

    def solve(self, attempts: int = 3, relax_step: float = 0.03) -> SolverSolution:
        relaxation = 0.0
        share_windows = dict(self.request.share_windows)
        for attempt in range(attempts):
            result = self._solve_once(share_windows, relaxation)
            if result.status is not SolverStatus.INFEASIBLE:
                return result
            relaxation += relax_step
            share_windows = {slot: window.relax(relax_step) for slot, window in share_windows.items()}
        return SolverSolution(
            status=SolverStatus.INFEASIBLE,
            portions={},
            calories=0.0,
            protein=0.0,
            objective=None,
            relaxation_applied=relaxation,
        )

    def _solve_once(
        self, share_windows: Mapping[MealSlot, ShareWindow], relaxation: float
    ) -> SolverSolution:
        model = cp_model.CpModel()
        request = self.request
        items = request.items

        # Decision variables per meal/item
        x_vars: Dict[Tuple[MealSlot, str], cp_model.IntVar] = {}
        meal_calories_expr: Dict[MealSlot, list] = {slot: [] for slot in MealSlot}
        meal_protein_expr: Dict[MealSlot, list] = {slot: [] for slot in MealSlot}

        item_total_counts: Dict[str, cp_model.IntVar] = {}
        item_used: Dict[str, cp_model.BoolVar] = {}

        total_calories_expr = []
        total_protein_expr = []

        item_lookup = {item.name: item for item in items}

        for item in items:
            profile = get_item_profile(item.name)
            if item.unit not in profile.grams_per_unit:
                raise ValueError(f"Nutrition profile missing unit {item.unit} for {item.name}")

            total_var = model.NewIntVar(0, item.max_steps, f"total_{item.name}")
            item_total_counts[item.name] = total_var
            used_var = model.NewBoolVar(f"used_{item.name}")
            item_used[item.name] = used_var

            per_meal_vars = []
            for slot in item.allowed_meals:
                var = model.NewIntVar(0, item.per_meal_max_steps, f"x_{slot.value}_{item.name}")
                x_vars[(slot, item.name)] = var
                meal_calories_expr[slot].append(var * item.calories_per_step)
                meal_protein_expr[slot].append(var * item.protein_per_step)
                per_meal_vars.append(var)

            if per_meal_vars:
                model.Add(total_var == sum(per_meal_vars))
            else:
                model.Add(total_var == 0)

            model.Add(total_var <= item.max_steps)
            model.Add(total_var >= 1).OnlyEnforceIf(used_var)
            model.Add(total_var == 0).OnlyEnforceIf(used_var.Not())

            total_calories_expr.append(total_var * item.calories_per_step)
            total_protein_expr.append(total_var * item.protein_per_step)

        # Aggregate totals
        max_total_calories = sum(item.calories_per_step * item.max_steps for item in items)
        max_total_protein = sum(item.protein_per_step * item.max_steps for item in items)

        total_calories = model.NewIntVar(0, max_total_calories, "total_calories")
        total_protein = model.NewIntVar(0, max_total_protein, "total_protein")
        model.Add(total_calories == sum(total_calories_expr))
        model.Add(total_protein == sum(total_protein_expr))

        # Target windows
        cal_target_scaled = request.calorie_target * MACRO_SCALE
        protein_target_scaled = request.protein_target * MACRO_SCALE

        tolerance = 0.05 + relaxation
        cal_lower = math.floor(cal_target_scaled * (1 - tolerance))
        cal_upper = math.ceil(cal_target_scaled * (1 + tolerance))
        prot_lower = math.floor(protein_target_scaled * (1 - tolerance))
        prot_upper = math.ceil(protein_target_scaled * (1 + tolerance))

        model.Add(total_calories >= cal_lower)
        model.Add(total_calories <= cal_upper)
        model.Add(total_protein >= prot_lower)
        model.Add(total_protein <= prot_upper)

        # Meal share windows and protein floors
        protein_floor_scaled = request.protein_floor_per_meal * MACRO_SCALE
        for slot in MealSlot:
            meal_cal = model.NewIntVar(0, max_total_calories, f"meal_cal_{slot.value}")
            meal_pro = model.NewIntVar(0, max_total_protein, f"meal_pro_{slot.value}")

            if meal_calories_expr[slot]:
                model.Add(meal_cal == sum(meal_calories_expr[slot]))
            else:
                model.Add(meal_cal == 0)

            if meal_protein_expr[slot]:
                model.Add(meal_pro == sum(meal_protein_expr[slot]))
                model.Add(meal_pro >= protein_floor_scaled)
            else:
                model.Add(meal_pro == 0)

            share_window = share_windows.get(slot)
            if share_window and meal_calories_expr[slot]:
                low_ratio = max(0.0, share_window.target - share_window.tolerance)
                high_ratio = min(1.0, share_window.target + share_window.tolerance)
                lower_coeff = int(math.floor(low_ratio * SHARE_SCALE))
                upper_coeff = int(math.ceil(high_ratio * SHARE_SCALE))
                model.Add(meal_cal * SHARE_SCALE >= lower_coeff * total_calories)
                model.Add(meal_cal * SHARE_SCALE <= upper_coeff * total_calories)

            main_vars = [x_vars[(slot, item.name)] for item in items if item.is_main and (slot, item.name) in x_vars]
            if main_vars:
                model.Add(sum(main_vars) >= 1)

        # Limit sauce usage
        sauce_vars = [var for (slot, name), var in x_vars.items() if item_lookup[name].is_sauce]
        if sauce_vars:
            max_sauce = sum(item.per_meal_max_steps for item in items if item.is_sauce)
            model.Add(sum(sauce_vars) <= max(1, max_sauce))

        # Objective
        cal_deviation = model.NewIntVar(-max_total_calories, max_total_calories, "cal_dev")
        prot_deviation = model.NewIntVar(-max_total_protein, max_total_protein, "prot_dev")
        model.Add(cal_deviation == total_calories - cal_target_scaled)
        model.Add(prot_deviation == total_protein - protein_target_scaled)

        abs_cal = model.NewIntVar(0, max_total_calories, "abs_cal")
        abs_prot = model.NewIntVar(0, max_total_protein, "abs_prot")
        model.AddAbsEquality(abs_cal, cal_deviation)
        model.AddAbsEquality(abs_prot, prot_deviation)

        sauce_usage = sum(sauce_vars) if sauce_vars else 0
        distinct_items = sum(item_used.values()) if item_used else 0
        main_protein_terms = []
        for (slot, name), var in x_vars.items():
            item = item_lookup[name]
            if item.is_main:
                main_protein_terms.append(item.protein_per_step * var)
        main_protein = sum(main_protein_terms) if main_protein_terms else 0

        objective = (
            abs_cal * 1000
            + abs_prot * 600
            + sauce_usage * 15
            + distinct_items * 10
            - main_protein * 2
        )
        model.Minimize(objective)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 8.0
        solver.parameters.num_search_workers = 8
        status = solver.Solve(model)

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            portions: Dict[MealSlot, Dict[str, int]] = {slot: {} for slot in MealSlot}
            for (slot, name), var in x_vars.items():
                value = solver.Value(var)
                if value > 0:
                    portions[slot][name] = value
            calories = solver.Value(total_calories) / MACRO_SCALE
            protein = solver.Value(total_protein) / MACRO_SCALE
            solver_status = SolverStatus.OPTIMAL if status == cp_model.OPTIMAL else SolverStatus.FEASIBLE
            objective_value = int(solver.Value(objective))
            return SolverSolution(
                status=solver_status,
                portions=portions,
                calories=calories,
                protein=protein,
                objective=objective_value,
                relaxation_applied=relaxation,
            )

        return SolverSolution(
            status=SolverStatus.INFEASIBLE,
            portions={},
            calories=0.0,
            protein=0.0,
            objective=None,
            relaxation_applied=relaxation,
        )


def build_solver_items(
    *,
    metadata: Mapping[str, Mapping[str, object]],
    meal_map: Mapping[str, Sequence[MealSlot]],
    mains: Iterable[str] = (),
    sauces: Iterable[str] = (),
) -> Sequence[SolverItem]:
    """Factory helper converting planner metadata into solver items."""

    items: list[SolverItem] = []
    main_set = {m.upper() for m in mains}
    sauce_set = {s.upper() for s in sauces}

    for name, data in metadata.items():
        unit = Unit(str(data["unit"]))
        step_qty = float(data["step_qty"])
        max_qty = float(data["max_qty"])
        per_meal_max = float(data["per_meal_max"])
        calories_per_step = int(round(float(data["calories_per_step"]) * MACRO_SCALE))
        protein_per_step = int(round(float(data["protein_per_step"]) * MACRO_SCALE))
        max_steps = max(0, math.floor(max_qty / step_qty + 1e-6))
        per_meal_steps = max(0, math.floor(per_meal_max / step_qty + 1e-6))
        if max_steps == 0 or per_meal_steps == 0:
            continue
        allowed = meal_map.get(name, tuple(MealSlot))
        allowed_meals = tuple(MealSlot(slot) if not isinstance(slot, MealSlot) else slot for slot in allowed)
        category_value = data.get("category", Category.VEG)
        category = category_value if isinstance(category_value, Category) else Category(str(category_value))
        canonical_name = name.upper()
        item = SolverItem(
            name=canonical_name,
            unit=unit,
            step_qty=step_qty,
            max_steps=max_steps,
            per_meal_max_steps=per_meal_steps,
            calories_per_step=calories_per_step,
            protein_per_step=protein_per_step,
            category=category,
            allowed_meals=allowed_meals,
            is_main=canonical_name in main_set,
            is_sauce=canonical_name in sauce_set,
        )
        items.append(item)
    return items
