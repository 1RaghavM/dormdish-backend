import asyncio
import json
import os
import queue
import random
import threading
import warnings
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google.genai import types
from pydantic import BaseModel, ValidationError, PydanticDeprecatedSince20
from dotenv import load_dotenv

# Suppress Pydantic V2 deprecation warnings from Google GenAI library
# This is a known issue that Google will fix in a future SDK update
warnings.filterwarnings("ignore", category=PydanticDeprecatedSince20)

load_dotenv()


_client = None

def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


STATIC_MEAL_PLAN_INSTRUCTIONS = """You are a university dining hall nutritionist. Your task is to create ONE daily meal plan with breakfast, lunch, and dinner using ONLY the provided menu items.

The user's prompt below specifies CRITICAL REQUIREMENTS including exact calorie and protein ranges. These are HARD CONSTRAINTS — the plan will be rejected if outside those ranges.

CALCULATION METHOD (MANDATORY):
1. For each item, calculate: servings × calories_per_serving = item_calories
2. For each item, calculate: servings × protein_per_serving = item_protein
3. For each item, calculate: servings × fat_per_serving = item_fat
4. For each item, calculate: servings × carbs_per_serving = item_carbs
5. Sum all item_calories across all meals = total_calories
6. Sum all item_protein across all meals = total_protein
7. Sum all item_fat across all meals = total_fat
8. Sum all item_carbs across all meals = total_carbs
9. VERIFY totals are within required ranges BEFORE finalizing

MEAL STRUCTURE:
• Create exactly 3 meals: breakfast, lunch, dinner
• Each meal should have 2-4 items (main dish + sides/accompaniments)
• Include companions (e.g., pasta→sauce, salad→dressing)
• Use realistic servings: 0.5, 1, 1.5, 2 (fractional amounts allowed)
• Vary items - don't repeat the same item more than once per day
• Don't stack multiple protein mains in one meal
• Do not include drinks, or desserts.
Do not repeat items in the same meal, do not give multiple variations of the same item in the same meal (ex. hard-boiled eggs and scrambled eggs).

OPTIMIZATION STRATEGY:
1. Start with high-protein items to meet protein target
2. Add calorie-dense items if under calorie target
3. Use fractional servings (0.5, 1.5) for fine-tuning
4. Adjust portion sizes to hit BOTH targets simultaneously
5. Double-check math before submitting

DIETARY PREFERENCES:
• Respect the user's dietary preference specified in the prompt

OUTPUT FORMAT (STRICT JSON, NO MARKDOWN):
{{"totals":{{"calories":<int>,"protein_g":<int>,"fat_g":<int>,"carbs_g":<int>}},"meals":{{"breakfast":{{"description":"<str>","items":[{{"name":"<exact_name>","servings":<float>,"calories":<float>,"protein_g":<float>,"fat_g":<float>,"carbs_g":<float>,"serving_size":"<str>"}}]}},"lunch":{{"description":"<str>","items":[...]}},"dinner":{{"description":"<str>","items":[...]}}}},"explanation":"<str>"}}

EXPLANATION:
• Write 1-3 sentences explaining how the plan meets the calorie/protein targets
• Mention any notable trade-offs or optimizations made

EXAMPLE CALCULATION:
If target is 2000 cal / 150g protein / 65g fat / 250g carbs:
- Grilled Chicken (250 cal, 45g protein, 5g fat, 0g carbs) × 2 servings = 500 cal, 90g protein, 10g fat, 0g carbs
- Brown Rice (200 cal, 4g protein, 1g fat, 42g carbs) × 1.5 servings = 300 cal, 6g protein, 1.5g fat, 63g carbs
Continue until totals reach targets within ±5%.

Remember: This is a mathematical optimization task. Precision is mandatory.

DO NOT output your reasoning or "thoughts". Output ONLY the final JSON object exactly matching the schema."""


