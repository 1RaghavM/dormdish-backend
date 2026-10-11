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

The parser accepts the literal forms above, with a target of one to six alphanumeric words (up to 200 characters); internal apostrophes and hyphens preserve Unicode names. Clause punctuation, pronouns, auxiliary verbs, negation, alternatives, temporal/conditional markers and persistence instructions inside the target require clarification. This is a bounded command grammar, not general natural-language intent recognition. Unsupported, quoted, multi-instruction, inferred, negated-memory, or hard-profile command forms cannot authorize a write. Clarification explains a supported form. Temporary requests remain ordinary conversational context, and inferred tastes remain suggestions. No profile allergies, restrictions or targets are changed. Supported single instructions do not require an extra confirmation.

The existing `user_preferences` table gains `scope` (`meal`/`global`), `preference_kind` (`food_item`/`meal_plan`/`flavor`/`cuisine`/`texture`), `source` (`legacy`/`button`/`explicit_chat`), `updated_at`, `source_conversation_id` and `source_request_id`. Existing `item_type` values remain `food_item`/`meal_plan` for old clients; broader tastes use `food_item` plus the typed kind. Global rows use `meal_type=all`, requiring a deliberate widening of its known check constraint. Existing button requests remain breakfast/lunch/dinner only.

Global set replaces conflicting same-name preferences across all meals. A meal-specific override splits an existing global row into the unaffected meals before applying the selected meal, preserving preferences outside the new instruction. Forget invalidates all SDK replay histories atomically; an in-flight run with an old history epoch cannot restore them. Display transcripts remain display-only and cannot authorize new writes.

Existing preference responses retain `{id,memory,metadata}`. Metadata adds the fields above; old clients ignore them. Meal-filtered reads include global rows. Existing `complete.data.preferences_changed=true` triggers the native refresh; no SSE envelope change is needed. Example global row metadata:

```json
{"category":"dislike","item_type":"food_item","preference_type":"food_item","meal_type":"all","item_name":"mushrooms","scope":"global","preference_kind":"food_item","source":"explicit_chat","created_at":"2026-10-11T12:00:00+00:00","updated_at":"2026-10-11T12:00:00+00:00","source_conversation_id":"00000000-0000-0000-0000-000000000001","source_request_id":"00000000-0000-0000-0000-000000000002"}
```

Implementation evidence: production/selftest commit `eee668b0506925896fae90f235389e9959f61d03` passed 578 tests (zero failures/skips) in 165.25s with `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''`. The local PostgreSQL run logged to `/private/tmp/dd-explicit-memory-source-fixed-full.log`; its one warning is the existing AnyIO deprecation. The 18 source selfchecks include real SQL authorization, rollback, scoped conflicts, replay invalidation and encoded-slash deletion.

Companion [PR20](https://github.com/1RaghavM/dormdish-backend/pull/20) independently adds 71 cases. Test code `4ab56667cce10f1b2934b2894b291f54eb7be29e` passed the combined 649-test suite (zero failures/skips) in 188.20s against that source commit; see its `docs/explicit-memory-test-evidence.md` and `/private/tmp/dd-explicit-memory-tests-full.log`. Original failing assertions for temporary/quoted instructions, Unicode target preservation and encoded-slash deletion passed unchanged after the fixes. Source/test evidence updates after these SHAs contain documentation only.

Review follow-up `60a14f2928bbb9fbdeba093b5c7079926389ab56` fixes trailing-clause authorization: the reviewer’s negated-saving and temporary instructions previously persisted verbatim. Before the fix, actual run-flow regressions reproduced eight false mutations (`/private/tmp/dd-memory-review-clauses-red.log`). The same assertions now pass with 15 actual-run clause cases and four additional name/length checks; Unicode target spelling is preserved. The complete source suite passed 597 tests, zero failures/skips, in 157.88s (`/private/tmp/dd-memory-review-fixed-full.log`, same command and existing warning). Fresh independent test integration/review of this follow-up remains required; the 649-test result above applies to the earlier source SHA only.

Keep rollout disabled until fresh source/test reviews, native acceptance, schema-first deployment and existing release gates pass. Apply `migrations/add_explicit_preference_memory.sql` only after verifying the deployed preference check constraints and prerequisite migrations. Roll back by disabling `DORMDISH_EXPLICIT_MEMORY_ENABLED`; retain the additive schema and existing preference rows so recorded tastes and global read compatibility survive. No production SQL, live model call or deployment was performed by this implementation.
