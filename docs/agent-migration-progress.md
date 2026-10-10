# Agent migration progress — October 10, 2026

Coordinator-owned ledger. Handoff: `/Users/raghavmehta/Downloads/DormDish/DORMDISH_AGENT_MIGRATION_PLAN.md`. Every worker reads Ponytail FULL, repository instructions and the handoff. No co-author trailers. Original source checkouts remain untouched; all paths below are under `.migration-worktrees/`.

## Current state

Foundation backend PRs 2 and 1 merged, main `aa01f47b207886be3f099ace921bf6f768514a5e`. All new runtime behavior remains disabled. No production schema migration or application rollout has run. iOS PR1/2/3 merged with ancestry retained, main6240cdaac6790bab88375782c6661bf152df4899 has an identical tree to the tested combined head. No main check runs configured. Full native simulator build and 16 XCTest cases pass at combined iOS `ba6d7b5d68e5ec6b0b77615dab9deddf49d02f5b`.

| Task / owner | Worktree / branch | Dependencies | PR / latest head | Checks / review / next |
|---|---|---|---|---|
| A-BE backend_foundation | backend-contracts / feat/dd-agent/contracts | baseline bccc1d1 | backend PR2 / 1bdb996b3658ece04525529d2ddfb1b358d92166 | Fresh review_foundation, 25 baseline +130 combined; merged 0d808698bf1e701ff4780f76ef93acad28c1dcd3 |
| T-BE backend_testing | backend-tests / tests/dd-agent/backend-contracts | A-BE | backend PR1 / 2fd80c2385dda305452f07506c72dd4d57114fb4 | 130 pass, 8 real old-behavior failures; independent initial/final reviews; merged aa01f47 |
| B-MEALS backend_meals | backend-meals / feat/dd-agent/meal-correctness | A-BE | backend PR3 / 00b1b3701f034750af809e4d1016fb4382fee442 | 50 own-branch pass; fresh feature reviewer queued |
| T-MEALS backend_meal_testing | backend-meal-tests / tests/dd-agent/meal-correctness | B-MEALS + main aa01f47 | backend PR4 / 1b9de306e675c477ef151787a7f4378fadfcc551 | 216 pass, genuine old behavior 4 failures; separate fresh reviewer queued |
| D-STATE backend_data | backend-data / feat/dd-agent/persistence | main aa01f47 | backend PR5 / 4b06aaaa4f1e8c951c18db72f3c4496de07363ba | 143 pass including13 new/local PostgreSQL checks; independent tests active then fresh review |
| T-STATE persistence_testing | backend-persistence-tests / tests/dd-agent/persistence | D-STATE exact4b06aaa | backend PR6 / d4eca728f723ca11b5d251b503bc41bfa290092d | 162pass0fail0skip including19independent realSQL cases; fresh review queued |
| A-IOS ios_implementation | ios-conversation / feat/dd-agent/ios-conversation | iOS main6672974 | iOS PR1 / 70d5c56574eb1fd396cdf6ea0ae4c663af6d89f5 | Fresh review_ios_foundation no findings; 16 CLT and combined native passes; merged a8bdb97df6cf8ca4956dcd2dc5cad2cbdb157c20 |
| T-IOS ios_testing | ios-tests / test/dd-agent/ios-streaming | A-IOS exact70d5c56 | iOS PR2 / ab1e2f11378226a27d58d5a623ecf5a4cdf2932d | 16pass; old decoder mutation2fail; fresh review_ios_tests no findings, confirmed native evidence; merged18d1745f2dba25e498459aaa0190b5d03ba52569 |
| XCODE coordinator | ios-xcode / fix/dd-agent/xcode27-revenuecat | T-IOS exactab1e2f1 | iOS PR3 / ba6d7b5d68e5ec6b0b77615dab9deddf49d02f5b | Full native build+16XCTest pass; fresh review_ios_xcode no findings; merged6240cdaac6790bab88375782c6661bf152df4899 |
| F-IOS ios_conversation | ios-chat / feat/dd-agent/ios-chat | XCODE exactba6d7b5 | iOS PR4 / f5942e4 | Service/state selfcheck passed; native UI active, ios_chat_testing active owns test/project files |
| C-RUNTIME queued | backend-runtime / feat/dd-agent/runtime | main aa01f47 + meals00b1b37 + persistence4b06aaa | prepared integration a0c321680741e2a69c2ab2ceb768db4923dfaaa3 | Own runtime/tools, focused checks and docs; no app.py ownership assigned yet. Coordinator updates this ledger here |

All implementation/testing owners must commit and push their own work, create an early draft PR and attach it. Every PR gets a separate fresh reviewer. Same-account reviews do not bypass any GitHub approval requirement. New heads require refreshed evidence. Test PRs remain explicit merge/rollout dependencies. Slot cap currently permits coordinator +two workers; queued assignments rotate after completion.