STATIC_PARTIAL_MEAL_PLAN_INSTRUCTIONS = """You are a university dining hall nutritionist. Your task is to create a PARTIAL daily meal plan using ONLY the provided menu items.

The user's prompt below specifies which meals to generate, exact calorie and protein ranges, and other constraints. These are HARD CONSTRAINTS — the plan will be rejected if outside those ranges.

The user has already eaten other meals today. The targets represent ONLY what remains for the specified meals.

CALCULATION METHOD (MANDATORY):
1. For each item, calculate: servings × calories_per_serving = item_calories
2. For each item, calculate: servings × protein_per_serving = item_protein
3. For each item, calculate: servings × fat_per_serving = item_fat
4. For each item, calculate: servings × carbs_per_serving = item_carbs
5. Sum all item_calories across all meals = total_calories
6. Sum all item_protein across all meals = total_protein
7. Sum all item_fat across all meals = total_fat
8. Sum all item_carbs across all meals = total_carbs
9. VERIFY totals are within required ranges BEFORE finalizing

MEAL STRUCTURE:
• Each meal should have 2-4 items (main dish + sides/accompaniments)
• Include companions (e.g., pasta→sauce, salad→dressing)
• Use realistic servings: 0.5, 1, 1.5, 2 (fractional amounts allowed)
• Vary items - don't repeat the same item more than once per day
• Don't stack multiple protein mains in one meal
• Do not include drinks, or desserts.
Do not repeat items in the same meal, do not give multiple variations of the same item in the same meal (ex. hard-boiled eggs and scrambled eggs).

OPTIMIZATION STRATEGY:
1. Start with high-protein items to meet protein target
2. Add calorie-dense items if under calorie target
3. Use fractional servings (0.5, 1.5) for fine-tuning
4. Adjust portion sizes to hit BOTH targets simultaneously
5. Double-check math before submitting

DIETARY PREFERENCES:
• Respect the user's dietary preference specified in the prompt

EXPLANATION:
• Write 1-3 sentences explaining how the plan meets the calorie/protein targets
• Mention any notable trade-offs or optimizations made

Remember: This is a mathematical optimization task. Precision is mandatory.

DO NOT output your reasoning or "thoughts". Output ONLY the final JSON object exactly matching the schema."""


_REPAIR_INSTRUCTIONS = """Your previous response could not be parsed/validated.
Return ONLY a single valid JSON object (no markdown, no comments, no extra text).
Use double quotes for all keys and string values."""


def _strip_code_fences(text: str) -> str:
  raw = (text or "").strip()
  if not raw.startswith("```"):
    return raw
  lines = raw.split("\n")
  if lines and lines[0].startswith("```"):
    lines = lines[1:]
  if lines and lines[-1].strip() == "```":
    lines = lines[:-1]
  return "\n".join(lines).strip()


def _extract_first_json_object(text: str) -> str:
  """Extract the first {...} JSON object from a model response.

  Gemini (or intermediaries) can sometimes prepend/append non-JSON text even when
  response_mime_type/schema are set. This makes parsing robust.
  """
  raw = _strip_code_fences(text)
  first = raw.find("{")
  last = raw.rfind("}")
  if first == -1 or last == -1 or last <= first:
    raise ValueError("No JSON object detected in model response")
  return raw[first:last + 1]


def _is_overloaded_error(exc: Exception) -> bool:
  msg = str(exc).lower()
  return (
    ("503" in msg and "unavailable" in msg)
    or "model is overloaded" in msg
    or "resource exhausted" in msg
    or "temporarily unavailable" in msg
    or "unavailable" in msg
  )


async def _sleep_backoff(attempt: int) -> None:
  # Exponential backoff with jitter, capped.
  base = float(os.getenv("MEALPLAN_OVERLOAD_BACKOFF_BASE_S", "1.0"))
  cap = float(os.getenv("MEALPLAN_OVERLOAD_BACKOFF_MAX_S", "30"))
  exp = min(cap, base * (2 ** max(0, attempt - 1)))
  await asyncio.sleep(random.uniform(0.0, exp))


class DormdishMealPlannerSchema__Totals(BaseModel):
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float


class DormdishMealPlannerSchema__MealItem(BaseModel):
    name: str
    servings: float
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float
    serving_size: str


class DormdishMealPlannerSchema__Meal(BaseModel):
    description: str
    items: List[DormdishMealPlannerSchema__MealItem]


class DormdishMealPlannerSchema__Meals(BaseModel):
    breakfast: DormdishMealPlannerSchema__Meal
    lunch: DormdishMealPlannerSchema__Meal
    dinner: DormdishMealPlannerSchema__Meal


class DormdishMealPlannerSchema(BaseModel):
    totals: DormdishMealPlannerSchema__Totals
    meals: DormdishMealPlannerSchema__Meals
    explanation: str


def build_partial_meals_schema(include_meals: List[str]):
    """Build a dynamic Pydantic model that only includes the specified meals."""
    PartialMeals = type(
        "PartialMeals",
        (BaseModel,),
        {"__annotations__": {m: DormdishMealPlannerSchema__Meal for m in include_meals}},
    )

    class PartialMealPlanSchema(BaseModel):
        totals: DormdishMealPlannerSchema__Totals
        meals: PartialMeals
        explanation: str

    return PartialMealPlanSchema


