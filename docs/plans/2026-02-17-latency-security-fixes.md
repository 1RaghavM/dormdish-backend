# Latency & Security Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix critical security vulnerabilities and reduce meal plan generation latency by 2-5 seconds through parallelization and lazy loading.

**Architecture:** Phased approach - security fixes first (no risk to existing behavior), then latency parallelization, then cold start reduction via lazy imports. Each phase is independently deployable.

**Tech Stack:** FastAPI, Supabase (sync client via `supabase-py`), Mem0 Platform (HTTP API via `httpx`), Google GenAI SDK, Vercel serverless Python.

---

## Phase 1: Critical Security Fixes

### Task 1: Strip password_hash from all auth responses

**Files:**
- Modify: `app.py:1589-1595` (register)
- Modify: `app.py:1628-1633` (login)
- Modify: `app.py:1914-1919` (google login)
- Modify: `app.py:2038-2043` (google register)
- Modify: `app.py:2117-2122` (apple login)
- Modify: `app.py:2267-2272` (apple register)

**Step 1: Add `password_hash` stripping before each return**

In each of the 6 locations, add `user_row.pop('password_hash', None)` on the line immediately before the `return` statement.

For `app.py:1589` (register):
```python
        user_row["session_token"] = token
        user_row.pop('password_hash', None)
        return {
```

For `app.py:1628` (login) - add before line 1628:
```python
        user_row.pop('password_hash', None)
        return {
```

For `app.py:1914` (google login) - add before line 1914:
```python
        user_row.pop('password_hash', None)
        return {
```

For `app.py:2036` (google register) - add after line 2036:
```python
        user_row["session_token"] = token
        user_row.pop('password_hash', None)
        print(f"[google.register] success user_id={user_row.get('id')}")
```

For `app.py:2117` (apple login) - add before line 2117:
```python
        user_row.pop('password_hash', None)
        return {
```

For `app.py:2265` (apple register) - add before line 2267:
```python
        user_row.pop('password_hash', None)
        return {
```

**Step 2: Verify locally**

Run: `grep -n "password_hash" app.py` and confirm no `user_row` containing `password_hash` is returned to clients.

**Step 3: Commit**
```bash
git add app.py
git commit -m "security: strip password_hash from all auth API responses"
```

---

### Task 2: Fix password reset code generation

**Files:**
- Modify: `app.py:483-485` (`generate_reset_code`)

**Step 1: Replace `random.randint` with `secrets.choice`**

Change `app.py:483-485` from:
```python
def generate_reset_code() -> str:
    """Generate a 6-digit reset code"""
    return ''.join([str(random.randint(0, 9)) for _ in range(6)])
```

To:
```python
def generate_reset_code() -> str:
    """Generate an 8-digit cryptographically secure reset code"""
    return ''.join([str(secrets.choice(range(10))) for _ in range(8)])
```

Note: `secrets` is already imported at `app.py:7`.

**Step 2: Commit**
```bash
git add app.py
git commit -m "security: use secrets module for password reset codes, increase to 8 digits"
```

---

### Task 3: Protect the daily reminders cron endpoint

**Files:**
- Modify: `app.py:2570-2571` (`send_daily_reminders`)

**Step 1: Add cron authentication**

Change `app.py:2570-2571` from:
```python
@app.api_route('/api/send-daily-reminders', methods=['GET', 'POST'], summary='Send daily meal plan reminders (admin/cron only)')
async def send_daily_reminders():
```

To:
```python
@app.api_route('/api/send-daily-reminders', methods=['GET', 'POST'], summary='Send daily meal plan reminders (admin/cron only)')
async def send_daily_reminders(
    request: Request,
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="X-Cron-Secret")
):
    # Verify: either Vercel cron, or valid secret
    is_vercel_cron = request.headers.get("x-vercel-cron") is not None
    cron_secret = os.getenv("CRON_SECRET", "")
    secret_valid = (x_cron_secret == cron_secret) or (secret == cron_secret)
    if not is_vercel_cron and cron_secret and not secret_valid:
        raise HTTPException(status_code=401, detail="Unauthorized")
```

