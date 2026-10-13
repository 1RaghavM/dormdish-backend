# Hosted search cap regression tests

Dedicated test branch: `tests/dd-agent/hosted-search-cap`. Source dependency:
`fix/dd-agent/hosted-search-cap` (published revision and PR pending).
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

Green validation and independent review are pending. Rollout remains disabled
until source and companion tests pass review and merge requirements.
