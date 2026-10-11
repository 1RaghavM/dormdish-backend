# Independent API test evidence

Test PR [#11](https://github.com/1RaghavM/dormdish-backend/pull/11), branch `tests/dd-agent/routes`, is explicitly stacked on API PR [#10](https://github.com/1RaghavM/dormdish-backend/pull/10), tested feature head `305f1763d7f94142f1fd104ed739525fe196fced`. Runtime companion tests [#9](https://github.com/1RaghavM/dormdish-backend/pull/9), exact `e020b0d075d29af77a569b202d688bdfcf6fc6a2`, were fetched and explicitly merged for combined verification. Tested combined code: `03402b9f803c345026cb2f005af2b495847a52b4`; the final documentation checkpoint/head is recorded in the PR. No production code was authored by this testing agent. Ponytail full and repository/handoff instructions were read and applied.

## Commands and results

Python 3.11, `openai-agents==0.8.1`, `openai==2.26.0`; interpreter `/private/tmp/dd-agent-runtime-uv/bin/python`.

- `DD_AGENT_LOCAL_PG=1 python -m pytest -q -o addopts=''`: **386 passed, zero failures/skips**, 38.02 seconds. Includes all 55 independently authored API/policy cases plus runtime and existing suites. One existing Starlette/AnyIO deprecation warning. Full output captured locally at `/tmp/dd-route-tests-combined-final.log`.
- `DD_AGENT_LOCAL_PG=1 python -m pytest tests/test_agent_route_policy.py -k 'legacy_hall_alias or alias_resolution' -q -o addopts=''`: **6 passed**, 25 deselected. The two same-hall regressions pass unchanged; four genuine other/unregistered hall cases still reject the request before SDK calls.
- `git diff --check`: passed.
- Two isolated archived-source mutations failed with behavior assertions: bypassing rollout admission and removing the owner/date SQL save guard. Original source files and non-test databases were not modified.

## Defect and red/green evidence

Against API source `1e1293c` (ledger-only head `524834c`), the combined checkpoint `7cb6425` produced **381 passed, 2 failed**. Existing saved halls `busch` and `Busch Dining Hall` incorrectly returned HTTP 409 “another hall” when the request selected `busch`. Native Planner sends registered hall IDs and legacy routes save them verbatim; new context canonicalization exposed the exact-string comparison error. The implementation owner fixed this in `305f176`. Unchanged tests now prove preview/Apply preserves the original plan ID/date/hall and untouched breakfast. SQL's exact hall/date/version guard remains strict. No assertions were weakened or production fixes made on this test branch.

## Coverage and boundaries

Actual FastAPI/application authentication, AgentStore RPCs, disposable local PostgreSQL, Agents Runner/Responses and deterministic meal validation execute. Synthetic session verification and Responses HTTP are external boundaries. A minimal SQL-backed PostgREST read transport serves actual rows; one focused legacy-update test spies on its outbound filters. No paid model, production data or deployed service is accessed.

Coverage includes auth/cross-owner resource access; strict body bounds and request schemas; stable server cohorts/kill switch with owner recovery/deletion; claim before history; duplicate replay without SDK rerun; actual ASGI terminal/action persistence before delivery; actual disconnect cancellation and persisted reconciliation; lost committed replies without repeated writes; preserved meal nutrition/date; exact stored Apply authority; expired/stale version refusal; legacy cross-owner update guards; and concurrent owner/date save policy without collisions across different owners/dates.

Fresh independent reviews are required for the API and test PRs. Retarget the explicit test stack after its dependencies merge and verify current heads before merging. Rollout remains disabled. Live model behavior/access, real PostgREST/deployed policies, Vercel limits, staging, atomic changed-profile safety, preference/search/completion integration and rollback gates remain outside this test evidence. Removing this PR only removes tests/evidence; local migration fixtures create and drop uniquely named synthetic databases.

## Current-profile safety extension (in progress)

The test branch explicitly merged verified integration `6f7255fa0618cef231ffa8c1a9079f29dbc01c95`, including runtime test dependency `fd6481aec323a3029c3715bf25c58efe6627f3be`. The new independent `test_agent_profile_guard.py` covers trusted database profile capture, protected-field changes, concurrent updates holding the user row, changes before terminal persistence, unchanged/unrelated account fields, applied-action replay, forged client guards, and legacy actions without guards. Existing route fixtures alone were adapted for the forward profile-safety migration; historical persistence fixtures remain unchanged.

This checkpoint compiles but the 26 new cases are **not yet run**: it awaits the implementation owner's `enforce_agent_profile_safety.sql` checkpoint. Earlier passing counts above describe the prior source only; this draft checkpoint is not review-ready. No production code was authored here.
