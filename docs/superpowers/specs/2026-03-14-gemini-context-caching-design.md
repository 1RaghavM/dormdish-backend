# Gemini Context Caching for Menu Data

**Date:** 2026-03-14
**Status:** Draft
**Problem:** Every food scan and meal plan request sends the full dining hall menu (2,000-5,000 tokens) to Gemini, burning tokens on identical data that only changes once per day.

## Goal

Reduce Gemini input token costs by 50-75% on menu-related endpoints by caching the static portions (system instructions + menu data) using Gemini's context caching APIs.

## Background

Two endpoints call Gemini with embedded menu data:

1. **Food Scan** (`/food-scan/analyze`) — Gemini receives the full menu JSON in `system_instruction` alongside a food photo. The system instruction is identical for all requests to the same dining hall on the same day.

2. **Meal Plan** (`/generate-meal-plan`) — Gemini receives system instructions + menu items + user-specific data (calorie targets, profile, preferences) concatenated into a single `contents` string. Per-user targets in the system instructions destroy prefix sharing.

Menus refresh once daily at 5 AM ET via cron. Deployment is Vercel serverless (no shared memory across instances).

## Caching Mechanisms

### Implicit Caching (automatic)

Gemini 2.5 Flash automatically caches request prefixes when:
- Requests share a common prefix with previous requests
- The shared prefix exceeds 1,024 tokens (minimum for Flash)

Cached tokens are billed at a reduced rate (check current Gemini pricing for exact discount — historically 75-90% off input token price). Requires only prompt restructuring — no infrastructure changes.

### Explicit Caching (managed)

The Gemini API allows creating persistent cache objects via `client.caches.create()`. These are referenced by name in subsequent `generate_content` calls:
- Same reduced rate as implicit caching (guaranteed on every request)
- Configurable TTL (must explicitly pass `ttl='86400s'` for 24 hours; default is only 1 hour)
- Small storage cost (~$1.00/M tokens/hour — negligible for our payload sizes, ~$0.001/day for all halls)
- Cache lives on Google's servers (works with serverless)
- Minimum cached content: 1,024 tokens for Gemini 2.5 Flash (our payloads of 2,500-6,000 tokens comfortably exceed this)

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
# Note: system_instruction param is NOT used currently
```

**New structure:**
```python
# Static system instruction: general rules + menu items (same for all users of same hall+date)
system_instruction = STATIC_MEAL_PLAN_INSTRUCTIONS + "\n\n" + menu_items_text

# Per-user content: targets, profile, preferences (includes calorie/protein constraints)
contents = user_specific_prompt
```

Split `BASE_SYSTEM_INSTRUCTIONS_TEMPLATE` into:
- **Static part** (`STATIC_MEAL_PLAN_INSTRUCTIONS`) — meal planning rules, meal structure guidelines, optimization strategy, output format, explanation rules, example calculations. No per-user placeholders.
- **Dynamic part** — calorie/protein targets and margins (`cal_min`/`cal_max`/`prot_min`/`prot_max`), dietary preferences. Moved into the user prompt built by `_build_agent_prompt`.

The same split applies to `PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE` for partial meal plans.

The `_build_agent_prompt` function already builds the user-specific sections (PROFILE, TARGETS, CONTEXT, USER PREFERENCES, MENU). The change:
- Moves MENU out of the user prompt and into the system instruction (it's static per hall+date)
- Moves target constraints from system instructions into the user prompt (they're per-user)
- Adds a new CONSTRAINTS section to the user prompt containing the calorie/protein ranges

**Important note:** The meal plan agent does not currently use the `system_instruction` parameter at all — it concatenates everything into `contents`. Introducing `system_instruction` is a behavioral change (Gemini may weight instructions differently). Regression testing of meal plan quality is recommended.

**Both `generate_meal_plan_with_agent` AND `generate_meal_plan_with_streaming` must be updated** — they have independent copies of the prompt construction and Gemini call logic. The streaming function (line 626 of `meal_plan_agent.py`) has the same concatenation pattern at line 748.

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
    university: str,
    date: str,
    use_case: str,
) -> str | None:
    """Look up a cache handle from Supabase. Returns None if not found/expired.
    Filters by expires_at > NOW() to skip stale rows."""

async def create_caches_for_date(date: str) -> dict:
    """Create caches for all dining halls for a given date.
    Called after cron menu refresh completes.

    Data flow: reads menu_data from Supabase menus table for each hall,
    builds the static system instruction for each use case,
    then creates the Gemini cache.

    Uses asyncio.gather() for parallel cache creation to stay within
    Vercel cron timeout limits."""
```

**Cache creation API usage:**
```python
from google.genai import types

cached_content = client.caches.create(
    model='gemini-2.5-flash',
    config=types.CreateCachedContentConfig(
        system_instruction=system_instruction_text,
        display_name=f'{use_case}_{dining_hall}_{date}',
        ttl='86400s',  # Must be explicit — default is only 1 hour
    ),
)
cache_name = cached_content.name  # Store this in Supabase
```

