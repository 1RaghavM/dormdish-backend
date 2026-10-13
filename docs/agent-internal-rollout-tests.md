# Internal-user rollout tests

Independent test branch: `tests/dd-agent/internal-rollout`. Implementation dependency: [PR #32](https://github.com/1RaghavM/dormdish-backend/pull/32), `feat/dd-agent/internal-rollout`.

Coverage includes canonical UUIDs, whitespace, zero-percent internal access, unchanged stable percentage cohorts, malformed list entries, disabled/kill/invalid-percent precedence, real authenticated HTTP identity spoof attempts, and owner-only restore/reconcile/delete while writes are disabled. HTTP tests use existing FastAPI authentication dependencies and real disposable PostgreSQL storage; no paid model or production data.

Before implementation, on `e364f45a4ca9a318116c838930321732cd95428c`:

```sh
DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -o addopts='' -q tests/test_agent_internal_rollout.py
```

Result: **7 failed, 15 passed, 0 skipped** in 5.17s. Genuine failures: allowed internal UUID and authenticated owner still received 503 at zero percent; all five malformed internal lists were silently accepted at 100 percent. Log: `/private/tmp/dd-internal-rollout-red.log`.

Combined implementation validation pending. Keep rollout disabled until implementation and companion tests receive independent reviews and all release gates pass. These tests do not verify live Vercel deployment, SDK/model access, or production configuration. No schema migration or dependency changes.
