# Gemini Context Caching for Menu Data

**Date:** 2026-03-14
**Status:** Draft
**Problem:** Every food scan and meal plan request sends the full dining hall menu (2,000-5,000 tokens) to Gemini, burning tokens on identical data that only changes once per day.

## Goal

Reduce Gemini input token costs by 50-70% on menu-related endpoints by caching the static portions (system instructions + menu data) using Gemini's context caching APIs.

## Background

Two endpoints call Gemini with embedded menu data:

1. **Food Scan** (`/food-scan/analyze`) — Gemini receives the full menu JSON in `system_instruction` alongside a food photo. The system instruction is identical for all requests to the same dining hall on the same day.

2. **Meal Plan** (`/generate-meal-plan`) — Gemini receives system instructions + menu items + user-specific data (calorie targets, profile, preferences) concatenated into a single `contents` string. Per-user targets in the system instructions destroy prefix sharing.

Menus refresh once daily at 5 AM ET via cron. Deployment is Vercel serverless (no shared memory across instances).

## Caching Mechanisms

### Implicit Caching (automatic)

Gemini 2.5 Flash automatically caches request prefixes at a 75% token discount when:
- Requests share a common prefix with previous requests
- The shared prefix exceeds 1,024 tokens

Requires only prompt restructuring — no infrastructure changes.

### Explicit Caching (managed)

The Gemini API allows creating persistent cache objects via `client.caches.create()`. These are referenced by name in subsequent `generate_content` calls:
- 75% token discount (guaranteed)
- Configurable TTL (we use 24 hours)
- Cache lives on Google's servers (works with serverless)

## Design

### 1. Prompt Restructuring (Meal Plan)

**Current structure** in `meal_plan_agent.py`:
```python
# System instructions contain per-user calorie/protein targets
instructions = BASE_SYSTEM_INSTRUCTIONS_TEMPLATE.format(
    cal_min=cal_min, cal_max=cal_max, cal_target=cal_target,
    prot_min=prot_min, prot_max=prot_max, prot_target=prot_target,
)
# Everything concatenated into contents — unique per user
contents = f"{instructions}\n\n{prompt}"
```

**New structure:**
```python
# Static system instruction: general rules + menu items (same for all users of same hall+date)
system_instruction = STATIC_MEAL_PLAN_INSTRUCTIONS + "\n\n" + menu_items_text

# Per-user content: targets, profile, preferences
contents = user_specific_prompt
```

Split `BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` into:
- **Static part** — meal planning rules, meal structure guidelines, optimization strategy, output format, explanation rules. No per-user placeholders.
- **Dynamic part** — calorie/protein targets, dietary preferences. Moved into the user prompt built by `_build_agent_prompt`.

The same split applies to `PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE` for partial meal plans.

The `_build_agent_prompt` function already builds the user-specific sections (PROFILE, TARGETS, CONTEXT, USER PREFERENCES, MENU). The change moves MENU out of the user prompt and into the system instruction, and moves the target constraints from system instructions into the user prompt.

### 2. Prompt Restructuring (Food Scan)

`food_scan_agent.py` already uses the `system_instruction` parameter correctly. The menu JSON + static instructions go into `system_instruction`, and the image + user text go into `contents`. No restructuring needed — implicit caching should already apply.

The only change is plumbing an optional `cached_content` name through to the `GenerateContentConfig`.

### 3. New Module: `menu_cache.py`

Manages Gemini explicit cache lifecycle.

**Functions:**

```python
async def create_menu_cache(
    dining_hall: str,
    university: str,
    date: str,
    use_case: str,          # "food_scan" or "meal_plan"
    system_instruction: str, # The full static system prompt to cache
    model: str,
) -> str | None:
    """Create a Gemini cached content object. Returns cache name or None on failure."""

async def get_cache_name(
    dining_hall: str,
    date: str,
    use_case: str,
) -> str | None:
    """Look up a cache handle from Supabase. Returns None if not found/expired."""

async def create_caches_for_date(date: str) -> dict:
    """Create caches for all dining halls for a given date.
    Called after cron menu refresh completes."""
```

**Cache creation flow:**
1. Build the static system instruction for the given hall+date+use_case
2. Call `client.caches.create()` with the system instruction as content, model name, display name, and 24h TTL
3. Store the returned cache `name` in Supabase `gemini_caches` table
4. Return the cache name

