import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from dotenv import load_dotenv


load_dotenv()


client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


BASE_SYSTEM_INSTRUCTIONS_TEMPLATE = """You are a university dining hall nutritionist. Your task is to create ONE daily meal plan with breakfast, lunch, and dinner using ONLY the provided menu items.

CRITICAL REQUIREMENTS - MUST BE MET:
• Total daily calories MUST be between {cal_min} and {cal_max} calories (target: {cal_target})
• Total daily protein MUST be between {prot_min}g and {prot_max}g (target: {prot_target}g)
• These are HARD CONSTRAINTS, not suggestions. The plan will be rejected if outside these ranges.

CALCULATION METHOD (MANDATORY):
1. For each item, calculate: servings × calories_per_serving = item_calories
2. For each item, calculate: servings × protein_per_serving = item_protein
3. Sum all item_calories across all meals = total_calories
4. Sum all item_protein across all meals = total_protein
5. VERIFY totals are within required ranges BEFORE finalizing

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
{{"totals":{{"calories":<int>,"protein_g":<int>}},"meals":{{"breakfast":{{"description":"<str>","items":[{{"name":"<exact_name>","servings":<float>,"calories":<float>,"protein_g":<float>,"serving_size":"<str>"}}]}},"lunch":{{"description":"<str>","items":[...]}},"dinner":{{"description":"<str>","items":[...]}}}},"explanation":"<str>"}}

EXPLANATION:
• Write 1-3 sentences explaining how the plan meets the calorie/protein targets
• Mention any notable trade-offs or optimizations made

EXAMPLE CALCULATION:
If target is 2000 cal / 150g protein:
- Grilled Chicken (250 cal, 45g protein) × 2 servings = 500 cal, 90g protein
- Brown Rice (200 cal, 4g protein) × 1.5 servings = 300 cal, 6g protein
Continue until totals reach 1900-2100 cal and 143-158g protein.

Remember: This is a mathematical optimization task. Precision is mandatory. Think step by step."""


class DormdishMealPlannerSchema__Totals(BaseModel):
    calories: float
    protein_g: float


class DormdishMealPlannerSchema__MealItem(BaseModel):
    name: str
    servings: float
    calories: float
    protein_g: float
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
    serving = item.get("serving_size")
    
    
    parts: List[str] = [name or "(unnamed)"]
    if cal is not None:
      parts.append(f"{float(cal):.0f}cal")  
    if prot is not None:
      parts.append(f"{float(prot):.1f}g")  
    if serving:
      parts.append(f"{serving}")  
    lines.append("|".join(parts))  

  remaining = max(0, len(items) - limit)
  if remaining:
    lines.append(f"…{remaining} more")
  return "\n".join(lines)


def _recalculate_totals_from_meals(
  result: DormdishMealPlannerSchema,
  menu_payload: Dict[str, Any],
) -> Dict[str, float]:
  """
  Recalculate totals deterministically from the meal plan items.
  This ensures perfect math instead of relying on LLM calculations.
  """
  allowed_items = menu_payload.get("allowed_items") or []
  
  
  item_lookup: Dict[str, Dict[str, Any]] = {}
  for item in allowed_items:
    name = str(item.get("name", "")).strip().lower()
    item_lookup[name] = {
      "calories": float(item.get("calories", 0)),
      "protein_g": float(item.get("protein_g", 0)),
    }
  
  total_calories = 0.0
  total_protein = 0.0
  
  
  for meal_name in ["breakfast", "lunch", "dinner"]:
    meal = getattr(result.meals, meal_name)
    for meal_item in meal.items:
      item_name = meal_item.name.strip().lower()
      servings = float(meal_item.servings)
      
      
      if item_name in item_lookup:
        item_data = item_lookup[item_name]
        total_calories += item_data["calories"] * servings
        total_protein += item_data["protein_g"] * servings
      else:
        
        print(f"[meal_plan_agent] WARNING: Item '{meal_item.name}' not found in menu, using LLM values")
        total_calories += float(meal_item.calories)
        total_protein += float(meal_item.protein_g)
  
  return {
    "calories": round(total_calories),
    "protein_g": round(total_protein),
  }


def _build_agent_prompt(
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  *,
  max_items: Optional[int] = None,
) -> str:
  """Build prompt for LLM."""
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

  dining_context = {"dining_hall": hall, "date": date}
  if menu_meta:
    dining_context["meta"] = menu_meta

  
  menu_header = "MENU (name|cal|prot|serving)"

  
  prompt_sections = [
    "PROFILE",
    json.dumps({
      "diet": user_payload.get("dietary_preference") if user_payload is not None else None
    }, ensure_ascii=False),
    "",
    "TARGETS",
    json.dumps(nutrition_targets, ensure_ascii=False),
    "",
    "CONTEXT",
    json.dumps(dining_context, ensure_ascii=False),
    "",
    menu_header,
    _render_allowed_items(allowed_items, limit=max_items),
    "",
    "Generate meal plan per schema above. Pure JSON, no markdown.",
  ]

  return "\n".join(section for section in prompt_sections if section is not None)