| MENU-SOURCE coordinator | backend-menu-source / fix/dd-agent/menu-provenance | main aa01f47 | backend PR7 /3197304 | 139pass including9new source tests;8initial cases fail meaningfully on oldmain; companion test author and fresh reviews queued |

| T-CHAT ios_chat_testing | ios-chat-tests / test/dd-agent/ios-chat | F-IOS exactf5942e4 + incoming UI update | iOS PR5 /63c17fb | 16new XCTest authored; simulator run active, final SHA/results pending |

## Access and evidence

- GitHub authenticated as1RaghavM, push/admin on both repositories. Git author1RaghavM <lebrongoated2323@gmail.com> mapped to that account by existing GitHub commit; no invented identity. Main branch protections404/unprotected and rules empty at initial check; recheck at merge.
- Original backend HEAD bccc1d17afea6ead9b38d660f178d21e45262c55 clean; original iOS HEAD95c8ade has substantial user modifications. Snapshot `/private/tmp/dd-ios-initial.patch` and status preserved; latest tracked diff byte comparison unchanged.
- Git network/keychain and Docker/Xcode require approved sandbox escalation. No actual Git credential blocker remains. Worktree push dryruns verified before assignment.
- User installed/accepted Xcode27. iPhone17/iOS26.5 simulator23205828-BF22-4146-AE1F-5EEF66819BC7 works. Successful command: `xcodebuild -project DormDish.xcodeproj -scheme DormDish -destination 'platform=iOS Simulator,id=23205828-BF22-4146-AE1F-5EEF66819BC7' -derivedDataPath /private/tmp/dd-agent-ios-xcode-derived -resultBundlePath /private/tmp/dd-agent-ios-xcode-tests.xcresult CODE_SIGNING_ALLOWED=NO test` in ios-xcode. Log `/private/tmp/dd-agent-ios-xcode.log`. Initial5.59 RevenueCat build failed65;5.78 includes upstream Xcode27 fix870899891ac9a05118ae6ee16d4ae189b2c1eac2 (#6949). No live purchase/restore verification.
- Python `/private/tmp/dd-agent-foundation-uv/bin/python`, openai-agents0.8.1/Pydantic2.12.3, 87-package compatibility check passed. Latest SDK0.23.1 conflicts with existing Starlette stack; no gratuitous framework upgrade. Real SDK streaming/history APIs verified offline. Local imitation agents package removed; Gemini remains.
- Local PostgreSQL17.11 in Docker `dd-agent-migration-db`,127.0.0.1:55432, synthetic databases only. Production Supabase schema metadata read (no user rows): custom public.users auth, menus, saved_meal_plans, meal_plan_generations, food_log. Production policies/SQL privileges not verified.
- Canonical JSON fixture hash68dd60b4e5e00520428996cc1a686b49eebc7578b8704aa5d3b59a292c60e04d shared with Swift tests.

## Gates, blockers and next work

1. Merge only fresh-reviewed exact heads in dependency order; combined tests and current protections required. iOS PR1 ->2 ->3 then native integration; backend meals/test reviews and persistence/test reviews queued.
2. Read-only runtime uses real SDK Responses/gpt-6-luna, request-local context, max8turns/one repair, deadlines/token/search limits, tracing disabled. Integrate authenticated routes, controlled actions and four legacy adapters in later bounded PRs.
3. Persistence supports preview+Apply; explicit generate-and-save still needs atomic auto-apply integration. Forget integration must invalidate history; preference conflict changes and completion sync not yet implemented. Confirmation uses a short table-wide saved-plan write lock until all legacy writers share verified uniqueness/version rules; documented throughput limit.
4. PR7 fixes meal/portion membership loss, zero nutrient loss, naive source timestamps and preserves explicitly supplied safety/source fields. Source collection does not yet supply verified safety taxonomy; retrieval still strips provenance. Fix actual source integration before claiming grounded production meals. Unknown safety metadata fails closed. iOS eaten sync is currently a stub, so server completion data is a rollout prerequisite.
5. Configured OpenAI key returned HTTP401 for official gpt-6-luna model lookup. Vercel CLI lacks credentials; browser dashboard redirects to login. User already asked to restore local credentials without sharing secrets. Main GitHub Vercel status eventually success, duplicate project sometimes blocked; status is not application staging acceptance. Actual account deadline, streaming/package limits, live evaluation and rollback drill unverified.
6. Schema first, backward-compatible backend second, gated iOS third. No enablement until model/staging/ownership/math/duplicate-write suites pass with measured baseline latency/cost thresholds. Rollback disables runtime and preserves data/Gemini; never retry ambiguous writes through legacy.

Official documentation already checked: https://developers.openai.com/api/docs/models/gpt-6-luna and Agents SDK running/results pages. General Vercel maximums do not establish this project's limits. Required unrun work: live model, deployment resource/streaming smoke, end-to-end user journeys, purchase compatibility, accessibility UI, rollout and rollback.
