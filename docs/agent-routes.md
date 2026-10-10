# Agent endpoint integration (draft, rollout disabled)

The v1 router uses existing custom session authentication and the current pass-through premium policy. `DORMDISH_AGENT_ENABLED=false`, `DORMDISH_AGENT_KILL_SWITCH=false`, and `DORMDISH_AGENT_ROLLOUT_PERCENT=0` are defaults. Admission requires enabled=true, kill=false, and membership in a stable SHA-256 user cohort; booleans are exact lowercase values and percentage is 0–100. Invalid configuration fails closed. Owner reads, run reconciliation and deletion remain available during rollback. No client flag authorizes backend access.

Initial checkpoint covers creation, restoration, deletion and run recovery. Turn streaming and exact action confirmation are still being implemented. The legacy save route now verifies a supplied plan ID belongs to the authenticated owner and scopes the update itself by owner, eliminating its cross-account upsert path. Existing generation endpoints remain intact.

Focused checks: `env -i PATH=/usr/bin:/bin /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_agent_routes_unit.py -q -o addopts=''` — 3 passed. No model, production database or deployment calls were made. The dedicated API testing agent's companion PR and fresh independent reviews remain merge dependencies.

Depends on the runtime branch and additive persistence/source-normalization PRs. Deploy reviewed additive schema before this backend; keep rollout off until live model access, staging streaming, actual deployment limits, privacy checks, compatibility adapters and rollback gates pass. Disabling the runtime preserves stored records and existing Gemini generation.
