# Explicit memory test evidence

This dedicated test PR verifies [source PR19](https://github.com/1RaghavM/dormdish-backend/pull/19) through authenticated FastAPI requests and real PostgreSQL RPCs. Only `tests/test_explicit_memory.py` and this evidence file are owned by the test author. Source revisions are explicitly merged; no source code is copied or changed by the test branch.

## Revisions and checks

- Source tested: `60a14f2928bbb9fbdeba093b5c7079926389ab56` (fresh review parser fix).
- Combined test/source code: `a82df6127f8af5690b6a6bd0bd5859d96bbe1052` on `tests/dd-agent/explicit-memory`.
- Original 65 dedicated cases: **65 passed, 0 failed, 0 skipped**, 28.67 seconds. The six additional typed-forget cases: **6 passed, 0 failed, 0 skipped** (65 deselected), 8.32 seconds.
- Complete backend suite at the combined code revision: **668 passed, 0 failed, 0 skipped**, 135.70 seconds, including all **71 dedicated memory cases**. The coordinator explicitly merged the reviewed parser fix and reran the complete suite; the 71 independent test cases remain unchanged. Later changes to this evidence document do not change the tested code.
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

Latest full-suite log: `/private/tmp/dd-explicit-memory-review-fix-integrated.log`.

Earlier regression logs: `/private/tmp/dd-explicit-memory-tests-expanded.log`, `/private/tmp/dd-explicit-memory-parser-red.log`, `/private/tmp/dd-explicit-memory-unicode-red.log`, `/private/tmp/dd-explicit-memory-tests-fixed.log`, `/private/tmp/dd-explicit-memory-typed-forget.log`, `/private/tmp/dd-explicit-memory-tests-full.log`. The existing AnyIO deprecation warning is not a test failure.

## Dependencies and limits

PR19 is the test PR's base. Routes PR10/11, preference PR13/14 and completion source PR18 are upstream dependencies. Retarget only after dependencies land, then validate the resulting combined tree. Fresh independent reviews are required for source and tests. Keep rollout disabled until additive schema deployment, all companion PRs, compatibility checks and release gates pass. Rollback disables the feature without deleting memory or run receipts.

Not checked here: production SQL, live OpenAI calls, deployment limits, staging streams, native UI or manual accessibility. These local tests do not authorize activation or claim successful deployment.

## Literal-name grammar review follow-up (in progress)

The agreed contract now accepts a bare single Unicode/alphanumeric token (internal apostrophe/hyphen permitted), or an explicit JSON string for a multiword or punctuation-bearing name. SET may have an exact outside-string breakfast/lunch/dinner suffix; FORGET may not. Decoded names retain internal spelling, punctuation and spaces, with existing surrounding-whitespace trimming and the 1–200 character limit. Malformed JSON and control characters are rejected. Whole-command quotes never authorize writes.

This deliberately changes the earlier unquoted `Chef’s rice` positive case to `"Chef’s rice"`; the unquoted form now has its own negative test. That prior expectation was changed for the approved grammar contract, not to conceal a failing invariant.

Before the source fix, source `60a14f2928bbb9fbdeba093b5c7079926389ab56` produced **7 actual assertion failures**, 71 deselected, 0 skipped in 4.93s. Reported `rice on Friday`, `rice briefly`, `rice assuming availability`, typed `spicy on weekends`, and forget variants all claimed a memory change; the deliberately tightened unquoted multiword case also wrote. Raw log: `/private/tmp/dd-explicit-memory-grammar-red.log`. The seven regression assertions remain unchanged. Strict JSON positives/negatives are authored; source merge and green/full runs pending.

The first expanded grammar suite passed **128 tests**, 0 failures/skips, in 69.10s at combined `581017e93684db673023f7a0e7f03401a972ca5d` / source `09dc425fca398fef5953b18c290ef0d8363cdbaa` (`/private/tmp/dd-explicit-memory-grammar-focused.log`). Further independently authored same-conversation checks found that a rejected target still erased existing SDK replay history. Three executed history assertions failed (`[]` versus the stored prior turn); six additional bare grammatical-placeholder cases incorrectly wrote memory. This follow-up ran **9 failed, 2 passed**, 128 deselected, 0 skipped, 5.69s (`/private/tmp/dd-explicit-memory-replay-placeholder-red.log`). These 11 new assertions block acceptance pending the source fix and will remain unchanged. No source edits were made by the test author.
