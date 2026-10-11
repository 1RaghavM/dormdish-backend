# Meal completion independent test evidence

Dedicated test branch: `tests/dd-agent/meal-completion`. Feature dependency: `feat/dd-agent/meal-completion` (first source checkpoint pending). Initial explicit integration base: `4b0efa96a4c487d5a5cebb1881f1d009a7069a81`.

Tests use synthetic users and saved rows in a disposable PostgreSQL database inside the local `dd-agent-migration-db` container. Only the session-token verification boundary is mocked; the existing authentication dependency, FastAPI request validation, service, SQL RPCs, transaction locks, and runtime intake tool execute normally. No model calls or production data.

Initial checkpoint contains test definitions and passes Python compilation. Behavior suite is **not yet run**, pending the explicit implementation merge. The tests cover selection nutrition, idempotency/recovery, concurrent and delayed writes, ownership, malformed inputs, immutable consumed snapshots after edits/deletion, explicit frozen undo, manual/linked intake, rollback, database roles, and account deletion.

Run with:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_meal_completion.py -q -o addopts=''
```

Rollout and production schema remain disabled/unapplied. A fresh independent review is required for this test PR and its feature dependency. The coordinator owns integration and rollout gates.

## First integrated behavior run

Explicitly merged feature source `86ff7f1b8444fdd0240bb2cf1e56a5f90dec624d` into test branch at `379bf5a880946aaf5a860bd153196e216497f275`. Companion feature PR: [#18](https://github.com/1RaghavM/dormdish-backend/pull/18); test PR: [#17](https://github.com/1RaghavM/dormdish-backend/pull/17), now targeting the feature branch.

- First focused run: **44 passed, 0 failed, 0 skipped**, 19.89 seconds (`/private/tmp/dd-completion-independent-focused.log`).
- Expanded run: **48 passed, 4 failed, 0 skipped**, 21.39 seconds (`/private/tmp/dd-completion-independent-expanded.log`). Four real behavior defects: malformed saved JSON at the root, nested meal plan, meal, or food level returned HTTP 503 instead of the documented 422. Reported to source owner; assertions retained unchanged for red/green verification.
- Four additional tests for multiple versions, old receipts/recheck, canonical ordering, and direct RPC catalog tampering are written but not yet run at this checkpoint.

One existing AnyIO/Starlette deprecation warning occurred; no paid model or production queries ran. Full-suite integration awaits the source fixes and final focused checks.

## Validation fix and duplicate/deletion race

Source fix `79fdf29be73437d24f85eedf4217bbdaa4ec1b51` was explicitly merged. The four malformed-row assertions passed unchanged; expanded dedicated suite reached **57 passed, 0 failed, 0 skipped** in 25.85 seconds, including preservation of a row inserted before migration (`/private/tmp/dd-completion-independent-final-focused.log`). Test code checkpoint: `a0a41e0bf1dd5e9b0b4285ebbca2ab34e6ee2a33`.

A subsequently added deterministic race reproduces a real HTTP 404 instead of the exact committed receipt: the original request commits and its plan is deleted after a duplicate's empty receipt read but before its current-plan lookup. The test uses a second real service instance and SQL deletion, not a mocked write result. **1 failed, 57 deselected**, 1.98 seconds at source `79fdf29` (`/private/tmp/dd-completion-receipt-race-red.log`). Source owner is fixing this via bounded receipt reconciliation; no write retry is authorized. The regression assertion remains unchanged.
