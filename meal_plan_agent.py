from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from openai import AsyncOpenAI
from openai.types.shared.reasoning import Reasoning

from agents import Agent, ModelSettings, TResponseInputItem, WebSearchTool

try:  # pragma: no cover - optional dependency
    from guardrails.runtime import (  # type: ignore
        instantiate_guardrails,
        load_config_bundle,
        run_guardrails,
    )
except Exception:  # pragma: no cover - fallback for local/test environments

    async def run_guardrails(  # type: ignore
        _ctx: Any,
        _input_text: str,
        _content_type: str,
        _guardrails: Any,
        suppress_tripwire: bool = True,
    ) -> List[Any]:
        return []

    def load_config_bundle(config: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore
        return config

    def instantiate_guardrails(config: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore
        return config


class MyAgentSchemaUserStats(BaseModel):
    bmr: float
    tdee: float
    goal: str
    dietary_preference: Optional[str]


class MyAgentSchemaGoals(BaseModel):
    calories: float
    protein_g: float


class MyAgentSchemaTotals(BaseModel):
    calories: float
    protein_g: float


class MyAgentSchemaMeal(BaseModel):
    description: str
    items: List[Dict[str, Any]]


class MyAgentSchemaMeals(BaseModel):
    breakfast: MyAgentSchemaMeal
    lunch: MyAgentSchemaMeal
    dinner: MyAgentSchemaMeal


class MyAgentSchema(BaseModel):
    user_stats: MyAgentSchemaUserStats
    goals: MyAgentSchemaGoals
    totals: MyAgentSchemaTotals
    meals: MyAgentSchemaMeals
    explanation: str


web_search_preview = WebSearchTool(
    filters={
        "allowed_domains": [
            "nutritionsource.hsph.harvard.edu",
            "www.healthline.com",
            "pmc.ncbi.nlm.nih.gov",
        ]
    },
    search_context_size="medium",
    user_location={"type": "approximate"},
)

# Guardrails configuration (kept empty but available for future rules)
guardrails_config: Dict[str, Any] = {
    "guardrails": []
}

AGENT_API_KEY = os.getenv("OPENAI_AGENT_API_KEY", os.getenv("OPENAI_API_KEY"))
AGENT_BASE_URL = os.getenv("OPENAI_AGENT_BASE_URL", "https://api.openai.com/v1")

client = AsyncOpenAI(
    api_key=AGENT_API_KEY,
    base_url=AGENT_BASE_URL,
)
ctx = SimpleNamespace(guardrail_llm=client)


def guardrails_has_tripwire(results: Optional[List[Any]]) -> bool:
    return any(getattr(r, "tripwire_triggered", False) for r in results or [])


def get_guardrail_checked_text(results: Optional[List[Any]], fallback_text: str) -> str:
    for result in results or []:
        info = getattr(result, "info", None) or {}
        if isinstance(info, dict) and "checked_text" in info:
            checked = info.get("checked_text")
            if isinstance(checked, str) and checked:
                return checked
    return fallback_text


def build_guardrail_fail_output(results: Optional[List[Any]]) -> Dict[str, Any]:
    failures: List[Dict[str, Any]] = []
    for result in results or []:
        if getattr(result, "tripwire_triggered", False):
            info = getattr(result, "info", None) or {}
            failure: Dict[str, Any] = {
                "guardrail_name": info.get("guardrail_name"),
            }
            for key in (
                "flagged",
                "confidence",
                "threshold",
                "hallucination_type",
                "hallucinated_statements",
                "verified_statements",
            ):
                if key in info:
                    failure[key] = info.get(key)
            failures.append(failure)
    return {"failed": bool(failures), "failures": failures}


my_agent = Agent(
    name="DormDish Meal Planner",
    instructions=(
        "You are a pragmatic, encouraging university dining hall nutritionist & meal designer.\n"
        "Create ONE daily meal plan (breakfast, lunch, dinner) using ONLY the allowed items. "
        "Think in terms of a plate: a main + sides. If an item requires a companion (pasta → sauce, "
        "sandwich → bread + protein, salad → dressing), include it. Avoid absurd pairings.\n\n"
        "STRICT OUTPUT: Return a single JSON object with these exact top-level keys (no extras, no markdown):\n"
        "{\n"
        "  \"user_stats\": { \"bmr\": <float>, \"tdee\": <float>, \"goal\": <cut|maintain|bulk>, \"dietary_preference\": <vegan|vegetarian|null> },\n"
        "  \"goals\": { \"calories\": <int>, \"protein_g\": <int> },\n"
        "  \"totals\": { \"calories\": <int>, \"protein_g\": <int> },\n"
        "  \"meals\": {\n"
        "    \"breakfast\": { \"description\": <string>, \"items\": [ <meal_item>, ... ] },\n"
        "    \"lunch\": { \"description\": <string>, \"items\": [ <meal_item>, ... ] },\n"
        "    \"dinner\": { \"description\": <string>, \"items\": [ <meal_item>, ... ] }\n"
        "  },\n"
        "  \"explanation\": <string>\n"
        "}\n\n"
        "Meal item schema:\n"
        "{ \"name\": <exact item>, \"servings\": <number>, \"calories\": <number>, \"protein_g\": <number>, \"serving_size\": <string|null> }\n\n"
        "Rules\n"
        "- 3 meals only. Each: short description + 2–4 items (main protein + carb/veg/sauce). A single larger item is OK if realistic.\n"
        "- Variety: avoid repeating exact items more than once/day.\n"
        "- Don’t stack multiple meats in one meal. Plant proteins may complement sensibly.\n"
        "- If pairing item missing from list, proceed without fabrication (note in explanation).\n"
        "- Respect dietary prefs (vegan/vegetarian).\n"
        "- Serving sizes can be fractional (0.5 increments).\n\n"
        "Nutrition Targets\n"
        "- Calories & protein must be within ±5% of goals.\n"
        "- Prioritize protein target first, then adjust calories.\n\n"
        "Meals must be realistic, balanced, appetizing.\n"
        "You are allowed to modify the serving sizes of the items. "
        "Use web search to see how to properly pair items and ensure nutritional balance.\n\n"
        "Explanation field: 1–3 sentences on overall balance and any unavoidable shortfall/excess."
    ),
    model="gpt-5-nano",
    tools=[web_search_preview],
    output_type=MyAgentSchema,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(effort="medium", summary="auto"),
    ),
    client=client,
)


def _format_conversation(input_text: str) -> List[TResponseInputItem]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": input_text,
                }
            ],
        }
    ]