**Cache lookup flow:**
1. Query Supabase for `gemini_caches` where `dining_hall`, `university`, `date`, and `use_case` match AND `expires_at > NOW()`
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

CREATE INDEX idx_gemini_caches_lookup ON gemini_caches (dining_hall, university, date, use_case);
```

Stale rows (past `expires_at`) are cleaned up during the daily cron run before creating new caches.

### 5. Integration: Cron Endpoint

In `app.py`, after the existing menu refresh loop completes successfully:

```python
# After all menus refreshed
from menu_cache import create_caches_for_date
cache_results = await create_caches_for_date(target_date)
```

This iterates through all dining halls that were successfully refreshed and creates two caches per hall (food_scan + meal_plan). Uses `asyncio.gather()` internally for parallel creation to minimize cron execution time (each Gemini cache creation takes 2-5 seconds; sequential creation for 8 caches could exceed Vercel timeout).

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
- If `cached_content` is provided: set `cached_content=cached_content` in config, **omit** `system_instruction` (they are mutually exclusive in the Gemini API — setting both raises an error)
- If not provided: use `system_instruction` as before (implicit caching fallback)
- `response_mime_type` and `response_schema` are compatible with `cached_content` and remain unchanged

### 7. Integration: Meal Plan Agent

In `meal_plan_agent.py`, both `generate_meal_plan_with_agent` and `generate_meal_plan_with_streaming` gain an optional `cached_content` parameter.

The `call_gemini` inner function changes to:
```python
config_kwargs = dict(
    temperature=attempt_temperature,
    max_output_tokens=max_tokens,
    response_mime_type="application/json",
    response_schema=response_schema,
)

# Preserve thinking_config for non-streaming (streaming does not use it)
if not is_streaming:
    config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

# system_instruction and cached_content are mutually exclusive
if cached_content:
    config_kwargs["cached_content"] = cached_content
else:
    config_kwargs["system_instruction"] = static_instructions

config = types.GenerateContentConfig(**config_kwargs)
```

**Retry behavior with cached content:** On retry attempts, the current code appends `_REPAIR_INSTRUCTIONS` to the system instructions. Since `system_instruction` cannot be set alongside `cached_content`, repair instructions are appended to `contents` (the user prompt) instead when using cached content. This maintains the retry mechanism without breaking the caching constraint.

### 8. Integration: App Endpoints

In `app.py`, before calling the agent functions:

```python
from menu_cache import get_cache_name

# Look up cache (non-blocking, returns None on any failure)
cache_name = await get_cache_name(
    dining_hall=hall_slug, university=university, date=date, use_case="food_scan"
)

# Pass to agent — None is fine, agent falls back to non-cached
result = await analyze_food_photo(..., cached_content=cache_name)
```

Same pattern for the meal plan endpoint with `use_case="meal_plan"`.

## Observability

- Log whether a cache handle was found vs. missed for each request
- Log the `cached_content_token_count` from Gemini's response `usage_metadata` to verify caching is working
- Log cache creation success/failure counts in the cron endpoint response

## Fallback Strategy

Every integration point treats caching as optional:
1. If explicit cache exists -> use it (guaranteed discount)
2. If explicit cache missing -> send full prompt with `system_instruction` (implicit caching may still apply)
3. If Supabase lookup fails -> send full prompt (no request ever fails due to caching)

Caching is a pure cost optimization with zero impact on correctness or availability.

## Files Changed

| File | Change |
|---|---|
| `menu_cache.py` (new) | Gemini cache management: create, lookup, cleanup |
| `meal_plan_agent.py` | Split system instructions into static/dynamic; use `system_instruction` param; accept `cached_content`; update both streaming and non-streaming functions |
| `food_scan_agent.py` | Accept `cached_content` param; conditionally omit `system_instruction` |
| `food_scan_prompts.py` | No change needed (already structured correctly) |
| `app.py` | Cron: call cache creation + stale cleanup. Endpoints: look up cache handle |
| Supabase migration | Create `gemini_caches` table |

## What Doesn't Change

- Menu fetching/scraping logic
- Food scan validation, models, response format
- Meal plan validation, schemas, response format
- User-facing API contracts (no endpoint signature changes)
- Retry/backoff logic (same retry counts and backoff, repair instructions moved to contents when cached)

## Expected Savings

- Menu data per hall: ~2,000-5,000 input tokens per request
- System instructions: ~500-1,000 additional tokens
- Cached tokens billed at reduced rate (check current Gemini pricing; historically 75-90% discount)
- Conservative estimate: **50-75% reduction in input token costs** across both endpoints
- With explicit caching: savings are guaranteed on every request
- With implicit caching fallback: savings are best-effort but still significant
- Cache storage cost: ~$0.001/day for all halls (negligible)

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Gemini API changes caching behavior/pricing | Fallback always works without caching |
| Cache creation fails during cron | Each request falls back to full prompt; logged for debugging |
| `system_instruction` param changes meal plan quality | Regression test meal plan output before/after restructuring |
| Vercel cron timeout with added cache creation | Parallel cache creation via `asyncio.gather()` |
| Stale `gemini_caches` rows accumulate | Cleanup step in cron deletes expired rows |