def _render_allowed_items(items: Sequence[Dict[str, Any]], limit: Optional[int] = None) -> str:
  """Render menu items for prompt in compact format to reduce token count."""
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
    fat = item.get("fat_g")
    carbs = item.get("carbs_g")
    serving = item.get("serving_size")


    parts: List[str] = [name or "(unnamed)"]
    if cal is not None:
      parts.append(f"{float(cal):.0f}cal")
    if prot is not None:
      parts.append(f"{float(prot):.1f}g prot")
    if fat is not None:
      parts.append(f"{float(fat):.1f}g fat")
    if carbs is not None:
      parts.append(f"{float(carbs):.1f}g carb")
    if serving:
      parts.append(f"{serving}")
    lines.append("|".join(parts))

  remaining = max(0, len(items) - limit)
  if remaining:
    lines.append(f"…{remaining} more")
  return "\n".join(lines)


def _recalculate_totals_from_meals(
  result,
  menu_payload: Dict[str, Any],
  include_meals: Optional[List[str]] = None,
) -> Dict[str, float]:
  """
  Recalculate totals deterministically from the meal plan items.
  This ensures perfect math instead of relying on LLM calculations.

  Args:
    result: Validated meal plan schema (full or partial)
    menu_payload: Menu data including allowed items
    include_meals: Optional list of meal names to iterate over.
                   Defaults to ["breakfast", "lunch", "dinner"].
  """
  allowed_items = menu_payload.get("allowed_items") or []


  item_lookup: Dict[str, Dict[str, Any]] = {}
  for item in allowed_items:
    name = str(item.get("name", "")).strip().lower()
    item_lookup[name] = {
      "calories": float(item.get("calories", 0)),
      "protein_g": float(item.get("protein_g", 0)),
      "fat_g": float(item.get("fat_g", 0)),
      "carbs_g": float(item.get("carbs_g", 0)),
    }

  total_calories = 0.0
  total_protein = 0.0
  total_fat = 0.0
  total_carbs = 0.0

  meals_to_iterate = include_meals if include_meals else ["breakfast", "lunch", "dinner"]

  for meal_name in meals_to_iterate:
    meal = getattr(result.meals, meal_name)
    for meal_item in meal.items:
      item_name = meal_item.name.strip().lower()
      servings = float(meal_item.servings)


      if item_name in item_lookup:
        item_data = item_lookup[item_name]
        total_calories += item_data["calories"] * servings
        total_protein += item_data["protein_g"] * servings
        total_fat += item_data["fat_g"] * servings
        total_carbs += item_data["carbs_g"] * servings
      else:

        print(f"[meal_plan_agent] WARNING: Item '{meal_item.name}' not found in menu, using LLM values")
        total_calories += float(meal_item.calories)
        total_protein += float(meal_item.protein_g)
        total_fat += float(meal_item.fat_g)
        total_carbs += float(meal_item.carbs_g)

  return {
    "calories": round(total_calories),
    "protein_g": round(total_protein),
    "fat_g": round(total_fat),
    "carbs_g": round(total_carbs),
  }


