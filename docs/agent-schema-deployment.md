# Production schema deployment evidence

On October 11, 2026, Supabase CLI 2.117.0 linked successfully to the configured DormDish project `rszkbhgowcrvwiwcmntt` (PostgreSQL 17.6). No browser authentication was used. The prior project-access blocker is resolved.

Metadata-only preflight verified the existing custom `public.users` identity, saved-plan UUID/date/JSONB columns, actual constraints, indexes and privileges. No user rows were read. The following independently reviewed SQL files were applied in order from route source `f11def45aee5133dd7cb1f6fad8f16aba6dde2ec`:

| File | SHA-256 | Outcome |
| --- | --- | --- |
| `add_agent_persistence.sql` | `90bd8dfdab8a16b64c1c4ef31a3aa039cbb49b4e1ad3526c65cfa528f29a391d` | Applied successfully |
| `enforce_agent_daily_save_policy.sql` | `ca8f5906fb9901fb113fc2960fa75c9e3eae645e2f95539cbce65a00e3640008` | Applied successfully |
| `enforce_agent_profile_safety.sql` | `dc8afa3b7f1a677106d1f0162a17a4ff6306b2054e8dce7b6fd5d1206e78c9c8` | Applied successfully |

Post-deployment metadata verification at `2026-10-11T18:02:18.595765Z` confirmed all four agent tables have RLS enabled; `anon` and `authenticated` have no SELECT privilege; service-role CRUD remains available. All 12 agent functions are security-invoker, deny execution to public client roles and allow the service role. Owner-scoped cascading foreign keys, profile fingerprint column, profile guard and daily-plan replacement guard are present. No flags were enabled or user records modified by these migrations.

CLI outputs and metadata snapshots: `/private/tmp/dd-agent-schema-preflight.json`, `/private/tmp/dd-agent-apply-persistence.json`, `/private/tmp/dd-agent-apply-daily-policy.json`, `/private/tmp/dd-agent-apply-profile-safety.json`, `/private/tmp/dd-agent-schema-verify.json`.

## Preference compatibility finding

Production has `idx_user_preferences_unique(user_id, category, lower(item_name))`, which rejects distinct per-meal choices. A local fixture matching that actual index reproduced an existing meal-isolation test failure. Source `dd25b364cc4a8ed713bcbb337026dd2709402e68` transactionally replaces it with the meal-scoped index without rewriting rows, bounds lock acquisition to five seconds, and restricts preference table access to the backend service role. The same regression and full source suite pass: **398 passed, 0 failed, 0 skipped** in 31.32 seconds. Logs: `/private/tmp/dd-preference-production-index-red.log` and `/private/tmp/dd-preference-production-schema-green.log`.

This preference SQL has **not** been deployed: dedicated tests and fresh review must pass first. Completion and explicit-memory schema are also pending. Production metadata also reveals an existing permissive public `food_log` policy and public-role SELECT grants; that requires a scoped compatibility-tested access fix before rollout. No food-log rows were queried.

Not verified: live model access, account-specific Vercel limits, staging streaming, production user journeys or rollback drill. Schema deployment alone does not establish application health or authorize rollout. Rollback leaves the additive data structures intact and disables the runtime.
