# DormDish Backend: Latency & Security Audit Design

**Date:** 2026-02-17
**Status:** Approved
**Context:** FastAPI backend on Vercel free plan (10s execution limit, 4.5MB body limit, serverless Python)

## Problem Statement

The DormDish backend has 60+ issues across three categories causing degraded performance, potential security breaches, and incompatibility with Vercel's free plan constraints. The most critical endpoint (`/generate-meal-plan`) takes 5-15 seconds due to sequential external API calls, cold starts consume 3-6 seconds from heavy imports, and multiple security vulnerabilities expose user data.

## Architecture Overview

```
Client Request
    → Vercel Edge (10s limit, 4.5MB body)
    → api/index.py (imports entire app.py monolith)
    → FastAPI (sync Supabase client blocking event loop)
    → Sequential: Auth → DB queries → Mem0 x3 → Gemini AI → Save
```

## Design: Phased Fix Strategy

The fixes are organized into 5 phases, ordered by impact and risk. Each phase is independently deployable and testable.

---

### Phase 1: Critical Security Fixes (no latency risk, deploy immediately)

**Goal:** Close the most dangerous security holes without touching performance-sensitive code.

| Fix | File | Change |
|-----|------|--------|
| Strip `password_hash` from all auth responses | app.py | Add `user_row.pop('password_hash', None)` before return in register, login, google login/register, apple login/register |
| Protect `/api/send-daily-reminders` | app.py | Add `x-vercel-cron` header + `CRON_SECRET` check (same pattern as `/cron/refresh-menus`) |
| Fix password reset code generation | app.py | Replace `random.randint` with `secrets.choice`, add attempt counter in DB |
| Make Google audience validation mandatory | app.py | Fail with 500 if `GOOGLE_CLIENT_ID` env var is unset |
| Add auth to push token registration | app.py | Add `Depends(get_current_user)`, verify `data.user_id == current_user["id"]` |
| Remove/protect debug endpoints | app.py | Gate `/meal-plan/debug` and `/preferences/debug` behind admin check or remove |
| Protect email test + brevo health | app.py | Add auth or remove `/email/test`, `/health/brevo` |
| Sanitize error responses | app.py | Replace `detail=f"...{e}"` with generic messages, log full error server-side |

**Risk:** Minimal. These are additive checks that don't change existing logic.

---

### Phase 2: Latency Quick Wins (high impact, low risk)

**Goal:** Cut 2-5 seconds from the meal plan generation hot path.

#### 2a. Parallelize Mem0 calls
**File:** `app.py` lines ~3895-3897 and ~4301-4303

Before:
```python
favorites = await preference_manager.get_favorites(user_id)
dislikes = await preference_manager.get_dislikes(user_id)
profile = await preference_manager.get_user_profile(user_id)
```

After:
```python
favorites, dislikes, profile = await asyncio.gather(
    preference_manager.get_favorites(user_id),
    preference_manager.get_dislikes(user_id),
    preference_manager.get_user_profile(user_id),
)
```

Apply to both streaming and non-streaming paths.

#### 2b. Parallelize pre-AI Supabase + Mem0 calls
**File:** `app.py` generate_meal_plan_route

After auth, run menu fetch, food log query, BMR update, and Mem0 calls in parallel:
```python
menu_data, food_log_result, (favorites, dislikes, profile), _ = await asyncio.gather(
    get_dining_hall_menu(dining_hall, date),
    asyncio.to_thread(lambda: supabase.table("food_log").select("*").eq(...).execute()),
    asyncio.gather(
        preference_manager.get_favorites(user_id),
        preference_manager.get_dislikes(user_id),
        preference_manager.get_user_profile(user_id),
    ),
    asyncio.to_thread(lambda: supabase.table("users").update({...}).eq(...).execute()),
)
```

#### 2c. Reduce Mem0 timeout
**File:** `memory/mem0_client.py` line 47

Change from `httpx.Client(timeout=30.0)` to:
```python
httpx.Client(timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0))
```

#### 2d. Remove `last_login` update from auth hot path
**File:** `app.py` `verify_session_token()`

Remove the `supabase.table("users").update({"last_login": ...})` from `verify_session_token()`. Update `last_login` only in the actual `/auth/login` endpoint.

#### 2e. Lazy-init Gemini client
**File:** `meal_plan_agent.py` line 22

Replace eager `client = genai.Client(...)` with lazy pattern matching `food_scan_agent.py`:
```python
_client = None
def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client
```

**Estimated savings:** 1.5-4.5s on meal plan generation, 500ms-1s cold start.

---

### Phase 3: Cold Start Reduction (medium risk, high impact)

**Goal:** Cut cold start from ~5s to ~2s by deferring heavy imports.

#### 3a. Lazy-load `extract` module
Move `from extract import ...` (app.py lines 32-38) inside `fetch_foodpro_menu_rutgers()`. This removes `requests`, `beautifulsoup4`, and `concurrent.futures` from cold start for non-cron requests.

