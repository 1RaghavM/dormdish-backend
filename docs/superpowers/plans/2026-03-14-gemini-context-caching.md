# Gemini Context Caching Implementation Plan

**Goal:** Reduce Gemini input token costs by 50-75% by caching static menu data + system instructions using Gemini's explicit context caching API, with implicit caching as fallback.

**Architecture:** A new `menu_cache.py` module manages Gemini cache lifecycle (create, lookup, cleanup). The daily cron creates caches after menu refresh. Food scan and meal plan agents accept an optional `cached_content` handle and conditionally omit `system_instruction` when using it. The meal plan agent is restructured to separate static system instructions from per-user content.

**Tech Stack:** Python, google-genai SDK (`client.caches.create`), Supabase (PostgreSQL), FastAPI, Vercel serverless

**Spec:** `docs/superpowers/specs/2026-03-14-gemini-context-caching-design.md`

---

## File Structure

| File | Role |
|---|---|
| `menu_cache.py` (new) | Gemini cache CRUD: create cache, lookup handle from Supabase, cleanup stale rows |
| `meal_plan_agent.py` (modify) | Split system instructions into static/dynamic; use `system_instruction` param; accept `cached_content` |
| `food_scan_agent.py` (modify) | Accept `cached_content` param; conditionally omit `system_instruction` |
| `food_scan_endpoints.py` (modify) | Look up cache handle before calling agent |
| `app.py` (modify) | Cron: call cache creation. Meal plan endpoints: look up cache handle |
| `migrations/add_gemini_caches.sql` (new) | Supabase table for cache handles |

---

## Chunk 1: Supabase Table + Cache Module

### Task 1: Create Supabase migration for `gemini_caches` table

**Files:**
- Create: `migrations/add_gemini_caches.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- Gemini context cache handles
-- Stores references to Gemini cached content objects created during daily cron.
-- Each row maps a (use_case, dining_hall, date) to a Gemini cache name.

CREATE TABLE IF NOT EXISTS gemini_caches (
    id TEXT PRIMARY KEY,
    cache_name TEXT NOT NULL,
    model TEXT NOT NULL,
    university TEXT NOT NULL,
    dining_hall TEXT NOT NULL,
    date TEXT NOT NULL,
    use_case TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gemini_caches_lookup
    ON gemini_caches (dining_hall, university, date, use_case);
```

The `id` format is `{use_case}_{university}_{dining_hall}_{date}`, e.g. `food_scan_rutgers_busch-dining-hall_2026-03-14`.

- [ ] **Step 2: Run the migration in Supabase**

Run this SQL in the Supabase SQL Editor at https://supabase.com/dashboard (project: rszkbhgowcrvwiwcmntt). Copy-paste the migration file contents and execute.

- [ ] **Step 3: Commit**

```bash
git add migrations/add_gemini_caches.sql
git commit -m "feat: add gemini_caches Supabase migration"
```

---

### Task 2: Create `menu_cache.py` — cache creation and lookup

**Files:**
- Create: `menu_cache.py`

This module handles all Gemini cache lifecycle operations. It uses the same Supabase client pattern as the rest of the codebase (import from `app.py`).

- [ ] **Step 1: Write `menu_cache.py`**

