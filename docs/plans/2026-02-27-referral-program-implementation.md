# Referral Program Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement a double-sided referral program where any user can invite friends, both get a free week of premium, and the referrer earns a bonus month when the referred user converts to premium.

**Architecture:** Backend-first approach. Add Supabase tables, then backend endpoints in `app.py`, then wire the activation threshold into existing endpoints. Finally, implement the frontend (SwiftUI) with onboarding screen, profile section, and share sheet. RevenueCat promotional entitlements deliver rewards.

**Tech Stack:** Python/FastAPI (backend), Supabase/PostgreSQL (database), RevenueCat Server API (rewards), SwiftUI/iOS 17+ (frontend)

**Design doc:** `docs/plans/2026-02-27-referral-program-design.md`

---

## Task 1: Create Supabase Tables

**Files:**
- Manual: Supabase Dashboard SQL Editor

**Step 1: Run SQL to create tables**

Execute the following SQL in Supabase Dashboard > SQL Editor:

```sql
-- Add referred_by_code column to users table
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_code VARCHAR(6);
ALTER TABLE users ADD COLUMN IF NOT EXISTS real_user_status TEXT;

-- Referral codes table
CREATE TABLE IF NOT EXISTS referral_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code VARCHAR(6) UNIQUE NOT NULL,
    is_active BOOLEAN DEFAULT true,
    successful_referrals INT DEFAULT 0,
    max_referrals INT DEFAULT 12,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_referral_codes_user_id ON referral_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_referral_codes_code ON referral_codes(code);

-- Referral events table
CREATE TABLE IF NOT EXISTS referral_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    referral_code_id UUID NOT NULL REFERENCES referral_codes(id) ON DELETE CASCADE,
    referrer_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referred_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'pending',
    referred_at TIMESTAMPTZ DEFAULT now(),
    rewarded_at TIMESTAMPTZ,
    bonus_granted BOOLEAN DEFAULT false,
    bonus_granted_at TIMESTAMPTZ,
    flagged BOOLEAN DEFAULT false,
    flag_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_referral_events_referrer ON referral_events(referrer_user_id);
CREATE INDEX IF NOT EXISTS idx_referral_events_referred ON referral_events(referred_user_id);
CREATE INDEX IF NOT EXISTS idx_referral_events_status ON referral_events(status);

-- Referral rewards audit ledger
CREATE TABLE IF NOT EXISTS referral_rewards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    referral_event_id UUID NOT NULL REFERENCES referral_events(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward_type TEXT NOT NULL,
    granted_via TEXT NOT NULL,
    duration_days INT NOT NULL,
    granted_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_referral_rewards_user ON referral_rewards(user_id);
```

**Step 2: Verify tables exist**

Run in SQL Editor:
```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
AND table_name IN ('referral_codes', 'referral_events', 'referral_rewards');
```

Expected: 3 rows returned.

**Step 3: Commit a note**

No code files to commit for this step -- tables are created directly in Supabase.

---

## Task 2: Add Backend Pydantic Models and Environment Config

**Files:**
- Modify: `app.py:1180-1211` (after existing Apple models)
- Modify: `app.py:1-28` (imports section -- add `string` import)

**Step 1: Add `string` to imports**

In `app.py`, at line 7 (after `import secrets`), add `import string`:

```python
import string
```

**Step 2: Add env vars after existing env loading**

Find where other env vars like `SUPABASE_URL` are loaded (around line 30-50) and add:

```python
REVENUECAT_SECRET_API_KEY = os.getenv("REVENUECAT_SECRET_API_KEY", "")
REVENUECAT_WEBHOOK_AUTH_KEY = os.getenv("REVENUECAT_WEBHOOK_AUTH_KEY", "")
```

**Step 3: Add Pydantic models**

After the `AppleRegisterRequest` model (line 1210), add:

```python
# ── Referral Models ──

class ApplyReferralCodeRequest(BaseModel):
    code: str

class ReferralCodeResponse(BaseModel):
    success: bool
    code: Optional[str] = None
    has_code: bool = False
    successful_referrals: int = 0
    max_referrals: int = 12
    message: Optional[str] = None
```

**Step 4: Add `real_user_status` to Apple request models**

Modify `AppleIdTokenRequest` (line 1180) to add `real_user_status`:

```python
class AppleIdTokenRequest(BaseModel):
    id_token: str
    user_data: Optional[Dict[str, Any]] = None
    real_user_status: Optional[int] = None  # 0=unsupported, 1=unknown, 2=likelyReal
```

Modify `AppleRegisterRequest` (line 1186) to add `real_user_status` and `referral_code`:

```python
class AppleRegisterRequest(BaseModel):
    id_token: str
    username: str
    email: EmailStr
    age: int
    height_feet: int
    height_inches: int
    weight_lbs: float
    gender: str
    activity_level: str
    goal: str
    university: str
    dietary_preference: Optional[str] = None
    birthday: Optional[str] = None
    goal_weight_lbs: Optional[float] = None
    weight_change_speed: Optional[str] = None
    accomplishments: Optional[List[str]] = None
    obstacles: Optional[List[str]] = None
    school_id: Optional[str] = None
    dining_halls: Optional[List[str]] = None
    dietary_restrictions: Optional[List[str]] = None
    attribution_source: Optional[str] = None
    apple_health_connected: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    real_user_status: Optional[int] = None  # 0=unsupported, 1=unknown, 2=likelyReal
    referral_code: Optional[str] = None
```

**Step 5: Commit**

```bash
git add app.py
git commit -m "feat(referral): add Pydantic models, env config, and real_user_status to Apple auth"
```

---

## Task 3: Store `real_user_status` in Apple Auth Endpoints

**Files:**
- Modify: `app.py:2030-2112` (apple login endpoint)
- Modify: `app.py:2114-2258` (apple register endpoint)

**Step 1: Update apple login to store `real_user_status`**

In `auth_apple_login` (line 2030), after the line that links `apple_sub` (around line 2080), add logic to store `real_user_status` if provided:

