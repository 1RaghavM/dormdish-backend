# Explicit conversational memory

Default OFF: `DORMDISH_EXPLICIT_MEMORY_ENABLED=true` is required, in addition to the existing authenticated agent rollout. Apply the new additive migration before enabling. Merge and rollout dependencies are preference PR13/14, routes PR10/11, the explicitly stacked completion source PR18, and companion independent test PR20. Native preference source/test PR12/13 in `dormdish-app-u` are explicit rollout dependencies. Production SQL is not applied.

Memory mutations use the existing conversation message endpoint and run IDs. They are handled before model execution: a server parser reads only the current stored authenticated user turn, then an atomic RPC applies the exact operation and records the complete run. No model-generated write arguments exist. `GET /agent/runs/{request_id}` recovers the committed outcome after a lost response; no write retry or alternate generator is launched.

Accepted literal examples:

- `Remember that I don't like mushrooms` (curly apostrophe also accepted): global soft dislike.
- `Remember that I like rice for dinner`: meal-scoped favorite.
- `Remember my flavor preference: spicy`, `Remember my cuisine preference: Indian`, `Remember my texture preference: crunchy`: typed global tastes, all soft.
- `Remember my flavor dislike: spicy` (also cuisine/texture), optionally `for dinner`: change the typed taste category without recasting it as a food.
- `Forget my flavor preference: spicy` or `Forget my flavor dislike: spicy` (also cuisine/texture): remove only the named kind/category.
- `Forget mushrooms`: remove the named soft preference across meals/categories.
- `Forget my dislike of mushrooms`: remove that category only.
- `Forget that preference`: resolve only an unambiguous preference written by the immediately preceding conversation turn; otherwise ask which.

The target boundary is deterministic: one bare alphanumeric token (internal straight/curly apostrophes or hyphens allowed), or one standard JSON string literal. Multiword/special names must be JSON-quoted: `Remember that I like "Chef’s rice" for dinner`, `Remember my flavor preference: "very spicy"`, `Forget "Chef’s rice"`. Clients should JSON-encode the target; embedded quotes/backslashes use standard JSON escaping, and Unicode can be literal or escaped. JSON decoding preserves the name, then existing surrounding-whitespace trimming and 1–200 character/control-character checks apply. Bare temporal/grammatical placeholders are rejected.

The only allowed suffix for a remember instruction is exactly ` for breakfast`, ` for lunch`, or ` for dinner`, outside the target. Forget has no meal suffix. One final `.` or `!` outside the literal is optional. Any other trailing clause, multiple targets, malformed JSON, curly/backtick pseudoquotes, whole-command quotation, or unknown unquoted multiword target produces clarification and no mutation. There is no known-name bypass and no model reinterpretation of a rejected command. Content inside the explicitly quoted target is literal name data; it cannot add another operation or change scope. This deliberately tightens the earlier contract: unquoted `Chef’s rice` now clarifies, while `"Chef’s rice"` persists the exact name. The corresponding positive fixture was intentionally replaced by both an unquoted no-write case and an exact quoted-name case; prior trailing-clause regression assertions remain unchanged.

Temporary requests remain ordinary conversational context, and inferred tastes remain suggestions. No profile allergies, restrictions or targets are changed. Supported single instructions do not require an extra confirmation.

The existing `user_preferences` table gains `scope` (`meal`/`global`), `preference_kind` (`food_item`/`meal_plan`/`flavor`/`cuisine`/`texture`), `source` (`legacy`/`button`/`explicit_chat`), `updated_at`, `source_conversation_id` and `source_request_id`. Existing `item_type` values remain `food_item`/`meal_plan` for old clients; broader tastes use `food_item` plus the typed kind. Global rows use `meal_type=all`, requiring a deliberate widening of its known check constraint. Existing button requests remain breakfast/lunch/dinner only.

Global set replaces conflicting same-name preferences across all meals. A meal-specific override splits an existing global row into the unaffected meals before applying the selected meal, preserving preferences outside the new instruction. Forget invalidates all SDK replay histories atomically; an in-flight run with an old history epoch cannot restore them. Display transcripts remain display-only and cannot authorize new writes.

Existing preference responses retain `{id,memory,metadata}`. Metadata adds the fields above; old clients ignore them. Meal-filtered reads include global rows. Existing `complete.data.preferences_changed=true` triggers the native refresh; no SSE envelope change is needed. Example global row metadata:

```json
{"category":"dislike","item_type":"food_item","preference_type":"food_item","meal_type":"all","item_name":"mushrooms","scope":"global","preference_kind":"food_item","source":"explicit_chat","created_at":"2026-10-11T12:00:00+00:00","updated_at":"2026-10-11T12:00:00+00:00","source_conversation_id":"00000000-0000-0000-0000-000000000001","source_request_id":"00000000-0000-0000-0000-000000000002"}
```

Implementation evidence: production/selftest commit `945a03740d1e6e0bddd5b01591949a732af5a0e7` (including reviewed atomic prerequisite `dd25b364cc4a8ed713bcbb337026dd2709402e68`) passed 635 tests, zero failures/skips, in 116.05s with `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''`. The local PostgreSQL run logged to `/private/tmp/dd-memory-json-final-full.log`; its one warning is the existing AnyIO deprecation. All 75 source selfchecks also passed in 15.77s.

Actual red runs exposed trailing temporal/conditional text being saved under the earlier free-name parser, and reserved bare words accepted during the boundary change. Their no-write assertions now pass unchanged. Independent testing also exposed same-conversation replay being cleared during clarification; rejected instructions now preserve the current locked replay, while successful changes still invalidate it through the existing history epoch mechanism. Source logs: `/private/tmp/dd-memory-boundary-second-red.log`, `/private/tmp/dd-memory-json-bare-red.log`, and `/private/tmp/dd-memory-json-final-focused.log`. The previous partial full run at `09dc425` was deliberately interrupted for these fixes and is not claimed as a completed check.

Companion [PR20](https://github.com/1RaghavM/dormdish-backend/pull/20) adds 139 independently authored cases. Combined checkpoint `de4644c831ef5c3d3d49bb9cd2877a303dcef7b3` explicitly integrates the current source; all 18 unchanged regression cases passed in 10.03s, and its full combined suite is pending at this documentation checkpoint. See its `docs/explicit-memory-test-evidence.md` for final independent evidence. Older 597/649-test results predate the JSON boundary and do not verify this revision. Source evidence updates after the tested SHA contain documentation only.

Keep rollout disabled until fresh source/test reviews, native acceptance, schema-first deployment and existing release gates pass. Apply `migrations/add_explicit_preference_memory.sql` only after verifying the deployed preference check constraints and prerequisite migrations. Roll back by disabling `DORMDISH_EXPLICIT_MEMORY_ENABLED`; retain the additive schema and existing preference rows so recorded tastes and global read compatibility survive. No production SQL, live model call or deployment was performed by this implementation.
