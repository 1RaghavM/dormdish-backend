# GitHub Actions Cron Jobs Migration

**Date:** 2026-02-20
**Status:** Draft
**Approach:** Standalone Python scripts (Approach A)

## Problem

Vercel Hobby plan has a hard 10-second function timeout. The `/cron/refresh-menus` endpoint scrapes 3 dining halls and can take 12-15 seconds. Additionally, the current Vercel cron auth logic checks a nonexistent `x-vercel-cron` header, so cron invocations fail authentication.

## Solution

Create standalone Python scripts that run as GitHub Actions cron jobs — same proven pattern as the existing `cleanup_menus.py` / `daily-cleanup.yml`. No Vercel timeout constraint. Vercel cron config stays as a redundant fallback.

## New Files

### `refresh_menus.py` — Standalone menu refresh script

Extracts the cron refresh logic from `app.py` into a self-contained script.

**What it does:**
1. Iterates `ALL_DINING_HALLS` (3 Rutgers FoodPro halls)
2. For each hall: calls FoodPro scraper via `extract.py` (imports `build_menu_url`, `fetch`, `parse_menu`)
3. Parses HTML, extracts menu items with macros (protein, fat, carbs, calories)
4. Upserts to Supabase `menus` table with deterministic ID `{university}_{hall}_{date}`
5. Uses `asyncio.gather()` for parallel meal fetching within each hall (already done in `fetch_foodpro_menu_rutgers`)

**Key functions ported from `app.py`:**
- `fetch_foodpro_menu_rutgers()` (lines 2821-3031) — FoodPro HTML scraping
- `save_menu_to_supabase()` (lines 2646-2700) — Supabase upsert
- `fetch_and_cache_menu()` (lines 2798-2818) — orchestrator
- `_fetch_with_retries()` — HTTP fetch with retry logic
- `title_case_name()` — name formatting helper

**Dependencies:** `httpx`, `beautifulsoup4`, `supabase`, `python-dotenv`, `extract.py` (existing module)

**Env vars:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`

### `send_reminders.py` — Standalone daily reminders script

Extracts the push notification logic from `app.py`.

**What it does:**
1. Queries Supabase `push_tokens` table for all active tokens
2. For each user: checks `saved_meal_plans` table for today's plan (ET timezone)
3. If no plan exists: sends Expo push notification via `https://exp.host/--/api/v2/push/send`
4. Logs sent/failed counts

**Key functions ported from `app.py`:**
- `send_push_notification()` (lines 297-347) — Expo API call
- `get_user_push_token()` (lines 350-366) — Supabase token query
- `check_user_has_meal_plan_today()` (lines 369-384) — meal plan check
- `send_meal_plan_reminder_notification()` (lines 387-416) — orchestrator

**Dependencies:** `httpx`, `supabase`, `python-dotenv`

**Env vars:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`

### `.github/workflows/cron-refresh-menus.yml`

```yaml
name: Refresh dining hall menus
on:
  schedule:
    - cron: '0 9 * * *'   # 9 AM UTC / 4 AM ET
  workflow_dispatch: {}
jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python refresh_menus.py
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

### `.github/workflows/cron-send-reminders.yml`

```yaml
name: Send daily meal plan reminders
on:
  schedule:
    - cron: '0 14 * * *'   # 2 PM UTC / 9 AM ET
  workflow_dispatch: {}
jobs:
  remind:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python send_reminders.py
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

## Idempotency (Vercel + GitHub Actions both active)

- **Menu refresh:** Supabase `upsert` on deterministic ID `{university}_{hall}_{date}`. Double execution is harmless — identical data overwrites itself.
- **Daily reminders:** Both could send duplicate notifications. Acceptable trade-off — users get at most 2 reminders instead of 1. If this becomes a problem, add a `reminder_sent` column to `push_tokens` table later.

## What stays unchanged

- `vercel.json` crons array (kept as redundant fallback)
- Vercel endpoints `/cron/refresh-menus` and `/api/send-daily-reminders` (kept for manual/admin use)
- Existing `daily-cleanup.yml` workflow (unrelated, continues as-is)
- `extract.py` module (used by both `app.py` and new `refresh_menus.py`)

## GitHub Secrets required

Already configured for the existing `daily-cleanup.yml`:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

No new secrets needed.