```python
        # Store real_user_status if provided (only sent on first auth)
        if payload.real_user_status is not None and not user_row.get("real_user_status"):
            try:
                real_status_map = {0: "unsupported", 1: "unknown", 2: "likelyReal"}
                status_str = real_status_map.get(payload.real_user_status, "unknown")
                supabase.table("users").update({"real_user_status": status_str}).eq("id", user_row.get("id")).execute()
                user_row["real_user_status"] = status_str
            except Exception as e:
                print(f"[apple.login] warning: failed to store real_user_status: {e}")
```

**Step 2: Update apple register to store `real_user_status` and `referral_code`**

In `auth_apple_register` (line 2114), find where the user record is being built for insertion (the dict that gets inserted into the `users` table). Add these fields:

```python
            "real_user_status": {0: "unsupported", 1: "unknown", 2: "likelyReal"}.get(data.real_user_status, "unknown") if data.real_user_status is not None else None,
            "referred_by_code": data.referral_code,
```

After the user record is created successfully, add referral event creation:

```python
        # If a referral code was provided, create a pending referral event
        if data.referral_code:
            try:
                code_res = supabase.table("referral_codes").select("*").eq("code", data.referral_code.upper()).eq("is_active", True).execute()
                if code_res.data:
                    code_row = code_res.data[0]
                    referrer_id = code_row["user_id"]
                    new_user_id = new_user["id"]  # adjust to match the variable holding the new user's id
                    # Don't allow self-referral
                    if str(referrer_id) != str(new_user_id):
                        # Check referrer hasn't hit cap
                        if code_row["successful_referrals"] < code_row["max_referrals"]:
                            supabase.table("referral_events").insert({
                                "referral_code_id": code_row["id"],
                                "referrer_user_id": referrer_id,
                                "referred_user_id": new_user_id,
                                "status": "pending",
                            }).execute()
                            print(f"[apple.register] referral event created for code={data.referral_code}")
            except Exception as e:
                print(f"[apple.register] warning: failed to process referral code: {e}")
```

**Note:** The variable holding the newly created user's ID may be named differently in the existing code. Check the register function to find the exact variable name (likely from the Supabase insert result).

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat(referral): store real_user_status and create referral events on Apple register"
```

---

## Task 4: Add RevenueCat Promotional Entitlement Helper

**Files:**
- Modify: `app.py` (add helper function near the push notification helpers, around line 348)

**Step 1: Add the helper function**

After `get_user_push_token` function (around line 370), add:

```python
# ===================================
# RevenueCat Referral Helpers
# ===================================

async def grant_revenuecat_promo(user_id: str, duration: str = "weekly") -> Dict[str, Any]:
    """
    Grant a promotional entitlement via RevenueCat's server API.

    Args:
        user_id: The app_user_id in RevenueCat (same as our user UUID)
        duration: "daily", "three_day", "weekly", "monthly", "two_month",
                  "three_month", "six_month", "yearly", or "lifetime"

    Returns:
        Dictionary with success status and response data
    """
    if not REVENUECAT_SECRET_API_KEY:
        logger.warning("REVENUECAT_SECRET_API_KEY not configured, skipping promo grant")
        return {"success": False, "error": "RevenueCat not configured"}

    url = f"https://api.revenuecat.com/v1/subscribers/{user_id}/entitlements/student_plan/promotional"

    try:
        async with httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT) as client:
            response = await client.post(
                url,
                json={"duration": duration},
                headers={
                    "Authorization": f"Bearer {REVENUECAT_SECRET_API_KEY}",
                    "Content-Type": "application/json",
                }
            )
            if response.status_code in (200, 201):
                logger.info(f"[revenuecat] promo granted user_id={user_id} duration={duration}")
                return {"success": True, "data": response.json()}
            else:
                logger.error(f"[revenuecat] promo failed user_id={user_id} status={response.status_code} body={response.text}")
                return {"success": False, "error": response.text}
    except Exception as e:
        logger.error(f"[revenuecat] promo exception user_id={user_id}: {e}")
        return {"success": False, "error": str(e)}
```

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat(referral): add RevenueCat promotional entitlement helper"
```

---

## Task 5: Add Referral Code Generation Endpoint

**Files:**
- Modify: `app.py` (add endpoint before `if __name__` block, around line 7065)

**Step 1: Add the endpoint**

Before the `if __name__ == '__main__':` block (line 7066), add:

```python
# ==================== REFERRAL ENDPOINTS ====================

def _generate_referral_code() -> str:
    """Generate a unique 6-character alphanumeric code."""
    chars = string.ascii_uppercase + string.digits
    # Remove ambiguous characters (0/O, 1/I/L)
    chars = chars.replace("0", "").replace("O", "").replace("1", "").replace("I", "").replace("L", "")
    return ''.join(secrets.choice(chars) for _ in range(6))


@app.post("/referral/generate-code", summary="Generate or retrieve referral code")
async def generate_referral_code(user: dict = Depends(get_current_user)):
    """Generate a unique referral code for the authenticated user.
    Idempotent -- returns existing code if one already exists.
    Only users with real_user_status == 'likelyReal' can generate codes.
    """
    try:
        user_id = str(user["id"])

        # Check real_user_status
        real_status = user.get("real_user_status")
        if real_status and real_status != "likelyReal":
            raise HTTPException(status_code=403, detail="Account not eligible for referral program")

        # Check if user already has a code
        existing = supabase.table("referral_codes").select("*").eq("user_id", user_id).execute()
        if existing.data:
            row = existing.data[0]
            return {
                "success": True,
                "code": row["code"],
                "has_code": True,
                "successful_referrals": row["successful_referrals"],
                "max_referrals": row["max_referrals"],
            }

        # Generate unique code with retry
        for _ in range(10):
            code = _generate_referral_code()
            try:
                result = supabase.table("referral_codes").insert({
                    "user_id": user_id,
                    "code": code,
                }).execute()
                if result.data:
                    return {
                        "success": True,
                        "code": code,
                        "has_code": True,
                        "successful_referrals": 0,
                        "max_referrals": 12,
                    }
            except Exception:
                continue  # Code collision, retry

        raise HTTPException(status_code=500, detail="Failed to generate referral code")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[referral.generate] error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate referral code")
```

