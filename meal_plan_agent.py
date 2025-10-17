import asyncio
import json
import os
import hashlib
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agents import WebSearchTool, Agent, ModelSettings, TResponseInputItem
from openai import AsyncOpenAI
from types import SimpleNamespace
from pydantic import BaseModel


try:  
  from openai.types.shared.reasoning import Reasoning  
except ImportError:  
  Reasoning = None  

web_search_preview = WebSearchTool(
  filters={
    "allowed_domains": [
      "nutritionsource.hsph.harvard.edu",
    ]
  },
  search_context_size="low",
  user_location={
    "type": "approximate"
  }
)

client = AsyncOpenAI()


_MEAL_PLAN_CACHE_LIMIT = int(os.getenv("MEALPLAN_CACHE_SIZE", "16"))
_MEAL_PLAN_CACHE: "OrderedDict[str, Tuple[DormdishMealPlannerSchema, str]]" = OrderedDict()
_MEAL_PLAN_CACHE_LOCK = asyncio.Lock()


def _build_mealplan_cache_key(
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  extra_system_messages: Optional[Sequence[str]]
) -> str:
  trimmed_menu_payload: Dict[str, Any] = {
    "requested_dining_hall": menu_payload.get("requested_dining_hall"),
    "requested_date": menu_payload.get("requested_date"),
    "hall": menu_payload.get("hall"),
    "allowed_items": menu_payload.get("allowed_items"),
  }
  source_menu = menu_payload.get("source_menu")
  if isinstance(source_menu, dict):
    trimmed_menu_payload["source_menu"] = {
      "hall_slug": source_menu.get("hall_slug"),
      "fetched_at": source_menu.get("fetched_at"),
      "meal_sources": (source_menu.get("menu", {}) or {}).get("meal_sources"),
    }

  serializable_payload = {
    "user_payload": user_payload,
    "menu_payload": trimmed_menu_payload,
    "nutrition_targets": nutrition_targets,
    "extra_system_messages": list(extra_system_messages) if extra_system_messages else None,
  }
  serialized = json.dumps(serializable_payload, sort_keys=True, ensure_ascii=False)
  return hashlib.sha256(serialized.encode("utf-8")).hexdigest()






# class DormdishMealPlannerSchema__Goals(BaseModel):
#     calories: float
#     protein_g: float


class DormdishMealPlannerSchema__Totals(BaseModel):
    calories: float
    protein_g: float


class DormdishMealPlannerSchema__MealItem(BaseModel):
    name: str
    servings: float
    calories: float
    protein_g: float
    serving_size: Optional[str] = None


class DormdishMealPlannerSchema__Meal(BaseModel):
    description: str
    items: List[DormdishMealPlannerSchema__MealItem]


class DormdishMealPlannerSchema__Meals(BaseModel):
    breakfast: DormdishMealPlannerSchema__Meal
    lunch: DormdishMealPlannerSchema__Meal
    dinner: DormdishMealPlannerSchema__Meal


class DormdishMealPlannerSchema(BaseModel):
    # user_stats: DormdishMealPlannerSchema__UserStats
    # goals: DormdishMealPlannerSchema__Goals
    totals: DormdishMealPlannerSchema__Totals
    meals: DormdishMealPlannerSchema__Meals
    explanation: str
    meta: Optional[Dict[str, Any]] = None


dormdish_meal_planner = Agent(
    name="DormDish Meal Planner",
    instructions="""
You are a pragmatic, encouraging university dining hall nutritionist & meal designer.
Create ONE daily meal plan (breakfast, lunch, dinner) using ONLY the allowed items. Think in terms of a plate: a main + sides. If an item requires a companion (pasta → sauce, sandwich → bread + protein, salad → dressing), include it. Avoid absurd pairings.

STRICT OUTPUT: Return a single JSON object with these exact top-level keys (no extras, no markdown):
{
  "totals": { "calories": "<int>", "protein_g": "<int>" },
  "meals": {
    "breakfast": { "description": "<string>", "items": [ "<meal_item>", ... ] },
    "lunch": { "description": "<string>", "items": [ "<meal_item>", ... ] },
    "dinner": { "description": "<string>", "items": [ "<meal_item>", ... ] }
  },
  "explanation": "<string>"
}

Meal item schema:
{ "name": "<exact item>", "servings": "<number>", "calories": "<number>", "protein_g": "<number>", "serving_size": "<string|null>" }

Rules
- 3 meals only. Each: short description + 2–4 items (main protein + carb/veg/sauce). A single larger item is OK if realistic.
- Variety: avoid repeating exact items more than once/day.
- Don’t stack multiple meats in one meal. Plant proteins may complement sensibly.
- If pairing item missing from list, proceed without fabrication (note in explanation).
- Respect dietary prefs (vegan/vegetarian).
- Serving sizes can be fractional (0.5 increments).

Nutrition Targets
- Calories & protein must be within ±5% of goals.
- Prioritize protein target first, then adjust calories.
- Meals must be realistic, balanced, appetizing.
- Explanation field: 1–3 sentences on how meal plan is beneficial and balanced, noting any unavoidable shortfall/excess.
""",
    model="gpt-5-mini",
    #tools=[web_search_preview],
    output_type=DormdishMealPlannerSchema,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(effort="low", summary="auto") if Reasoning else None,
    ),
    client=client,
    temperature=1.0,
)


