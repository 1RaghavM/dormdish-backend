"""Helpers for wiring the meal planner into LM Studio structured outputs."""

from __future__ import annotations

from typing import Sequence

from .planner import MealPlanner
from .schemas import MealPlan, MealRequest, MenuItem, StructuredSchema, build_structured_output_schema
from .taxonomy import Taxonomy


def get_response_format_schema(menu: Sequence[MenuItem], policy: dict | None = None) -> StructuredSchema:
    """Construct a schema compatible with LM Studio's response_format parameter."""

    taxonomy = Taxonomy.from_menu(
        menu,
        policy_overrides=(policy or {}).get("items") if policy else None,
        alias_overrides=(policy or {}).get("aliases") if policy else None,
    )
    constraints = taxonomy.structured_constraints()
    schema_dict = build_structured_output_schema(constraints)
    return StructuredSchema.model_validate(schema_dict)


def build_tool_spec() -> dict:
    """Return a tool spec for LM Studio function calling."""

    return {
        "type": "function",
        "function": {
            "name": "solve_meal_plan",
            "description": "Deterministic meal plan solver that enforces calorie/protein targets and dietary preferences.",
            "parameters": MealRequest.model_json_schema(),
        },
    }


def invoke_tool(planner: MealPlanner, payload: dict) -> MealPlan:
    """Utility for executing the solver tool inside LM Studio callbacks."""

    request = MealRequest.model_validate(payload)
    return planner.solve(request)


EXAMPLE_USAGE = """
from mealplanner.lmstudio_tools import build_tool_spec, get_response_format_schema, invoke_tool
from mealplanner.planner import MealPlanner

planner = MealPlanner()
tool_spec = build_tool_spec()
response_schema = get_response_format_schema(menu)

# Inside LM Studio callback for tool invocation:
result = invoke_tool(planner, tool_call_payload)
"""
