# Public dining search

The runtime exposes one fixed-topic search per turn: opening hours, location, or public dining access/payment policies. The server builds the query from the registered university, hall, selected date, and fixed topic. A separate Responses request receives no user text, identity, health profile, preferences, or conversation history. Results never establish selectable menu foods or allergy safety.

The hosted request uses `gpt-6-luna`, `web_search`, `max_tool_calls=1`, 2,048 output tokens, a 15-second timeout, no retries, and `store=false`. It shares the outer eight-model-request and 45-second turn budgets. A failed search consumes its attempt. Completed answers require HTTP(S) source citations and bounded text; invalid results become sanitized tool failures. Complete events carry the sources under the existing v1 contract. The main agent is instructed to cite sources beside web-derived claims.

Verified against the installed Agents 0.8.1 / OpenAI 2.26.0 API and official documentation on 2026-10-10:
- [Web search and citation display](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Responses create parameters](https://developers.openai.com/api/reference/python/resources/responses/methods/create)

Local verification: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''` passed **351 tests**, with zero failures or skips (18.81 seconds). Eight new checks exercise the actual SDK over synthetic HTTP responses, including private-query isolation, shared request budgets, missing/invalid citations, and failure without retry. Dedicated independent test authorship and fresh source/test reviews remain required.

No live search or deployment was run. The configured OpenAI key previously returned 401. Keep rollout disabled until live model/search access, deployment limits, schema-first route deployment, and native visible clickable inline citation behavior (including interrupted streams) are verified. Backend source availability alone does not enable routes. Existing Gemini paths remain unchanged for rollback.
