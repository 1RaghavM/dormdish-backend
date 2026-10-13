# Repeatable agent release smoke

From the backend repository root, use the pinned runtime environment:

```sh
python scripts/agent_release_smoke.py
```

This default is offline. It executes the **real pinned Agents SDK, Responses stream decoding, AgentTurn, function tools and meal validator** against scripted HTTP responses and synthetic menu/intake data. No app, Supabase client, production user or database is loaded. The four tools must read profile/menu/intake and emit exactly one deterministically validated rice preview. The synthetic lunch reduces the synthetic daily targets. A completed conversation without the required tool evidence or exact trusted meal totals is a failed smoke.

The default output reports `usage_is_synthetic: true`; its token counts and duration are **not live model performance or cost evidence**. It checks package versions against the application's `openai-agents==0.8.1`, `openai==2.26.0`, and `pydantic==2.12.3` pins and exact `gpt-6-luna` model. Version drift fails closed.

## Explicit live run

Set `OPENAI_API_KEY` through your approved secret mechanism, then explicitly run:

```sh
python scripts/agent_release_smoke.py --live --deadline-seconds 45
```

Do not put the key on the command line or in an evidence file. The script does not load `.env` or retrieve credentials itself. A missing key exits nonzero before a network request. `--live` makes paid requests to the fixed official Responses endpoint with the same synthetic scenario. Custom API base URLs, redirects and hosted search requests are rejected. No production flag is changed, no Supabase operation is possible, and Gemini generation is untouched. Running the isolated smoke while rollout is off does not enable an application user.

The normal runtime enforces eight model turns, sixteen tools, one validation repair, 2048 output tokens per model request, no HTTP retries and a maximum 45-second turn. You may shorten this deadline for the measured deployment limit; increasing it is rejected. The script collects only mode/model/package versions, request and tool counts, preview count, duration, aggregate input/output/total tokens, terminal outcome and sanitized error code/HTTP status. It never prints exception strings, raw responses, prompts, history or profile values. Live token counters are summed from completed Responses usage; interrupted runs can incur additional **unreported** usage and must not be treated as complete billing measurements.

A nonzero exit or `outcome: error` blocks the smoke gate. `passed` proves only this bounded synthetic read-only runtime scenario. It does not prove database RPCs, deployed HTTP streaming, client UI, hosted search, latency/cost targets, recommendation quality, or all migration release criteria. An available model name in documentation does not prove this account can use it.

## Evidence and remaining rollout gates

For each run record the exact Git SHA, command, JSON report and environment name. Keep keys out of reports. Do not estimate cost from synthetic tokens or invent thresholds. Compare repeated live results with a separately measured Gemini baseline using comparable sanitized inputs and actual account pricing; agree explicit latency/cost thresholds before increasing rollout. The legacy generator has database/menu dependencies, so this script deliberately does not pretend its isolated fixture is a representative Gemini benchmark.

The dedicated companion test PR is a merge dependency. It must cover offline isolation, real tool/validator execution, sanitized failures, missing credentials, runtime bounds and acceptance failure; existing route regression tests verify feature-off legacy behavior. Rollout remains off until those checks, reviewed additive migrations, deployed streaming/model access, actual Vercel resource limits, iOS acceptance/accessibility and rollback drill pass. Disabling the backend agent flag/kill switch remains the application rollback; never drop migration tables or retry an ambiguous save through Gemini.

Official documentation rechecked October 13, 2026: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) documents Responses/function calling/streaming support; [running agents](https://developers.openai.com/api/docs/guides/agents/running-agents) documents the tool/model loop and streaming. The installed pinned SDK is exercised here because current documentation alone cannot establish compatibility with an older installed version.

Implementation checkpoint `d742f7c64993232c7eabaeb5db1dcd70bc5b9830` ([PR #29](https://github.com/1RaghavM/dormdish-backend/pull/29)): offline smoke passed with five requests, four tools, one validated preview and 100 input/50 output **synthetic** tokens. `pytest -o addopts='' -q tests/test_agent_runtime.py tests/test_agent_tools.py tests/test_agent_runtime_unit.py` passed **60 tests in 8.36 seconds**. Explicit live mode with the API key unset returned `missing_api_key`, zero requests and exit 1; deadline 46 returned `invalid_deadline`, zero requests and exit 1. No paid live call was run for this checkpoint; companion tests and independent review remain pending.
