# Release deployment checks — October 13, 2026

Backend source: `e364f45a4ca9a318116c838930321732cd95428c`. Native source: `a357c7d5fa311b6ee9621b922e7e3b2f15820045`. These checks do not enable rollout.

## Verified deployment and limits

The authenticated Vercel CLI identifies the current production deployment as `dpl_FsTPKFkG9Tj7S457Y1vmp8iyfyZy`, project `prj_bV09FcFRwsPnmquVnp08G3bfiQVX`, team `team_JYbLtO4ZaWEF5Bwxe1VYi7s7`. Deployment metadata matches backend main above and reports READY. `https://api.dormdish.ai/health` returned HTTP 200. The original checkout's `.vercel/project.json` points to an older project; do not use it for deployment operations.

Actual build output (`GET /v1/deployments/{id}/builds`) reports the `api/index.py` Lambda running Python 3.12 in iad1, **300-second timeout**, **2048 MiB memory**, and artifact `size: 59905262` bytes. This size is the API's artifact size, not independently measured uncompressed size. Project metadata has Fluid enabled; deployment plan is Hobby. [Official Vercel limits](https://vercel.com/docs/functions/limitations), rechecked today, state Hobby Fluid has a 300-second maximum and Python's standard uncompressed bundle maximum is 500 MB. The application's 45-second generation bound fits the recorded function deadline; deployed SSE buffering/disconnect behavior is still untested.

Production environment **names only** were inspected. Agent, generation, controlled-save, explicit-memory and completion enabling flags are absent, retaining their code defaults of off. No environment value or flag was changed. A targeted non-secret endpoint check confirmed production `OPENAI_BASE_URL` uses `https://api.openai.com/v1/`. Existing health is not evidence of authenticated agent journeys.

Local metadata evidence: `/private/tmp/dd-release-deployment.json`, `dd-release-deployment-detail.json`, `dd-release-project.json`, `dd-release-builds.json`, `dd-release-build.log`, `dd-release-health.json`. Build-cache upload size is not a function-bundle measurement.

## Database metadata

Supabase CLI against exact DormDish project `rszkbhgowcrvwiwcmntt` confirmed deployed `agent_save_intent`, `agent_save_turn`, `agent_confirm_action`, and `agent_finish_turn`: security invoker, service_role EXECUTE allowed, anon/authenticated EXECUTE denied. This supersedes the earlier handoff's controlled-save schema uncertainty. Current saved-plan indexes include unique `(user_id,date,dining_hall)`; concurrency tests must model that actual constraint. No production rows were read or modified during these checks.

## Model access

Automatic approval review rejected `vercel env pull` because it would download all production secrets; it was not executed. The user approved targeted in-memory OpenAI checks. Vercel's single-variable API reports `OPENAI_API_KEY` as sensitive, secret visibility and `decrypted:false`, with no value returned. No attempt was made to bypass that restriction. The existing local development key, held only in memory, successfully retrieved `gpt-6-luna` (HTTP 200) from the official OpenAI API.

A bounded synthetic live run of `scripts/agent_release_smoke.py` at `d56c9c494762b4219eecaa584944902152a0dd2f` returned passed in 10.958 seconds: 5 model requests, 4 tools, 1 trusted preview, 4823 input tokens and 260 output tokens. No database/app import, production data or writes. This is a single local sample, not production-key validation or a latency/cost benchmark. Independent review subsequently found its acceptance check only proved tool calls, not successful intake output; source `5fd495f9b6aef65cd04de5f345a7951d40b3102c` fixed this and passed an independently refreshed review. A new live run at that exact source also passed: 10.596 seconds, 5 model requests, 4 successful tools, 1 trusted preview, 4812 input / 260 output / 5072 total tokens. Local smoke gate cleared; deployed model/key and staging gates remain open.

Official [Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna) and [SDK execution](https://developers.openai.com/api/docs/guides/agents/running-agents) pages were fetched today. They document required model/tool/streaming capabilities; real pinned-SDK execution is required separately. No dependency upgrade was inferred from newer documentation.

## Cross-feature acceptance

Native test PR14 merged after a fresh independent review: `967dd4b056165953fe2b6b7bd3399f22139d17c2`, matching reviewed head `153595142e375cd3fbfbcb5e6450270be5b4171b`. Author ran 147 tests with no failures/skips; reviewer independently ran 17 conversation tests, no failures/skips, and verified the genuine negative control. No app release occurred.

Backend main plus initial acceptance tests at `3cdb2f18a44945a4a53fb98cfee976ac15c81a5e` passed 1045 tests with no failures/skips in 428.26 seconds. Subsequent concurrency coverage found a real release blocker: simultaneous same-owner/date requests at different halls save two plans on all four enabled legacy routes. The fixture includes production's unique `(user_id,date,dining_hall)` index; same-hall requests pass. Test PR28 retains four failing regression cases pending an atomic database save fix. Prior green suites do not clear this newly covered gate.

## Remaining gates

- Final independently reviewed source/test heads and combined acceptance results.
- Production-key execution, hosted search, staging authenticated SSE/disconnect recovery and rollback.
- Comparable measured Gemini baseline and explicit latency/cost thresholds before cohort increases.
- Manual native VoiceOver, Dynamic Type, keyboard, purchase/restore and end-to-end app checks.

Keep rollout off. Preserve additive schema and user records during rollback; reconcile ambiguous writes before retrying any generator.
