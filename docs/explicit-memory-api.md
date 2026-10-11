# Explicit conversational memory (contract draft)

Default OFF: `DORMDISH_EXPLICIT_MEMORY_ENABLED=true` is required, in addition to the existing authenticated agent rollout. Apply the new additive migration before enabling. This feature will depend on preference PR13/14, routes PR10/11 and the explicitly stacked completion source PR18; production SQL is not applied.

Memory mutations use the existing conversation message endpoint and run IDs. They are handled before model execution: a server parser reads only the current stored authenticated user turn, then an atomic RPC applies the exact operation and records the complete run. No model-generated write arguments exist. `GET /agent/runs/{request_id}` recovers the committed outcome after a lost response; no write retry or alternate generator is launched.

Accepted literal examples:

- `Remember that I don't like mushrooms` (curly apostrophe also accepted): global soft dislike.
- `Remember that I like rice for dinner`: meal-scoped favorite.
- `Remember my flavor preference: spicy`, `Remember my cuisine preference: Indian`, `Remember my texture preference: crunchy`: typed global tastes, all soft.
- `Forget mushrooms`: remove the named soft preference across meals/categories.
- `Forget my dislike of mushrooms`: remove that category only.
- `Forget that preference`: resolve only an unambiguous preference written by the immediately preceding conversation turn; otherwise ask which.

Unsupported, quoted, conditional, multi-instruction, temporary, inferred, negated-memory, or hard-profile instructions cannot authorize a write. Clarification explains a supported form. Temporary requests remain ordinary conversational context, and inferred tastes remain suggestions. No profile allergies, restrictions or targets are changed. Supported single instructions do not require an extra confirmation.

The existing `user_preferences` table gains `scope` (`meal`/`global`), `preference_kind` (`food_item`/`meal_plan`/`flavor`/`cuisine`/`texture`), `source` (`legacy`/`button`/`explicit_chat`), `updated_at`, `source_conversation_id` and `source_request_id`. Existing `item_type` values remain `food_item`/`meal_plan` for old clients; broader tastes use `food_item` plus the typed kind. Global rows use `meal_type=all`, requiring a deliberate widening of its known check constraint. Existing button requests remain breakfast/lunch/dinner only.

Global set replaces conflicting same-name preferences across all meals. A meal-specific override splits an existing global row into the unaffected meals before applying the selected meal, preserving preferences outside the new instruction. Forget invalidates all SDK replay histories atomically; an in-flight run with an old history epoch cannot restore them. Display transcripts remain display-only and cannot authorize new writes.

Existing preference responses retain `{id,memory,metadata}`. Metadata adds the fields above; old clients ignore them. Meal-filtered reads include global rows. Existing `complete.data.preferences_changed=true` triggers the native refresh; no SSE envelope change is needed. Example global row metadata:

```json
{"category":"dislike","item_type":"food_item","preference_type":"food_item","meal_type":"all","item_name":"mushrooms","scope":"global","preference_kind":"food_item","source":"explicit_chat","created_at":"2026-10-11T12:00:00+00:00","updated_at":"2026-10-11T12:00:00+00:00","source_conversation_id":"00000000-0000-0000-0000-000000000001","source_request_id":"00000000-0000-0000-0000-000000000002"}
```

This is an early contract checkpoint; implementation and tests are not yet complete. Keep rollout disabled until independent backend/native tests, fresh source/test reviews, schema-first deployment and existing release gates pass.
