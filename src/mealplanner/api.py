"""FastAPI service exposing the deterministic meal planner."""

from __future__ import annotations

from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import nutrition_engine
from .lmstudio_tools import get_response_format_schema
from .planner import MealPlanner
from .schemas import MealPlan, MealRequest, MenuItem, Totals, TotalsRequest
from .taxonomy import Taxonomy

app = FastAPI(title="Deterministic Meal Planner", version="0.1.0")
planner = MealPlanner()


@app.post("/solve", response_model=MealPlan)
def solve_endpoint(request: MealRequest) -> MealPlan:
    """Solve a meal plan for the provided menu and goals."""

    try:
        return planner.solve(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/sum", response_model=Totals)
def sum_endpoint(request: TotalsRequest) -> Totals:
    """Re-compute totals for a previously generated plan."""

    taxonomy = Taxonomy.from_menu(
        request.menu,
        policy_overrides=(request.policy or {}).get("items") if request.policy else None,
        alias_overrides=(request.policy or {}).get("aliases") if request.policy else None,
    )

    canonical_items: Dict[str, MenuItem] = {}
    for item in request.menu:
        policy = taxonomy.resolve(item.name)
        canonical_items[policy.name] = item.model_copy(update={"name": policy.name})

    nutrition_engine.configure_menu(list(canonical_items.values()))
    return nutrition_engine.sum_plan(request.meal_plan)


@app.get("/schema")
def schema_endpoint() -> JSONResponse:
    """Return a base JSON schema for LM Studio structured output."""

    schema = MealPlan.model_json_schema()
    return JSONResponse(schema)


@app.post("/schema", response_model=dict)
def schema_from_menu(request: MealRequest) -> dict:
    """Return a strict schema for the provided menu."""

    response_schema = get_response_format_schema(request.menu, policy=request.policy)
    return response_schema.root