async def run_workflow(
    workflow_input: "WorkflowInput",
) -> Tuple[MyAgentSchema, str]:
    guardrail_bundle = load_config_bundle(guardrails_config)
    guardrail_instance = instantiate_guardrails(guardrail_bundle)

    guardrails_inputtext = workflow_input.input_as_text
    guardrails_result = await run_guardrails(
        ctx,
        guardrails_inputtext,
        "text/plain",
        guardrail_instance,
        suppress_tripwire=True,
    )

    guardrails_hastripwire = guardrails_has_tripwire(guardrails_result)
    guardrails_text = get_guardrail_checked_text(guardrails_result, guardrails_inputtext)

    if guardrails_hastripwire:
        payload = build_guardrail_fail_output(guardrails_result)
        raise ValueError(f"Guardrail tripwire triggered: {json.dumps(payload)}")

    conversation_history = _format_conversation(guardrails_text)
    response_model, raw_text = await my_agent.arun(
        conversation_history=conversation_history,
    )
    return response_model, raw_text


class WorkflowInput(BaseModel):
    input_as_text: str


async def generate_meal_plan_with_agent(
    *,
    user_payload: Dict[str, Any],
    menu_payload: Dict[str, Any],
    nutrition_targets: Dict[str, Any],
) -> Tuple[MyAgentSchema, str]:
    serialized_input = json.dumps(
        {
            "user": user_payload,
            "menu": menu_payload,
            "nutrition_targets": nutrition_targets,
        },
        ensure_ascii=False,
    )
    workflow_input = WorkflowInput(input_as_text=serialized_input)
    return await run_workflow(workflow_input)