**Step 2: Commit**
```bash
git add app.py
git commit -m "security: add cron secret verification to send-daily-reminders endpoint"
```

---

### Task 4: Make Google audience validation mandatory

**Files:**
- Modify: `app.py:1420-1422` (`_verify_google_id_token_via_google`)

**Step 1: Change conditional to mandatory**

Change `app.py:1420-1422` from:
```python
    aud_expected = os.getenv("GOOGLE_CLIENT_ID")
    if aud_expected and data.get("aud") != aud_expected:
        raise HTTPException(status_code=401, detail="Google token audience mismatch")
```

To:
```python
    aud_expected = os.getenv("GOOGLE_CLIENT_ID")
    if not aud_expected:
        logger.error("GOOGLE_CLIENT_ID env var not set - cannot validate Google tokens")
        raise HTTPException(status_code=500, detail="Server configuration error")
    if data.get("aud") != aud_expected:
        raise HTTPException(status_code=401, detail="Google token audience mismatch")
```

**Step 2: Commit**
```bash
git add app.py
git commit -m "security: make Google token audience validation mandatory"
```

---

### Task 5: Add auth to push token registration

**Files:**
- Modify: `app.py:2510-2511` (`register_push_token`)

**Step 1: Add authentication dependency and ownership check**

Change `app.py:2510-2511` from:
```python
@app.post('/api/register-push-token', summary='Register device push notification token')
async def register_push_token(data: PushTokenRequest):
```

To:
```python
@app.post('/api/register-push-token', summary='Register device push notification token')
async def register_push_token(data: PushTokenRequest, current_user=Depends(get_current_user)):
    # Verify the caller owns this user_id
    if str(data.user_id) != str(current_user.get("id")):
        raise HTTPException(status_code=403, detail="Cannot register push token for another user")
```

**Step 2: Commit**
```bash
git add app.py
git commit -m "security: require auth and ownership check for push token registration"
```

---

### Task 6: Remove or protect debug and test endpoints

**Files:**
- Modify: `app.py:4695-4696` (`debug_meal_plan_db`)
- Modify: `app.py:5233-5235` (`debug_all_memories`)
- Modify: `app.py:918-919` (`brevo_health`)
- Modify: `app.py:951-952` (`email_test`)

**Step 1: Gate debug endpoints behind DEBUG env var**

For `app.py:4695` (`debug_meal_plan_db`), add at the start of the function body:
```python
    if os.getenv("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
```

Do the same for `app.py:5233` (`debug_all_memories`), `app.py:918` (`brevo_health`), and `app.py:951` (`email_test`).

**Step 2: Commit**
```bash
git add app.py
git commit -m "security: gate debug/test endpoints behind DEBUG env var"
```

---

### Task 7: Sanitize error responses

**Files:**
- Modify: `app.py` - multiple locations

**Step 1: Replace error detail leaks with generic messages**

Find and replace these patterns (the line numbers are approximate - search for the exact strings):

| Line | Before | After |
|------|--------|-------|
| ~1561 | `detail=f"Supabase insert error: {msg}"` | `detail="Registration failed"` |
| ~1576 | `detail=f"Registration failed: {e}"` | `detail="Registration failed"` |
| ~2029 | `detail=f"Google registration failed: {e}"` | `detail="Google registration failed"` |
| ~3536 | `detail=f"Failed to update profile: {str(e)}"` | `detail="Failed to update profile"` |
| ~4496 | `detail=f"Failed to save meal plan: {str(e)}"` | `detail="Failed to save meal plan"` |
| ~5017 | `detail=f"Failed to log item: {str(e)}"` | `detail="Failed to log food item"` |
| ~960 | `detail=f"Test email failed: {exc}"` | `detail="Test email failed"` |

Keep the `logger.error(f"...: {e}")` calls - those log server-side and are safe.

**Step 2: Commit**
```bash
git add app.py
git commit -m "security: remove internal error details from API responses"
```

