# Independent food-log permission tests

Companion to source PR [#21](https://github.com/1RaghavM/dormdish-backend/pull/21), source `e5cc959c28ca30ffe31f4be996f0d8f4c8fbd05a`. Test PR [#22](https://github.com/1RaghavM/dormdish-backend/pull/22), tested code `92a7741965d270276b30798816e6d0ebe939566e`. Tests own no production code or SQL. Ponytail full applied.

The synthetic PostgreSQL fixture reproduces all 17 observed food-log column definitions, the primary key, RLS, the PUBLIC `USING (true)` policy and verified anon/authenticated SELECT grants. It proves both client roles can read a synthetic pre-migration entry. Migration is applied twice before testing retained rows/IDs and denied client CRUD. Old mutation privileges were not inferred from the observed SELECT grants.

Actual FastAPI add/list/delete handlers and session validation run against local PostgreSQL as service_role through a small PostgREST transport shim. No authentication or route business logic is mocked. The unrelated referral hook is mocked, environment credentials are removed on import, and socket networking is blocked during requests. Tests cover owned add/list/delete wire responses, forged body owner, other-owner/date read isolation, non-disclosing foreign-delete no-op, and missing/invalid/expired sessions on every route.

Executed with `/private/tmp/dd-agent-runtime-uv/bin/python` and `DD_AGENT_LOCAL_PG=1`:

| Revision/check | Command | Result | Local evidence |
| --- | --- | --- | --- |
| `406b7fcfdfaa74bd1bceae1c9867e2cd9576adba`, exact-schema focused | `python -m pytest -o addopts='' -q tests/test_food_log_access.py` | 14 passed, 0 failed/skipped, 8.24s | `/private/tmp/dd-food-log-tests-focused.log` |
| Final `92a7741`, old-policy control | Same focused command with `DD_FOOD_LOG_MIGRATION=/private/tmp/dd-food-log-noop.sql` | 3 failed, 11 passed, 0 skipped, 7.31s | `/private/tmp/dd-food-log-tests-red.log` |
| Final `92a7741`, complete suite | `python -m pytest -o addopts='' -q` | 428 passed, 0 failed/skipped, 37.53s | `/private/tmp/dd-food-log-tests-full.log` |

The negative control substitutes a SQL comment for this new migration, retaining the reconstructed old policy/grants. Identical final assertions fail because anon/authenticated SELECT succeeds and the PUBLIC policy remains. These are executed permission/assertion failures, not imports or compilation errors. The final test-only change captures preservation evidence immediately after migration so later fixtures cannot affect that assertion. All 14 new tests pass in the final full run. One pre-existing Starlette/AnyIO deprecation warning remains.

All PostgreSQL tests use disposable synthetic databases in local `dd-agent-migration-db`. No production rows, live authenticated service, deployed PostgREST, or deployment have been exercised. Source and companion tests need independent review before production application; rollback must preserve the access restriction and all food-log rows. Subsequent evidence-only commits do not change the tested code.
