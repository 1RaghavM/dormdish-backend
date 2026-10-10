# Independent runtime test evidence

Implementation: PR [#8](https://github.com/1RaghavM/dormdish-backend/pull/8), `ada94f78c866d7c14c08b74a80f48de4d144a394`. Test branch `tests/dd-agent/runtime` is explicitly stacked on that exact revision. Ponytail full was read and applied; no production code or dependency manifests changed.

First milestone: 26 newly authored cases pass using `/private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_agent_tools.py tests/test_agent_runtime.py -q` with `openai-agents==0.8.1`, `openai==2.26.0`, Python 3.11. The real Runner, Responses streaming, tool schemas, validators, usage accounting and SDK history serialization execute. Only HTTP and database transports contain synthetic data. No paid, external model, production database or deployment call is made.

Coverage: campus/hall/date/freshness mismatch, safety metadata, cross-owner/date food logs and duplicate intake, storage bounds, expired snapshots, profile privacy, exact menu-grounded math, read-only tool set, injected instructions remaining untrusted data, matching function-call/output replay, wire run/sequence identity, sanitized model failures, no HTTP retries, eight model turns and excessive model text. Runtime failures clear replay history and never produce a saved plan ID.

One initial failed assertion was a fixture error: this pinned SDK serializes tool dictionaries as Python literal strings, not JSON. The HTTP fixture now reads those synthetic outputs with `ast.literal_eval`; no production assertion was weakened.

Pending: cancellation/timeout/stream-malformation boundaries, additional budget/replay/repair checks, full local PostgreSQL suite and fresh independent review. Routes, authorization integration, hosted search, completion sync, live-model availability, Vercel limits and rollout remain separate prerequisites; this test PR does not enable anything.