```python
"""
Gemini context cache management for dining hall menus.

Creates, looks up, and cleans up Gemini cached content objects.
Caches are created during the daily cron after menu refresh and
referenced by food scan and meal plan agents to reduce token costs.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton Gemini client (shared with agents)
# ---------------------------------------------------------------------------
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set")
        _client = genai.Client(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Cache creation
# ---------------------------------------------------------------------------

async def create_menu_cache(
    dining_hall: str,
    university: str,
    date: str,
    use_case: str,
    system_instruction: str,
    model: str,
) -> Optional[str]:
    """Create a Gemini cached content object and store handle in Supabase.

    Args:
        dining_hall: Hall slug (e.g. "busch-dining-hall")
        university: University key (e.g. "rutgers")
        date: Date string YYYY-MM-DD
        use_case: "food_scan" or "meal_plan"
        system_instruction: The full static system prompt to cache
        model: Gemini model name (e.g. "gemini-2.5-flash")

    Returns:
        Cache name string if successful, None on failure.
    """
    from app import supabase  # lazy import to avoid circular deps

    cache_id = f"{use_case}_{university}_{dining_hall}_{date}"
    display_name = f"{use_case}_{dining_hall}_{date}"
    ttl = os.getenv("GEMINI_CACHE_TTL", "86400s")

    try:
        def _create():
            return _get_client().caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    display_name=display_name,
                    ttl=ttl,
                ),
            )

        cached_content = await asyncio.to_thread(_create)
        cache_name = cached_content.name

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl.rstrip("s")))).isoformat()

        def _store():
            supabase.table("gemini_caches").upsert({
                "id": cache_id,
                "cache_name": cache_name,
                "model": model,
                "university": university,
                "dining_hall": dining_hall,
                "date": date,
                "use_case": use_case,
                "expires_at": expires_at,
            }).execute()

        await asyncio.to_thread(_store)

        logger.info("Created Gemini cache: %s -> %s", cache_id, cache_name)
        return cache_name

    except Exception as exc:
        logger.error("Failed to create Gemini cache %s: %s", cache_id, exc)
        return None


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------

async def get_cache_name(
    dining_hall: str,
    university: str,
    date: str,
    use_case: str,
) -> Optional[str]:
    """Look up a Gemini cache handle from Supabase.

    Returns cache_name if found and not expired, None otherwise.
    Never raises — returns None on any failure.
    """
    from app import supabase

    cache_id = f"{use_case}_{university}_{dining_hall}_{date}"

    try:
        def _fetch():
            now_iso = datetime.now(timezone.utc).isoformat()
            resp = (
                supabase.table("gemini_caches")
                .select("cache_name")
                .eq("id", cache_id)
                .gt("expires_at", now_iso)
                .execute()
            )
            if resp.data and len(resp.data) > 0:
                return resp.data[0]["cache_name"]
            return None

        cache_name = await asyncio.to_thread(_fetch)
        if cache_name:
            logger.info("Cache hit: %s -> %s", cache_id, cache_name)
        else:
            logger.info("Cache miss: %s", cache_id)
        return cache_name

    except Exception as exc:
        logger.warning("Cache lookup failed for %s: %s", cache_id, exc)
        return None


# ---------------------------------------------------------------------------
# Cache cleanup
# ---------------------------------------------------------------------------

async def cleanup_stale_caches() -> int:
    """Delete expired rows from gemini_caches table. Returns count deleted."""
    from app import supabase

    try:
        def _cleanup():
            now_iso = datetime.now(timezone.utc).isoformat()
            resp = (
                supabase.table("gemini_caches")
                .delete()
                .lt("expires_at", now_iso)
                .execute()
            )
            return len(resp.data) if resp.data else 0

        count = await asyncio.to_thread(_cleanup)
        if count > 0:
            logger.info("Cleaned up %d stale cache rows", count)
        return count

    except Exception as exc:
        logger.warning("Cache cleanup failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Bulk cache creation (called from cron)
# ---------------------------------------------------------------------------

async def create_caches_for_date(date: str, halls: list) -> dict:
    """Create Gemini caches for all dining halls for a given date.

    Args:
        date: Date string YYYY-MM-DD
        halls: List of (university, hall_slug) tuples that were successfully refreshed

    Returns:
        dict with "created", "failed", and "cleaned_up" counts
    """
    from app import get_menu_from_supabase
    from food_scan_prompts import build_system_prompt
    from food_scan_menu import _extract_nutrition_items
    import json

    # Clean up stale rows first
    cleaned = await cleanup_stale_caches()

    foodscan_model = os.getenv("FOODSCAN_MODEL", "gemini-2.5-flash")
    mealplan_model = os.getenv("MEALPLAN_MODEL", "gemini-2.5-flash")

    async def _create_for_hall(university: str, hall_slug: str):
        """Create both food_scan and meal_plan caches for one hall."""
        results = {"hall": hall_slug, "food_scan": None, "meal_plan": None}

        # Fetch menu from Supabase (use university slug, NOT display name —
        # get_menu_from_supabase builds menu_id as "{university}_{dining_hall}_{date}")
        menu_data = await get_menu_from_supabase(university, hall_slug, date)
        if not menu_data:
            logger.warning("No menu data for %s/%s on %s, skipping cache creation", university, hall_slug, date)
            results["food_scan"] = "no_menu"
            results["meal_plan"] = "no_menu"
            return results

        # Extract nutrition items
        items = _extract_nutrition_items(menu_data)
        if not items:
            logger.warning("Empty menu items for %s/%s on %s", university, hall_slug, date)
            results["food_scan"] = "empty_menu"
            results["meal_plan"] = "empty_menu"
            return results

        # --- Food scan cache ---
        menu_json = json.dumps(items, indent=2, ensure_ascii=False)
        food_scan_system = build_system_prompt(
            dining_hall=hall_slug,
            date=date,
            menu_json=menu_json,
        )
        fs_name = await create_menu_cache(
            dining_hall=hall_slug,
            university=university,
            date=date,
            use_case="food_scan",
            system_instruction=food_scan_system,
            model=foodscan_model,
        )
        results["food_scan"] = "ok" if fs_name else "failed"

        # --- Meal plan cache ---
        from meal_plan_agent import STATIC_MEAL_PLAN_INSTRUCTIONS, _render_allowed_items
        menu_items_text = _render_allowed_items(items)
        meal_plan_system = (
            STATIC_MEAL_PLAN_INSTRUCTIONS
            + "\n\nMENU (name|cal|prot|fat|carb|serving)\n"
            + menu_items_text
        )
        mp_name = await create_menu_cache(
            dining_hall=hall_slug,
            university=university,
            date=date,
            use_case="meal_plan",
            system_instruction=meal_plan_system,
            model=mealplan_model,
        )
        results["meal_plan"] = "ok" if mp_name else "failed"

        return results

    # Run all halls in parallel
    tasks = [_create_for_hall(univ, hall) for univ, hall in halls]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    created = 0
    failed = 0
    details = []
    for r in all_results:
        if isinstance(r, Exception):
            failed += 2  # both use cases failed
            details.append({"error": str(r)})
        else:
            details.append(r)
            for use_case in ("food_scan", "meal_plan"):
                if r.get(use_case) == "ok":
                    created += 1
                elif r.get(use_case) == "failed":
                    failed += 1

    logger.info("Cache creation complete: %d created, %d failed, %d stale cleaned", created, failed, cleaned)
    return {"created": created, "failed": failed, "cleaned_up": cleaned, "details": details}
```

