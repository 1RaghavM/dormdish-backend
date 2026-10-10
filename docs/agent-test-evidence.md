# Agent contract testing evidence

Dedicated backend testing branch: `tests/dd-agent/backend-contracts`.
Implementation dependency: backend foundation contract PR (pending); test-only ownership is `tests/test_agent_contracts.py`, `tests/fixtures/agent/`, and this file. Rollout remains disabled until the implementation and companion tests have independent review and combined checks pass.

## Baseline

- Source: `bccc1d17afea6ead9b38d660f178d21e45262c55`.
- Python 3.11.13; Pydantic 2.10.4; FastAPI 0.116.2; pytest 8.2.2; pytest-asyncio 0.24.0.
- Command: `env -i PATH=/usr/bin:/bin /private/tmp/dd-agent-backend-contract-venv/bin/python -m pytest -q`.
- Result: **25 passed**, zero failures or skipped tests. Existing pytest-asyncio fixture-scope deprecation warning.
- Isolated environment created with `uv venv --python 3.11 /private/tmp/dd-agent-backend-contract-venv` and `uv pip install --python /private/tmp/dd-agent-backend-contract-venv/bin/python 'pydantic==2.10.4' 'fastapi==0.116.2' 'pytest>=8.2,<8.3' 'pytest-asyncio==0.24.0'`.
- No environment credentials, production data, or paid model calls used. No `.env` files are present in this isolated Git worktree.

## Pending contract coverage

Tests will exercise the production Pydantic models and shared JSON fixtures: input boundaries, UUID and owner-field rejection, finite bounded servings, deterministic total consistency, event envelopes, and recovery terminal semantics. Feature code will be explicitly merged at its recorded commit into this test branch; no production implementation will be copied into tests.

HTTP ownership, persistence, SDK execution, and end-to-end migration behavior are not implemented in this foundation slice and are not claimed as tested. Model field rejection alone does not establish endpoint authorization.
