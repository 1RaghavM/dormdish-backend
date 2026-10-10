# Agent migration progress — October 10, 2026

Coordinator: primary Codex agent. Source handoff: `/Users/raghavmehta/Downloads/DormDish/DORMDISH_AGENT_MIGRATION_PLAN.md`. All agents must read Ponytail full; no co-author trailers.

## Verified prerequisites
- Backend origin/main: bccc1d17afea6ead9b38d660f178d21e45262c55; original checkout clean.
- iOS origin/main: 667297438d11496b5c2047d37f3920efa605fcb6; original checkout has extensive user changes and stays untouched.
- Authenticated GitHub login 1RaghavM; both repositories grant push/admin. GitHub maps existing commit identity 1RaghavM <lebrongoated2323@gmail.com> to this account. Email verification API unavailable without user scope; no invented identity.
- Git/GitHub work with approved sandbox escalation; initial sandbox-only DNS/keychain errors were not credential failures.
- Both main branches report no branch protection; active branch rules lists empty. Recheck at merge.
- Xcode 27 installed but license acceptance blocks simulator commands. Do not accept legal terms for user.
- Official GPT-6 Luna and Agents SDK pages fetched. GET official OpenAI /v1/models/gpt-6-luna using configured key returned HTTP 401; live evaluation blocked. Vercel whoami/project inspect reported no existing credentials; actual deployed deadline, plan, streaming and package limits remain unverified. User asynchronously asked to restore local credentials (never paste secrets).
- Command Line Tools Swift 6.4 works with DEVELOPER_DIR=/Library/Developer/CommandLineTools; isolated Swift checks may run while simulator tests remain blocked.
- Original iOS tracked diff/status snapshots preserved in /private/tmp/dd-ios-initial.patch and /private/tmp/dd-ios-initial-status.

- Read-only Supabase REST schema metadata verified: public.users.id UUID; user_preferences.user_id references public.users; generation history stored in meal_plan_generations (UUID id, user_id, date, meal_plan_data jsonb, updated_at); save routes also use saved_meal_plans; no meal_plans table. No user records read, no DB mutations. RLS/SQL execution access still unverified.
- Existing backend main bccc1d1 has successful Vercel GitHub status; existing checks are scheduled refresh/cleanup jobs, not migration tests. iOS main has no check runs. No new deployment claimed.

## Dependency order and ownership
| ID | Owner | Worktree under .migration-worktrees | Branch | Base | Owned files | Dependencies | PR / tested SHA / result | Next |
|---|---|---|---|---|---|---|---|---|
| A-BE | backend foundation implementation | backend-contracts | feat/dd-agent/contracts | bccc1d1 | AGENTS.md, docs/agent-api-v1.md, contracts/, dormdish_agent/models.py, agents collision, manifests | none | pending | Freeze v1 contracts, prepare SDK |
| A-IOS | iOS implementation | ios-conversation | feat/dd-agent/ios-conversation | 6672974 | AGENTS.md, CLAUDE.md, Core/Networking agent/SSE files, Features/Agent, Home/Planner entry points; excludes project/test files | agreed v1 contract | pending | Native gated client |
| T-BE | dedicated backend test author | backend-tests | tests/dd-agent/backend-contracts | bccc1d1 | tests/test_agent_contracts.py, tests/fixtures/agent/, testing evidence docs | A-BE exact commit once ready | [PR 1](https://github.com/1RaghavM/dormdish-backend/pull/1), d77f25d; baseline 25 passed | Author model contract tests; stack foundation SHA |
| T-IOS | dedicated iOS test author (queued slot) | ios-tests | test/dd-agent/ios-streaming | 6672974 | DormDishTests/, project.yml, generated Xcode project, testing evidence docs | A-IOS exact commit once ready | pending; Xcode license blocker | Write/run native SSE/model tests |

Coordinator exclusively maintains this ledger; implementation owner stages/commits it at coherent milestones. Each PR needs a fresh reviewer and exact-head test evidence. Separate test PRs are explicit merge/rollout dependencies. No deployment or rollout is yet authorized by passing checks; all new behavior remains disabled until gates pass.

Next phases: shared deterministic meal service → read-only runtime → owner-scoped database state → controlled actions and route compatibility → native integration → staging/rollback gates. Existing Gemini contracts remain available.

## Current correctness findings
- Eaten checkmarks are device-local today: MealPlanService.updateEatenStatus is a stub. A future owner-scoped completion sync is required before claiming remaining-target correctness; count food log and completion records once.
- SDK latest dependency compatibility under investigation by A-BE; pin verified compatible release without gratuitous framework upgrade.

## Verification boundaries
- Current official sources: https://developers.openai.com/api/docs/models/gpt-6-luna, https://developers.openai.com/api/docs/guides/agents/running-agents, https://developers.openai.com/api/docs/guides/agents/results. Confirm Responses + streaming/history against pinned SDK, not latest docs alone.
- Vercel current changelog (2026-06-15): https://vercel.com/changelog/vercel-functions-can-now-run-up-to-30-minutes. General maximums depend on plan/Fluid/beta enrollment and do not establish this project’s deployed limit.
- Local Docker daemon unavailable (missing ~/.docker/run/docker.sock); Supabase CLI exits 137 without output. SQL migration execution not verified; schema-only REST reads succeeded.
- Original iOS tracked diff compared byte-for-byte to startup snapshot after initial agent work: unchanged.