---

## Phase 2: Latency Quick Wins

### Task 8: Remove `last_login` update from verify_session_token

**Files:**
- Modify: `app.py:556-578` (`verify_session_token`)

**Step 1: Remove the UPDATE query from the auth hot path**

Change `app.py:567-574` from:
```python
        if response.data and len(response.data) > 0:
            session = response.data[0]

            supabase.table("users")\
                .update({"last_login": datetime.now(ZoneInfo("UTC")).isoformat()})\
                .eq("id", session["user_id"])\
                .execute()
            return session["users"]
```

To:
```python
        if response.data and len(response.data) > 0:
            session = response.data[0]
            return session["users"]
```

The `last_login` update already exists in `auth_login` at line 1624. That's sufficient.

**Step 2: Commit**
```bash
git add app.py
git commit -m "perf: remove last_login UPDATE from session verification hot path"
```

---

### Task 9: Parallelize Mem0 calls in meal plan generation

**Files:**
- Modify: `app.py:3894-3897` (non-streaming path)
- Modify: `app.py:4300-4303` (streaming path)

**Step 1: Parallelize the three sequential calls (non-streaming)**

Change `app.py:3894-3897` from:
```python
            # Retrieve all preference types (only favorites and dislikes)
            favorites = await preference_manager.get_favorites(user_id)
            dislikes = await preference_manager.get_dislikes(user_id)
            profile = await preference_manager.get_user_profile(user_id)
```

To:
```python
            # Retrieve all preference types in parallel
            favorites, dislikes, profile = await asyncio.gather(
                preference_manager.get_favorites(user_id),
                preference_manager.get_dislikes(user_id),
                preference_manager.get_user_profile(user_id),
            )
```

**Step 2: Do the same for streaming path**

Change `app.py:4300-4303` from:
```python
            favorites = await preference_manager.get_favorites(user_id)
            dislikes = await preference_manager.get_dislikes(user_id)
            profile = await preference_manager.get_user_profile(user_id)
```

To:
```python
            favorites, dislikes, profile = await asyncio.gather(
                preference_manager.get_favorites(user_id),
                preference_manager.get_dislikes(user_id),
                preference_manager.get_user_profile(user_id),
            )
```

**Step 3: Commit**
```bash
git add app.py
git commit -m "perf: parallelize Mem0 preference calls with asyncio.gather (saves 1-3s)"
```

---

### Task 10: Reduce Mem0 client timeout

**Files:**
- Modify: `memory/mem0_client.py:47`

**Step 1: Replace 30s timeout with granular 5s timeout**

Change `memory/mem0_client.py:47` from:
```python
        self._client = httpx.Client(timeout=30.0)
```

To:
```python
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
        )
```

**Step 2: Commit**
```bash
git add memory/mem0_client.py
git commit -m "perf: reduce Mem0 HTTP timeout from 30s to 5s (matches Vercel 10s limit)"
```

---

### Task 11: Lazy-init Gemini client in meal_plan_agent.py

**Files:**
- Modify: `meal_plan_agent.py:10,22` and all usages of `client`

**Step 1: Replace eager init with lazy pattern**

Change `meal_plan_agent.py:10,22` from:
```python
from google import genai
from google.genai import types
...
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
```

To:
```python
from typing import Optional as _Optional
...

_client: _Optional[Any] = None

def _get_client():
    global _client
    if _client is None:
        from google import genai as _genai
        _client = _genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client
```

Keep `from google.genai import types` since it's used for type references in function signatures. Move it to a lazy import inside functions if possible, or keep it (it's lighter than the Client constructor).

**Step 2: Replace all `client.` usages with `_get_client().`**

Search for `client.models.generate_content` in `meal_plan_agent.py` and replace with `_get_client().models.generate_content`. There should be ~2-3 call sites (non-streaming and streaming paths).

**Step 3: Commit**
```bash
git add meal_plan_agent.py
git commit -m "perf: lazy-init Gemini client to reduce cold start by ~500ms-1s"
```