def _render_allowed_items(items: Sequence[Dict[str, Any]], limit: Optional[int] = None) -> str:
  if not items:
    return "- (no items provided)"

  limit = limit or len(items)
  lines: List[str] = []
  for idx, item in enumerate(items):
    if idx >= limit:
      break
    name = str(item.get("name", "")).strip()
    cal = item.get("calories")
    prot = item.get("protein_g")
    serving = item.get("serving_size")
    parts: List[str] = [name or "(unnamed item)"]
    if cal is not None:
      parts.append(f"{float(cal):.0f} cal")
    if prot is not None:
      parts.append(f"{float(prot):.1f} g protein")
    if serving:
      parts.append(f"serving: {serving}")
    lines.append(" - " + " | ".join(parts))

  remaining = max(0, len(items) - limit)
  if remaining:
    lines.append(f" - … {remaining} more items omitted for brevity")
  return "\n".join(lines)


def _build_agent_prompt(
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  *,
  max_items: Optional[int] = None,
) -> str:
  hall = menu_payload.get("requested_dining_hall") or menu_payload.get("hall")
  date = menu_payload.get("requested_date")
  allowed_items = menu_payload.get("allowed_items") or []
  if max_items is None:
    try:
      max_items = int(os.getenv("MEALPLAN_ALLOWED_ITEMS_MAX", "175"))
    except ValueError:
      max_items = 175

  menu_meta = {}
  source_menu = menu_payload.get("source_menu")
  if isinstance(source_menu, dict):
    menu_meta = {
      "hall_slug": source_menu.get("hall_slug"),
      "meal_sources": source_menu.get("menu", {}).get("meal_sources")
      if isinstance(source_menu.get("menu"), dict)
      else None,
    }

  prompt_sections = [
    "# USER PROFILE",
    json.dumps({
      "dietary_preference": user_payload.get("dietary_preference") if user_payload is not None else None
    }, indent=2, ensure_ascii=False),
    "",
    "# NUTRITION TARGETS",
    json.dumps(nutrition_targets, indent=2, ensure_ascii=False),
    "",
    "# DINING CONTEXT",
    json.dumps({"dining_hall": hall, "date": date, "meta": menu_meta}, indent=2, ensure_ascii=False),
    "",
    "# ALLOWED MENU ITEMS (name | calories | protein | serving)",
    _render_allowed_items(allowed_items, limit=max_items),
    "",
    "Generate the meal plan strictly in the JSON schema described in the system instructions above.",
    "Do not include markdown backticks or additional commentary outside the JSON object.",
  ]

  return "\n".join(section for section in prompt_sections if section is not None)


async def generate_meal_plan_with_agent(
  *,
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  extra_system_messages: Optional[Sequence[str]] = None,
) -> Tuple[DormdishMealPlannerSchema, str]:
  """Run the DormDish agent and return the validated response plus raw text."""

  prompt = _build_agent_prompt(
    user_payload,
    menu_payload,
    nutrition_targets,
  )
  prompt_chars = len(prompt)
  allowed_items_count = len(menu_payload.get("allowed_items") or [])

  # Lightweight telemetry to help diagnose latency in downstream model calls.
  print(
    f"[meal_plan_agent] prompt_size chars={prompt_chars} "
    f"approx_tokens={prompt_chars // 4} allowed_items={allowed_items_count}"
  )
  cache_key = _build_mealplan_cache_key(
    user_payload,
    menu_payload,
    nutrition_targets,
    extra_system_messages,
  )

  async with _MEAL_PLAN_CACHE_LOCK:
    cached_entry = _MEAL_PLAN_CACHE.get(cache_key)
    if cached_entry:
      cached_model, cached_raw = cached_entry
      _MEAL_PLAN_CACHE.move_to_end(cache_key)
      return cached_model.model_copy(deep=True), cached_raw

  result, raw_response = await dormdish_meal_planner.arun(
    input_text=prompt,
    extra_system_messages=extra_system_messages,
  )

  cached_tuple = (result.model_copy(deep=True), raw_response)
  async with _MEAL_PLAN_CACHE_LOCK:
    _MEAL_PLAN_CACHE[cache_key] = cached_tuple
    _MEAL_PLAN_CACHE.move_to_end(cache_key)
    while len(_MEAL_PLAN_CACHE) > _MEAL_PLAN_CACHE_LIMIT:
      _MEAL_PLAN_CACHE.popitem(last=False)

  return result, raw_response


class WorkflowInput(BaseModel):
  input_as_text: str



async def run_workflow(workflow_input: WorkflowInput):
  state = {

  }
  workflow = workflow_input.model_dump()
  conversation_history: list[TResponseInputItem] = [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": workflow["input_as_text"]
        }
      ]
    }
  ]
  # Guardrails removed. Return the input text (or a minimal workflow result).
  return {"input_as_text": workflow["input_as_text"], "conversation_history": conversation_history}
