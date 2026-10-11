# Independent API test evidence

Feature PR [#10](https://github.com/1RaghavM/dormdish-backend/pull/10), source `1e1293cde4b72d82036de1e43443b99befe0ba82`, ledger-only base update `524834c9236a0ad65b3496f1d4ed0699ce90a8a1`. Dedicated test branch: `tests/dd-agent/routes`. Ponytail full and repository/handoff instructions read and applied. Only independent test/evidence files are owned here.

First milestone: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_agent_routes.py -q -o addopts=''` — **15 passed**, no failures/skips; one existing Starlette/AnyIO deprecation warning. Uses real FastAPI, application authentication dependency, AgentStore RPCs, local PostgreSQL, Agents Runner/Responses and deterministic validators. Synthetic session verification and HTTP Responses are the external boundaries; no paid models or production data are accessed.

Covers authentication, owner isolation across all resource types, strict input preflight, claim-before-history, genuine preview/action persistence, canonical nutrition/date/hall, restore/reconcile, duplicate stream replay without SDK rerun and idempotent Apply. The fixture uses a minimal SQL-backed PostgREST read transport rather than replacing domain services.

Pending: admission/rollback, disconnect/terminal ordering, ambiguous writes, legacy save guards, concurrent policy SQL, full-suite evidence and fresh independent review. Depends on runtime PR #8 and companion tests #9 plus route PR #10. No schema is applied outside disposable local test databases. Rollout remains disabled; live-model/deployment limits and other documented release gates remain unverified.
