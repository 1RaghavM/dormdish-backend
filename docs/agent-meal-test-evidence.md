# Independent meal-domain test evidence

Dedicated backend testing owner applied Ponytail full and authored `tests/test_agent_meal_correctness.py` in isolation. Only that test file and this evidence document are owned by the testing branch. No production code, dependency, fixture contract, route, or rollout flag is changed.

## Dependency and revision

- Feature: [B-MEALS PR #3](https://github.com/1RaghavM/dormdish-backend/pull/3), `feat/dd-agent/meal-correctness`, tested implementation `00b1b3701f034750af809e4d1016fb4382fee442`.
- Test branch: `tests/dd-agent/meal-correctness`; draft PR is stacked on the feature branch, not an independent implementation.
- Integration ancestor: `201ae232f759815e6c1338441030da08880984cf`, explicitly merging the feature into main `aa01f47b207886be3f099ace921bf6f768514a5e` before test authorship.
- Both feature and companion test PR require fresh independent review. After feature merge, retarget tests to main and rerun combined checks. Rollout remains disabled until both are reviewed, merged, and integration gates pass.

## Executed checks

Run from the isolated backend test worktree with Python 3.11.13, pytest 8.2.2, Pydantic 2.12.3:

```sh
/private/tmp/dd-agent-foundation-uv/bin/python -m pytest tests/test_agent_meal_correctness.py -ra
# 61 passed, 0 failed, 0 skipped
/private/tmp/dd-agent-foundation-uv/bin/python -m pytest -ra
# 216 passed, 0 failed, 0 skipped
```

Coverage includes duplicate food names with distinct trusted keys; stale keys after source, order, or fetch changes; fractional nutrition across every macro after substitution; requested local date across the UTC date boundary; exact university/hall/meal/date provenance; save-time freshness/context validation; missing nutrition exclusion; booleans, strings, NaN, infinity, and out-of-bound portions; forbidden model overrides; unknown IDs causing whole-proposal rejection; explicit safety metadata and normalized labels; malicious menu text remaining inert; already-eaten evidence deduplication without multiplying log servings; planned meals excluded; separate intake retained; clamped remaining targets; cross-user/date rejection; conflicting identities/macros and missing intake evidence rejected.

The prompt-injection case verifies only the deterministic service: hostile text cannot alter restrictions or add authority fields to the returned proposal. It does not claim LLM tool-policy or HTTP endpoint security coverage.

## Real behavior regression check

The same independent tests ran against the earlier service implementation `a5777998f9ee6d42bae985714cdcb1f5361278e7` without changing the checked-out production file. Python loaded `git show a577799:services/meal_planning.py` into `sys.modules['services.meal_planning']`, then invoked pytest with:

```text
tests/test_agent_meal_correctness.py -k 'unknown_or_restricted_allergen_metadata or conflicting_or_incomplete_intake' -ra
```

Result: **4 failed, 6 passed, 51 deselected**. Failures were behavior assertions (`DID NOT RAISE`), not imports: unknown allergen taxonomy, tree-nut alias mismatch, dairy/milk mismatch, and one food-log ID represented by two source identities. All pass against `00b1b3701f034750af809e4d1016fb4382fee442`. These defects were already fixed by the feature owner before this independent test assignment; no new production defect was found.

The installed pytest-asyncio plugin emits an existing unset-loop-scope deprecation warning. These tests are synchronous and do not use asynchronous fixtures.

## Remaining gates

No paid model, database, deployment, simulator, HTTP ownership, ingestion migration, or persisted completion checks ran here. This pure service does not establish authenticated storage provenance: integration must supply server-owned context, verified aware fetch timestamps and dietary/allergen metadata, and persisted consumed-food links. The existing ingestion/completion limitations in `docs/agent-meal-correctness.md` remain rollout blockers. Backend runtime/action, old-client compatibility, staging limits, and rollback require their own evidence. Rollback keeps the unused domain service and tests while the legacy generator remains selected.
