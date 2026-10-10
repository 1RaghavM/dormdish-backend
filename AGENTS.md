# Repository agent instructions

Read the migration handoff at `../DORMDISH_AGENT_MIGRATION_PLAN.md` when available and `docs/agent-api-v1.md` before migration work. Preserve existing generation contracts and Gemini scanning/rollback.

Every coding, testing, integration, and review agent must read and apply Ponytail at **full** level before working: `/Users/raghavmehta/.codex/plugins/cache/ponytail/ponytail/5.1.0/skills/ponytail/SKILL.md`. Locate the installed skill if that machine-specific path is unavailable; report a missing skill before dependent work. Propagate this requirement to nested agents. Map callers, tests, fixtures, configuration, and exports; make the smallest complete change without dropping validation, security, accessibility, error handling, or meaningful tests.

## Ownership and checkpoints

- Use isolated worktrees and separate branches for parallel tasks. Assign explicit file ownership and dependencies; preserve unrelated uncommitted work. Never develop or push directly on `main`.
- Verify access before push-dependent assignments. Use Raghav's verified Git identity; never invent an email or use a bot identity. Do not add any `Co-authored-by` trailers, including in squash/merge messages.
- Each implementation/testing agent commits and pushes its own coherent milestones (target 30–60 minutes during sustained work), and always before handoff or stopping. Stage only owned files and inspect staged changes.
- Open draft PRs early, attach them when tooling allows, and keep behavior, checks, tested SHAs, migrations, deployment order, dependencies, and rollback current. Unrun checks remain explicitly unrun.
- Keep the migration ledger in `docs/agent-migration-progress.md` current through its assigned owner.

## Tests, review, and merge

- Assign dedicated backend and iOS testing agents alongside feature implementation. They author and run behavior/regression tests in their own worktrees, commit/push their own branches, and open linked test PRs. Do not weaken assertions to hide defects.
- Track the exact implementation SHA tested. Explicitly stack dependent test branches; keep test commits separate and verify combined revisions in an isolated integration worktree. Companion test PRs are merge dependencies; rollout stays disabled until both feature and tests are reviewed, merged, and passing.
- Every PR, including tests, receives a fresh independent reviewer who reads Ponytail full, the complete diff, connected code, and actual test evidence. Authors do not approve their own work. New commits require updated review.
- Merge only the reviewed head when required checks, approvals, branch protections, acceptance tests, and dependencies pass. Never bypass protection or approvals. Same-account agent review may not satisfy GitHub's distinct-account approval requirement.
- Prefer a head-conditioned squash merge when allowed; verify its message contains no unauthorized attribution. Verify main checks and applicable deployment health afterwards; merge does not prove rollout.
- Default tests use synthetic fixtures and no paid models or production data. Validate ownership, deterministic nutrition, duplicate writes, recovery, compatibility, and failure paths. Report skipped checks and remaining risks.

## Rollout

Keep backend rollout disabled until additive migrations, staging streaming/model access, measured deployment limits, acceptance suites, and rollback checks pass. Deploy schema, compatible backend, then gated iOS. Rollback disables the runtime without deleting user data; reconcile ambiguous writes before retrying any generator. Do not log secrets, health data, or raw conversations.