**Step 2: Verify the server starts**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend
python -c "import app; print('OK')"
```

Expected: `OK` (no import errors)

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat(referral): add POST /referral/generate-code endpoint"
```

---

## Task 6: Add Get My Code and Apply Code Endpoints

**Files:**
- Modify: `app.py` (add after generate-code endpoint)

**Step 1: Add GET /referral/my-code endpoint**

```python
@app.get("/referral/my-code", summary="Get user's referral code and stats")
async def get_my_referral_code(user: dict = Depends(get_current_user)):
    """Get the authenticated user's referral code and referral stats."""
    try:
        user_id = str(user["id"])
        existing = supabase.table("referral_codes").select("*").eq("user_id", user_id).execute()
        if not existing.data:
            return {
                "success": True,
                "has_code": False,
                "code": None,
                "successful_referrals": 0,
                "max_referrals": 12,
            }
        row = existing.data[0]
        return {
            "success": True,
            "has_code": True,
            "code": row["code"],
            "successful_referrals": row["successful_referrals"],
            "max_referrals": row["max_referrals"],
        }
    except Exception as e:
        logger.error(f"[referral.my-code] error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch referral code")
```

**Step 2: Add POST /referral/apply-code endpoint**

```python
@app.post("/referral/apply-code", summary="Apply a referral code")
async def apply_referral_code(req: ApplyReferralCodeRequest, user: dict = Depends(get_current_user)):
    """Apply a referral code to the authenticated user's account.
    Creates a pending referral event. Rewards granted after activation threshold.
    """
    try:
        user_id = str(user["id"])
        code = req.code.upper().strip()

        # Check user hasn't already used a referral code
        if user.get("referred_by_code"):
            raise HTTPException(status_code=400, detail="You've already used a referral code")

        # Check real_user_status
        real_status = user.get("real_user_status")
        if real_status and real_status != "likelyReal":
            raise HTTPException(status_code=403, detail="Account not eligible for referral program")

        # Validate code exists and is active
        code_res = supabase.table("referral_codes").select("*").eq("code", code).eq("is_active", True).execute()
        if not code_res.data:
            raise HTTPException(status_code=404, detail="Invalid referral code")

        code_row = code_res.data[0]
        referrer_id = code_row["user_id"]

        # No self-referral
        if str(referrer_id) == user_id:
            raise HTTPException(status_code=400, detail="You cannot use your own referral code")

        # Check referrer hasn't hit cap
        if code_row["successful_referrals"] >= code_row["max_referrals"]:
            raise HTTPException(status_code=400, detail="This referral code has reached its limit")

        # Velocity check: max 3 referrals in last 24h for this referrer
        from datetime import timezone
        twenty_four_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent_res = supabase.table("referral_events")\
            .select("id")\
            .eq("referrer_user_id", referrer_id)\
            .gte("referred_at", twenty_four_hours_ago)\
            .execute()
        if len(recent_res.data) >= 3:
            # Flag the referrer's code
            supabase.table("referral_codes").update({"is_active": False}).eq("id", code_row["id"]).execute()
            raise HTTPException(status_code=429, detail="This referral code is temporarily unavailable")

        # Check for existing referral event (idempotency)
        existing_event = supabase.table("referral_events")\
            .select("id")\
            .eq("referred_user_id", user_id)\
            .execute()
        if existing_event.data:
            raise HTTPException(status_code=400, detail="You've already used a referral code")

        # Create pending referral event
        supabase.table("referral_events").insert({
            "referral_code_id": code_row["id"],
            "referrer_user_id": referrer_id,
            "referred_user_id": user_id,
            "status": "pending",
        }).execute()

        # Update user's referred_by_code
        supabase.table("users").update({"referred_by_code": code}).eq("id", user_id).execute()

        return {
            "success": True,
            "message": "Referral code applied! Complete your first meal to unlock rewards for both of you.",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[referral.apply] error: {e}")
        raise HTTPException(status_code=500, detail="Failed to apply referral code")
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat(referral): add GET /referral/my-code and POST /referral/apply-code endpoints"
```

---

## Task 7: Add Referral Stats Endpoint

**Files:**
- Modify: `app.py` (add after apply-code endpoint)

**Step 1: Add GET /referral/stats endpoint**

