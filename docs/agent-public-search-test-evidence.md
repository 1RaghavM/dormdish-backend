# Public search independent test evidence

Dedicated test branch: `tests/dd-agent/public-search`; [test PR16](https://github.com/1RaghavM/dormdish-backend/pull/16) depends on [feature PR15](https://github.com/1RaghavM/dormdish-backend/pull/15). Ponytail full and the complete migration handoff were read and applied. Only this evidence and `tests/test_agent_public_search.py` belong to this task. The isolated worktree is `backend-public-search-tests`; original checkouts were untouched.

## Verified revisions and results

| Revision | Verification | Result |
| --- | --- | --- |
| Source `f718156b39081a7d5e092247316284ec6724197e`, initial test checkpoint `c1db7fa7485d789afc71bff3a6cb9e929fbfb341` | Complete synthetic suite with local PostgreSQL | **377 passed, 2 failed, 0 skipped**, 26.80s |
| Same source, expanded tests `7f9c50a` | 32 independent search cases | **30 passed, 2 failed** |
| Fixed source `f72eb065163e36fc9b131e7b8002f025c59b7fb3`, explicitly integrated at `5ed9334ed82974cc5f8c70848a49c7d6fe33ae70` | Full suite; original 32 assertions unchanged | **383 passed, 0 failed, 0 skipped**, 18.11s |
| Final tested code `8a6366f7f98f8a82bdb96e828f17fcd146504e12`, same source | Full suite including three additional buffering checks | **386 passed, 0 failed, 0 skipped**, 18.01s |

Full command: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest -q -o addopts=''`. Focused command: `/private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_agent_public_search.py -q -o addopts=''` (**35 passed**). Environment uses pinned Agents 0.8.1/OpenAI 2.26.0, synthetic HTTP and disposable local PostgreSQL 17. No Runner or meal-validator mocks are used. Final evidence-only documentation changes do not change the tested code.

Local raw outputs: `/private/tmp/dd-public-search-independent-full-red.log`, `/private/tmp/dd-public-search-independent-red.log`, `/private/tmp/dd-public-search-independent-full-green.log`, `/private/tmp/dd-public-search-independent-final.log`.

## Coverage and reproduced defects

The 35 independent cases exercise all public topics after private profile/history loading, forged model topics and scope/query arguments, simultaneous search attempts, malformed/oversized evidence and URLs, private errors and no retries, actual unauthorized write-tool attempts after malicious search content, menu-only food validation and valid repair, shared eight-model/sixteen-tool budgets, and closure of in-flight hosted-search or continuation bodies on timeout/cancellation. Citation suffixes remain inside the 16,000-character output limit.

Two genuine assertion failures on the original feature revision reproduced lost source links in emitted/persistable assistant text and uncited partial web claims after abrupt EOF. `test_search_sources_survive_persisted_assistant_text_without_model_supplied_links` and `test_failed_search_continuation_never_emits_uncited_web_claims` are unchanged and pass after the source owner's fix. These are behavior failures, not missing imports or fixtures. The fix was authored separately; this test branch changes no production code relative to PR15.

## Remaining gates

A fresh independent reviewer must review this test PR. The feature and its tests are explicit merge/rollout dependencies; backend rollout stays disabled. This suite checks the runtime's persistence input and event payload, not a database round-trip for citations or native rendering. No paid model, production data, deployed endpoint, native UI, real hosted-search accuracy, or rollout checks were run. Model credentials, measured deployment limits, schema order, and native inline-citation compatibility remain separate release gates.