async def generate_meal_plan_with_agent(
  *,
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  extra_system_messages: Optional[Sequence[str]] = None,
) -> Tuple[DormdishMealPlannerSchema, str]:
  """Generate a meal plan using Claude Haiku 4.5 with extended thinking for precision.
  
  Uses Claude's extended thinking feature to perform more accurate calculations
  and hit nutritional targets with high precision.
  
  Args:
    user_payload: User profile and preferences
    menu_payload: Menu data including allowed items
    nutrition_targets: Calorie and protein goals
    extra_system_messages: Optional additional instructions (not used with Claude)
    
  Returns:
    Tuple of (validated meal plan schema, raw JSON string)
    
  Raises:
    ValueError: If model fails to return valid JSON
  """

  prompt = _build_agent_prompt(
    user_payload,
    menu_payload,
    nutrition_targets,
  )
  prompt_chars = len(prompt)
  allowed_items_count = len(menu_payload.get("allowed_items") or [])

  
  print(
    f"[meal_plan_agent] Claude Haiku 4.5 prompt_size chars={prompt_chars} "
    f"approx_tokens={prompt_chars // 4} allowed_items={allowed_items_count} "
    f"extended_thinking=enabled"
  )

  
  cal_target = nutrition_targets['calories']
  prot_target = nutrition_targets['protein_g']
  cal_min = int(cal_target * 0.95)
  cal_max = int(cal_target * 1.05)
  prot_min = int(prot_target * 0.95)
  prot_max = int(prot_target * 1.05)
  
  instructions = BASE_SYSTEM_INSTRUCTIONS_TEMPLATE.format(
    cal_min=cal_min,
    cal_max=cal_max,
    cal_target=cal_target,
    prot_min=prot_min,
    prot_max=prot_max,
    prot_target=prot_target
  )

  
  chosen_model = os.getenv("MEALPLAN_MODEL", "claude-haiku-4-5")
  temperature = float(os.getenv("MEALPLAN_TEMPERATURE", "1.0"))
  max_tokens = int(os.getenv("MEALPLAN_MAX_TOKENS", "20000"))

  
  response = await client.messages.create(
    model=chosen_model,
    max_tokens=max_tokens,
    temperature=temperature,
    thinking={
      "type": "enabled",
      "budget_tokens": 8000  
    },
    
    system=instructions,
    messages=[
      {"role": "user", "content": prompt}
    ]
  )

  
  raw_response = None
  thinking_content = None
  
  for block in response.content:
    if hasattr(block, 'text') and block.type == 'text':
      raw_response = block.text
    elif hasattr(block, 'thinking') and block.type == 'thinking':
      thinking_content = block.thinking
  
  if thinking_content:
    print(f"[meal_plan_agent] Claude thinking process: {len(thinking_content)} chars")
  
  if not raw_response:
    raise ValueError("Claude returned no text response")

  
  raw_json = raw_response.strip()
  if raw_json.startswith("```"):
    lines = raw_json.split("\n")
    if lines[0].startswith("```"):
      lines = lines[1:]
    if lines and lines[-1].strip() == "```":
      lines = lines[:-1]
    raw_json = "\n".join(lines).strip()

  
  try:
    parsed = json.loads(raw_json)
  except json.JSONDecodeError as e:
    print(f"[meal_plan_agent] JSON parsing failed: {e}")
    print(f"[meal_plan_agent] Raw response: {raw_response[:500]}")
    raise ValueError(f"Claude output was not valid JSON: {e}")

  
  if "explanation" not in parsed:
    print("[meal_plan_agent] Adding default explanation")
    parsed["explanation"] = "Meal plan generated to meet nutritional targets."

  
  result = DormdishMealPlannerSchema.model_validate(parsed)

  
  recalculated_totals = _recalculate_totals_from_meals(result, menu_payload)
  claude_calories = result.totals.calories
  claude_protein = result.totals.protein_g
  actual_calories = recalculated_totals["calories"]
  actual_protein = recalculated_totals["protein_g"]
  
  
  cal_diff = abs(actual_calories - claude_calories)
  prot_diff = abs(actual_protein - claude_protein)
  if cal_diff > 0 or prot_diff > 0:
    print(f"[meal_plan_agent] Math correction: {cal_diff} cal, {prot_diff}g protein")
  
  result.totals.calories = actual_calories
  result.totals.protein_g = actual_protein

  return result, raw_response


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