```python
@app.get("/referral/stats", summary="Get referral dashboard stats")
async def get_referral_stats(user: dict = Depends(get_current_user)):
    """Get detailed referral statistics for the authenticated user."""
    try:
        user_id = str(user["id"])

        # Get user's referral code
        code_res = supabase.table("referral_codes").select("*").eq("user_id", user_id).execute()
        if not code_res.data:
            return {
                "success": True,
                "has_code": False,
                "code": None,
                "successful_referrals": 0,
                "max_referrals": 12,
                "total_rewards_earned": {"free_weeks": 0, "free_months": 0},
                "referrals": [],
            }

        code_row = code_res.data[0]

        # Get referral events for this user as referrer
        events_res = supabase.table("referral_events")\
            .select("*")\
            .eq("referrer_user_id", user_id)\
            .order("referred_at", desc=True)\
            .execute()

        # Get rewards earned
        rewards_res = supabase.table("referral_rewards")\
            .select("*")\
            .eq("user_id", user_id)\
            .execute()

        free_weeks = sum(1 for r in rewards_res.data if r["reward_type"] == "free_week")
        free_months = sum(1 for r in rewards_res.data if r["reward_type"] == "free_month")

        # Build referral list with referred user names
        referrals = []
        for event in events_res.data:
            referred_user_res = supabase.table("users")\
                .select("username")\
                .eq("id", event["referred_user_id"])\
                .execute()
            referred_name = referred_user_res.data[0]["username"] if referred_user_res.data else "Unknown"
            referrals.append({
                "referred_user": referred_name,
                "status": event["status"],
                "referred_at": event["referred_at"],
                "bonus_granted": event["bonus_granted"],
            })

        return {
            "success": True,
            "has_code": True,
            "code": code_row["code"],
            "successful_referrals": code_row["successful_referrals"],
            "max_referrals": code_row["max_referrals"],
            "total_rewards_earned": {
                "free_weeks": free_weeks,
                "free_months": free_months,
            },
            "referrals": referrals,
        }
    except Exception as e:
        logger.error(f"[referral.stats] error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch referral stats")
```

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat(referral): add GET /referral/stats endpoint"
```

---

## Task 8: Add Activation Threshold Trigger

**Files:**
- Modify: `app.py` (add helper function + wire into existing endpoints)

**Step 1: Add the activation check helper**

Add this helper near the other referral helpers (after `grant_revenuecat_promo`):

```python
async def check_and_fulfill_referral(user_id: str):
    """
    Check if a user has a pending referral event and fulfill it.
    Called after a user's first meal plan generation or food log entry.
    Grants 1 free week of premium to both referrer and referred user.
    """
    try:
        # Find pending referral event for this user
        event_res = supabase.table("referral_events")\
            .select("*")\
            .eq("referred_user_id", user_id)\
            .eq("status", "pending")\
            .execute()

        if not event_res.data:
            return  # No pending referral

        event = event_res.data[0]
        referrer_id = event["referrer_user_id"]

        # Grant 1 free week to both users via RevenueCat
        referrer_result = await grant_revenuecat_promo(referrer_id, "weekly")
        referred_result = await grant_revenuecat_promo(user_id, "weekly")

        # Update referral event status
        supabase.table("referral_events").update({
            "status": "rewarded",
            "rewarded_at": datetime.now().isoformat(),
        }).eq("id", event["id"]).execute()

        # Increment referrer's successful_referrals
        supabase.table("referral_codes")\
            .update({"successful_referrals": event.get("referral_code_id", "")})\
            .eq("id", event["referral_code_id"])\
            .execute()
        # Use RPC or raw SQL for atomic increment. Simpler approach:
        code_res = supabase.table("referral_codes").select("successful_referrals").eq("id", event["referral_code_id"]).execute()
        if code_res.data:
            new_count = code_res.data[0]["successful_referrals"] + 1
            supabase.table("referral_codes").update({"successful_referrals": new_count}).eq("id", event["referral_code_id"]).execute()

        # Insert reward audit records
        for uid, rtype in [(referrer_id, "free_week"), (user_id, "free_week")]:
            supabase.table("referral_rewards").insert({
                "referral_event_id": event["id"],
                "user_id": uid,
                "reward_type": rtype,
                "granted_via": "revenuecat_promo",
                "duration_days": 7,
            }).execute()

        # Send push notifications
        referrer_token = await get_user_push_token(referrer_id)
        if referrer_token:
            await send_push_notification(
                referrer_token,
                "Your friend joined DormDish!",
                "You both earned a free week of premium",
                data={"type": "referral_reward"},
            )

        referred_token = await get_user_push_token(user_id)
        if referred_token:
            await send_push_notification(
                referred_token,
                "Welcome to DormDish Premium!",
                "Your free week of premium is now active",
                data={"type": "referral_reward"},
            )

        logger.info(f"[referral] fulfilled referral: referrer={referrer_id} referred={user_id}")
    except Exception as e:
        logger.error(f"[referral] check_and_fulfill error for user {user_id}: {e}")
```

**Step 2: Wire into `generate_meal_plan_route`**

In `app.py:3773` (`generate_meal_plan_route`), after the meal plan is successfully generated and saved (near the end of the function, before the return statement), add:

```python
        # Check referral activation threshold
        try:
            await check_and_fulfill_referral(str(authenticated_user["id"]))
        except Exception as e:
            logger.error(f"[referral] activation check failed: {e}")
```

**Step 3: Wire into `generate_single_meal_route`**

In `app.py:4263` (`generate_single_meal_route`), similarly add after successful generation:

```python
        # Check referral activation threshold
        try:
            await check_and_fulfill_referral(str(authenticated_user["id"]))
        except Exception as e:
            logger.error(f"[referral] activation check failed: {e}")
```

**Step 4: Wire into `add_food_log_entry`**

In `app.py:6003` (`add_food_log_entry`), after successful insert (around line 6032), add:

```python
        # Check referral activation threshold
        try:
            await check_and_fulfill_referral(str(user["id"]))
        except Exception as e:
            logger.error(f"[referral] activation check failed: {e}")
```

**Step 5: Commit**

```bash
git add app.py
git commit -m "feat(referral): add activation threshold trigger to meal plan and food log endpoints"
```

---

## Task 9: Add RevenueCat Webhook Endpoint

**Files:**
- Modify: `app.py` (add after referral stats endpoint)

**Step 1: Add the webhook endpoint**

```python
@app.post("/referral/webhook/revenuecat", summary="RevenueCat webhook for referral bonus")
async def revenuecat_webhook(request: Request):
    """Handle RevenueCat webhook events.
    Grants referrer a bonus free month when a referred user makes their first purchase.
    """
    try:
        # Validate webhook authorization
        auth_header = request.headers.get("Authorization")
        if REVENUECAT_WEBHOOK_AUTH_KEY and auth_header != f"Bearer {REVENUECAT_WEBHOOK_AUTH_KEY}":
            raise HTTPException(status_code=401, detail="Unauthorized")

        body = await request.json()
        event = body.get("event", {})
        event_type = event.get("type")

        # Only process initial purchases
        if event_type != "INITIAL_PURCHASE":
            return {"success": True, "message": f"Ignored event type: {event_type}"}

        app_user_id = event.get("app_user_id")
        if not app_user_id:
            return {"success": True, "message": "No app_user_id in event"}

        # Check if this user was referred and hasn't already triggered the bonus
        event_res = supabase.table("referral_events")\
            .select("*")\
            .eq("referred_user_id", app_user_id)\
            .eq("bonus_granted", False)\
            .execute()

        if not event_res.data:
            return {"success": True, "message": "No pending referral bonus for this user"}

        referral_event = event_res.data[0]
        referrer_id = referral_event["referrer_user_id"]

        # Grant referrer 1 free month
        promo_result = await grant_revenuecat_promo(referrer_id, "monthly")

        # Update referral event
        supabase.table("referral_events").update({
            "bonus_granted": True,
            "bonus_granted_at": datetime.now().isoformat(),
        }).eq("id", referral_event["id"]).execute()

        # Audit record
        supabase.table("referral_rewards").insert({
            "referral_event_id": referral_event["id"],
            "user_id": referrer_id,
            "reward_type": "free_month",
            "granted_via": "revenuecat_promo",
            "duration_days": 30,
        }).execute()

        # Notify referrer
        referrer_token = await get_user_push_token(referrer_id)
        if referrer_token:
            await send_push_notification(
                referrer_token,
                "You earned a free month!",
                "Your referral subscribed to premium. Enjoy your free month!",
                data={"type": "referral_bonus"},
            )

        logger.info(f"[referral.webhook] bonus granted to referrer={referrer_id} from referred={app_user_id}")
        return {"success": True, "message": "Bonus granted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[referral.webhook] error: {e}")
        return {"success": False, "error": str(e)}
