# Meal completion independent test evidence

Dedicated test branch: `tests/dd-agent/meal-completion`. Feature dependency: `feat/dd-agent/meal-completion` (first source checkpoint pending). Initial explicit integration base: `4b0efa96a4c487d5a5cebb1881f1d009a7069a81`.

Tests use synthetic users and saved rows in a disposable PostgreSQL database inside the local `dd-agent-migration-db` container. Only the session-token verification boundary is mocked; the existing authentication dependency, FastAPI request validation, service, SQL RPCs, transaction locks, and runtime intake tool execute normally. No model calls or production data.

Initial checkpoint contains test definitions and passes Python compilation. Behavior suite is **not yet run**, pending the explicit implementation merge. The tests cover selection nutrition, idempotency/recovery, concurrent and delayed writes, ownership, malformed inputs, immutable consumed snapshots after edits/deletion, explicit frozen undo, manual/linked intake, rollback, database roles, and account deletion.

Run with:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_meal_completion.py -q -o addopts=''
```

Rollout and production schema remain disabled/unapplied. A fresh independent review is required for this test PR and its feature dependency. The coordinator owns integration and rollout gates.
