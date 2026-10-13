# Controlled synthetic model comparison

Run from the backend root:

```sh
python scripts/agent_baseline_compare.py --repetitions 3
```

Default offline mode runs the existing Gemini generator and real Luna SDK/tools with synthetic provider responses. It never reads `.env`, imports the deployed app, connects to a database, or calls paid models. Synthetic token counts and timings are not performance evidence.

Explicit `--live` requires `OPENAI_API_KEY` and `GEMINI_API_KEY` already supplied through the process environment. Never put credentials on the command line, in reports or in committed files. The harness sends only the checked-in synthetic fixture to fixed official provider endpoints. No user rows, writes, history, or search are involved. Both paths receive the same vegan profile, mushroom dislike, five-item menu, empty intake ledger, and dinner targets of 600 kcal / 36 g protein. One serving each of rice, beans and tofu satisfies the fixture, but the live models choose their own combinations. Both outputs pass the same trusted menu/restriction/nutrition validator and a separate 5% calorie/protein target check. This target check measures this task's success; it is not a new production policy or a rollout performance threshold.

This is an **uncached, controlled synthetic dinner comparison**, not the production Gemini baseline. Gemini is fixed to `gemini-2.5-flash`, temperature 0.4, 512 thinking tokens, 2048 maximum output tokens, two application requests, no SDK retries and a 10-second provider HTTP timeout. These intentionally bounded settings differ from the application's larger token/retry defaults. Luna uses the real `gpt-6-luna` runtime, at most eight streamed model requests, 2048 output tokens per request, a 45-second runtime deadline and the runtime’s 30-second HTTP timeout, with SDK retries disabled. Repetitions are limited to 1–3 paired runs, alternating provider order. A Gemini network request executes in the legacy worker thread; its configured provider timeout remains necessary because cancelling an asyncio waiter does not terminate that thread.

JSON reports contain installed SDK versions, fixed configuration, each case's elapsed time, outcome, and each attempted request's provider-native usage counters. Failed requests retain `usage: null` when the provider supplied no usable counters; that case cannot pass. Counters from malformed-response/repair attempts are retained. Gemini thought/cache/tool-input counters and Luna reasoning/cache counters are separate; missing optional metadata stays null rather than being invented as zero. Exceptions, model responses, prompts and credentials are omitted; legacy console output is suppressed. A nonzero exit blocks this comparison check. The script is sequential and temporarily patches legacy environment/client bindings; do not embed it in a serving process.

Record exact Git SHA, command, environment and sanitized JSON for each live run. Use actual account billing rates to calculate cost; the harness does not guess dollars. Three pairs give directional evidence, not robust percentiles, user recommendation acceptance or regeneration frequency. Establish explicit latency/cost thresholds from a representative measured Gemini baseline and actual deployment constraints before increasing rollout. Warm-cache behavior, full-day planning, eaten-food scenarios, hosted search, staging HTTP/SSE, native accessibility, and user acceptance remain separate gates. The existing migration's zero-failure safety/ownership/math and rollback requirements still apply.

Source and independently authored companion tests must both receive fresh review before merge. No live model call or deployment is claimed by this change.