**Cache lookup flow:**
1. Query Supabase for `gemini_caches` where `dining_hall`, `date`, and `use_case` match
2. Return `cache_name` if found, `None` otherwise

### 4. Supabase Table: `gemini_caches`

```sql
CREATE TABLE gemini_caches (
    id TEXT PRIMARY KEY,              -- e.g. "food_scan_rutgers_busch-dining-hall_2026-03-14"
    cache_name TEXT NOT NULL,         -- Gemini cache handle string
    model TEXT NOT NULL,              -- e.g. "gemini-2.5-flash"
    university TEXT NOT NULL,
    dining_hall TEXT NOT NULL,
    date TEXT NOT NULL,
    use_case TEXT NOT NULL,           -- "food_scan" or "meal_plan"
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_gemini_caches_lookup ON gemini_caches (dining_hall, date, use_case);
```

### 5. Integration: Cron Endpoint

In `app.py`, after the existing menu refresh loop completes successfully:

```python
# After all menus refreshed
from menu_cache import create_caches_for_date
cache_results = await create_caches_for_date(target_date)
```

This iterates through all dining halls that were successfully refreshed and creates two caches per hall (food_scan + meal_plan).

### 6. Integration: Food Scan Agent

In `food_scan_agent.py`, the `analyze_food_photo` function gains an optional `cached_content` parameter:

```python
async def analyze_food_photo(
    image_data: bytes,
    menu_json: str,
    dining_hall: str,
    date: str,
    description: str | None = None,
    cached_content: str | None = None,  # NEW
) -> dict:
```

When building the `GenerateContentConfig`:
- If `cached_content` is provided: set `cached_content=cached_content` in config, omit `system_instruction` (it's in the cache)
- If not provided: use `system_instruction` as before (implicit caching fallback)

### 7. Integration: Meal Plan Agent

In `meal_plan_agent.py`, both `generate_meal_plan_with_agent` and `generate_meal_plan_with_streaming` gain an optional `cached_content` parameter.

The `call_gemini` inner function changes from:
```python
contents=f"{attempt_instructions}\n\n{prompt}"
```
To:
```python
config=types.GenerateContentConfig(
    system_instruction=static_instructions,  # Only if no cached_content
    cached_content=cached_content,           # If available
    ...
),
contents=user_specific_prompt,
```

### 8. Integration: App Endpoints

In `app.py`, before calling the agent functions:

```python
from menu_cache import get_cache_name

# Look up cache (non-blocking, returns None on any failure)
cache_name = await get_cache_name(dining_hall=hall_slug, date=date, use_case="food_scan")

# Pass to agent — None is fine, agent falls back to non-cached
result = await analyze_food_photo(..., cached_content=cache_name)
```

Same pattern for the meal plan endpoint with `use_case="meal_plan"`.

## Fallback Strategy

Every integration point treats caching as optional:
1. If explicit cache exists → use it (guaranteed 75% discount)
2. If explicit cache missing → send full prompt (implicit caching may still apply)
3. If Supabase lookup fails → send full prompt (no request ever fails due to caching)

Caching is a pure cost optimization with zero impact on correctness or availability.

## Files Changed

| File | Change |
|---|---|
| `menu_cache.py` (new) | Gemini cache management: create, lookup |
| `meal_plan_agent.py` | Restructure prompts; accept `cached_content` param |
| `food_scan_agent.py` | Accept `cached_content` param |
| `food_scan_prompts.py` | No change needed (already structured correctly) |
| `app.py` | Cron: call cache creation. Endpoints: look up cache handle |
| Supabase migration | Create `gemini_caches` table |

## What Doesn't Change

- Menu fetching/scraping logic
- Food scan validation, models, response format
- Meal plan validation, schemas, response format
- User-facing API contracts (no endpoint signature changes)
- Retry/backoff logic

## Expected Savings

- Menu data per hall: ~2,000-5,000 input tokens per request
- System instructions: ~500-1,000 additional tokens
- 75% discount on cached portion
- Conservative estimate: **50-70% reduction in input token costs** across both endpoints
- With explicit caching: savings are guaranteed on every request
- With implicit caching fallback: savings are best-effort but still significant