- [ ] **Step 2: Verify the module loads without import errors**

Run: `cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend && python -c "import menu_cache; print('OK')"`

This will fail because of lazy imports from `app.py` at runtime, which is expected — the functions only import `app` when called. The module itself should parse without syntax errors.

- [ ] **Step 3: Commit**

```bash
git add menu_cache.py
git commit -m "feat: add menu_cache module for Gemini context caching"
```

---

## Chunk 2: Meal Plan Agent Restructuring

### Task 3: Split meal plan system instructions into static and dynamic parts

**Files:**
- Modify: `meal_plan_agent.py:31-84` (BASE_SYSTEM_INSTRUCTIONS_TEMPLATE)
- Modify: `meal_plan_agent.py:87-136` (PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE)
- Modify: `meal_plan_agent.py:342-411` (_build_agent_prompt)

The key change: extract per-user placeholders (`{cal_min}`, `{cal_max}`, `{cal_target}`, `{prot_min}`, `{prot_max}`, `{prot_target}`) out of the system instructions and into the user prompt. Also move the MENU section from the user prompt into the system instruction.

- [ ] **Step 1: Replace `BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` with `STATIC_MEAL_PLAN_INSTRUCTIONS`**

In `meal_plan_agent.py`, replace the entire `BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` (lines 31-84) with a new constant that has NO per-user placeholders:

