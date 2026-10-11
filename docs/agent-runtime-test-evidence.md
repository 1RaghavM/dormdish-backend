# Independent runtime test evidence

Implementation: PR [#8](https://github.com/1RaghavM/dormdish-backend/pull/8), `9bc049af57a2dae45895c2614b42facded09dfc5` (current-main integration). Test PR [#9](https://github.com/1RaghavM/dormdish-backend/pull/9), branch `tests/dd-agent/runtime`, is explicitly stacked on that revision. First pushed milestone: `58143bb32856d4a18290b3996fc017ba4ef325d5`. The PR records its final tested test head. Ponytail full was read and applied; no production code or dependency manifests changed.

## Commands and results

Environment: Python 3.11, `openai-agents==0.8.1`, `openai==2.26.0`. Run from the test worktree with `/private/tmp/dd-agent-runtime-uv/bin/python` (or an environment installed from the pinned manifests).

- `python -m pytest tests/test_agent_tools.py tests/test_agent_runtime.py -q -o addopts=''`: 49 new independent tests passed, zero failures/skips.
- `DD_AGENT_LOCAL_PG=1 python -m pytest -q -o addopts=''`: 343 combined tests passed, zero failures/skips, 17.78s at merge `ff247d3db90c2d6d30d533efef4f1dd48aa24265`; log `/private/tmp/dd-runtime-tests-main-integration.log`. PostgreSQL tests use only local Docker `dd-agent-migration-db` with unique synthetic databases dropped by fixtures.
- `git diff --check`: passed.
- Two isolated temporary source mutations were rejected by assertion failures: deleting the food-log owner filter; increasing the SDK model-turn bound from eight to nine. Original production files and branches were not modified. Both original behaviors pass. This is mutation evidence, not a claim that either defect existed in the feature.

The real Runner, Responses streaming, tool schemas, validators, usage construction and SDK history serialization execute. Only HTTP and database transports are synthetic. No paid/external model, production database or deployment call is made. Fixture corrections handled the pinned SDK's Python-literal tool outputs and a duplicated synthetic tool call ID. The corrected test proves rejected model-supplied nutrition produces safe tool-error evidence, no preview, and a subsequent explanatory answer. The intermediate draft checkpoint `b73c326` had one stale fixture assertion (48 passed/1 failed); the following checkpoint fixes it and reruns the complete suite. No production defect was reproduced or production assertion weakened.

## Covered behavior

- Campus/hall/date/freshness mismatch, unknown/conflicting safety metadata, scoped food logs, duplicate-intake math, storage bounds and expired snapshots.
- Canonical portions/trusted nutrition; refusal of model-supplied nutrition; adversarial menu/preferences cannot grant write tools or alter server-owned query scope. Synthetic HTTP responses test authority boundaries, not live model prompt-injection resistance.
- Real tool-call/output replay pairs, bounded input/result histories, stable run IDs, contiguous event sequences and one typed terminal outcome.
- HTTP auth/quota/server failures, timeout, malformed/empty/failed/incomplete responses, unknown write tools, sanitized tool/database failures, no automatic HTTP retries or saved IDs.
- Real streaming-body closure on deadline, cancelled consumer task and closed generator; cancelled/error runs discard replay history.
- Eight model turns, sixteen tool/read calls, one validation repair, 2048-token request limit, 16000-character output limit and bounded application events. The stricter text/tool bounds constrain ordinary event count below the separate 480-event ceiling; no artificial Runner event flood is claimed.
- Concurrent account isolation, hidden reasoning suppression, private log suppression and no tracing callbacks during real execution. A positive-control trace verifies the observer is active before checking tracing suppression.

## Remaining gates

Fresh independent review is required for both PRs, with combined current-head checks before merging. Retarget/rebase this explicit test stack when the feature merges; do not interpret feature merge as rollout authorization.

Routes/auth/subscription integration, real database transport reads for these runtime tools, hosted search, meal-completion synchronization, conversation UI, live-model behavior/access, actual Vercel limits, staging and rollback remain separate prerequisites. The fake storage deliberately returns unfiltered rows to test application revalidation; it is not evidence about deployed database policies. Tests do not activate rollout or change schema/data. Rollback of this PR only removes tests and evidence.

Current-main integration explicitly merged feature `9bc049af57a2dae45895c2614b42facded09dfc5`, which contains main `99d146199415debe98d3088257f863e619431cfa` and reviewed scheduled menu source/test changes. The 49 independent runtime tests and runtime production code are unchanged. Later evidence-only commits do not change executable files. Prior 317-test evidence and mutation results apply to the original runtime stack; this rerun covers the combined current stack.