```

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat(referral): add RevenueCat webhook for premium conversion bonus"
```

---

## Task 10: Frontend -- Capture `realUserStatus` in Apple Sign-In

**Files:**
- Modify: `dormdish-app-u/DormDish/Features/Auth/LoginView.swift:17-100`
- Modify: `dormdish-app-u/DormDish/Core/Networking/AuthService.swift:86-110`

**Step 1: Capture `realUserStatus` in `handleAppleSignIn`**

In `LoginView.swift`, inside `handleAppleSignIn` (line 20), after `if let appleIDCredential = authorization.credential as? ASAuthorizationAppleIDCredential {`, add:

```swift
                // Capture real user status for anti-fraud
                let realUserStatus = appleIDCredential.realUserStatus.rawValue  // 0, 1, or 2
```

Update the `PendingAppleCredential` creation (line 58) to include it:

```swift
                                pendingAppleCredential = PendingAppleCredential(
                                    idToken: idToken,
                                    email: response.email ?? email,
                                    firstName: response.givenName ?? firstName
                                        ?? UserDefaults.standard.string(forKey: "apple_cached_first_name"),
                                    lastName: response.familyName ?? lastName
                                        ?? UserDefaults.standard.string(forKey: "apple_cached_last_name"),
                                    appleSub: response.appleSub,
                                    realUserStatus: realUserStatus
                                )
```

Update the `appleLogin` call (line 47) to pass `realUserStatus`:

```swift
                        let response = try await AuthService.shared.appleLogin(
                            idToken: idToken,
                            firstName: firstName,
                            lastName: lastName,
                            realUserStatus: realUserStatus
                        )
```

**Step 2: Update `PendingAppleCredential` struct**

Find the `PendingAppleCredential` struct definition (in `LoginView.swift` or a shared models file) and add:

```swift
    let realUserStatus: Int?  // 0=unsupported, 1=unknown, 2=likelyReal
```

**Step 3: Update `AuthService.appleLogin`**

In `AuthService.swift:86`, update the function signature:

```swift
    func appleLogin(idToken: String, firstName: String?, lastName: String?, realUserStatus: Int? = nil) async throws -> AuthResponse {
```

Update the request model to include `realUserStatus`:

```swift
        let request = AppleLoginRequest(idToken: idToken, userData: userData, realUserStatus: realUserStatus)
```

Update `AppleLoginRequest` struct (find it in AuthService or APIModels) to add:

```swift
    let realUserStatus: Int?
```

**Step 4: Update `AuthService.appleRegisterWithOnboarding`**

In `AuthService.swift:160`, add `realUserStatus: Int? = nil` and `referralCode: String? = nil` parameters. Pass them through in the request body.

**Step 5: Commit**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-app-u
git add DormDish/Features/Auth/LoginView.swift DormDish/Core/Networking/AuthService.swift
git commit -m "feat(referral): capture and send realUserStatus in Apple Sign-In flow"
```

---

## Task 11: Frontend -- Add Referral Code Screen to Onboarding

**Files:**
- Modify: `dormdish-app-u/DormDish/Features/Onboarding/OnboardingStore.swift:8-32` (enum + properties)
- Modify: `dormdish-app-u/DormDish/Features/Onboarding/OnboardingCoordinator.swift:32-81`
- Create: `dormdish-app-u/DormDish/Features/Onboarding/Screens/ReferralCodeScreen.swift`

**Step 1: Add `referralCode` enum case to `OnboardingStep`**

In `OnboardingStore.swift:8-32`, insert `referralCode` after `attribution` (case 14). Renumber all subsequent cases:

```swift
enum OnboardingStep: Int, CaseIterable {
    case welcome = 0
    case birthday = 1
    case gender = 2
    case height = 3
    case weight = 4
    case comparisonMotivational = 5
    case primaryGoal = 6
    case goalWeight = 7
    case weightLossSpeed = 8
    case activityLevel = 9
    case realisticTarget = 10
    case accomplishments = 11
    case obstacles = 12
    case weightTransition = 13
    case attribution = 14
    case referralCode = 15       // NEW
    case dietaryAllergies = 16   // was 15
    case schoolSelection = 17   // was 16
    case appleHealth = 18       // was 17
    case notifications = 19     // was 18
    case nameInput = 20         // was 19
    case accountCreation = 21   // was 20
    case paywall = 22           // was 21
    case setupLoading = 23      // was 22
}
```

**Step 2: Add referral properties to `OnboardingStore`**

In `OnboardingStore.swift`, after the Attribution section (line 325), add:

```swift
    // MARK: - Referral
    @Published var referralCode: String = ""
    @Published var referralCodeApplied: Bool = false
```

**Step 3: Pass `referralCode` in `completeRegistration`**

In `OnboardingStore.swift:649-677`, update the `appleRegisterWithOnboarding` call to include `referralCode`:

Add after `notificationsEnabled: notificationsEnabled`:

```swift
                    referralCode: referralCodeApplied ? referralCode : nil
```

**Step 4: Add case to `OnboardingCoordinator`**

In `OnboardingCoordinator.swift:32-81`, after the `.attribution` case (line 63), add:

```swift
        case .referralCode:
            AnyView(ReferralCodeScreen())
