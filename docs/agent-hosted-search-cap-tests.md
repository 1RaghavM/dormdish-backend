# Hosted search cap regression tests

Dedicated test branch: `tests/dd-agent/hosted-search-cap`. Source dependency:
[PR #36](https://github.com/1RaghavM/dormdish-backend/pull/36), source
`b4934f5c202ecb1020abf434d422b8e09076f5b2`. Companion tests:
[PR #37](https://github.com/1RaghavM/dormdish-backend/pull/37).
Ponytail full applied; only the new test file and this evidence document are owned here.

The synthetic HTTP fixture models the observed Luna response: overall completed,
one completed hosted search, one ignored searching entry, reasoning, and a cited
message. The real OpenAI and Agents SDKs decode it and execute the full tool loop;
no paid model, credential, private data, or Gemini run is used.

The tests require usable cited evidence while preserving a public-only query,
`max_tool_calls=1`, `store=false`, one hosted request, and no retry even if the
model requests another search. Negative cases reject multiple completed searches,
no completed search, reversed entries, failed entries/responses, incomplete
responses, and missing citations. Existing public-search tests cover deadlines,
cancellation, response bounds, and malicious source content.

## Initial regression

Against main `3609664602267f2a24d4c1ecade3a59be4889697`:

```sh
/private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q tests/test_agent_hosted_search_cap.py
```

Result: **1 failed, 9 passed**. The valid capped response is rejected as
`This tool could not verify the requested data`; the fixture asserts that this
failure fallback must not replace verified evidence. Log:
`/private/tmp/dd-hosted-search-red.log`.

## Combined validation

Source revision above was explicitly merged into the isolated test branch as
`6482f84a905dcd7dc16adb51ae1d915636d4107a` (test commit `ce5c902`).

```sh
/private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q tests/test_agent_hosted_search_cap.py tests/test_agent_public_search.py tests/test_agent_runtime.py
```

Result: **77 passed, 0 failed, 0 skipped in 1.96 seconds**. Log:
`/private/tmp/dd-hosted-search-green.log`. This includes the existing real HTTP
body deadline/cancellation and failure sanitization tests, not only new cases.

No database migration or production data change is needed. Merge reviewed source
#36 before this explicitly stacked test PR; keep rollout disabled until both
reviews/merges and the coordinator's live validation gates pass. Rollback remains
the existing kill switch. Live hosted calls and deployment were not run by this
test author. Independent review is pending.
