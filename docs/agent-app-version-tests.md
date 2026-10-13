# App-version gate tests

Dedicated backend tests for exact `X-App-Version` admission. Implementation dependency: `feat/dd-agent/app-version`, commit `2fa1b0ec47ef3aca6809bf4387c00281a54afe94`. Test branch: `tests/dd-agent/app-version`. Apply Ponytail full; no new dependencies or production calls.

Coverage uses authenticated FastAPI requests and disposable local PostgreSQL: missing/empty/older/newer/prefix/whitespace versions hide all capabilities and reject create, message, and confirmation before storage; current-version owners outside the internal list can create, send, and confirm at full rollout; blank configuration fails closed; custom exact version works; authentication, premium dependency, rollout, and kill switch remain authoritative. Old clients retain owner-scoped recovery and deletion.

Existing route fixtures send the current version so ownership, memory, save, recovery, and profile checks still exercise their original behavior.

Negative control against original source `b5407b9`: 11 behavioral assertion failures and 9 passes. Mismatched clients still received enabled capabilities and version configuration was ignored. An initial attempt before starting Docker failed in fixture setup and is excluded from this evidence.

Combined executable revision `c875017bb5394c752cb99c544f3b2fba39010196`, based on source `2fa1b0ec47ef3aca6809bf4387c00281a54afe94`: **327 passed, 0 failed, 0 skipped in 135.56s**, exit 0. One existing Starlette/AnyIO deprecation warning. Command:

```sh
DD_AGENT_LOCAL_PG=1 python -m pytest tests/test_agent_app_version.py tests/test_agent_app_availability.py tests/test_agent_routes.py tests/test_agent_routes_unit.py tests/test_agent_internal_rollout.py tests/test_agent_route_policy.py tests/test_agent_profile_guard.py tests/test_explicit_memory.py -o addopts='' -x -q
```

The first wider run was interrupted by Docker failure during disk exhaustion (154 passes, one storage-unavailable failure, 272 fixture errors). After the failed daemon recovered, PostgreSQL could not start because its `postmaster.pid` was empty. Only the disposable synthetic test container/volume was recreated, then all directly affected suites above passed. The earlier interrupted run is not passing evidence; wider controlled-save/release-acceptance suites were not rerun. No paid models or production data were used.

Fresh independent review remains required. Source must merge first; do not expand rollout until source and independently reviewed tests pass and merge. Rollback keeps existing owner reads/deletion and stored data.