def _build_agent_prompt(
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  *,
  max_items: Optional[int] = None,
  user_preferences_context: Optional[str] = None,
  cal_min: Optional[int] = None,
  cal_max: Optional[int] = None,
  prot_min: Optional[int] = None,
  prot_max: Optional[int] = None,
  include_meals: Optional[List[str]] = None,
  output_schema_hint: Optional[str] = None,
) -> str:
  """Build the user-specific prompt for LLM. Does NOT include menu items (those go in system_instruction)."""
  hall = menu_payload.get("requested_dining_hall") or menu_payload.get("hall")
  date = menu_payload.get("requested_date")

  dining_context = {"dining_hall": hall, "date": date}
  restaurant = menu_payload.get("restaurant")
  if restaurant:
    dining_context["restaurant"] = restaurant

  menu_meta = {}
  source_menu = menu_payload.get("source_menu")
  if isinstance(source_menu, dict):
    menu_meta = {
      "hall_slug": source_menu.get("hall_slug"),
      "meal_sources": source_menu.get("menu", {}).get("meal_sources")
      if isinstance(source_menu.get("menu"), dict)
      else None,
    }
  if menu_meta:
    dining_context["meta"] = menu_meta

  prompt_sections = [
    "PROFILE",
    json.dumps({
      "diet": user_payload.get("dietary_preference") if user_payload is not None else None
    }, ensure_ascii=False),
    "",
    "TARGETS",
    json.dumps(nutrition_targets, ensure_ascii=False),
    "",
  ]

  # Add per-user calorie/protein constraints (moved from system instructions)
  if cal_min is not None:
    constraints = {
      "calories_min": cal_min,
      "calories_max": cal_max,
      "calories_target": nutrition_targets.get("calories"),
      "protein_min_g": prot_min,
      "protein_max_g": prot_max,
      "protein_target_g": nutrition_targets.get("protein_g"),
    }
    if include_meals:
      constraints["meals_to_generate"] = include_meals
    prompt_sections.extend([
      "CONSTRAINTS (HARD - plan rejected if outside these ranges)",
      json.dumps(constraints, ensure_ascii=False),
      "",
    ])

  if output_schema_hint:
    prompt_sections.extend([
      "OUTPUT SCHEMA",
      output_schema_hint,
      "",
    ])

  prompt_sections.extend([
    "CONTEXT",
    json.dumps(dining_context, ensure_ascii=False),
    "",
  ])

  if user_preferences_context:
    prompt_sections.extend([
      "USER PREFERENCES (Learned from Past Interactions)",
      user_preferences_context,
      "",
    ])

  prompt_sections.append("Generate meal plan per schema above. Pure JSON, no markdown.")

  return "\n".join(section for section in prompt_sections if section is not None)


