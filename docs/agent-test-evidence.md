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

## Foundation contract milestone

- Implementation: `482b2b10636b710173e7ecaa1a3fe88621d5d071`, explicitly merged from `feat/dd-agent/contracts` into the test branch. No production files copied or edited by the test author.
- Combined command: `env -i PATH=/usr/bin:/bin /private/tmp/dd-agent-backend-contract-venv/bin/python -m pytest -o addopts='' -q`.
- Environment now matches the foundation Pydantic **2.12.3** pin; upgrade the baseline environment with `uv pip install --python /private/tmp/dd-agent-backend-contract-venv/bin/python 'pydantic==2.12.3'`. Other baseline versions unchanged.
- Result: **121 passed** (96 new contract cases and 25 existing tests), zero failures or skipped tests; 0.36 seconds. Only the existing pytest-asyncio fixture-scope deprecation warning.
- Coverage: input/UUID/length boundaries, forbidden authority fields, strict confirmation versions, server-held action request shape, finite bounded portions/nutrition, totals after substitutions, duplicate canonical IDs, meal size bounds, citation protocols, canonical saved IDs, ordered recovery events, incomplete runs, conflicting/duplicate terminal events, and the cross-platform fixture for all six event types plus restored pending actions.
- No production defects reproduced. These are new contract tests; no meaningful old-behavior red/green is claimed because these models did not exist at the baseline.
- The final tested test-branch SHA is recorded in the companion PR after committing this evidence; reviewers must verify the current head and rerun if implementation changes.

The suite validates contract behavior, not trusted menu membership, allergen safety, HTTP ownership, duplicate database writes, SSE transport, SDK execution, or deployments. Those require later service/runtime/integration tests. The foundation remains rollout-disabled. The coordinator must verify the combined revisions in its isolated integration worktree and assign a fresh independent reviewer before merging the companion PR.