```python
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
```

Keep the old `BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` temporarily as `_LEGACY_BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` (for reference during testing) — delete it once the restructuring is verified.

- [ ] **Step 2: Create a similar `STATIC_PARTIAL_MEAL_PLAN_INSTRUCTIONS` constant**

Replace `PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE` (lines 87-136). The partial version omits per-user placeholders and uses generic references to "the meals specified in the user prompt" instead of `{meal_list}` and `{meal_count}`:

```python
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
```

- [ ] **Step 3: Update `_build_agent_prompt` to include CONSTRAINTS section and remove MENU**

Modify `_build_agent_prompt` (line 342) to:
1. Accept new `cal_min`, `cal_max`, `prot_min`, `prot_max` keyword args
2. Accept new `include_meals` and `output_schema_hint` keyword args (for partial plans)
3. Add a CONSTRAINTS section to the user prompt with calorie/protein ranges
4. Remove the MENU section (menu goes into system instruction now)

```python
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
```

- [ ] **Step 4: Verify the module still parses**

Run: `cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend && python -c "from meal_plan_agent import STATIC_MEAL_PLAN_INSTRUCTIONS; print('OK, len:', len(STATIC_MEAL_PLAN_INSTRUCTIONS))"`

Expected: `OK, len:` followed by a number (~1500-1800 chars).

- [ ] **Step 5: Commit**

```bash
git add meal_plan_agent.py
git commit -m "feat: split meal plan system instructions into static/dynamic parts"
```

---

### Task 4: Update `generate_meal_plan_with_agent` to use `system_instruction` param and accept `cached_content`

**Files:**
- Modify: `meal_plan_agent.py:414-604` (generate_meal_plan_with_agent)

- [ ] **Step 1: Update function signature and prompt construction**

Add `cached_content: Optional[str] = None` parameter. Restructure the Gemini call to use `system_instruction` param instead of concatenating into `contents`.

In `generate_meal_plan_with_agent` (line 414), change the signature:

```python
async def generate_meal_plan_with_agent(
  *,
  user_payload: Dict[str, Any],
  menu_payload: Dict[str, Any],
  nutrition_targets: Dict[str, Any],
  extra_system_messages: Optional[Sequence[str]] = None,
  user_preferences_context: Optional[str] = None,
  exclude_meals: Optional[List[str]] = None,
  cached_content: Optional[str] = None,  # NEW: Gemini cache handle
) -> Tuple[DormdishMealPlannerSchema, str]:
```

Then update the body. The key changes in the function:

1. Build `static_system_instruction` from `STATIC_MEAL_PLAN_INSTRUCTIONS` + menu items (only needed when `cached_content` is None).
2. Pass `cal_min`/`cal_max`/`prot_min`/`prot_max` to `_build_agent_prompt`.
3. Use `system_instruction` param in `GenerateContentConfig` instead of concatenating.
4. When `cached_content` is set, use `cached_content` in config and omit `system_instruction`.
5. On retry, append `_REPAIR_INSTRUCTIONS` to `contents` instead of `system_instruction` when using cached content.

**IMPORTANT: Preserve the existing code block at lines 458-507** that computes `cal_target`, `prot_target`, `cal_min`, `cal_max`, `prot_min`, `prot_max`, `include_meals`, `meal_count`, `meal_list`, `output_schema`, `instructions`, and `response_schema`. These variables are still needed. The replacement below goes AFTER that block, replacing lines ~442-456 (the old `_build_agent_prompt` call) and lines ~509-534 (the old Gemini call with concatenated contents):

Replace the `_build_agent_prompt` call and Gemini call section with:

```python
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

    # ... rest of retry/overload logic stays the same ...
```

The overload retry loop (lines 536-553), response parsing (lines 555-600), and totals recalculation all remain unchanged.

After the response is received, add observability logging:

```python
    # Log caching info
    if hasattr(response, 'usage_metadata'):
      usage = response.usage_metadata
      cached_tokens = getattr(usage, 'cached_content_token_count', 0)
      if cached_tokens:
        print(f"[meal_plan_agent] Cache hit: {cached_tokens} tokens served from cache")
```