---

## Phase 3: Cold Start Reduction

### Task 12: Lazy-load extract module

**Files:**
- Modify: `app.py:31-59` (top-level import)

**Step 1: Remove the top-level import block**

Replace `app.py:31-59` (the entire `try: from extract import ...` block and the fallback `title_case_name` function) with:
```python
# extract module loaded lazily inside fetch_foodpro_menu_rutgers() - only needed for cron
fp_build_menu_url = None
fp_fetch = None
fp_parse_menu = None

def title_case_name(name: str | None) -> str:
    """Convert menu item name to proper title case."""
    if not name:
        return ""
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return ""
    if name.isupper():
        name = name.title()
        name = re.sub(r'\bAnd\b', 'and', name, flags=re.IGNORECASE)
        name = re.sub(r'\bOr\b', 'or', name, flags=re.IGNORECASE)
        name = re.sub(r'\bOf\b', 'of', name, flags=re.IGNORECASE)
        name = re.sub(r'\bThe\b', 'the', name, flags=re.IGNORECASE)
        name = re.sub(r'\bA\b', 'a', name, flags=re.IGNORECASE)
        if name:
            name = name[0].upper() + name[1:]
    else:
        if name:
            name = name[0].upper() + name[1:]
    return name
```

**Step 2: Add lazy import inside `fetch_foodpro_menu_rutgers`**

Find the `fetch_foodpro_menu_rutgers` function (search for `async def fetch_foodpro_menu_rutgers`). At the top of the function body, add:
```python
    from extract import build_menu_url as fp_build_menu_url, fetch as fp_fetch, parse_menu as fp_parse_menu
```

**Step 3: Commit**
```bash
git add app.py
git commit -m "perf: lazy-load extract module (removes requests+bs4 from cold start)"
```

---

### Task 13: Lazy-load google_calendar_service

**Files:**
- Modify: `app.py:5451` (top-level import)
- Modify: All calendar endpoints that use `calendar_service`

**Step 1: Remove top-level import**

Change `app.py:5451` from:
```python
from google_calendar_service import calendar_service
```

To:
```python
# Google Calendar service loaded lazily inside calendar endpoints
_calendar_service = None

def _get_calendar_service():
    global _calendar_service
    if _calendar_service is None:
        from google_calendar_service import GoogleCalendarService
        _calendar_service = GoogleCalendarService()
    return _calendar_service
```

**Step 2: Replace `calendar_service` with `_get_calendar_service()` in all calendar endpoints**

Search for `calendar_service.` in `app.py` (should be ~10-15 usages) and replace each with `_get_calendar_service().`. Examples:
- `calendar_service.refresh_credentials_if_needed(...)` -> `_get_calendar_service().refresh_credentials_if_needed(...)`
- `calendar_service.get_events_for_day(...)` -> `_get_calendar_service().get_events_for_day(...)`
- `calendar_service.find_meal_time_slots(...)` -> `_get_calendar_service().find_meal_time_slots(...)`
- `calendar_service.export_meal_plan_to_calendar(...)` -> `_get_calendar_service().export_meal_plan_to_calendar(...)`

**Step 3: Commit**
```bash
git add app.py
git commit -m "perf: lazy-load google_calendar_service (removes google-api-python-client from cold start)"
```

---

### Task 14: Lazy-load meal_plan_agent and food_scan_endpoints

**Files:**
- Modify: `app.py:174` (meal_plan_agent import)
- Modify: `app.py:5065-5066` (food_scan imports)

**Step 1: Remove top-level meal_plan_agent import**

Change `app.py:174` from:
```python
from meal_plan_agent import generate_meal_plan_with_agent
```

To nothing (remove the line). Then in both `generate_meal_plan_route` and the streaming endpoint, add at the top of the function body:
```python
    from meal_plan_agent import generate_meal_plan_with_agent
```

**Step 2: Remove top-level food_scan imports**