async def generate_meal_plan_with_agent(
  *,
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  extra_system_messages: Optional[Sequence[str]] = None,
  user_preferences_context: Optional[str] = None,
  exclude_meals: Optional[List[str]] = None,
  cached_content: Optional[str] = None,
) -> Tuple[DormdishMealPlannerSchema, str]:
  """Generate a meal plan using Gemini 2.5 Flash with optional user preferences from mem0.

  Uses Gemini's structured output for precision and accuracy.

  Args:
    user_payload: User profile and preferences
    menu_payload: Menu data including allowed items
    nutrition_targets: Calorie and protein goals
    extra_system_messages: Optional additional instructions (not used with Gemini)
    user_preferences_context: Optional user preferences from mem0
    exclude_meals: Optional list of meal names to exclude (e.g. ["breakfast"])
    cached_content: Optional Gemini cache handle for context caching

  Returns:
    Tuple of (validated meal plan schema, raw JSON string)

  Raises:
    ValueError: If model fails to return valid JSON
  """

  cal_target = nutrition_targets['calories']
  prot_target = nutrition_targets['protein_g']
  cal_min = int(cal_target * 0.95)
  cal_max = int(cal_target * 1.05)
  prot_min = int(prot_target * 0.95)
  prot_max = int(prot_target * 1.05)

  # Determine which meals to include
  all_meals = ["breakfast", "lunch", "dinner"]
  if exclude_meals:
    include_meals = [m for m in all_meals if m.lower() not in [e.lower() for e in exclude_meals]]
  else:
    include_meals = all_meals

  meal_count = len(include_meals)
  meal_list = " and ".join(include_meals)

  if exclude_meals and len(include_meals) < 3:
    # Build partial output schema string
    meal_schema_parts = []
    for m in include_meals:
      meal_schema_parts.append(
        f'"{m}":' + '{"description":"<str>","items":[{"name":"<exact_name>","servings":<float>,"calories":<float>,"protein_g":<float>,"fat_g":<float>,"carbs_g":<float>,"serving_size":"<str>"}]}'
      )
    output_schema = '{{"totals":{{"calories":<int>,"protein_g":<int>,"fat_g":<int>,"carbs_g":<int>}},"meals":{{{meals_schema}}},"explanation":"<str>"}}'.format(
      meals_schema=",".join(meal_schema_parts)
    )
    response_schema = build_partial_meals_schema(include_meals)
  else:
    output_schema = None
    response_schema = DormdishMealPlannerSchema

  prompt = _build_agent_prompt(
    user_payload,
    menu_payload,
    nutrition_targets,
    user_preferences_context=user_preferences_context,
    cal_min=cal_min,
    cal_max=cal_max,
    prot_min=prot_min,
    prot_max=prot_max,
    include_meals=include_meals if exclude_meals else None,
    output_schema_hint=output_schema if (exclude_meals and len(include_meals) < 3) else None,
  )
  prompt_chars = len(prompt)
  allowed_items_count = len(menu_payload.get("allowed_items") or [])

  print(
    f"[meal_plan_agent] Gemini 2.5 Flash prompt_size chars={prompt_chars} "
    f"approx_tokens={prompt_chars // 4} allowed_items={allowed_items_count}"
  )

  # Build static system instruction (menu + rules) — only needed when no cached_content
  static_instructions = None
  if not cached_content:
    allowed_items = menu_payload.get("allowed_items") or []
    max_items_limit = int(os.getenv("MEALPLAN_ALLOWED_ITEMS_MAX", "175"))
    menu_items_text = _render_allowed_items(allowed_items, limit=max_items_limit)

    if exclude_meals and len(include_meals) < 3:
      base_instructions = STATIC_PARTIAL_MEAL_PLAN_INSTRUCTIONS
    else:
      base_instructions = STATIC_MEAL_PLAN_INSTRUCTIONS

    static_instructions = (
      base_instructions
      + "\n\nMENU (name|cal|prot|fat|carb|serving)\n"
      + menu_items_text
    )

  chosen_model = os.getenv("MEALPLAN_MODEL", "gemini-2.5-flash")
  temperature = float(os.getenv("MEALPLAN_TEMPERATURE", "0.4"))
  max_tokens = int(os.getenv("MEALPLAN_MAX_TOKENS", "15000"))
  thinking_budget = int(os.getenv("MEALPLAN_THINKING_BUDGET", "2048"))

  last_error: Optional[Exception] = None
  for attempt in (1, 2):
    attempt_temperature = temperature if attempt == 1 else 0.1

    # Build user prompt and config.
    # On retry: append repair instructions to contents when using cached_content
    # (since system_instruction can't be modified), or to system_instruction
    # when not using cached_content (matching current production behavior).
    attempt_prompt = prompt
    config_kwargs = dict(
      temperature=attempt_temperature,
      max_output_tokens=max_tokens,
      response_mime_type="application/json",
      response_schema=response_schema,
      thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
    )
    if cached_content:
      config_kwargs["cached_content"] = cached_content
      if attempt > 1:
        attempt_prompt = f"{prompt}\n\n{_REPAIR_INSTRUCTIONS}"
    else:
      attempt_system = static_instructions
      if attempt > 1:
        attempt_system = f"{static_instructions}\n\n{_REPAIR_INSTRUCTIONS}"
      config_kwargs["system_instruction"] = attempt_system

    def call_gemini():
      return _get_client().models.generate_content(
        model=chosen_model,
        contents=attempt_prompt,
        config=types.GenerateContentConfig(**config_kwargs),
      )

    # Retry overloaded/503 responses a couple times with backoff.
    overloaded_error: Optional[Exception] = None
    response = None
    overload_retries = int(os.getenv("MEALPLAN_OVERLOAD_RETRIES", "6"))
    for overload_attempt in range(1, overload_retries + 1):
      try:
        response = await asyncio.to_thread(call_gemini)
        overloaded_error = None
        break
      except Exception as e:
        if _is_overloaded_error(e):
          overloaded_error = e
          if overload_attempt < overload_retries:
            print(f"[meal_plan_agent] WARNING: Model overloaded (attempt={attempt} overload_try={overload_attempt})")
            await _sleep_backoff(overload_attempt)
            continue
          break
        raise

    if response is None:
      last_error = overloaded_error or ValueError("Model did not return a response")
      continue
    raw_response = response.text or ""

    # Log caching info
    if hasattr(response, 'usage_metadata'):
      usage = response.usage_metadata
      cached_tokens = getattr(usage, 'cached_content_token_count', 0)
      if cached_tokens:
        print(f"[meal_plan_agent] Cache hit: {cached_tokens} tokens served from cache")

    if not raw_response.strip():
      last_error = ValueError("Model returned empty response")
      print(f"[meal_plan_agent] WARNING: Empty response (attempt={attempt})")
      continue

    try:
      raw_json = _extract_first_json_object(raw_response)
      parsed = json.loads(raw_json)
    except Exception as e:
      last_error = e
      print(f"[meal_plan_agent] JSON parsing failed (attempt={attempt}): {e}")
      print(f"[meal_plan_agent] Raw response preview: {raw_response[:500]}")
      continue

    if "explanation" not in parsed:
      print("[meal_plan_agent] Adding default explanation")
      parsed["explanation"] = "Meal plan generated to meet nutritional targets."

    try:
      result = response_schema.model_validate(parsed)
    except ValidationError as e:
      last_error = e
      print(f"[meal_plan_agent] Model validation failed (attempt={attempt}): {e}")
      continue

    recalculated_totals = _recalculate_totals_from_meals(result, menu_payload, include_meals=include_meals)
    gemini_calories = result.totals.calories
    gemini_protein = result.totals.protein_g
    actual_calories = recalculated_totals["calories"]
    actual_protein = recalculated_totals["protein_g"]

    cal_diff = abs(actual_calories - gemini_calories)
    prot_diff = abs(actual_protein - gemini_protein)
    if cal_diff > 0 or prot_diff > 0:
      print(f"[meal_plan_agent] Math correction: {cal_diff} cal, {prot_diff}g protein")

    result.totals.calories = actual_calories
    result.totals.protein_g = actual_protein
    result.totals.fat_g = recalculated_totals["fat_g"]
    result.totals.carbs_g = recalculated_totals["carbs_g"]

    return result, raw_response

  if last_error:
    raise ValueError(f"Model returned invalid response: {last_error}")
  raise ValueError("Model returned invalid response")


