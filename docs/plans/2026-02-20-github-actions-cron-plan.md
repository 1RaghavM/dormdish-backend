# GitHub Actions Cron Migration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move cron jobs (menu refresh + daily reminders) from Vercel serverless to standalone Python scripts triggered by GitHub Actions, eliminating the 10-second timeout constraint.

**Architecture:** Two standalone scripts (`refresh_menus.py`, `send_reminders.py`) following the existing `cleanup_menus.py` pattern. Each has a matching GitHub Actions workflow. Functions are ported from `app.py` without importing it (avoids pulling in FastAPI + all side effects).

**Tech Stack:** Python 3.11, httpx, beautifulsoup4, supabase-py, extract.py (existing FoodPro scraper)

**Design doc:** `docs/plans/2026-02-20-github-actions-cron-design.md`

---

### Task 1: Create `refresh_menus.py` — menu refresh standalone script

**Files:**
- Create: `refresh_menus.py`
- Reference (read-only, port logic from): `app.py:2620-2625`, `app.py:2646-2700`, `app.py:2746-2763`, `app.py:2798-3031`, `app.py:35-54`, `app.py:223-240`, `app.py:128-133`
- Reference (imported by script): `extract.py` (functions: `build_menu_url`, `parse_menu`)

**Step 1: Write `refresh_menus.py`**

Port these functions from `app.py` into a self-contained async script:

- `title_case_name()` — from `app.py:35-54` (title case helper)
- `_fetch_with_retries()` — from `app.py:223-240` (HTTP retry logic, adapted to use a local httpx client instead of `app.state`)
- `save_menu_to_supabase()` — from `app.py:2646-2700` (Supabase upsert, converted from async-threaded to sync since no event loop contention)
- `fetch_foodpro_menu_rutgers()` — from `app.py:2821-3031` (FoodPro scraping, uses `extract.py` imports)
- `fetch_and_cache_menu()` — from `app.py:2798-2818` (orchestrator per hall)
- `_rutgers_today_str()` — from `app.py:2620-2624` (timezone-aware date)
- `main()` — new entrypoint that loops `ALL_DINING_HALLS` and calls `fetch_and_cache_menu` for each

Key adaptations from app.py:
- Replace `_get_http_client()` / `app.state.http` with a module-level `httpx.AsyncClient`
- Replace `supabase` global (from app.py module-level init) with `create_client()` called in main
- `UNIVERSITY_DISPLAY_NAMES` dict copied inline: `{"rutgers": "Rutgers University", "hopkins": "Johns Hopkins University"}`
- `ALL_DINING_HALLS` list copied inline (3 Rutgers halls)
- Use `load_dotenv()` for local dev, env vars from GitHub Secrets in CI
- Strip all debug `print(f"🔍 ...")` statements — use clean `print()` logging like `cleanup_menus.py`
- `save_menu_to_supabase` becomes synchronous (no `asyncio.to_thread` needed — script owns the event loop)
- `location_map` from `app.py:2831-2836` copied inline

Script structure:
```python
"""
Daily Menu Refresh Script

Fetches dining hall menus from FoodPro and caches them in Supabase.
Designed to run via GitHub Actions cron but can be run manually.

Usage:
    python refresh_menus.py [--date YYYY-MM-DD] [--dry-run]
"""
import os, sys, asyncio, re, argparse, time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv
from supabase import create_client
from extract import build_menu_url as fp_build_menu_url, parse_menu as fp_parse_menu

# ... constants (ALL_DINING_HALLS, UNIVERSITY_DISPLAY_NAMES, location_map, HTTP config)
# ... title_case_name()
# ... _fetch_with_retries() using module-level httpx.AsyncClient
# ... fetch_foodpro_menu_rutgers() — ported from app.py:2821-3031
# ... save_menu_to_supabase() — synchronous version
# ... fetch_and_cache_menu() — ported from app.py:2798-2818
# ... main() with argparse (--date, --dry-run)

if __name__ == "__main__":
    asyncio.run(main())
```

