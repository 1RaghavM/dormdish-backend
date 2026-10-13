# Native availability test evidence

Dedicated test PR: [#39](https://github.com/1RaghavM/dormdish-backend/pull/39).
Source dependency: [#38](https://github.com/1RaghavM/dormdish-backend/pull/38),
`f76cf6a7f7e6b23b2e62e892eae016363de7669a`, explicitly merged into
tested commit `8ec42a8e55f987025da7236eaa2410d597478fed`.

The 23 new HTTP tests use the real authentication dependencies with synthetic session lookup.
They cover internal/percentage admission, invalid credentials, spoofed owner claims, malformed
rollout and kill switches, independent completion/preference flags, and `Cache-Control: no-store`.
Storage, model, and target constructors deliberately fail if discovery invokes them.

On 2026-10-13, all 23 cases failed on prior source
`b7e8182f6f8d9ff1c945c3e4fbb8dbb8d46a5519` because the endpoint returned HTTP 404.
All 23 passed on the combined commit in 0.80s. Logs:
`/private/tmp/dd-app-availability-red.log` and `/private/tmp/dd-app-availability-green.log`.

Focused regression command:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q \
  tests/test_agent_app_availability.py tests/test_agent_routes_unit.py \
  tests/test_agent_internal_rollout.py tests/test_agent_routes.py
```

Result: **97 passed, 0 failed, 0 skipped**, 15.29s, on the combined commit above.
Log: `/private/tmp/dd-app-availability-focused.log`. PostgreSQL fixtures were local and synthetic.
The existing AnyIO deprecation warning remains unrelated to this feature.

Ponytail full applied using installed 5.2.0 because the specified 5.1.0 path was unavailable.
Only this evidence file and the new test file are test-owned changes. Merge source first,
then retarget and merge this independently reviewed test PR before enabling dependent UI.
No Gemini build/benchmark, paid model, production-data test, or full backend rerun was performed.