```

**Step 5: Create `ReferralCodeScreen.swift`**

Create at `dormdish-app-u/DormDish/Features/Onboarding/Screens/ReferralCodeScreen.swift`:

```swift
import SwiftUI
import UIKit

/// Onboarding screen: "Have a referral code?"
/// Allows new users to enter a friend's referral code during signup.
struct ReferralCodeScreen: View {
    @EnvironmentObject var store: OnboardingStore
    @FocusState private var isTextFieldFocused: Bool

    var body: some View {
        ZStack {
            Theme.Colors.backgroundPrimary
                .ignoresSafeArea()

            VStack(spacing: 0) {
                OnboardingHeader(
                    currentStep: 15,
                    totalSteps: 19,
                    progress: store.progressPercentage,
                    onBack: {
                        triggerHaptic(.light)
                        store.previousStep()
                    }
                )

                Spacer()

                VStack(spacing: Theme.Spacing.xl) {
                    // Icon
                    ZStack {
                        Circle()
                            .fill(Theme.Colors.accentPrimary.opacity(0.15))
                            .frame(width: 80, height: 80)
                        Image(systemName: "gift.fill")
                            .font(.system(size: 36))
                            .foregroundStyle(Theme.Colors.accentPrimary)
                    }

                    // Title
                    VStack(spacing: Theme.Spacing.sm) {
                        Text("Have a referral code?")
                            .font(Theme.Typography.largeTitle)
                            .foregroundStyle(Theme.Colors.textPrimary)
                            .multilineTextAlignment(.center)

                        Text("Enter a friend's code and you'll both get a free week of premium")
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.textSecondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding(.horizontal, Theme.Spacing.lg)

                    // Code input
                    VStack(spacing: Theme.Spacing.md) {
                        TextField("Enter code", text: $store.referralCode)
                            .textInputAutocapitalization(.characters)
                            .font(.system(size: 24, weight: .bold, design: .monospaced))
                            .multilineTextAlignment(.center)
                            .padding()
                            .background(
                                RoundedRectangle(cornerRadius: Theme.Radius.lg, style: .continuous)
                                    .fill(Theme.Colors.backgroundSecondary)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: Theme.Radius.lg, style: .continuous)
                                    .stroke(
                                        store.referralCodeApplied ? Color.green : Theme.Colors.accentPrimary.opacity(0.3),
                                        lineWidth: store.referralCodeApplied ? 2 : 1
                                    )
                            )
                            .onChange(of: store.referralCode) { _, newValue in
                                // Limit to 6 characters
                                if newValue.count > 6 {
                                    store.referralCode = String(newValue.prefix(6))
                                }
                            }
                            .focused($isTextFieldFocused)
                            .padding(.horizontal, Theme.Spacing.xl)

                        if store.referralCodeApplied {
                            Label("Code applied!", systemImage: "checkmark.circle.fill")
                                .font(Theme.Typography.callout)
                                .foregroundStyle(.green)
                        }
                    }
                }

                Spacer()

                // Bottom buttons
                VStack(spacing: Theme.Spacing.md) {
                    if !store.referralCodeApplied && store.referralCode.count == 6 {
                        Button {
                            triggerHaptic(.medium)
                            store.referralCodeApplied = true
                            store.nextStep()
                        } label: {
                            Text("Apply & Continue")
                                .font(Theme.Typography.headline)
                                .foregroundStyle(.white)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, Theme.Spacing.md)
                                .background(
                                    RoundedRectangle(cornerRadius: Theme.Radius.lg, style: .continuous)
                                        .fill(Theme.Colors.accentPrimary)
                                )
                        }
                    }

                    Button {
                        triggerHaptic(.light)
                        store.referralCode = ""
                        store.referralCodeApplied = false
                        store.nextStep()
                    } label: {
                        Text("Skip")
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.textSecondary)
                    }
                }
                .padding(.horizontal, Theme.Spacing.lg)
                .padding(.bottom, Theme.Spacing.xxxl)
            }
        }
        .navigationBarHidden(true)
        .onAppear {
            isTextFieldFocused = true
        }
    }

    private func triggerHaptic(_ style: UIImpactFeedbackGenerator.FeedbackStyle) {
        let generator = UIImpactFeedbackGenerator(style: style)
        generator.impactOccurred()
    }
}

#Preview {
    ReferralCodeScreen()
        .environmentObject(OnboardingStore())
        .preferredColorScheme(.dark)
}
```

**Step 6: Commit**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-app-u
git add DormDish/Features/Onboarding/OnboardingStore.swift \
        DormDish/Features/Onboarding/OnboardingCoordinator.swift \
        DormDish/Features/Onboarding/Screens/ReferralCodeScreen.swift
git commit -m "feat(referral): add referral code screen to onboarding flow"
```

---

## Task 12: Frontend -- Add ReferralService

**Files:**
- Create: `dormdish-app-u/DormDish/Core/Networking/ReferralService.swift`

**Step 1: Create `ReferralService.swift`**