Add this right after `raw_response = response.text or ""` (around line 558).

- [ ] **Step 2: Verify the module parses**

Run: `cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend && python -c "from meal_plan_agent import generate_meal_plan_with_agent; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add meal_plan_agent.py
git commit -m "feat: restructure non-streaming meal plan agent for context caching"
```

---

### Task 5: Update `generate_meal_plan_with_streaming` with same restructuring

**Files:**
- Modify: `meal_plan_agent.py:626-931` (generate_meal_plan_with_streaming)

Apply the identical restructuring as Task 4 to the streaming function. The changes mirror Task 4:

- [ ] **Step 1: Update function signature**

Add `cached_content: Optional[str] = None` to the signature at line 626.

- [ ] **Step 2: Restructure prompt construction and Gemini call**

Replace the prompt/instructions construction (approximately lines 659-755) with the same pattern as Task 4. Key differences from the non-streaming version:
- Uses `generate_content_stream` instead of `generate_content`
- Does NOT use `thinking_config` (already absent in current streaming code)
- The config_kwargs should NOT include `thinking_config`

```python
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
```

- [ ] **Step 3: Update the non-streaming fallback call**

At line 912, the streaming function falls back to `generate_meal_plan_with_agent`. Pass `cached_content` through:

```python
    result, raw_response = await generate_meal_plan_with_agent(
      user_payload=user_payload,
      menu_payload=menu_payload,
      nutrition_targets=nutrition_targets,
      extra_system_messages=extra_system_messages,
      user_preferences_context=user_preferences_context,
      exclude_meals=exclude_meals,
      cached_content=cached_content,  # Pass through
    )
```

- [ ] **Step 4: Verify the module parses**

Run: `cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend && python -c "from meal_plan_agent import generate_meal_plan_with_streaming; print('OK')"`

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add meal_plan_agent.py
git commit -m "feat: restructure streaming meal plan agent for context caching"
```

---

## Chunk 3: Food Scan Agent + Endpoint Integration

### Task 6: Update `food_scan_agent.py` to accept `cached_content`

**Files:**
- Modify: `food_scan_agent.py:105-186` (analyze_food_photo function)

- [ ] **Step 1: Add `cached_content` parameter and conditional config**

Update the function signature at line 105:

```python
async def analyze_food_photo(
    image_data: bytes,
    menu_json: str,
    dining_hall: str,
    date: str,
    description: str | None = None,
    cached_content: str | None = None,
) -> dict:
```

Update the `_call_gemini` inner function (line 165) to conditionally use `cached_content`:

```python
        # Build config — system_instruction and cached_content are mutually exclusive
        config_kwargs = dict(
            response_mime_type="application/json",
            response_schema=FoodScanGeminiSchema,
            temperature=attempt_temperature,
            max_output_tokens=max_tokens,
        )
        if cached_content and attempt == 1:
            # Use cached content on first attempt; fall back to system_instruction on retry
            config_kwargs["cached_content"] = cached_content
        else:
            config_kwargs["system_instruction"] = attempt_system

        def _call_gemini():
            return client.models.generate_content(
                model=model_name,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(
                                data=image_data, mime_type="image/jpeg"
                            ),
                            types.Part.from_text(text=user_prompt),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(**config_kwargs),
            )
```

Note: On retry attempts (attempt > 1), fall back to `system_instruction` with the repair hint appended, since we can't modify the cached content.

After `raw_text = response.text or ""`, add cache observability logging:

```python
        # Log caching info
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            cached_tokens = getattr(usage, 'cached_content_token_count', 0)
            if cached_tokens:
                logger.info("Food scan %s: %d tokens served from cache", scan_id, cached_tokens)