Change `app.py:5065-5066` from:
```python
from food_scan_endpoints import handle_food_scan_analyze, handle_food_scan_feedback
from food_scan_models import FoodScanFeedbackRequest
```

To nothing (remove these lines). Then in `food_scan_analyze` and `food_scan_feedback` endpoints, add at the top of each function body:
```python
    from food_scan_endpoints import handle_food_scan_analyze  # or handle_food_scan_feedback
    from food_scan_models import FoodScanFeedbackRequest  # only where needed
```

**Step 3: Commit**
```bash
git add app.py
git commit -m "perf: lazy-load meal_plan_agent and food_scan modules (removes google-genai from cold start)"
```

---

### Task 15: Remove duplicate FastAPI instantiation

**Files:**
- Modify: `app.py:72-96`

**Step 1: Delete the first app and orphaned route**

Delete `app.py:72-96` entirely (the first `app = FastAPI(...)`, the `SchoolRequest` model, and the first `request_school` route). These are overwritten by the second `app = FastAPI(...)` at line 176, and `request_school` is re-registered later at ~line 1022.

Note: Verify `SchoolRequest` model is redefined later (search for `class SchoolRequest` - it should appear twice, keep the second one).

**Step 2: Commit**
```bash
git add app.py
git commit -m "cleanup: remove duplicate FastAPI instantiation and orphaned route"
```

---

### Task 16: Trim requirements.txt

**Files:**
- Modify: `requirements.txt:3`

**Step 1: Remove uvicorn standard extras**

Change `requirements.txt:3` from:
```
uvicorn[standard]==0.35.0
```

To:
```
uvicorn==0.35.0
```

The `[standard]` extra installs `uvloop`, `httptools`, `watchfiles`, `websockets` - none are needed on Vercel.

**Step 2: Commit**
```bash
git add requirements.txt
git commit -m "perf: remove uvicorn[standard] extras (not needed on Vercel serverless)"
```

---

## Phase 4: Vercel Configuration Fixes

### Task 17: Stagger cron jobs and lower food scan image limit

**Files:**
- Modify: `vercel.json:19-28`
- Modify: `food_scan_endpoints.py:80`

**Step 1: Stagger cron schedules**

Change `vercel.json:19-28` from:
```json
  "crons": [
    {
      "path": "/api/send-daily-reminders",
      "schedule": "0 10 * * *"
    },
    {
      "path": "/cron/refresh-menus",
      "schedule": "0 10 * * *"
    }
  ]
```

To:
```json
  "crons": [
    {
      "path": "/cron/refresh-menus",
      "schedule": "0 9 * * *"
    },
    {
      "path": "/api/send-daily-reminders",
      "schedule": "0 14 * * *"
    }
  ]
```

**Step 2: Lower food scan image limit to match Vercel's 4.5MB body limit**

In `food_scan_endpoints.py`, find the 10MB check (line ~80):
```python
if len(image_data) > 10 * 1024 * 1024:
```

Change to:
```python
if len(image_data) > 4 * 1024 * 1024:
    raise HTTPException(status_code=400, detail="Image too large (max 4MB)")
```

**Step 3: Commit**
```bash
git add vercel.json food_scan_endpoints.py
git commit -m "config: stagger cron jobs, lower image limit to 4MB for Vercel compatibility"
```

---

## Verification Checklist

After all phases are complete, verify:

- [ ] `curl POST /auth/login` response does NOT contain `password_hash`
- [ ] `curl POST /auth/register` response does NOT contain `password_hash`
- [ ] `curl GET /api/send-daily-reminders` returns 401 without cron secret
- [ ] `curl GET /email/test?email=x` returns 404 when `DEBUG != true`
- [ ] `curl GET /meal-plan/debug` returns 404 when `DEBUG != true`
- [ ] Meal plan generation completes successfully (Mem0 calls now parallel)
- [ ] Food scan with >4MB image returns 400
- [ ] Cold start time reduced (check Vercel function logs)
- [ ] All existing functionality still works (register, login, generate meal plan, food scan)
