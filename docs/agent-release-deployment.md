# Release deployment checks — October 13, 2026

Starting backend source: `e364f45a4ca9a318116c838930321732cd95428c`. Final merged backend: `3609664602267f2a24d4c1ecade3a59be4889697`. Final native: `967dd4b056165953fe2b6b7bd3399f22139d17c2`. These checks do not enable rollout.

## Verified deployment and limits

The authenticated Vercel CLI identifies the current production deployment as `dpl_FsTPKFkG9Tj7S457Y1vmp8iyfyZy`, project `prj_bV09FcFRwsPnmquVnp08G3bfiQVX`, team `team_JYbLtO4ZaWEF5Bwxe1VYi7s7`. Deployment metadata matches backend main above and reports READY. `https://api.dormdish.ai/health` returned HTTP 200. The original checkout's `.vercel/project.json` points to an older project; do not use it for deployment operations.

Actual build output (`GET /v1/deployments/{id}/builds`) reports the `api/index.py` Lambda running Python 3.12 in iad1, **300-second timeout**, **2048 MiB memory**, and artifact `size: 59905262` bytes. This size is the API's artifact size, not independently measured uncompressed size. Project metadata has Fluid enabled; deployment plan is Hobby. [Official Vercel limits](https://vercel.com/docs/functions/limitations), rechecked today, state Hobby Fluid has a 300-second maximum and Python's standard uncompressed bundle maximum is 500 MB. The application's 45-second generation bound fits the recorded function deadline; progressive deployed SSE is now verified below; actual client disconnect remains untested.

Production environment **names only** were inspected. Agent, generation, controlled-save, explicit-memory and completion enabling flags are absent, retaining their code defaults of off. No production environment value or flag was changed. Temporary branch-only preview configuration is recorded below. A targeted non-secret endpoint check confirmed production `OPENAI_BASE_URL` uses `https://api.openai.com/v1/`. Existing health is not evidence of authenticated agent journeys.

Local metadata evidence: `/private/tmp/dd-release-deployment.json`, `dd-release-deployment-detail.json`, `dd-release-project.json`, `dd-release-builds.json`, `dd-release-build.log`, `dd-release-health.json`. Build-cache upload size is not a function-bundle measurement.

## Database metadata

Supabase CLI against exact DormDish project `rszkbhgowcrvwiwcmntt` confirmed deployed `agent_save_intent`, `agent_save_turn`, `agent_confirm_action`, and `agent_finish_turn`: security invoker, service_role EXECUTE allowed, anon/authenticated EXECUTE denied. This supersedes the earlier handoff's controlled-save schema uncertainty. Current saved-plan indexes include unique `(user_id,date,dining_hall)`; concurrency tests must model that actual constraint. No production rows were read or modified during these checks.

The reviewed additive `add_agent_legacy_save.sql` migration was subsequently applied through the Supabase CLI. SHA256: `76df486000ba66fc1b11c69778ba3821eec81441082794fc032faefb7b22e8a5`. `agent_save_legacy_plan` is security invoker, service_role executable, and denied to anon/authenticated. Existing user rows were not changed by the migration. Evidence: `/private/tmp/dd-release-atomic-save-apply.json`, `/private/tmp/dd-release-atomic-save-verify.json`.

## Model access

Automatic approval review rejected `vercel env pull` because it would download all production secrets; it was not executed. The user approved targeted in-memory OpenAI checks. Vercel's single-variable API reports `OPENAI_API_KEY` as sensitive, secret visibility and `decrypted:false`, with no value returned. No attempt was made to bypass that restriction. The existing local development key, held only in memory, successfully retrieved `gpt-6-luna` (HTTP 200) from the official OpenAI API.

