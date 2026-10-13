# Synthetic baseline test evidence

Dedicated [test PR #34](https://github.com/1RaghavM/dormdish-backend/pull/34):
`tests/dd-agent/baseline-validation`, based on main `3609664`. Companion
[implementation PR #35](https://github.com/1RaghavM/dormdish-backend/pull/35):
`feat/dd-agent/baseline-validation`, explicitly merged at `0a7a1a0`.
Ponytail full applied. Test ownership is limited to
`tests/test_agent_baseline_compare.py` and this evidence file.

The tests start a fresh interpreter to forbid app/database imports and real HTTP
transports, then exercise the default command. Gemini's offline path uses the
existing generator with a synthetic response; separate tests exercise the real
Google SDK with only HTTP transport mocked. Luna uses the real Agents/OpenAI SDKs.

Coverage includes explicit live opt-in and both keys, bounded repetition and
provider retries, per-request usage including failed/incomplete responses,
deadline cancellation/late results, failed context tools, private error output,
and shared menu, dietary, preference, target, and finite nutrition validation.

At executable test commit `0684c30` with source `0a7a1a0`:

```sh
/private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q \
  tests/test_agent_baseline_compare.py tests/test_agent_runtime.py tests/test_agent_tools.py
```

Result: **78 passed, 0 failed, 0 skipped in 2.87s**, including 29 new tests.
Local log: `/private/tmp/dd-baseline-tests-combined.log`. Focused run: 29 passed
in 2.41s, `/private/tmp/dd-baseline-tests-focused.log`.

A temporary in-memory mutation removed the finite-nutrition guard and restored
completed-only usage capture. The relevant checks produced **4 expected failures,
1 pass, 24 deselected**: both NaN cases and usage from incomplete/failed responses.
No source files were altered; these were behavioral failures, not import errors.
Local log: `/private/tmp/dd-baseline-tests-negative.log`.

Both PRs require fresh independent review and the coordinator's dependency gates
before merge. This file is a documentation-only update after the tested commit.

No live calls, credentials, deployment, or rollout changes are part of this test PR.
