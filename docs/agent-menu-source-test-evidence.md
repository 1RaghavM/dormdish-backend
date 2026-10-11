# Menu-source regression evidence

Companion test PR: https://github.com/1RaghavM/dormdish-backend/pull/12
Feature dependency: https://github.com/1RaghavM/dormdish-backend/pull/7
Feature tested: `2d93909af83f5b66673cc63ebef73efcefb317f8`
Tested combined source commit: `8e5ec480ed2ec92f0fff478d30d07859924ca389`
Branch: `tests/dd-agent/menu-provenance`, explicitly stacked on `fix/dd-agent/menu-provenance`.

Ponytail FULL and repository/handoff instructions applied. The test author owns only `tests/test_menu_source.py` and this evidence file. The test author did not implement PR #7. Git identity was verified as `1RaghavM <lebrongoated2323@gmail.com>`; no co-author trailers were added. The repository ignores `test_*.py`, so only this explicitly assigned test file was force-added; ignore rules were unchanged.

## Coverage and results

24 independent cases exercise both API ingestion and the scheduled writer beyond the author's 11 self-checks:

- Raw Nutrislice JSON, both top-level and week-wrapped days, through the real `fetch_meal_items`, normalization, cron caller, full/per-meal storage and cache-only hall lookup. Same-name portion variants survive all three meals; exact duplicates collapse; Rutgers/Hopkins special lunch slugs remain lunch.
- All five supported grouped-item layouts keep unknown metadata absent, preserve explicit null values and refuse to infer food safety from outer wrapper claims.
- All three flat-item layouts preserve measured zeros, null-alias fallback, incomplete nutrition and normalized names; invalid measurements do not become fallback nutrition.
- Both FoodPro parser-result formats travel through real ingestion/storage/retrieval. Atrium restaurant mapping, distinct portions, cross-meal membership and known/unknown allergen metadata survive. FoodPro HTML parsing itself is mocked.
- The actual `refresh_menus.fetch_and_cache_menu` scheduled pipeline overwrites the same five canonical cache IDs previously written by API ingestion. Both parser formats retain two portions across four meals, remove exact duplicates, preserve measured zero aliases and supplied safety/source fields, keep missing fields unknown, and remain readable through the API Busch alias. Scheduled storage also retains an older source timestamp and uses aware new timestamps.
- A different requested date cannot cache another day's food. Missing macros never become zeros. New timestamps have an offset; a supplied older fetch timestamp is retained rather than made artificially fresh.

Commands run from the isolated test worktree:

```sh
env -i PATH=/usr/bin:/bin /private/tmp/dd-agent-foundation-uv/bin/python -m pytest -o addopts='' -q tests/test_menu_source.py
env -i PATH=/usr/bin:/bin /private/tmp/dd-agent-foundation-uv/bin/python -m pytest -o addopts='' -q
```

Results: **24 passed** focused; **165 passed, 0 failed, 0 skipped** full suite. Local logs: `/private/tmp/dd-menu-source-green-updated.log` and `/private/tmp/dd-menu-source-all-updated.log`. Pytest reports the existing unset `asyncio_default_fixture_loop_scope` deprecation warning; no failure was suppressed.

The tests patch dotenv loading and clear import-time credentials, use synthetic HTTP responses and an in-memory `menus` database, and block socket connections during test execution. No model, live upstream, production database or paid service is called.

## Genuine pre-fix failure

At the original test commit `c116710166d3365897aef187f9d3ea68235d7a65`, the actual parent source was extracted into a temporary file, without changing tracked application files:

```sh
git show aa01f47b207886be3f099ace921bf6f768514a5e:app.py > /private/tmp/dd-menu-source-before.py
env -i PATH=/usr/bin:/bin MENU_SOURCE_APP_FILE=/private/tmp/dd-menu-source-before.py /private/tmp/dd-agent-foundation-uv/bin/python -m pytest -o addopts='' -q tests/test_menu_source.py
```

The unchanged tests produced **16 failed, 2 passed**, with assertion failures in membership, provenance/zero handling and timestamp awareness; there were no import/setup errors. Log: `/private/tmp/dd-menu-source-red.log`. The current source passes those same assertions. An initial test assumption that generic FoodPro categories were returned was corrected to exercise the existing Atrium restaurant mapping (categories are intentionally stripped); this was a test-design correction, not a production defect.

The scheduled-writer extension was separately replayed unchanged against the actual scheduled source immediately before its fix:

```sh
git show 31973049b5963f00aabc74c0a1fb271ffe2b9c71:refresh_menus.py > /private/tmp/dd-menu-scheduled-before.py
env -i PATH=/usr/bin:/bin MENU_SOURCE_REFRESH_FILE=/private/tmp/dd-menu-scheduled-before.py /private/tmp/dd-agent-foundation-uv/bin/python -m pytest -o addopts='' -q tests/test_menu_source.py -k scheduled
```

Result: **6 failed, 18 deselected**, all assertion failures: four pipeline cases return one item instead of eight, and two storage cases produce naive timestamps. No missing API/import/setup failures occurred. Log: `/private/tmp/dd-menu-scheduled-red.log`. These same six cases pass on feature `2d93909`. The existing API PR #11 worktree was not changed.

## Merge and release boundary

A fresh reviewer must inspect this test PR independently of the feature reviewer. Merge/reconcile PR #7 first, then this companion, preserving dependency checks and rerunning the combined suite if source or dependency commits change. This test PR changes no runtime state, schema or rollout setting.

Unverified: live FoodPro HTML/Nutrislice behavior, deployed database policies, model access, deployment and staging acceptance. Safety metadata can still be absent in upstream menus; downstream validation must continue treating absence as unknown.