A bounded synthetic live run of `scripts/agent_release_smoke.py` at `d56c9c494762b4219eecaa584944902152a0dd2f` returned passed in 10.958 seconds: 5 model requests, 4 tools, 1 trusted preview, 4823 input tokens and 260 output tokens. No database/app import, production data or writes. This is a single local sample, not production-key validation or a latency/cost benchmark. Independent review subsequently found its acceptance check only proved tool calls, not successful intake output; source `5fd495f9b6aef65cd04de5f345a7951d40b3102c` fixed this and passed an independently refreshed review. A new live run at that exact source also passed: 10.596 seconds, 5 model requests, 4 successful tools, 1 trusted preview, 4812 input / 260 output / 5072 total tokens. Local smoke gate cleared. Deployed preview model execution with the temporary local key subsequently passed; production-key execution remains unverified. Sanitized coordinator evidence is `/private/tmp/dd-release-live-strict.json`.

Official [Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna) and [SDK execution](https://developers.openai.com/api/docs/guides/agents/running-agents) pages were fetched today. They document required model/tool/streaming capabilities; real pinned-SDK execution is required separately. No dependency upgrade was inferred from newer documentation.

## Cross-feature acceptance

Native test PR14 merged after a fresh independent review: `967dd4b056165953fe2b6b7bd3399f22139d17c2`, matching reviewed head `153595142e375cd3fbfbcb5e6450270be5b4171b`. Author ran 147 tests with no failures/skips; reviewer independently ran 17 conversation tests, no failures/skips, and verified the genuine negative control. No app release occurred.

Backend main plus initial acceptance tests at `3cdb2f18a44945a4a53fb98cfee976ac15c81a5e` passed 1045 tests with no failures/skips in 428.26 seconds. Subsequent concurrency coverage found a real release blocker: simultaneous duplicate same-meal/full-day requests for one owner/date at different halls save two plans on all four enabled legacy routes. The fixture includes production's unique `(user_id,date,dining_hall)` index; same-hall requests pass. Test PR28 reproduced four failing regression cases. Source PR31 fixes those cases. Final candidate `5a9cc64928b8327d200727654f65b320a7df3ba4` combines PR29/31/28/32/33 and passed **1106 tests, zero failures/skips**, in 304.38 seconds with real local PostgreSQL. Every source and test PR has a fresh independent clear review. The pushed candidate has tree `9b2353e07363cad9326fd697aee1d3115ec676b6`; offline SDK smoke also passed. Full log: `/private/tmp/dd-release-final-full.log`. Valid different-meal behavior is preserved. Prior green suites do not clear this newly covered gate.

## Deployed preview and merged-main acceptance

Merged main `3609664602267f2a24d4c1ecade3a59be4889697` exactly matches the entire1106-test candidate tree, including documentation. Correct-project deployment `dpl_2zSbWMuRcebMohqdMfJQuBWKx9uZ` reached READY at that SHA; production `/health` returned200 after readiness at2026-10-13T20:54:34Z. No rollout flags were enabled in production.

The user authorized temporary test-branch secret storage and synthetic accounts. Source `1b43297` preview `dpl_5JSNFr2wdQ2EGaoVH1Hox7wvfxwh` admitted only an explicit synthetic UUID at0% general rollout. A second account was denied503. Conversation creation201, no-context completion, explicit dislike memory update, and exact stored-event recovery passed. A real menu-based dinner request streamed14events: first status3.402s, meal_preview10.677s, complete16.453s; an action was proposed but no save applied. This is one observed sample, not a performance threshold or Gemini comparison.

Kill-switch preview `dpl_2it3DvUVcrMYjnRx1ucBG7WQDs41` denied new writes503 and retained existing run reads200. All three test accounts were then deleted200 and an exact-ID database count confirmed zero remain. All six temporary branch variables and three key-containing preview deployments were removed; metadata confirms no variables remain on that branch. Production enabling flags remain absent. No secret or session was saved to a local file. Sanitized evidence: `/private/tmp/dd-preview-acceptance-result.json`.

## Remaining gates

- Production-key execution, hosted search and actual deployed disconnect/recovery.
- Comparable measured Gemini baseline and explicit latency/cost thresholds before cohort increases.
- Manual native VoiceOver, Dynamic Type, keyboard, purchase/restore and end-to-end app checks.

Keep rollout off. Preserve additive schema and user records during rollback; reconcile ambiguous writes before retrying any generator.
