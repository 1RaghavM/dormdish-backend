# Explicit memory test evidence

This dedicated test PR verifies [source PR19](https://github.com/1RaghavM/dormdish-backend/pull/19) through authenticated FastAPI requests and real PostgreSQL RPCs. Only `tests/test_explicit_memory.py` and this evidence file are owned by the test author. Source revisions are explicitly merged; no source code is copied or changed by the test branch.

## Revisions and checks

- Source tested: `eee668b0506925896fae90f235389e9959f61d03`.
- Combined test/source code: `4ab56667cce10f1b2934b2894b291f54eb7be29e` on `tests/dd-agent/explicit-memory`.
- Original 65 dedicated cases: **65 passed, 0 failed, 0 skipped**, 28.67 seconds. The six additional typed-forget cases: **6 passed, 0 failed, 0 skipped** (65 deselected), 8.32 seconds.
- Complete backend suite at the combined code revision: **649 passed, 0 failed, 0 skipped**, 188.20 seconds, including all **71 dedicated memory cases**. Later changes to this evidence document do not change the tested code.
- Python runtime: `/private/tmp/dd-agent-runtime-uv/bin/python`; local Docker PostgreSQL container `dd-agent-migration-db`; every test module uses a disposable randomly named database. No production database, user records, or paid models are used. Model construction is forbidden for memory-command API cases.

Commands:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_explicit_memory.py -o addopts='' -q
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q
```

Coverage includes literal global and meal-scoped instructions; typed flavor/cuisine/texture provenance; global-to-meal splitting and global collapse; exact Unicode identity; temporary, inferred, negated, quoted, conditional, injected and hard-profile instructions; prior-turn reference ambiguity; kind/category-specific forgetting; ownership and session authentication; exact request replay and payload collisions; lost-write replies and delayed reconciliation; owner-concurrent mutations; history-epoch invalidation and stale in-flight resurrection; SQL-role denial; transactional receipt/history rollback; source isolation from assistant/tool history; expiry and account deletion; default-off behavior; existing preference button/read/delete contracts, including encoded slash names.

## Actual regression evidence

Tests ran against source `f8a51d9add17ceeb08f479ed7779e81757956dfc` before the fix and kept their assertions unchanged afterward.

- First expanded suite: **54 passed, 3 failed**, 0 skipped, 24.09 seconds. “Remember that I like rice for this meal” incorrectly persisted a permanent global preference. Deleting `Mac/Cheese? #%` returned HTTP 404 for both favorite and dislike routes.
- Expanded parser checks: **20 passed, 6 failed** (37 deselected), 8.73 seconds. “for this meal”, “for now”, “this week”, “during this conversation”, curly-quoted values and backtick-quoted values incorrectly persisted.
- Exact-name check: **1 failed** (63 deselected), 5.38 seconds. `Chef’s rice` was incorrectly stored as `Chef's rice`.
- All of these behavioral assertions passed unchanged against `eee668b`. Failures were actual HTTP/SQL outcomes, not import errors, missing contracts or weakened fixtures.

Local logs: `/private/tmp/dd-explicit-memory-tests-expanded.log`, `/private/tmp/dd-explicit-memory-parser-red.log`, `/private/tmp/dd-explicit-memory-unicode-red.log`, `/private/tmp/dd-explicit-memory-tests-fixed.log`, `/private/tmp/dd-explicit-memory-typed-forget.log`, `/private/tmp/dd-explicit-memory-tests-full.log`. The existing AnyIO deprecation warning is not a test failure.

## Dependencies and limits

PR19 is the test PR's base. Routes PR10/11, preference PR13/14 and completion source PR18 are upstream dependencies. Retarget only after dependencies land, then validate the resulting combined tree. Fresh independent reviews are required for source and tests. Keep rollout disabled until additive schema deployment, all companion PRs, compatibility checks and release gates pass. Rollback disables the feature without deleting memory or run receipts.

Not checked here: production SQL, live OpenAI calls, deployment limits, staging streams, native UI or manual accessibility. These local tests do not authorize activation or claim successful deployment.