**Step 2: Test locally**

Run: `python refresh_menus.py --dry-run`
Expected: Script runs, prints what would be fetched, no Supabase writes.

Run: `python refresh_menus.py`
Expected: Fetches 3 dining hall menus, upserts to Supabase, prints success/fail summary.

**Step 3: Commit**

```bash
git add refresh_menus.py
git commit -m "feat: add standalone menu refresh script for GitHub Actions cron"
```

---

### Task 2: Create `send_reminders.py` — daily reminders standalone script

**Files:**
- Create: `send_reminders.py`
- Reference (read-only, port logic from): `app.py:297-347`, `app.py:350-366`, `app.py:369-384`, `app.py:387-416`, `app.py:2556-2617`

**Step 1: Write `send_reminders.py`**

Port these functions from `app.py`:

- `send_push_notification()` — from `app.py:297-347` (Expo push API call)
- `get_user_push_token()` — from `app.py:350-366` (Supabase query)
- `check_user_has_meal_plan_today()` — from `app.py:369-384` (Supabase query, ET timezone)
- `send_meal_plan_reminder_notification()` — from `app.py:387-416` (per-user orchestrator)
- `main()` — from `app.py:2556-2617` (query all active tokens, loop users, send reminders)

Key adaptations:
- All Supabase calls become synchronous (no `asyncio.to_thread`)
- `httpx.AsyncClient` for Expo API call (simple POST, fine to use sync `httpx.Client` too)
- Notification text: title=`"🍽️ Time to Plan Your Meals!"`, body=`"Haven't created your meal plan for today yet. Tap here to get started!"`
- Data payload: `{"screen": "home", "action": "create_meal_plan"}`
- Token validation: must start with `ExponentPushToken[`
- Expo endpoint: `https://exp.host/--/api/v2/push/send`
- Use Eastern Time for "today" check: `datetime.now(ZoneInfo("America/New_York")).date().isoformat()`

Script structure:
```python
"""
Daily Meal Plan Reminder Script

Sends push notifications to users who haven't created their meal plan for today.
Designed to run via GitHub Actions cron but can be run manually.

Usage:
    python send_reminders.py [--dry-run]
"""
import os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from supabase import create_client

# ... send_push_notification() — sync httpx POST to Expo
# ... get_user_push_token() — Supabase query
# ... check_user_has_meal_plan_today() — Supabase query
# ... send_meal_plan_reminder_notification() — per-user logic
# ... main() with argparse (--dry-run)

if __name__ == "__main__":
    main()
```

**Step 2: Test locally**

Run: `python send_reminders.py --dry-run`
Expected: Queries Supabase for active tokens, prints what reminders would be sent, no actual push notifications.

**Step 3: Commit**

```bash
git add send_reminders.py
git commit -m "feat: add standalone daily reminders script for GitHub Actions cron"
```

---

### Task 3: Create GitHub Actions workflow files

**Files:**
- Create: `.github/workflows/cron-refresh-menus.yml`
- Create: `.github/workflows/cron-send-reminders.yml`
- Reference: `.github/workflows/daily-cleanup.yml` (existing pattern to follow)

**Step 1: Write `.github/workflows/cron-refresh-menus.yml`**

```yaml
name: Refresh dining hall menus

on:
  schedule:
    - cron: '0 9 * * *'  # 9:00 AM UTC / 4:00 AM ET
  workflow_dispatch: {}

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Refresh menus
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
        run: python refresh_menus.py
```

**Step 2: Write `.github/workflows/cron-send-reminders.yml`**

```yaml
name: Send daily meal plan reminders

on:
  schedule:
    - cron: '0 14 * * *'  # 2:00 PM UTC / 9:00 AM ET
  workflow_dispatch: {}

jobs:
  remind:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Send reminders
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
        run: python send_reminders.py
```

**Step 3: Commit**