```swift
import Foundation

/// API models for referral endpoints
struct ReferralCodeResponse: Codable {
    let success: Bool
    let code: String?
    let hasCode: Bool
    let successfulReferrals: Int
    let maxReferrals: Int
    let message: String?

    enum CodingKeys: String, CodingKey {
        case success, code, message
        case hasCode = "has_code"
        case successfulReferrals = "successful_referrals"
        case maxReferrals = "max_referrals"
    }
}

struct ApplyCodeResponse: Codable {
    let success: Bool
    let message: String
}

struct ReferralStatsResponse: Codable {
    let success: Bool
    let hasCode: Bool
    let code: String?
    let successfulReferrals: Int
    let maxReferrals: Int
    let totalRewardsEarned: RewardsEarned
    let referrals: [ReferralEntry]

    enum CodingKeys: String, CodingKey {
        case success, code, referrals
        case hasCode = "has_code"
        case successfulReferrals = "successful_referrals"
        case maxReferrals = "max_referrals"
        case totalRewardsEarned = "total_rewards_earned"
    }
}

struct RewardsEarned: Codable {
    let freeWeeks: Int
    let freeMonths: Int

    enum CodingKeys: String, CodingKey {
        case freeWeeks = "free_weeks"
        case freeMonths = "free_months"
    }
}

struct ReferralEntry: Codable, Identifiable {
    var id: String { referredAt }
    let referredUser: String
    let status: String
    let referredAt: String
    let bonusGranted: Bool

    enum CodingKeys: String, CodingKey {
        case status
        case referredUser = "referred_user"
        case referredAt = "referred_at"
        case bonusGranted = "bonus_granted"
    }
}

private struct ApplyCodeRequest: Codable {
    let code: String
}

/// Service for referral-related API calls
actor ReferralService {
    static let shared = ReferralService()
    private let client = APIClient.shared

    func generateCode() async throws -> ReferralCodeResponse {
        return try await client.request(
            endpoint: "/referral/generate-code",
            method: .post,
            body: Optional<String>.none
        )
    }

    func getMyCode() async throws -> ReferralCodeResponse {
        return try await client.request(
            endpoint: "/referral/my-code",
            method: .get,
            body: Optional<String>.none
        )
    }

    func applyCode(_ code: String) async throws -> ApplyCodeResponse {
        let request = ApplyCodeRequest(code: code)
        return try await client.request(
            endpoint: "/referral/apply-code",
            method: .post,
            body: request
        )
    }

    func getStats() async throws -> ReferralStatsResponse {
        return try await client.request(
            endpoint: "/referral/stats",
            method: .get,
            body: Optional<String>.none
        )
    }
}
```

**Step 2: Commit**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-app-u
git add DormDish/Core/Networking/ReferralService.swift
git commit -m "feat(referral): add ReferralService API client"
```

---

## Task 13: Frontend -- Add ReferralView and Profile Integration

**Files:**
- Create: `dormdish-app-u/DormDish/Features/Profile/ReferralView.swift`
- Modify: `dormdish-app-u/DormDish/Features/Profile/ProfileView.swift:109-115`

**Step 1: Create `ReferralView.swift`**

```swift
import SwiftUI

struct ReferralView: View {
    @State private var stats: ReferralStatsResponse?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var copied = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            Theme.Colors.backgroundPrimary
                .ignoresSafeArea()

            ScrollView(showsIndicators: false) {
                VStack(spacing: Theme.Spacing.xl) {
                    // Header
                    headerSection

                    if isLoading {
                        ProgressView()
                            .padding(.top, Theme.Spacing.xxxl)
                    } else if let stats = stats, stats.hasCode, let code = stats.code {
                        // Code display
                        codeSection(code: code)

                        // Stats
                        statsSection(stats: stats)

                        // Referral list
                        if !stats.referrals.isEmpty {
                            referralListSection(referrals: stats.referrals)
                        }
                    } else if let error = errorMessage {
                        Text(error)
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.textSecondary)
                            .padding()
                    }
                }
                .padding(.horizontal, Theme.Spacing.md)
                .padding(.top, Theme.Spacing.md)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await loadStats()
        }
    }

    private var headerSection: some View {
        VStack(spacing: Theme.Spacing.sm) {
            ZStack {
                Circle()
                    .fill(Theme.Colors.accentPrimary.opacity(0.15))
                    .frame(width: 72, height: 72)
                Image(systemName: "gift.fill")
                    .font(.system(size: 32))
                    .foregroundStyle(Theme.Colors.accentPrimary)
            }

            Text("Invite Friends")
                .font(Theme.Typography.title)
                .foregroundStyle(Theme.Colors.textPrimary)

            Text("Share your code and you'll both get a free week of premium")
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.textSecondary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, Theme.Spacing.lg)
    }

    private func codeSection(code: String) -> some View {
        VStack(spacing: Theme.Spacing.md) {
            // Code display
            Text(code)
                .font(.system(size: 36, weight: .bold, design: .monospaced))
                .foregroundStyle(Theme.Colors.textPrimary)
                .padding(.vertical, Theme.Spacing.lg)
                .padding(.horizontal, Theme.Spacing.xl)
                .background(
                    RoundedRectangle(cornerRadius: Theme.Radius.lg, style: .continuous)
                        .fill(Theme.Colors.backgroundSecondary)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.Radius.lg, style: .continuous)
                        .stroke(Theme.Colors.accentPrimary.opacity(0.3), lineWidth: 1)
                )

            // Action buttons
            HStack(spacing: Theme.Spacing.md) {
                Button {
                    UIPasteboard.general.string = code
                    copied = true
                    let generator = UINotificationFeedbackGenerator()
                    generator.notificationOccurred(.success)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                        copied = false
                    }
                } label: {
                    Label(copied ? "Copied!" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(Theme.Typography.callout)
                        .foregroundStyle(Theme.Colors.accentPrimary)
                        .padding(.horizontal, Theme.Spacing.lg)
                        .padding(.vertical, Theme.Spacing.sm)
                        .background(
                            RoundedRectangle(cornerRadius: Theme.Radius.md, style: .continuous)
                                .fill(Theme.Colors.accentPrimary.opacity(0.1))
                        )
                }

                ShareLink(
                    item: "Try DormDish with my code \(code) and we both get free premium! Download: https://apps.apple.com/app/dormdish/id1234567890"
                ) {
                    Label("Share", systemImage: "square.and.arrow.up")
                        .font(Theme.Typography.callout)
                        .foregroundStyle(.white)
                        .padding(.horizontal, Theme.Spacing.lg)
                        .padding(.vertical, Theme.Spacing.sm)
                        .background(
                            RoundedRectangle(cornerRadius: Theme.Radius.md, style: .continuous)
                                .fill(Theme.Colors.accentPrimary)
                        )
                }
            }
        }
    }

    private func statsSection(stats: ReferralStatsResponse) -> some View {
        VStack(spacing: Theme.Spacing.sm) {
            HStack {
                Text("Your Referrals")
                    .font(Theme.Typography.headline)
                    .foregroundStyle(Theme.Colors.textPrimary)
                Spacer()
            }

            HStack(spacing: Theme.Spacing.lg) {
                statCard(
                    value: "\(stats.successfulReferrals)/\(stats.maxReferrals)",
                    label: "Referrals",
                    icon: "person.2.fill"
                )
                statCard(
                    value: "\(stats.totalRewardsEarned.freeWeeks)",
                    label: "Free Weeks",
                    icon: "calendar"
                )
                statCard(
                    value: "\(stats.totalRewardsEarned.freeMonths)",
                    label: "Free Months",
                    icon: "star.fill"
                )
            }
        }
        .padding(Theme.Spacing.md)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.lg, style: .continuous)
                .fill(Theme.Colors.backgroundSecondary)
        )
    }

    private func statCard(value: String, label: String, icon: String) -> some View {
        VStack(spacing: Theme.Spacing.xs) {
            Image(systemName: icon)
                .font(.system(size: 18))
                .foregroundStyle(Theme.Colors.accentPrimary)
            Text(value)
                .font(Theme.Typography.headline)
                .foregroundStyle(Theme.Colors.textPrimary)
            Text(label)
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.textSecondary)
        }
        .frame(maxWidth: .infinity)
    }

    private func referralListSection(referrals: [ReferralEntry]) -> some View {
        VStack(spacing: Theme.Spacing.sm) {
            HStack {
                Text("History")
                    .font(Theme.Typography.headline)
                    .foregroundStyle(Theme.Colors.textPrimary)
                Spacer()
            }

            ForEach(referrals) { referral in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(referral.referredUser)
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.textPrimary)
                    }
                    Spacer()
                    statusBadge(status: referral.status, bonusGranted: referral.bonusGranted)
                }
                .padding(Theme.Spacing.md)
                .background(
                    RoundedRectangle(cornerRadius: Theme.Radius.md, style: .continuous)
                        .fill(Theme.Colors.backgroundSecondary)
                )
            }
        }
    }

    private func statusBadge(status: String, bonusGranted: Bool) -> some View {
        let (text, color): (String, Color) = {
            if bonusGranted {
                return ("Subscribed", .green)
            }
            switch status {
            case "rewarded": return ("Active", Theme.Colors.accentPrimary)
            case "pending": return ("Pending", .orange)
            default: return (status.capitalized, Theme.Colors.textSecondary)
            }
        }()

        return Text(text)
            .font(Theme.Typography.caption)
            .foregroundStyle(color)
            .padding(.horizontal, Theme.Spacing.sm)
            .padding(.vertical, 4)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.sm, style: .continuous)
                    .fill(color.opacity(0.15))
            )
    }

    private func loadStats() async {
        isLoading = true
        do {
            // Generate code if needed, then get stats
            let _ = try await ReferralService.shared.generateCode()
            stats = try await ReferralService.shared.getStats()
        } catch {
            errorMessage = "Failed to load referral data"
        }
        isLoading = false
    }
}