class WorkflowInput(BaseModel):
  input_as_text: str



async def run_workflow(workflow_input: WorkflowInput):
  
  workflow = workflow_input.model_dump()
  conversation_history: List[Dict[str, Any]] = [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": workflow["input_as_text"]}
      ],
    }
  ]
  return {"input_as_text": workflow["input_as_text"], "conversation_history": conversation_history}


async def generate_meal_plan_with_streaming(
  *,
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  extra_system_messages: Optional[Sequence[str]] = None,
  user_preferences_context: Optional[str] = None,
  exclude_meals: Optional[List[str]] = None,
  cached_content: Optional[str] = None,
):
  """Generate a meal plan using Gemini with streaming updates.

  This is an async generator that yields progress chunks during generation
  and a final complete chunk with the meal plan result.

  Args:
    user_payload: User profile and preferences
    menu_payload: Menu data including allowed items
    nutrition_targets: Calorie and protein goals
    extra_system_messages: Optional additional instructions (not used with Gemini)
    user_preferences_context: Optional user preferences from mem0
    exclude_meals: Optional list of meal names to exclude (e.g. ["breakfast"])
    cached_content: Optional Gemini cache handle for context caching

  Yields:
    Dict with 'type' field:
      - {"type": "thinking", "content": "..."} - Progress updates
      - {"type": "response", "content": "..."} - Partial content
      - {"type": "complete", "meal_plan": {...}} - Final result
      - {"type": "error", "error": "..."} - Error message
  """

  # Yield initial thinking state
  yield {"type": "thinking", "content": "Analyzing menu items and nutritional requirements..."}

  # Build system instructions with targets
  cal_target = nutrition_targets['calories']
  prot_target = nutrition_targets['protein_g']
  cal_min = int(cal_target * 0.95)
  cal_max = int(cal_target * 1.05)
  prot_min = int(prot_target * 0.95)
  prot_max = int(prot_target * 1.05)

  # Determine which meals to include
  all_meals = ["breakfast", "lunch", "dinner"]
  if exclude_meals:
    include_meals = [m for m in all_meals if m.lower() not in [e.lower() for e in exclude_meals]]
  else:
    include_meals = all_meals

  meal_count = len(include_meals)
  meal_list = " and ".join(include_meals)

  if exclude_meals and len(include_meals) < 3:
    # Build partial output schema string
    meal_schema_parts = []
    for m in include_meals:
      meal_schema_parts.append(
        f'"{m}":' + '{"description":"<str>","items":[{"name":"<exact_name>","servings":<float>,"calories":<float>,"protein_g":<float>,"fat_g":<float>,"carbs_g":<float>,"serving_size":"<str>"}]}'
      )
    output_schema = '{{"totals":{{"calories":<int>,"protein_g":<int>,"fat_g":<int>,"carbs_g":<int>}},"meals":{{{meals_schema}}},"explanation":"<str>"}}'.format(
      meals_schema=",".join(meal_schema_parts)
    )
    response_schema = build_partial_meals_schema(include_meals)
  else:
    output_schema = None
    response_schema = DormdishMealPlannerSchema

  prompt = _build_agent_prompt(
    user_payload,
    menu_payload,
    nutrition_targets,
    user_preferences_context=user_preferences_context,
    cal_min=cal_min,
    cal_max=cal_max,
    prot_min=prot_min,
    prot_max=prot_max,
    include_meals=include_meals if exclude_meals else None,
    output_schema_hint=output_schema if (exclude_meals and len(include_meals) < 3) else None,
  )
  prompt_chars = len(prompt)
  allowed_items_count = len(menu_payload.get("allowed_items") or [])

  print(
    f"[meal_plan_agent] Streaming: Gemini 2.5 Flash prompt_size chars={prompt_chars} "
    f"approx_tokens={prompt_chars // 4} allowed_items={allowed_items_count}"
  )

  yield {"type": "thinking", "content": f"Processing {allowed_items_count} menu items..."}

  # Build static system instruction (menu + rules) — only needed when no cached_content
  static_instructions = None
  if not cached_content:
    allowed_items = menu_payload.get("allowed_items") or []
    max_items_limit = int(os.getenv("MEALPLAN_ALLOWED_ITEMS_MAX", "175"))
    menu_items_text = _render_allowed_items(allowed_items, limit=max_items_limit)

    if exclude_meals and len(include_meals) < 3:
      base_instructions = STATIC_PARTIAL_MEAL_PLAN_INSTRUCTIONS
    else:
      base_instructions = STATIC_MEAL_PLAN_INSTRUCTIONS

    static_instructions = (
      base_instructions
      + "\n\nMENU (name|cal|prot|fat|carb|serving)\n"
      + menu_items_text
    )

  chosen_model = os.getenv("MEALPLAN_MODEL", "gemini-2.5-flash")
  temperature = float(os.getenv("MEALPLAN_TEMPERATURE", "0.4"))
  max_tokens = int(os.getenv("MEALPLAN_MAX_TOKENS", "15000"))

  yield {"type": "thinking", "content": f"Generating meal plan targeting {cal_target} calories and {prot_target}g protein..."}

  last_error: Optional[Exception] = None
  accumulated_content = ""

  for attempt in (1, 2):
    attempt_temperature = temperature if attempt == 1 else 0.1

    if attempt > 1:
      yield {"type": "thinking", "content": "Refining meal plan generation..."}

    # Build user prompt and config (same pattern as non-streaming, minus thinking_config)
    attempt_prompt = prompt
    config_kwargs = dict(
      temperature=attempt_temperature,
      max_output_tokens=max_tokens,
      response_mime_type="application/json",
      response_schema=response_schema,
    )
    # Note: streaming does NOT use thinking_config
    if cached_content:
      config_kwargs["cached_content"] = cached_content
      if attempt > 1:
        attempt_prompt = f"{prompt}\n\n{_REPAIR_INSTRUCTIONS}"
    else:
      attempt_system = static_instructions
      if attempt > 1:
        attempt_system = f"{static_instructions}\n\n{_REPAIR_INSTRUCTIONS}"
      config_kwargs["system_instruction"] = attempt_system

    def call_gemini_stream():
      return _get_client().models.generate_content_stream(
        model=chosen_model,
        contents=attempt_prompt,
        config=types.GenerateContentConfig(**config_kwargs),
      )

    # Retry overloaded/503 responses with backoff
    overloaded_error: Optional[Exception] = None
    stream = None
    overload_retries = int(os.getenv("MEALPLAN_OVERLOAD_RETRIES", "6"))

    for overload_attempt in range(1, overload_retries + 1):
      try:
        stream = await asyncio.to_thread(call_gemini_stream)
        overloaded_error = None
        break
      except Exception as e:
        if _is_overloaded_error(e):
          overloaded_error = e
          if overload_attempt < overload_retries:
            print(f"[meal_plan_agent] WARNING: Model overloaded (attempt={attempt} overload_try={overload_attempt})")
            yield {"type": "thinking", "content": "Model busy, retrying..."}
            await _sleep_backoff(overload_attempt)
            continue
          break
        raise

    if stream is None:
      last_error = overloaded_error or ValueError("Model did not return a response")
      continue

    # Stream the response chunks
    accumulated_content = ""
    try:
      yield {"type": "thinking", "content": "Building your personalized meal plan..."}

      # Process streaming chunks with proper async iteration
      # We need to iterate in a thread since the Gemini stream is synchronous
      chunk_queue: queue.Queue = queue.Queue()
      stream_error: List[Exception] = []

      def stream_to_queue():
        try:
          for chunk in stream:
            # Thinking chunks may have None candidates; accessing .text
            # raises ValueError. Guard with try/except to skip them safely.
            try:
              text = chunk.text
            except (ValueError, AttributeError):
              continue
            if text:
              chunk_queue.put(text)
          chunk_queue.put(None)  # Signal completion
        except Exception as e:
          stream_error.append(e)
          chunk_queue.put(None)

      # Start streaming in background thread
      stream_thread = threading.Thread(target=stream_to_queue, daemon=True)
      stream_thread.start()

      # Process chunks as they arrive
      chunk_count = 0
      thinking_messages = [
        "Selecting optimal breakfast options...",
        "Balancing your protein intake...",
        "Planning your lunch...",
        "Optimizing calorie distribution...",
        "Crafting your dinner...",
        "Fine-tuning portion sizes...",
        "Almost there...",
      ]
      while True:
        try:
          # Wait for chunk with timeout to allow async cooperation
          chunk_text = await asyncio.to_thread(chunk_queue.get, True, 0.1)
          if chunk_text is None:
            break
          accumulated_content += chunk_text
          chunk_count += 1

          # Yield thinking updates periodically to show progress
          if chunk_count % 2 == 0 and chunk_count // 2 <= len(thinking_messages):
            msg_idx = min(chunk_count // 2 - 1, len(thinking_messages) - 1)
            yield {"type": "thinking", "content": thinking_messages[msg_idx]}
        except queue.Empty:
          await asyncio.sleep(0.05)
          continue

      # Check for streaming errors
      if stream_error:
        raise stream_error[0]

    except Exception as e:
      if _is_overloaded_error(e):
        last_error = e
        continue
      raise

    if not accumulated_content.strip():
      last_error = ValueError("Model returned empty response")
      print(f"[meal_plan_agent] WARNING: Empty streaming response (attempt={attempt})")
      continue

    # Parse the accumulated response
    try:
      raw_json = _extract_first_json_object(accumulated_content)
      parsed = json.loads(raw_json)
    except Exception as e:
      last_error = e
      print(f"[meal_plan_agent] Streaming JSON parsing failed (attempt={attempt}): {e}")
      print(f"[meal_plan_agent] Raw response preview: {accumulated_content[:500]}")
      continue

    if "explanation" not in parsed:
      print("[meal_plan_agent] Adding default explanation")
      parsed["explanation"] = "Meal plan generated to meet nutritional targets."

    try:
      result = response_schema.model_validate(parsed)
    except ValidationError as e:
      last_error = e
      print(f"[meal_plan_agent] Streaming model validation failed (attempt={attempt}): {e}")
      continue

    # Recalculate totals for accuracy
    recalculated_totals = _recalculate_totals_from_meals(result, menu_payload, include_meals=include_meals)
    gemini_calories = result.totals.calories
    gemini_protein = result.totals.protein_g
    actual_calories = recalculated_totals["calories"]
    actual_protein = recalculated_totals["protein_g"]

    cal_diff = abs(actual_calories - gemini_calories)
    prot_diff = abs(actual_protein - gemini_protein)
    if cal_diff > 0 or prot_diff > 0:
      print(f"[meal_plan_agent] Math correction: {cal_diff} cal, {prot_diff}g protein")

    result.totals.calories = actual_calories
    result.totals.protein_g = actual_protein
    result.totals.fat_g = recalculated_totals["fat_g"]
    result.totals.carbs_g = recalculated_totals["carbs_g"]

    # Convert to dict for response
    meal_plan_dict = result.model_dump()

    # Final thinking update before completion
    yield {"type": "thinking", "content": "Finalizing your meal plan..."}

    # Yield the complete result
    yield {
      "type": "complete",
      "meal_plan": meal_plan_dict,
      "raw_response": accumulated_content,
    }
    return

  # If we get here, all streaming attempts failed — fall back to non-streaming
  print(f"[meal_plan_agent] Streaming failed after all attempts, falling back to non-streaming: {last_error}")
  yield {"type": "thinking", "content": "Switching to alternate generation method..."}

  try:
    result, raw_response = await generate_meal_plan_with_agent(
      user_payload=user_payload,
      menu_payload=menu_payload,
      nutrition_targets=nutrition_targets,
      extra_system_messages=extra_system_messages,
      user_preferences_context=user_preferences_context,
      exclude_meals=exclude_meals,
      cached_content=cached_content,
    )
    meal_plan_dict = result.model_dump()
    yield {"type": "thinking", "content": "Finalizing your meal plan..."}
    yield {
      "type": "complete",
      "meal_plan": meal_plan_dict,
      "raw_response": raw_response,
    }
    return
  except Exception as fallback_error:
    print(f"[meal_plan_agent] Non-streaming fallback also failed: {fallback_error}")
    yield {"type": "error", "error": "Failed to generate meal plan. Please try again."}