```

- [ ] **Step 2: Verify the module parses**

Run: `cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend && python -c "from food_scan_agent import analyze_food_photo; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add food_scan_agent.py
git commit -m "feat: add cached_content support to food scan agent"
```

---

### Task 7: Update `food_scan_endpoints.py` to look up cache before calling agent

**Files:**
- Modify: `food_scan_endpoints.py:102-126`

- [ ] **Step 1: Add cache lookup before the Gemini call**

In `handle_food_scan_analyze`, between the menu fetch (step 4) and the Gemini call (step 5), add a cache lookup:

```python
    # ── 4b. Look up Gemini cache handle (optional optimization) ──
    cache_name = None
    try:
        from menu_cache import get_cache_name
        from food_scan_menu import resolve_dining_hall
        university, hall_slug = resolve_dining_hall(dining_hall)
        cache_name = await get_cache_name(
            dining_hall=hall_slug,
            university=university,
            date=date,
            use_case="food_scan",
        )
    except Exception as e:
        logger.debug(f"Cache lookup skipped: {e}")
```

Then update the `analyze_food_photo` call to pass `cached_content`:

```python
        result = await analyze_food_photo(
            image_data=image_data,
            menu_json=menu_info["menu_json"],
            dining_hall=dining_hall,
            date=date,
            description=description,
            cached_content=cache_name,
        )
```

- [ ] **Step 2: Verify the module parses**

Run: `cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend && python -c "from food_scan_endpoints import handle_food_scan_analyze; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add food_scan_endpoints.py
git commit -m "feat: add cache lookup to food scan endpoint"
```

---

## Chunk 4: App.py Integration (Cron + Meal Plan Endpoints)

### Task 8: Add cache creation to cron endpoint

**Files:**
- Modify: `app.py:2697-2714` (cron_refresh_menus function)

- [ ] **Step 1: Add cache creation after menu refresh loop**

After the existing menu refresh loop (line 2704) and before the return statement (line 2709), add:

```python
    # Create Gemini context caches for refreshed halls
    cache_summary = {"created": 0, "failed": 0, "cleaned_up": 0}
    try:
        from menu_cache import create_caches_for_date
        # Build refreshed_halls from the cron loop's ALL_DINING_HALLS tuples.
        # NOTE: fetch_and_cache_menu returns {"success": bool, "hall": str, "items": int}
        # — it does NOT include "university", so we derive it from ALL_DINING_HALLS.
        refreshed_halls = [
            (university, hall_slug)
            for idx, (university, hall_slug, _is_foodpro) in enumerate(ALL_DINING_HALLS)
            if idx < len(results) and results[idx].get("success")
        ]
        if refreshed_halls:
            cache_summary = await create_caches_for_date(rutgers_date, refreshed_halls)
            print(f"🗄️ [CRON] Gemini caches: {cache_summary['created']} created, {cache_summary['failed']} failed, {cache_summary['cleaned_up']} stale cleaned")
    except Exception as e:
        print(f"⚠️ [CRON] Gemini cache creation failed (non-fatal): {e}")
```

Also update the return value to include cache info:

```python
    return {
        "success": True,
        "message": f"Refreshed {success_count}/{len(ALL_DINING_HALLS)} menus",
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
        "cache_summary": cache_summary,
    }
```

- [ ] **Step 2: Commit**

```bash
git add app.py
git commit -m "feat: add Gemini cache creation to cron endpoint"
```

---

### Task 9: Add cache lookup to meal plan endpoints

**Files:**
- Modify: `app.py:4270-4278` (generate_meal_plan_route — non-streaming call)
- Modify: `app.py:5168-5174` (generate_meal_plan_stream_route — streaming call)
- Also modify the single-meal variant calls at `app.py:4755` and `app.py:5490`

- [ ] **Step 1: Add cache lookup before non-streaming meal plan call**

Before the `generate_meal_plan_with_agent` call at line 4272, add a cache lookup. You'll need access to the dining hall slug and university — these should be available from the request processing earlier in the function. Find where `agent_menu_payload` is constructed to get the hall slug.

```python
        # Look up Gemini cache handle
        cache_name = None
        try:
            from menu_cache import get_cache_name
            _mp_hall = agent_menu_payload.get("requested_dining_hall", "")
            _mp_univ = "rutgers"  # Currently all active halls are Rutgers
            from app import DINING_HALL_MAPPING
            _mapping = DINING_HALL_MAPPING.get(_mp_hall.strip().lower())
            if _mapping:
                _mp_univ, _mp_hall = _mapping
            cache_name = await get_cache_name(
                dining_hall=_mp_hall,
                university=_mp_univ,
                date=today,
                use_case="meal_plan",
            )
        except Exception as e:
            logger.debug(f"Meal plan cache lookup skipped: {e}")