#### 3b. Lazy-load `google_calendar_service`
Move `from google_calendar_service import calendar_service` (app.py line 5451) inside each calendar endpoint. This removes `google-api-python-client`, `google-auth`, `google-auth-oauthlib` from cold start for non-calendar requests.

#### 3c. Lazy-load `meal_plan_agent`
Move `from meal_plan_agent import generate_meal_plan_with_agent` (app.py line 174) inside the meal plan endpoints. This removes `google.genai` + protobuf from cold start for non-meal-plan requests.

#### 3d. Lazy-load `food_scan_endpoints`
Move `from food_scan_endpoints import ...` (app.py line 5065) inside the food scan router setup or endpoints.

#### 3e. Remove duplicate FastAPI instantiation
Delete the first `app = FastAPI(...)` at line 72 and the orphaned `request_school` route.

#### 3f. Change `uvicorn[standard]` to `uvicorn`
In `requirements.txt`, remove the `[standard]` extra (uvloop, httptools, watchfiles, websockets not needed on Vercel).

**Risk:** Medium. Import errors could surface if modules have circular dependencies. Test each lazy import individually.

---

### Phase 4: Async/Blocking Fixes (medium risk, systematic)

**Goal:** Stop blocking the event loop with sync Supabase calls.

#### 4a. Wrap all sync Supabase calls in `asyncio.to_thread()`
Create a helper:
```python
async def _db(fn):
    """Run a synchronous Supabase operation without blocking the event loop."""
    return await asyncio.to_thread(fn)
```

Apply to every `supabase.table(...).select/insert/update/delete(...).execute()` call in async endpoints. There are ~50+ call sites. Keep sync endpoints (declared as `def`) unchanged since FastAPI already runs them in a threadpool.

#### 4b. Convert Mem0Client to async
**File:** `memory/mem0_client.py`

Replace `httpx.Client` with `httpx.AsyncClient`. Make `_make_request` async. Remove `asyncio.to_thread()` wrappers in `preference_manager.py`.

#### 4c. Replace SMTP with Brevo HTTP API
**Files:** `app.py` `send_password_reset_email()`, `send_bug_report_email()`

Replace `smtplib` SMTP calls with Brevo's transactional email API (already integrated for other emails). Execute inline before response instead of as background task.

#### 4d. Use shared httpx client everywhere
Replace throwaway `async with httpx.AsyncClient() as client:` patterns in `send_push_notification()` and `barcode_lookup()` with the shared `_get_http_client()`.

**Risk:** Medium. Requires touching many files. Each change is mechanical but the volume requires careful testing.

---

### Phase 5: Vercel Architecture (high effort, high impact)

**Goal:** Make the app viable on Vercel free plan for all endpoints.

#### 5a. Stagger cron jobs
```json
{"path": "/cron/refresh-menus", "schedule": "0 9 * * *"},
{"path": "/api/send-daily-reminders", "schedule": "0 14 * * *"}
```

#### 5b. Lower food scan image limit to 4MB
Match Vercel's 4.5MB body limit. Read image in chunks instead of all-at-once.

#### 5c. Add per-endpoint timeout
Wrap long-running endpoints with `asyncio.wait_for()`:
```python
return await asyncio.wait_for(_generate_meal_plan_impl(...), timeout=9.0)
```

#### 5d. Parallelize account deletion
Use `asyncio.gather()` for the 7 independent DELETE calls.

#### 5e. Parallelize daily reminders
Batch Expo push notifications (up to 100 per request) and use `asyncio.gather()` with a semaphore for concurrent sends.

#### 5f. Add OAuth state verification (server-side)
Store OAuth state in a `oauth_states` Supabase table with TTL. Verify on callback instead of trusting the state parameter.

**Risk:** Higher. Architecture changes require integration testing. Phase 5f changes the OAuth flow which affects calendar linking.

---

## What We Are NOT Doing

- **Not splitting into multiple Vercel functions** - Too much restructuring for now. Lazy imports achieve 80% of the cold start benefit.
- **Not adding Redis/external cache** - In-memory caches with serverless limitations are acceptable for now.
- **Not migrating to async Supabase client** - `asyncio.to_thread()` wrapper is sufficient.
- **Not adding a fallback AI provider** - Gemini's retry logic is already robust. Can revisit if reliability becomes an issue.
- **Not implementing rate limiting** - Requires a persistence layer (Redis/Supabase). Can add later with `slowapi` + Supabase-backed store.

## Success Criteria

| Metric | Before | After |
|--------|--------|-------|
| Cold start | ~5-6s | ~2-3s |
| `/generate-meal-plan` latency | 8-15s | 5-10s |
| Password hash in responses | Exposed | Stripped |
| Unprotected cron endpoints | 1 | 0 |
| Sync Supabase in async context | ~50 sites | 0 |
| Mem0 sequential calls | 3 serial | 3 parallel |

## Testing Strategy

- Run existing tests after each phase
- Manual smoke test of: register, login, generate meal plan, food scan, calendar link
- Verify cold start improvement via Vercel function logs
- Check no `password_hash` in any API response via manual curl
