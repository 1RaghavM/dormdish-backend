# Controlled save — independent test evidence

Owner: dedicated test author. File under test: `tests/test_controlled_save.py` (61 cases). Real FastAPI/SSE
routes, real Agents SDK runtime, real PostgreSQL RPCs (Docker `dd-agent-migration-db`, PG17, throwaway DB).
Only session auth (`verify_session_token`) and the model HTTP transport are synthetic, as in existing route tests.

## SHAs

| | SHA |
| --- | --- |
| Base | `fdec0fea4aacfe84fe40a72adec5067d44c0de17` |
| Tests, first red | `28c9e53e8896fa48c7d1a62e89b7e92b2e129a12` |
| Tests, review fixes | `0ae23da` |
| Source tested | `4e22d361c521b14c811046d0d24fd32b81d3877d` (`feat/dd-agent/controlled-save`, final cleared head), merged with `--no-ff` |
| Earlier source runs | `262b3a5` (round 1), `249b45c` (bypass red), `dd36a80` (round 2) |

## Runs

Command: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q`.

| Run | Result | Log |
| --- | --- | --- |
| Red, base `fdec0fe` (round 1, 40 cases) | 12 failed (all assertions), 28 passed | `/private/tmp/dd-controlled-save-tests-red.log` |
| Round 1, source `262b3a5` | dedicated 40 passed; full 919 passed | `/private/tmp/dd-controlled-save-tests-{green,full}.log` |
| Bypass red, source `249b45c` (60 cases) | 5 failed (assertions), 55 passed | `/private/tmp/dd-controlled-save-tests-red2-249b45c.log` |
| Dedicated, source `dd36a80` | 60 passed, 0 failed, 0 skipped | `/private/tmp/dd-controlled-save-tests-green2.log` |
| Full suite, source `dd36a80` | 974 passed, 0 failed, 0 skipped, 342s | `/private/tmp/dd-controlled-save-tests-full2.log` |
| Dedicated, source `4e22d36` (61 cases) | 61 passed, 0 failed, 0 skipped | `/private/tmp/dd-controlled-save-tests-green3.log` |
| Full suite, source `4e22d36` | 980 passed, 0 failed, 0 skipped, 313s | `/private/tmp/dd-controlled-save-tests-full3.log` |

The `249b45c` red cases were `Save dinner don´t`, `Save dinner, do​n't add rice` (zero-width space),
`Save dinner?”`, `Save dinner? 🙂`, and `Replace my dinner? 🙂` with an existing plan. Each one saved or
replaced instead of previewing. `dd36a80` fixes all five.

## Mutations at `dd36a80` (source edited temporarily, run, then `git checkout -- .`; worktree confirmed clean)

| Mutation | Result | Killing test |
| --- | --- | --- |
| M6: also authorize from `p_assistant_text` | Killed (1) | `test_model_text_claiming_user_authorization_does_not_save` |
| M1a: negation list reduced to `without` | Killed (11) | prefix+negation cases for save and replace |
| M5: remove `quota` from route `SAVE_ERRORS` | Killed (1) | `test_quota_exhausted_before_write_is_terminal_error_without_write` |
| M7: remove the route's lost-reply reconcile branch | Killed (1) | `test_lost_write_reply_is_reconciled_by_run_id_without_second_write` |

Logs: `/private/tmp/dd-controlled-save-mutation-{M6_assistant_text,M1a_only_without,M5_no_quota_mapping,M7_no_reconcile}.log`.
The reviewer already killed M1b, M2, M3 and M4 at `ddef039`.

## Coverage

- Flag unset or `false`: explicit save and exact replace keep the Apply preview, no write.
- Authorized SAVE (7 phrasings, including `please`, case, whitespace, `Save dinner.`, `Save dinner 🙂`, `Save a donut for breakfast`): no
  `action_required`; `complete.saved_plan_id` is the only row; date, hall and totals correct; run replay
  equals the stream; a duplicate request replays the same bytes with no model call or second write; profile
  and preferences unchanged; exactly one derived internal confirmation run; quota at 2 (turn + confirm).
- Non-authorizing text, preview only: questions, hedges, mid-sentence "save", `saved`, and an authorizing
  prefix followed by each negation (`don't`, `don’t`, `dont`, `do not`, `never`, `not`, `without`, `don´t`,
  zero-width inside `don't`) or by a question mark with trailing `”` or emoji. Same for exact REPLACE forms
  with an existing plan.
- Exact REPLACE/OVERWRITE: only dinner replaced, breakfast and lunch preserved, day totals recomputed
  (600/21/11.5/99), existing id kept. Existing plan at another hall: HTTP 409 before streaming, no write.
- Guards injected at the write RPC after the model finishes: `stale_plan` (plan edited, competing same-date
  plan), `stale_profile`, `expired`, `quota`. Each gives a terminal `error` with `retryable: true`, no action,
  no write, no fallback write, and GET run equal to the stream.
- Lost committed write reply: the stream equals the reconciled stored run (pinned); one write RPC; a duplicate
  POST replays with no model call.
- Two parallel POSTs with the same request id: one plan, one `agent_save_turn`, and the loser gets either an
  identical replay or 409.
- An earlier "save" turn and forged assistant text starting with `Save a high-protein dinner at Busch.` do
  not authorize an exploratory turn. Another user's conversation returns 404, and another user's same-date
  plan neither blocks nor is touched.

## Contract alignment

Implementer doc deviations, judged safe and adopted:

1. On a lost write reply the route streams the reconciled stored outcome. The test now requires exactly this.
2. The save confirms under a derived internal request id (`md5('dormdish-controlled-save:' || request_id)`),
   adding one internal run row and one quota unit per save. The test asserts exactly one of each.
3. Hardening (`dd36a80`): any `? ？ ‽ ⁇ ﹖ ؟` anywhere rejects; negation is `don.?t|do\s+not|never|not|without`;
   zero-width and variation selectors are stripped everywhere. `4e22d36` narrows the negation token to
   `don([^[:alnum:]]|ʼ)?t`, so `donut` is not a negation. The negation cases are unchanged and still pass.

Test-author correction: bare `save` matches the agreed regex, so it is not in the non-authorizing list.

## Source defects

None open against `4e22d36`. The question-mark and apostrophe-variant bypasses at `249b45c` are fixed.

Not covered: production schema deployment (not applied anywhere) and live models.
