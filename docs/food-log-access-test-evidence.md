# Independent food-log permission tests

Companion to source PR [#21](https://github.com/1RaghavM/dormdish-backend/pull/21), source `e5cc959c28ca30ffe31f4be996f0d8f4c8fbd05a`. Tests own no production code or SQL. Ponytail full applied.

The synthetic PostgreSQL fixture reproduces all 17 observed food-log column definitions, the primary key, RLS, the PUBLIC `USING (true)` policy and verified anon/authenticated SELECT grants. It proves both client roles can read a synthetic pre-migration entry. Migration is applied twice before testing retained rows/IDs and denied client CRUD.

Actual FastAPI add/list/delete handlers and session validation run against local PostgreSQL as service_role through a small PostgREST transport shim. No authentication or route business logic is mocked. The unrelated referral hook is mocked, environment credentials are removed on import, and socket networking is blocked during requests. Tests cover owned add/list/delete wire responses, forged body owner, other-owner/date read isolation, non-disclosing foreign-delete no-op, and missing/invalid/expired sessions on every route.

Initial API/SQL test draft: 14 passed, 0 failed/skipped in 8.54s. Fixture refined to the exact production column types after receiving metadata; final focused/full runs and unchanged no-op migration negative control pending.

All PostgreSQL tests use disposable synthetic databases in local `dd-agent-migration-db`. No production rows, live authenticated service, deployed PostgREST, or deployment have been exercised. Source and companion tests need independent review before production application; rollback must preserve the access restriction and all food-log rows.
