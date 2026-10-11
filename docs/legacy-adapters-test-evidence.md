# Legacy generation adapters: test evidence

Owner: dedicated testing agent. Test PR: [#27](https://github.com/1RaghavM/dormdish-backend/pull/27) (base
`feat/dd-agent/legacy-adapters`, companion to [#26](https://github.com/1RaghavM/dormdish-backend/pull/26)).
Tests: `tests/test_legacy_adapters.py` (51). Contract source: `docs/agent-legacy-adapters.md` at `0651e8e`.

## Harness

Real `app.app` routes through `TestClient`, real `dormdish_agent` runtime/tools/SDK, and a real disposable
PostgreSQL database (`DD_AGENT_LOCAL_PG=1`, reuses `route_pg`/`stores`). A PostgREST-shaped client executes
every table read/write and RPC as SQL. Synthetic only: session auth, referral side effect, the Gemini
generator (`meal_plan_agent`), the Gemini cache lookup, and the OpenAI HTTP transport (patched at
`httpx.AsyncHTTPTransport`, so any construction path is covered and nothing reaches the network). The scripted
model calls `retrieve_menu`, `read_intake`, `preview_meal` (preferred names, one repair), then returns text that
falsely claims 9999 kcal. The requested date is always today + 3 days, so today-dated saves are visible.

## Coverage

- Flag off (unset and `false`): exact JSON key sets and SSE chunk shapes for all four routes, the Gemini call
  and `exclude_meals`, model-provided totals, today-dated autosave, the 409/429 limits, the stream error chunk,
  and the app's HTTP error envelope (`{"error":"HTTP error","message",...,"status_code"}`).
- Both flags: 401 and the unknown-hall 400 happen before any generation or write.
- Flag on: same key sets and SSE framing (only `thinking`/`complete`, no v1 envelope fields); no Gemini call;
  items and totals come from trusted menu data × servings (never the model text); requested-date autosave with
  `saved_plan_id`/`generation_id` matching the row; `exclude_meals`; single-meal targets equal flag-off for
  the same date, food log and already-planned meals; peanut allergen and unknown allergen metadata are never
  selected; model failure returns the existing error shapes with no write; model-call budget of ≤8 per meal and
  ≤16 for a full day; one validation repair per request; the existing 429 limit runs before the model; retries
  never double-save; a lost write reply produces no Gemini fallback and no second row; owner scoping; and the
  kill switch keeps the Gemini path.

## Results

| Run | Source | Result | Log |
| --- | --- | --- | --- |
| Red (dedicated) | base `e70decd` | 27 failed, 24 passed: assertion failures only (Gemini called, today-dated save, Gemini totals, no budget/duplicate guard) | `/private/tmp/dd-legacy-adapters-tests-red.log` |
| Full suite | base `e70decd` + tests | 27 failed, 1004 passed (980 baseline all pass) | `/private/tmp/dd-legacy-adapters-tests-base-full.log` |
| Dedicated | merged `19dd0be` | 2 failed, 49 passed | `/private/tmp/dd-legacy-adapters-tests-dedicated.log` |
| Full suite | merged `19dd0be` | 2 failed, 1039 passed | `/private/tmp/dd-legacy-adapters-tests-full.log` |

Command: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q`.

Two failures after the merge were test-harness bugs and were fixed without changing any assertion: the menu
snapshot items are a tuple, and the PostgREST client was missing `.in_()`.

## Source defect (reproduced at `19dd0be`)

`/generate-meal-plan-stream` with the flag on has no plan-exists check, either for today or for the requested
date. `/generate-meal-plan` does check both. Every retry therefore saves another full-day plan for the
requested date. This also breaks the conversation agent for that date: `read_target` returns 409 "Multiple plans
exist for this date".
- `test_flag_on_retry_for_same_date_never_double_saves[/generate-meal-plan-stream]`: the second request
  returns 200 and saves a second row.
- `test_flag_on_lost_write_reply_never_falls_back_or_writes_twice[/generate-meal-plan-stream]`: after a lost
  insert reply (the row was committed), a retry saves a second row.

Fix belongs in the source: apply the same `[today, requested date]` check in the stream route before the stream
starts.

## Risks (not asserted)

- A lost insert reply returns `auto_saved: false` while the row exists (the documented legacy behavior). A client
  that then calls `/meal-plan/save` could create a duplicate. Reconciliation is not implemented.
- In the agent path, a non-`YYYY-MM-DD` date such as `today` and any unsupported dietary label return a generic
  500. Legacy accepted `today`.
- In the agent path, an existing plan for today blocks full-day generation for a different requested date.
- Not covered: concurrent duplicate requests, deadline expiry, Atrium `restaurant` filtering, stale-menu rejection.
