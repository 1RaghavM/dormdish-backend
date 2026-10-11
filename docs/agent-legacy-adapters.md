# Legacy generation adapters

`POST /generate-meal-plan`, `/generate-meal`, `/generate-meal-plan-stream`, `/generate-meal-stream`
keep their request bodies, auth, limits, response JSON and SSE shapes. Behind a backend flag they
call the same shared agent generation as the conversation (`dormdish_agent` runtime and read tools)
instead of Gemini.

## Flag and rollback

- `DORMDISH_AGENT_GENERATION_ENABLED` (default `false`). Agent generation runs only when it is exactly
  `true` **and** `DORMDISH_AGENT_KILL_SWITCH` is not `true`. Read once per request.
- Off (any other value): the routes run the existing Gemini code unchanged. This is the rollback path.
- On: no Gemini call and no fallback to Gemini, before or after a write (section 12 specifies none).

## What is shared

`dormdish_agent/legacy.py` is the one adapter used by all four routes. Per meal it builds the same
`FoodContext` as the conversation (`routes.build_context`: trusted users row, hard restrictions and
allergens, `user_preferences`, `agent_daily_targets`), then runs `AgentTurn` with a synthetic
`TurnRequest` (`context = {dining_hall, date, meal_type}`). The model must use `retrieve_menu`,
`read_intake`, and `preview_meal`; only the server-validated `MealProposal` (canonical menu items,
nutrition recomputed from menu data, hard restrictions enforced) becomes the legacy meal. Model text
never supplies items or nutrition. No conversation, run, or agent quota rows are written.

Test seams (stubs replace the model boundary): `dormdish_agent.legacy.AgentTurn`,
`dormdish_agent.legacy.AsyncOpenAI`, `dormdish_agent.legacy.build_context`.

## Request → agent inputs

| Route | Meals | `dining_hall` / `date` | Per-meal target hint in request text |
| --- | --- | --- | --- |
| `/generate-meal-plan`, `-stream` | breakfast, lunch, dinner minus `exclude_meals` (case-insensitive) | from body; `date` must be `YYYY-MM-DD` | `_calculate_meal_targets(daily, {}, meal)` from the route's existing daily targets |
| `/generate-meal`, `-stream` | `meal` | from body; `date` must be `YYYY-MM-DD` | existing `meal_targets` |

`restaurant` (Atrium) is enforced by filtering the agent's menu snapshot to that restaurant before
selection. Hall must exist for the user's university in `universities_config.json`.

## Budget

One budget per HTTP request, shared across all meals, run sequentially:

- deadline 45 s total (each meal's `AgentTurn` gets the remaining time);
- model calls: 8 for one meal, 16 total for a multi-meal plan (each meal still capped at 8);
- validation attempts: one per meal plus **one** repair for the whole request.

Exhausting any bound fails the whole request before any write.

## Agent result → legacy shapes

`meal_plan = {meals: {<meal>: {description, items}}, totals, explanation}`. Items are
`MealItem.model_dump()` (`menu_item_id, name, serving_size, servings, calories, protein_g, fat_g,
carbs_g`; nutrition already includes servings) — the same item shape the agent's controlled save
writes. `totals` are recomputed sums over all returned meals. `description`/`explanation` are the
agent's text. The legacy fuzzy-name post-processing is skipped (it could drop items and leave stale
totals); `meta.dropped_unknown` is never set. Everything else the routes add afterwards is unchanged:
`goals`, `user_stats`, `preferences_snapshot`, `nutrition_targets`, `daily_targets`,
`already_planned`, `available_items`, `generation_id`, `ask_feedback`, `processing_info`,
`meta.agent_raw_response` (agent text), `saved_plan_id`, `auto_saved`.

Streams: each real tool status becomes `{"type":"thinking","content":<status message>}` (the single-meal
route's own initial thinking frame is kept). Completion is `{"type":"complete","meal_plan":...,
"raw_response":<agent text>}` plus the route's existing additions and autosave fields.

## Autosave and dates

Autosave uses each route's existing `saved_meal_plans` insert (same row shape, same `auto_saved`
semantics, same referral check). In the agent path the saved row's `date` (and the stream payload's
`date`) is the **requested** date, not today. `/generate-meal-plan`'s existing one-plan-per-day
check still checks today and, in the agent path, also the requested date (no plan can be generated
for a date that already has one). In the agent path `/generate-meal-plan-stream` applies the same
today/requested-date check with the same 409 before the stream opens (flag off: no check, as today).
The 45 s deadline includes the per-meal context reads. Single-meal routes already check the requested date.

## Errors

Failures before the write use each contract's existing shape: non-stream `500 {"detail":"Please try
again."}`; stream `data: {"type":"error","error":"Please try again."}`. This includes unsupported
dietary profiles, unknown hall/campus, non-`YYYY-MM-DD` dates (e.g. `today`), missing/stale agent
menu, infeasible constraints (no validated proposal), deadline/budget exhaustion and model errors.
Pre-generation checks (401, 409/429 limits, 422 meal name, legacy menu 400/404) are unchanged and
happen before the agent runs. A failed insert after generation returns success with
`auto_saved:false`, as today; nothing is retried or regenerated.

## Not preserved

- Gemini-specific fields have agent equivalents only (`raw_response` is agent text).
- Infeasible requests are a generic error rather than a partial plan.
- Generation retries after a failure are bounded only by the existing per-day limits (no agent quota).