```bash
git add .github/workflows/cron-refresh-menus.yml .github/workflows/cron-send-reminders.yml
git commit -m "ci: add GitHub Actions cron workflows for menu refresh and daily reminders"
```

---

### Task 4: Fix Vercel cron auth (so the fallback actually works)

**Files:**
- Modify: `app.py:2449-2460` (refresh-menus auth block)
- Modify: `app.py:2567-2576` (send-reminders auth block)

**Step 1: Fix auth in `/cron/refresh-menus`**

In `app.py:2449-2460`, replace the `x-vercel-cron` header check with the correct `Authorization: Bearer` check per Vercel docs:

Old code (lines 2449-2460):
```python
    is_vercel_cron = request.headers.get("x-vercel-cron") is not None

    if not is_vercel_cron:
        cron_secret = os.getenv("CRON_SECRET")
        if not cron_secret:
            logger.error("CRON_SECRET env var not set - rejecting cron request")
            raise HTTPException(status_code=500, detail="Server configuration error")
        secret_valid = (x_cron_secret == cron_secret) or (secret == cron_secret)
        if not secret_valid:
            raise HTTPException(status_code=401, detail="Unauthorized - pass ?secret=YOUR_CRON_SECRET")
```

New code:
```python
    # Vercel cron sends Authorization: Bearer <CRON_SECRET> and User-Agent: vercel-cron/1.0
    cron_secret = os.getenv("CRON_SECRET")
    auth_header = request.headers.get("authorization") or ""
    is_vercel_cron = auth_header == f"Bearer {cron_secret}" if cron_secret else False
    is_manual_secret = (x_cron_secret == cron_secret) or (secret == cron_secret) if cron_secret else False

    if not is_vercel_cron and not is_manual_secret:
        raise HTTPException(status_code=401, detail="Unauthorized - set CRON_SECRET or pass ?secret=YOUR_CRON_SECRET")
```

**Step 2: Fix auth in `/api/send-daily-reminders`**

In `app.py:2567-2576`, apply same fix:

Old code (lines 2567-2576):
```python
    is_vercel_cron = request.headers.get("x-vercel-cron") is not None
    if not is_vercel_cron:
        cron_secret = os.getenv("CRON_SECRET")
        if not cron_secret:
            logger.error("CRON_SECRET env var not set - rejecting cron request")
            raise HTTPException(status_code=500, detail="Server configuration error")
        secret_valid = (x_cron_secret == cron_secret) or (secret == cron_secret)
        if not secret_valid:
            raise HTTPException(status_code=401, detail="Unauthorized")
```

New code:
```python
    cron_secret = os.getenv("CRON_SECRET")
    auth_header = request.headers.get("authorization") or ""
    is_vercel_cron = auth_header == f"Bearer {cron_secret}" if cron_secret else False
    is_manual_secret = (x_cron_secret == cron_secret) or (secret == cron_secret) if cron_secret else False

    if not is_vercel_cron and not is_manual_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "fix: correct Vercel cron auth to check Authorization Bearer header"
```

---

### Task 5: Manual verification via workflow_dispatch

**Step 1: Push branch and trigger workflows manually**

```bash
git push origin <branch-name>
```

Go to GitHub repo → Actions tab → select "Refresh dining hall menus" → "Run workflow" button.
Expected: Workflow runs, installs deps, executes `refresh_menus.py`, logs show 3 halls refreshed.

**Step 2: Trigger reminders workflow**

Actions tab → "Send daily meal plan reminders" → "Run workflow".
Expected: Workflow runs, queries push tokens, sends/skips reminders, logs show sent/failed count.

**Step 3: Verify Supabase data**

Check `menus` table for today's date — should have 3 rows with IDs:
- `rutgers_busch-dining-hall_YYYY-MM-DD`
- `rutgers_livingston-dining-commons_YYYY-MM-DD`
- `rutgers_neilson-dining-hall_YYYY-MM-DD`