#Preview {
    NavigationStack {
        ReferralView()
    }
    .preferredColorScheme(.dark)
}
```

**Step 2: Add referral section to ProfileView**

In `ProfileView.swift`, between `followUsSection` (line 110-111) and `accountSection` (line 113-114), add:

```swift
                // Invite Friends section
                NavigationLink(destination: ReferralView()) {
                    SettingsSection(
                        title: "Invite Friends",
                        items: [
                            SettingsItem(
                                icon: "gift.fill",
                                iconColor: Theme.Colors.accentPrimary,
                                title: "Invite Friends",
                                subtitle: "Share your code & earn free premium",
                                type: .navigation
                            )
                        ]
                    )
                }
                .buttonStyle(PlainButtonStyle())
                .padding(.horizontal, Theme.Spacing.md)
```

**Step 3: Commit**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-app-u
git add DormDish/Features/Profile/ReferralView.swift DormDish/Features/Profile/ProfileView.swift
git commit -m "feat(referral): add ReferralView and integrate into Profile tab"
```

---

## Task 14: Add Environment Variables

**Files:**
- Modify: `dormdish-backend/.env`

**Step 1: Add RevenueCat keys to `.env`**

```
REVENUECAT_SECRET_API_KEY=sk_REPLACE_WITH_ACTUAL_KEY
REVENUECAT_WEBHOOK_AUTH_KEY=REPLACE_WITH_ACTUAL_KEY
```

**Step 2: Configure RevenueCat webhook**

In RevenueCat Dashboard:
1. Go to Project Settings > Integrations > Webhooks
2. Add webhook URL: `https://your-backend-url.vercel.app/referral/webhook/revenuecat`
3. Set Authorization Header to: `Bearer REPLACE_WITH_ACTUAL_KEY` (matching `REVENUECAT_WEBHOOK_AUTH_KEY`)
4. Enable events: `INITIAL_PURCHASE`

**Step 3: Do NOT commit `.env`** -- it contains secrets.

---

## Task 15: End-to-End Verification

**Step 1: Verify backend starts without errors**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend
python -c "from app import app; print('Backend OK')"
```

Expected: `Backend OK`

**Step 2: Verify Supabase tables exist**

Run in Supabase SQL Editor:
```sql
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'referral_codes'
ORDER BY ordinal_position;
```

Expected: 7 columns (id, user_id, code, is_active, successful_referrals, max_referrals, created_at)

**Step 3: Verify frontend builds**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-app-u
xcodebuild -scheme DormDish -destination 'platform=iOS Simulator,name=iPhone 16' build 2>&1 | tail -5
```

Expected: `BUILD SUCCEEDED`

**Step 4: Manual test checklist**

- [ ] Generate a referral code via `POST /referral/generate-code`
- [ ] Fetch code via `GET /referral/my-code`
- [ ] Apply code via `POST /referral/apply-code` (with a different user)
- [ ] Verify referral event created in `referral_events` table with status `pending`
- [ ] Generate a meal plan with the referred user
- [ ] Verify referral event transitions to `rewarded`
- [ ] Verify `referral_rewards` entries created for both users
- [ ] Check `GET /referral/stats` returns correct data
- [ ] Test onboarding flow with referral code screen
- [ ] Test Profile > Invite Friends shows ReferralView with code and share sheet

**Step 5: Final commit**

```bash
cd /Users/raghavmehta/Downloads/DormDish/dormdish-backend
git add -A
git commit -m "feat(referral): complete referral program implementation"
```
