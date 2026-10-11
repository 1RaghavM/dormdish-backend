# Controlled save — independent test evidence

Owner: dedicated test author. File under test: `tests/test_controlled_save.py` (40 cases). Real FastAPI/SSE
routes, real Agents SDK runtime, real PostgreSQL RPCs (Docker `dd-agent-migration-db`, PG17, throwaway DB).
Only session auth (`verify_session_token`) and the model HTTP transport are synthetic, as in existing route tests.

## SHAs

| | SHA |
| --- | --- |
| Base | `fdec0fea4aacfe84fe40a72adec5067d44c0de17` |
| Tests (red commit) | `28c9e53e8896fa48c7d1a62e89b7e92b2e129a12` |
| Source tested | `262b3a510878f3b767dd6373df42797fae66c029` (`feat/dd-agent/controlled-save`) |
| Merge of source into tests | `fc47c6d` (explicit `git merge --no-ff`) |

## Runs

| Run | Result | Log |
| --- | --- | --- |
| Red, base `fdec0fe` | 12 failed (all `AssertionError`), 28 passed | `/private/tmp/dd-controlled-save-tests-red.log` |
| Dedicated, source `262b3a5` | 40 passed, 0 failed, 0 skipped | `/private/tmp/dd-controlled-save-tests-green.log` |
| Full suite, source `262b3a5` | 919 passed, 0 failed, 0 skipped, 250s | `/private/tmp/dd-controlled-save-tests-full.log` |

Command: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q`.
919 = 863 baseline + 40 dedicated + 16 implementer unit tests.

Red failures were behavior, not setup: authorized SAVE/REPLACE still emitted `action_required` with no write
(7), guard paths persisted an action instead of a terminal `error` (3), and lost-reply/owner-scoped saves
produced no plan (2). The 28 base passes are regression controls (flag off, non-authorizing text, existing
plan + SAVE, stale profile, model/history claims). The migration fixture applies
`migrations/add_agent_controlled_save.sql` only when present, so the base run reached assertions.

## Coverage

- Flag unset/`false`: explicit save still previews, no write.
- Authorized SAVE (4 phrasings incl. `please`, case, whitespace): no `action_required`, `complete.saved_plan_id`
  equals the only row, correct date/hall/totals, run replay equals stream, duplicate request replays bytes
  without a model call or second write, profile and preferences unchanged.
- 14 non-authorizing texts (questions, every negation token, hedges, mid-sentence "save", `saved`) and REPLACE
  with no plan: preview only.
- Exact REPLACE/OVERWRITE (3 phrasings): only dinner replaced, breakfast/lunch preserved, day totals recomputed
  (600/21/11.5/99), existing id kept, replay without rewrite.
- Existing plan + SAVE, mismatched meal, `?`, negation, `without`: preview, plan untouched.
- Guards injected at the write RPC after the model finishes: plan edited (`stale_plan`), competing same-date
  plan at another hall (`stale_plan`), profile changed (`stale_profile`), proposal expired (`expired`); each
  terminal `error`, `retryable: true`, no action, no write, no fallback write, GET run equals stream.
- Lost committed write reply: reconciled via `GET /agent/runs/{request_id}`, one write RPC, duplicate POST
  replays without a model call.
- Earlier "save" turn does not authorize a later exploratory turn; forged assistant text claiming user
  authorization does not authorize; another user's conversation returns 404 and another user's same-date plan
  neither blocks nor is touched.

## Contract alignment

Implementer doc `docs/agent-controlled-save.md` (`eb89547`) deviations, both judged safe and adopted:

1. Lost write reply: the route reads the stored run and streams the terminal outcome when committed, instead of
   always ending without a terminal. The test accepts either, but requires the streamed events to equal the
   stored run when a terminal is streamed.
2. The save confirms under a derived internal request id (`md5('dormdish-controlled-save:' || request_id)`),
   adding one internal `agent_runs` row and one extra quota unit per save, like a manual Apply. Not asserted.

Test-author correction: bare `save` matches the agreed regex, so it was removed from the non-authorizing list.

## Source defects

None found against `262b3a5`.

Not covered: `quota` save error code, concurrent duplicate saves of the same request, and production schema
deployment (not applied anywhere).
