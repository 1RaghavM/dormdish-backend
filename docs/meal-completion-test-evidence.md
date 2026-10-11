# Meal completion independent test evidence

Feature [PR #18](https://github.com/1RaghavM/dormdish-backend/pull/18), branch `feat/dd-agent/meal-completion`; companion [test PR #17](https://github.com/1RaghavM/dormdish-backend/pull/17), branch `tests/dd-agent/meal-completion`. The test PR explicitly stacks the feature and requires its own fresh independent review.

## Verified revisions and results

- Source head: `50b6f0a7c816035af10228312b4c6d9828dce134` (production code `68d493e346647792f31a82d4c93111ccf55201a3`).
- Tested combined source/test code: `a6aea56cfc2b9a3b1ce15ae86613869d8a2e0a79`. The subsequent evidence update changes documentation only.
- Dedicated tests: **61 passed, 0 failed, 0 skipped**, 50.68 seconds. Log: `/private/tmp/dd-completion-quota-dedicated.log`.
- Full backend suite: **621 passed, 0 failed, 0 skipped**, 190.68 seconds. Log: `/private/tmp/dd-completion-quota-full.log`.
- Both runs emitted one existing Starlette/AnyIO deprecation warning.

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_meal_completion.py -q -o addopts=''
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''
```

Tests use synthetic users and saved rows in disposable PostgreSQL 17 databases inside the local `dd-agent-migration-db` container. Only session-token verification and the PostgREST transport boundary are replaced; the existing authentication dependency, FastAPI validation, service, SQL RPCs, transaction locks, and actual runtime intake tool execute normally. The test transport preserves real database errors, including `completion_*` codes. No paid model calls or production data.

## Coverage and real regressions

The 61 cases cover trusted partial/whole nutrition without multiplying servings twice; custom authentication and owner/date isolation; forged/invalid inputs; duplicate receipts and conflicting request IDs; concurrent distinct toggles and delayed stale revisions; plan edits/deletion/owner changes without loss of actual intake; explicit frozen undo/recheck; independent manual logs and trusted linked-source deduplication; conflict and record-bound fail-closed behavior; transactional rollback and ambiguous transport recovery; untrusted database roles; account deletion and old-receipt retention; preserved legacy row data and additive migration compatibility.

Review follow-up [5484296809](https://github.com/1RaghavM/dormdish-backend/pull/17#pullrequestreview-5484296809) adds two quota tests through the actual HTTP/service/SQL path. Bulk synthetic history efficiently exercises the 1,000th accepted and 1,001st rejected rolling-day write, expired receipts reopening one slot, the 100th accepted and 101st rejected owner/date version, existing-version updates at capacity, exact replay at both caps, and owner/date isolation. Rejections return HTTP 429 without changing recorded states/revisions or creating receipts. The focused run passed **2, failed 0, skipped 0** (59 deselected), 5.99 seconds; `/private/tmp/dd-completion-quota-focused.log`.

Separate temporary migration copies changed each cap comparison from `>=` to `>`; each unchanged regression failed because the rejected boundary write returned HTTP 200 rather than 429. Write-cap control: **1 failed**, 60 deselected, 2.67 seconds (`/private/tmp/dd-completion-quota-writes-mutant.log`). Version-cap control: **1 failed**, 60 deselected, 3.43 seconds (`/private/tmp/dd-completion-quota-versions-mutant.log`). These are mutation controls, not defects claimed in the current source. The controls only changed temporary migration copies loaded into disposable databases; repository production files remained untouched.

Two behavior defects were reproduced and fixed by the feature owner without weakening assertions:

1. At source `86ff7f1b8444fdd0240bb2cf1e56a5f90dec624d`, four malformed saved JSON shapes returned 503 instead of documented 422. Expanded run: **48 passed, 4 failed**, 21.39 seconds (`/private/tmp/dd-completion-independent-expanded.log`). Source fix `79fdf29be73437d24f85eedf4217bbdaa4ec1b51` made these same assertions pass.
2. At source `79fdf29`, a duplicate returned 404 if the original request committed and its plan was deleted between the duplicate's empty receipt read and plan lookup. The deterministic test uses a second real service instance and SQL deletion. **1 failed, 57 deselected**, 1.98 seconds (`/private/tmp/dd-completion-receipt-race-red.log`). Source fix `68d493e` adds bounded receipt reconciliation without retrying writes. Final tests verify exact 200 replay and 409 for a conflicting body in this race.

Intermediate checkpoints were pushed with honest unrun/failing evidence. The test PR changes only this document and `tests/test_meal_completion.py`; no production source or historical fixture was edited. Git identity is `1RaghavM <lebrongoated2323@gmail.com>` and no co-author trailers were added.

## Remaining gates

Production SQL, deployment health, live models, native simulation, and UI accessibility were not performed by this backend test task. Native completion integration has its own owner and tests. `DORMDISH_COMPLETION_ENABLED` remains off. Apply schema first, satisfy feature/test independent reviews and dependency gates, and verify native integration before activation. Local suite success does not establish those rollout gates.
