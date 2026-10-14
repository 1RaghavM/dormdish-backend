# Backend 2.2.5 release verification

Implementation: `75efb5028e3037fa00b63c62fb4c36a0830f32f3`, branch `release/agent-2.2.5`, [PR 42](https://github.com/1RaghavM/dormdish-backend/pull/42).
Test source: `497a8f1aaa127ff9cfa192343170ca8562652fff`, branch `tests/agent-2.2.5`, [companion PR 43](https://github.com/1RaghavM/dormdish-backend/pull/43).

Updated only current-client fixtures and explicit version-admission cases. Default `2.2.5` admits authenticated owners; `2.2.4`, `2.2.6`, missing, empty and malformed versions cannot start conversations, send turns or confirm actions, and do not write saved meals. Outdated clients can still read/reconcile/delete their own records; cross-owner requests remain rejected. An explicit `DORMDISH_AGENT_APP_VERSION=2.2.4` override continues to admit 2.2.4 and block 2.2.5 until configuration changes. Blank configuration fails closed. Auth, premium, kill-switch, feature-disable and rollout cohort gates remain enforced.

Executed against synthetic fixtures and throwaway databases in the existing local `dd-agent-migration-db` PostgreSQL container:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_agent_app_version.py tests/test_agent_app_availability.py tests/test_agent_routes.py tests/test_agent_routes_unit.py tests/test_agent_internal_rollout.py tests/test_agent_route_policy.py tests/test_agent_profile_guard.py tests/test_explicit_memory.py -o addopts='' -x -q
```

**331 tests passed, 0 failures, 0 skips, 1 existing Starlette/AnyIO deprecation warning**, in 166.93 seconds. Log: `/private/tmp/dd-release225-backend-tests.log`. The following evidence-only commit does not change tested executable code.

These tests do not change live flags or prove deployment. Independent review, matching native release 2.2.5, and the production exact-version configuration switch remain release dependencies. No paid model calls, production user data, schema migration or live backend mutation was performed. Rollback must coordinate the configured app version with the shipped client while preserving owned result recovery.
