# Versioned meal completion

Draft implementation; rollout is OFF. Apply `migrations/add_meal_completion.sql` after the existing additive agent migrations, deploy backend, then release the gated native client. Enable `DORMDISH_COMPLETION_ENABLED=true` only after reviewed companion tests, production schema verification, and native acceptance. Disabling this setting stops writes and switches agent intake back to explicitly incomplete food-log-only coverage. Recovery/list reads remain available. Do not drop tables during rollback.

## Contract

Existing saved rows gain `completion_version` (UUID); changing owner, date, or meal data rotates it. Timestamp-only updates preserve it. Existing specific/today/save responses already select entire rows; history includes it when completion is enabled. Native clients must retain the version **with the displayed meal data**, not fetch a new version and submit old indices. Cached rows without a version require refreshing and displaying the current row first.

All routes use existing authentication; the server supplies the owner. Unknown fields are rejected, request bodies are bounded by the existing 32 KiB route guard. UUIDs, real `YYYY-MM-DD` dates, integer revisions and unique selections are validated. No client-provided nutrition or owner is accepted.

- `GET /meal-plan/{plan_id}/completion`: `{plan,state,available_items}`. `plan` is the current owned saved row. `available_items` contains the server-derived food catalog for that exact version.
- `PUT /meal-plan/{plan_id}/completion`: `{request_id,date,plan_version,expected_revision,mode,selected_items}`. `mode` is `current` (default) or `frozen`; selection entries are `{meal_type,index}` with breakfast/lunch/dinner and zero-based index below 30. At most 90 unique entries. Whole-meal checkoff means all items minus unchecked items; partial checkoff sends only checked items. Empty selection explicitly undoes consumption for this version.
- `GET /meal-completions?date=YYYY-MM-DD`: all owned states on that date, including prior versions and deleted plans. Old consumption must be displayed separately, not silently attached to the current plan's indices.
- `GET /meal-completion-requests/{request_id}`: original successful state receipt; 404 means no committed receipt was found at read time. A network failure is ambiguous: reconcile, then retry the **same exact request ID and body**, never manufacture a new write.

State: `{plan_id,plan_version,date,revision,recorded_items,selected_items,items,totals}`. Each food has `{meal_type,index,name,source_id,totals}`. `items` contains currently consumed foods. `recorded_items` retains all foods previously marked eaten in this version, so explicit undo/recheck remains possible after deletion. `totals` sums `items` (calories/protein_g/fat_g/carbs_g). Revision starts at zero for an unseen version and increases on each accepted new request. An exact duplicate returns its original receipt even after later writes, edits, or deletion; a reused request ID with a different body returns 409. Stale revisions return 409. Unknown/cross-owner resources return 404, invalid saved nutrition 422, quota 429, unavailable storage 503 without private error details.

`current` requires the saved row's exact version and date at commit; immutable server item amounts already include servings. `frozen` only edits a previously persisted version's **ever-eaten** catalog. It cannot add newly encountered foods after editing/deleting a plan. Both modes retain earlier versions; no refresh silently erases actual intake. Native clients serialize writes per version and reconcile before rebasing a pending edit onto another revision.

## Storage and safety

Transactions lock the owner before the saved row, matching agent save lock order. The write RPC verifies the service catalog against the locked saved row; nutrition is never accepted from the HTTP body. The version guard prevents an edit between reading and committing from changing which food an index means. Stable source identities include plan, version, meal and index. No meal completion writes a duplicate food log.

Completion intake joins selected frozen foods to existing logs. The additive nullable `food_log.source_id` permits **trusted server-created** links to the same consumption; no public write API accepts it. Shared `remaining_targets` counts equal source identities once and rejects conflicting nutrition. Independent manual logs retain their own IDs and are never deduplicated by food name. The tool states coverage accurately: server-synced checkoffs and logs only, with unlogged/device-only food unknown. More than 500 records fails closed instead of truncating nutrition.

Tables have RLS enabled, no anon/authenticated grants, service-only RPC execution, and owner filters even with a privileged client. Custom `public.users` account deletion cascades both new tables. Saved-plan deletion does not cascade consumption. Frozen intake, revision state and receipts are retained for the account lifetime, like user food records; no receipt expiry can replay an old request against a reset revision. Bounds: 100 stored plan versions per owner/date, 1,000 new writes per rolling day, 90 foods per version, 64 KiB catalog, 16 KiB stored request, 96 KiB receipt. Reads are owner/date scoped. No private meals or raw errors are logged.

## Verification and dependencies

Based on `integration/dd-agent/combined` at ded498899ac5cea4237c428e86581dd90b65bc7a, explicitly stacking backend PRs 10/11, 13/14, 15/16. Source PR [18](https://github.com/1RaghavM/dormdish-backend/pull/18) and independent companion test PR [17](https://github.com/1RaghavM/dormdish-backend/pull/17) require fresh reviews and native acceptance before enabling.

Final production-code SHA `68d493e346647792f31a82d4c93111ccf55201a3`: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''` passed **560, failed 0, skipped 0** in 98.39s against disposable PostgreSQL 17. One existing AnyIO deprecation warning. Log: `/private/tmp/dd-completion-source-final-full.log`. The following documentation-only commit leaves tested production/test files unchanged.

The dedicated author independently passed **59 completion cases, failed 0, skipped 0** in 26.45s at combined test SHA `ed9c6c8307a783a312de682f36d0767714f81c48` over the same source SHA. Four malformed saved-shape cases genuinely failed with 503 before the 422 validation fix; a concurrent original-commit/plan-delete race genuinely failed with 404 before receipt recovery returned the original 200. Conflicting retry bodies still return 409. Coverage includes authentication, owner/date/version guards, SQL rollback/concurrency/privileges, account deletion, stable intake source deduplication, frozen undo/recheck and exact aged receipts. The companion PR owns final combined full-suite evidence.

No production SQL, live model calls, deployment, native acceptance, or account-specific limits were verified by this feature.