```

Then add `cached_content=cache_name` to the call:

```python
            agent_result, agent_raw_text = await generate_meal_plan_with_agent(
                user_payload=agent_user_payload,
                menu_payload=agent_menu_payload,
                nutrition_targets=agent_nutrition_targets,
                user_preferences_context=user_preferences_context,
                exclude_meals=meal_plan_request.exclude_meals,
                cached_content=cache_name,
            )
```

- [ ] **Step 2: Add same cache lookup before streaming meal plan call**

Apply the same pattern before the `generate_meal_plan_with_streaming` call at line 5168. Add `cached_content=cache_name` to the call.

- [ ] **Step 3: Add same cache lookup before single-meal variant calls**

Apply to the calls at lines 4755 and 5490. These are the same `generate_meal_plan_with_agent` / `generate_meal_plan_with_streaming` functions called for single-meal generation.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add cache lookup to meal plan endpoints"
```

---

## Chunk 5: Verification and Cleanup

### Task 10: End-to-end verification

- [ ] **Step 1: Verify all modules load without errors**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend
python -c "
from menu_cache import create_menu_cache, get_cache_name, create_caches_for_date
from meal_plan_agent import STATIC_MEAL_PLAN_INSTRUCTIONS, generate_meal_plan_with_agent, generate_meal_plan_with_streaming
from food_scan_agent import analyze_food_photo
print('All imports OK')
print('Static instructions length:', len(STATIC_MEAL_PLAN_INSTRUCTIONS))
"
```

Expected: `All imports OK` followed by instructions length.

- [ ] **Step 2: Verify the app starts**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend
python -c "from app import app; print('App loads OK')"
```

Expected: `App loads OK` (may print some initialization logs).

- [ ] **Step 3: Delete legacy template constants**

If `_LEGACY_BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` was kept for reference in Task 3, delete it now.

Also delete `BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` and `PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE` if they are no longer referenced anywhere. Check with grep:

```bash
grep -rn "BASE_SYSTEM_INSTRUCTIONS_TEMPLATE\|PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE" *.py
```

If they appear only in their definition (not imported elsewhere), remove them.

**Do NOT delete** `STATIC_MEAL_PLAN_INSTRUCTIONS`, `STATIC_PARTIAL_MEAL_PLAN_INSTRUCTIONS`, or `_render_allowed_items` — these are imported by `menu_cache.py`'s `create_caches_for_date` function.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete Gemini context caching implementation"
```

---

### Task 11: Manual integration test (when ready)

This task is for manual verification once deployed. Not automated.

- [ ] **Step 1: Trigger cron menu refresh**

Call the cron endpoint with the secret:
```
POST /cron/refresh-menus?secret=YOUR_CRON_SECRET
```

Verify the response includes `"cache_summary"` with `"created"` > 0.

- [ ] **Step 2: Test food scan with cached context**

Call `/food-scan/analyze` with a food photo. Check server logs for:
- `"Cache hit: food_scan_..."` — cache lookup succeeded
- `"X tokens served from cache"` — Gemini used the cached context

- [ ] **Step 3: Test meal plan generation with cached context**

Call `/generate-meal-plan`. Check server logs for:
- `"Cache hit: meal_plan_..."` — cache lookup succeeded
- `"X tokens served from cache"` — Gemini used the cached context

- [ ] **Step 4: Verify fallback works**

Delete the `gemini_caches` rows from Supabase, then repeat steps 2 and 3. Verify:
- `"Cache miss: ..."` in logs
- Endpoints still return correct results (fallback to system_instruction)
