# Per-Meal Generation Design

## Problem

Users eat at different dining halls for different meals. The current system generates a full-day plan (breakfast + lunch + dinner) from a single dining hall. Users need to generate each meal independently, choosing a different dining hall (and optionally Atrium restaurant) per meal.

## Decision: New Per-Meal Endpoint (Approach A)

Add new `POST /generate-meal` and `POST /generate-meal-stream` endpoints. The existing `/generate-meal-plan` and `/generate-meal-plan-stream` remain unchanged.

## Request Model

```python
class SingleMealRequest(BaseModel):
    meal: str              # "breakfast", "lunch", or "dinner"
    dining_hall: str
    date: str
    restaurant: Optional[str] = None  # For Atrium sub-restaurant
```

## Response Shape

Same structure as existing meal plan response, but `meals` only contains the requested meal. Adds `daily_targets` and `already_planned` for cross-meal visibility.

```json
{
  "success": true,
  "meal_plan": {
    "totals": {"calories": 650, "protein_g": 50, "fat_g": 22, "carbs_g": 75},
    "meals": {
      "breakfast": {"description": "...", "items": [...]}
    },
    "explanation": "..."
  },
  "nutrition_targets": {"calories": 650, "protein_g": 50, "fat_g": 22, "carbs_g": 75},
  "daily_targets": {"calories": 2000, "protein_g": 150, "fat_g": 65, "carbs_g": 250},
  "already_planned": {
    "lunch": {"calories": 700, "protein_g": 55, "fat_g": 23, "carbs_g": 87}
  },
  "generation_id": "...",
  "auto_saved": true
}
```

## Cross-Meal Tracking

When generating a meal, the backend:

1. Calculates user's full daily targets (BMR/TDEE-based)
2. Queries `saved_meal_plans` for individual meals already generated for this user+date
3. Subtracts already-generated totals from daily targets
4. Splits remainder proportionally across remaining meals

Default meal proportions: `{"breakfast": 0.30, "lunch": 0.35, "dinner": 0.35}`

Example: No meals generated yet, user requests breakfast. Remaining budget is full daily targets. Breakfast gets `0.30 / (0.30 + 0.35 + 0.35) = 30%` of daily targets.

Example: Breakfast already done (600 cal used), user requests lunch. Remaining = daily - 600. Lunch gets `0.35 / (0.35 + 0.35) = 50%` of remaining.

## Rate Limiting

3 generations per day, one per meal type. Check `saved_meal_plans` for `user_id + date` where `meal_plan_data.meal` matches the requested meal. Reject with 429 if exists.

## Endpoint Flow

1. Authenticate user, calculate BMR/TDEE/daily targets
2. Fetch menu for specified dining hall (+ filter by restaurant if Atrium)
3. Query existing individual meal plans for user+date
4. Calculate this meal's targets via proportional split
5. Call `generate_meal_plan_with_agent()` with `exclude_meals` excluding the other two meals
6. Leverages existing `PARTIAL_SYSTEM_INSTRUCTIONS_TEMPLATE` and `build_partial_meals_schema()`
7. Post-process, validate, auto-save with `meal` field tagged

## LLM Integration

No new prompt template needed. The existing partial plan system handles single-meal generation. The only change is passing adjusted nutrition targets (this meal's proportional share).

`dining_context` in `_build_agent_prompt` already includes `dining_hall` and `restaurant`.

## Database Storage

No schema migration. Individual meals stored in `saved_meal_plans` with a `meal` field in the JSON `meal_plan_data`:

```json
{
  "meal_plan_data": {
    "meal": "breakfast",
    "dining_hall": "busch-dining-hall",
    "restaurant": null,
    "meal_plan": {...},
    "nutrition_targets": {...}
  }
}
```

Full-day plans (from existing endpoint) have no `meal` field, so old and new coexist.

## Backward Compatibility

- `/generate-meal-plan` and `/generate-meal-plan-stream` remain unchanged
- No changes to existing request/response models
- Old saved plans unaffected (no `meal` field = full-day plan)

## Files to Modify

| File | Changes |
|------|---------|
| `app.py` | New `SingleMealRequest` model, `POST /generate-meal`, `POST /generate-meal-stream`, meal proportion constants, cross-meal tracking query |
| `meal_plan_agent.py` | No changes (existing partial plan system handles it) |

## Streaming

`POST /generate-meal-stream` mirrors `POST /generate-meal-plan-stream` with the same SSE format (`thinking`, `complete`, `error` event types). Same flow as non-streaming but returns `StreamingResponse`.
