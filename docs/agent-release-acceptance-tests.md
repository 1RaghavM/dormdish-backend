# Backend release acceptance evidence

Dedicated test PR: [#28](https://github.com/1RaghavM/dormdish-backend/pull/28).
Dependencies: smoke harness [#29](https://github.com/1RaghavM/dormdish-backend/pull/29) and atomic legacy saves [#31](https://github.com/1RaghavM/dormdish-backend/pull/31), both explicitly merged into the test branch.

## Revisions and scope

- Base application implementation: `e364f45a4ca9a318116c838930321732cd95428c`.
- Smoke implementation: `5fd495f` (successful tool-output validation; earlier executable revision `d742f7c64993232c7eabaeb5db1dcd70bc5b9830`).
- Test milestone: `3cdb2f18a44945a4a53fb98cfee976ac15c81a5e` (four release/rollback cases).
- Initial complete smoke tests: `cdd779d8f4340cba0874820cb0be8b8362d51ee1` (17 cases).
- Regression checkpoint: `a7c6eb966fa946bdd9f68423306a68ef0f2910f9` (27 cases; historical four cross-hall failures).
- Atomic source: `311a465c2289945c65fc0bd452f718653388af80`, documentation only after production `de9e12e03540618c38979f9518f0eef8019eb05e`.
- Current combined test code: `09776432319f6a49016ffae934c7570d73dfbbd0` (38 release cases plus existing adapter suites).
- Branch: `tests/dd-agent/release-acceptance`; worktree: `.migration-worktrees/backend-release-tests`.

Ponytail full applied. Owned files are `tests/test_agent_release_acceptance.py`, this evidence document, and the necessary fixture/assertion adaptations in `tests/test_legacy_adapters.py` and `tests/test_legacy_adapters_unit.py`. Existing fixtures are reused; no production code or migration changes. The test PR is stacked on the harness branch until its dependency merges. Both sources and the test PR require independent review; these results do not authorize rollout.

The new cases exercise cross-feature rollback after a real controlled save, not merely flags in isolation: all three disable mechanisms stop new writes while receipt/conversation recovery remains owner-scoped; deleting the conversation preserves the saved meal. A deterministic clock verifies that loading successive meal contexts consumes the full-day request deadline and never emits a partial successful plan.

The smoke tests execute the real pinned SDK offline and check network isolation even with credentials configured, required tools, usage totals, input bounds, missing credentials, actual cancellation of a stalled transport, incomplete scenario rejection, malformed stream sanitation, and byte-by-byte UTF-8/usage framing. A mocked live transport makes no paid API calls.

## Current combined checks

At `09776432319f6a49016ffae934c7570d73dfbbd0`:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q tests/test_agent_release_acceptance.py tests/test_legacy_adapters.py tests/test_legacy_adapters_unit.py
```

**99 passed, 0 failed, 0 skipped**, 54.51 seconds (`/private/tmp/dd-atomic-legacy-focused.log`). All eight concurrent endpoint cases now pass with unchanged saved-row assertions; only the synchronized write boundary moved from table insert to the new RPC, and the additive migration was installed in the disposable fixture. Separate focused concurrency check: 8 passed, 11.10 seconds (`/private/tmp/dd-atomic-legacy-initial.log`).

New real-SQL cases verify denied anon/authenticated execution, allowed service execution, lost/empty committed-response recovery with exactly one RPC, exact-ID replay and cross-owner collision isolation, preserved different-meal/full-plan policy, and repeated additive migration without changing existing rows. All four existing route lost-response assertions now require `auto_saved:true` and the exact canonical saved ID instead of tolerating a missing saved receipt.

The full combined suite passed on this exact committed code: **1083 passed, 0 failed, 0 skipped**, 338.31 seconds (`/private/tmp/dd-atomic-legacy-full.log`). Command: the same invocation without filenames. Fresh independent test review also ran these three files plus `tests/test_agent_legacy_save_unit.py`: **103 passed**, 60.86 seconds (`/private/tmp/dd-review-release-tests-focused.log`). No production code edits were made by this testing agent.

## Earlier executed checks

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q tests/test_agent_release_acceptance.py
```

Earlier smoke test milestone: **17 passed, 0 failed, 0 skipped**, 5.19 seconds; `/private/tmp/dd-release-backend-focused-final.log`. This ran the working tree immediately committed as `cdd779d8f4340cba0874820cb0be8b8362d51ee1`. The only warning is an upstream Starlette/AnyIO deprecation.

The complete merged backend suite was started at `3cdb2f18a44945a4a53fb98cfee976ac15c81a5e` with the same command omitting the test filename: **1045 passed, 0 failed, 0 skipped**, 428.26 seconds (`/private/tmp/dd-release-backend-full.log`). It includes the four original release cases. The subsequently added harness/regression cases have separate evidence below; the 1045 result must not be attributed to the later failing test checkpoint. Application source did not change between these runs.

## Reproduced defects and current gate

The reviewer found the smoke harness could return `passed` after an intake tool exception. Two independently authored regressions reproduced that false success and incorrect remaining calories (2000 instead of 1700): one genuine failure each against `d742f7c`, logs `/private/tmp/dd-release-intake-red.log` and `/private/tmp/dd-release-intake-math-red.log`. Source fix `5fd495f` was explicitly merged; the unchanged tests plus previous cases pass: **19 passed, 8 concurrency cases deselected**, 11.94 seconds (`/private/tmp/dd-release-smoke-fixed.log`).

A new concurrency regression models separate ASGI workers and synchronizes their final inserts. The fixture includes the coordinator-verified production unique index `saved_meal_plans_user_date_hall(user_id, date, dining_hall)` and canonical cached menus for Busch/Livingston. Two same-owner/date requests at the same hall save one row: **4 passed** across all legacy routes (`/private/tmp/dd-release-concurrent-workers.log`). At different halls, every route saves **two rows**, violating the existing daily-plan/meal admission policy: **4 failed**, 7.73 seconds (`/private/tmp/dd-release-concurrent-halls-correct.log`). This is a real behavioral failure with the production index shape, not an inference from a missing index. Source PR31 fixes this behavior; current green results are above. Fresh independent review, source dependency merges, and full combined checks remain merge gates. The original red logs are retained.

Discarded fixture attempts are not defect evidence: a barrier inside one shared TestClient blocked its sole event loop; the corrected test uses one TestClient per worker. A first Livingston fixture used a noncanonical menu ID; the corrected run above uses the actual expected ID and both requests load cached synthetic menus.

## Mutation sensitivity

Temporary `/private/tmp/dd_release_mutation.py` uses a pytest plugin to replace functions in memory, without editing repository production files. These are deliberate behavior mutations, not observed product defects:

- Reset each meal's remaining deadline to 45 seconds: **1 failed**, proving context work cannot silently reset the shared budget (`/private/tmp/dd-release-deadline-mutant.log`).
- Mark incomplete smoke scenarios successful: **3 failed, 1 passed**, rejecting missing usage, wrong portions, and omitted profile tools (`/private/tmp/dd-release-smoke-mutant.log`). The malformed-stream case still correctly fails closed.

Commands use `DD_RELEASE_MUTATION=deadline` or `scenario`, `PYTHONPATH=/private/tmp:.`, `-p dd_release_mutation`, and `-k full_day_context` or `-k incomplete_or_corrupt`. Removing the plugin yields the unchanged 17 passing tests.

## Environment and limits

Tests use disposable databases in the existing `dd-agent-migration-db` Docker container, with synthetic users/model responses. Docker socket and GitHub keychain access require sandbox escalation. The old temporary Python environment had missing installed package files (`py`, then `typing_extensions`); reinstalling committed requirements and pytest tooling repaired it before successful runs. Pinned Agents 0.8.1, OpenAI 2.26.0 and Pydantic 2.12.3 were preserved; unpinned google-auth resolved to 2.62.0 and soupsieve to 3.0.3. The import failures and initial Docker permission errors are environment failures, not regression red evidence.

Not checked here: paid/live model access, production data or schema, deployed Vercel limits/streaming, Gemini performance/cost baseline, native accessibility, or production rollout. Rollout stays disabled until the separate release gates pass.
