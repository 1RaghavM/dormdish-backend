# Public search independent test evidence

Dedicated test branch: `tests/dd-agent/public-search`; feature dependency: [PR15](https://github.com/1RaghavM/dormdish-backend/pull/15), source `f718156b39081a7d5e092247316284ec6724197e`. Ponytail full and the complete migration handoff were read and applied. Only this evidence and `tests/test_agent_public_search.py` belong to this testing task.

Initial checkpoint: **26 passed, 2 failed**, `/private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_agent_public_search.py -q -o addopts=''`; local output `/private/tmp/dd-public-search-independent-initial.log`. This is intentionally not merge-ready.

Actual pinned Agents/Responses execution uses synthetic HTTP and existing read-only database fixtures. Tests cover all three public topics after private profile/history loading, rejected forged topics, simultaneous search attempts, malformed/oversized evidence, private failures and no retries, server-only menu validation and repair, shared eight-model/sixteen-tool budgets, and closure of an in-flight hosted-search body on timeout/cancellation. No Runner or meal-validator mocks are used.

Two genuine assertion failures reproduce missing source persistence and uncited partial answers on the feature revision: `test_search_sources_survive_persisted_assistant_text_without_model_supplied_links` and `test_failed_search_continuation_never_emits_uncited_web_claims`. The owner received the defects; assertions remain unchanged pending a fix.

Full-suite integration and fresh independent review remain pending. No paid model, production data, deployed endpoint, native UI, real hosted-search accuracy, or rollout checks were run. Backend rollout remains disabled; model credentials, deployment limits, schema order, and native citation compatibility remain separate release gates.

Expanded independent coverage: **30 passed, 2 failed** across 32 cases, `/private/tmp/dd-public-search-independent-red.log`. Additional checks submit forged model search scope/query and actual save/remember/forget function attempts after malicious search content. The source remains unchanged at `f718156`.

The first full-suite checkpoint `c1db7fa7485d789afc71bff3a6cb9e929fbfb341` produced **377 passed, 2 failed, 0 skipped** in 26.80s with `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''`; `/private/tmp/dd-public-search-independent-full-red.log`. Both failures are the same citation assertions, not fixture/import failures. Four subsequent security cases passed in the focused run; their full-suite integration awaits the feature fix.
