# Stripe Pre-Order — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Stripe Checkout, webhook fulfillment, and deferred activation endpoints to the FastAPI backend so users can pre-purchase DormDish Pro for next semester.

**Architecture:** 3 new endpoints (create checkout session, Stripe webhook, admin activation), a new `prepaid_subscriptions` Supabase table, Apple JWT multi-audience support, and a GitHub Actions cron for triggering activation. RevenueCat promo grants handle actual subscription fulfillment via the existing `grant_revenuecat_promo()` function.

**Tech Stack:** FastAPI, Stripe Python SDK, Supabase, RevenueCat, GitHub Actions

**Spec:** `dormdish-campus-plan/docs/superpowers/specs/2026-04-05-stripe-preorder-design.md`

**Parallel track:** The website changes are in a separate plan at `dormdish-campus-plan/docs/superpowers/plans/2026-04-05-stripe-preorder-website.md`. Both tracks can be implemented in parallel. Run the end-to-end smoke test (Task 8 below) after both tracks are complete.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `migrations/add_prepaid_subscriptions.sql` | Create | SQL migration for `prepaid_subscriptions` table |
| `app.py` | Modify | Add 3 new endpoints, Stripe SDK integration, update Apple JWT audience validation |
| `requirements.txt` | Modify | Add `stripe` dependency |
| `.env` | Modify | Add `STRIPE_MONTHLY_PRICE_ID`, `STRIPE_YEARLY_PRICE_ID`, `ACTIVATION_SECRET`, `APPLE_WEB_SERVICE_ID` |
| `.github/workflows/activate-prepaid.yml` | Create | Daily cron to trigger activation |

---

## Task 1: Create `prepaid_subscriptions` Supabase Table

**Files:**
- Create: `migrations/add_prepaid_subscriptions.sql`

- [ ] **Step 1: Write the migration SQL**

Create the file `migrations/add_prepaid_subscriptions.sql`:

```sql
-- Pre-paid subscription purchases via Stripe (deferred activation)
CREATE TABLE IF NOT EXISTS prepaid_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    stripe_session_id TEXT NOT NULL UNIQUE,
    stripe_payment_intent_id TEXT,
    plan_type TEXT NOT NULL CHECK (plan_type IN ('monthly', 'yearly')),
    amount_cents INTEGER NOT NULL,
    revenuecat_duration TEXT NOT NULL CHECK (revenuecat_duration IN ('monthly', 'yearly')),
    activation_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'activated', 'refunded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ
);

CREATE INDEX idx_prepaid_subscriptions_status ON prepaid_subscriptions(status);
CREATE INDEX idx_prepaid_subscriptions_user_id ON prepaid_subscriptions(user_id);
CREATE INDEX idx_prepaid_subscriptions_activation ON prepaid_subscriptions(activation_date) WHERE status = 'pending';
```

- [ ] **Step 2: Run the migration against Supabase**

Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New Query → paste and run). Verify the table exists by checking Table Editor.

- [ ] **Step 3: Commit**

```bash
git add migrations/add_prepaid_subscriptions.sql
git commit -m "feat: add prepaid_subscriptions table migration"
```

---

## Task 2: Add Stripe SDK to Backend

**Files:**
- Modify: `requirements.txt`
- Modify: `app.py` (top-level imports and config, around lines 1-130)
- Modify: `.env`

- [ ] **Step 1: Add stripe to requirements.txt**

Add `stripe` to the dependencies in `requirements.txt`.

- [ ] **Step 2: Install the dependency**

```bash
pip install stripe
```

- [ ] **Step 3: Add Stripe imports and config to app.py**

At the top of `app.py`, after the existing imports (around line 30), add:

```python
import stripe
```

After the existing environment variable reads (around line 125, near `REVENUECAT_SECRET_API_KEY`), add:

```python
# Stripe configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_MONTHLY_PRICE_ID = os.getenv("STRIPE_MONTHLY_PRICE_ID", "")
STRIPE_YEARLY_PRICE_ID = os.getenv("STRIPE_YEARLY_PRICE_ID", "")
ACTIVATION_SECRET = os.getenv("ACTIVATION_SECRET", "")
ACTIVATION_DATE = "2025-08-25"  # Semester start date for deferred activation
```

- [ ] **Step 4: Add env vars to .env**

Add to `.env`:

```
STRIPE_MONTHLY_PRICE_ID=price_REPLACE_AFTER_STRIPE_SETUP
STRIPE_YEARLY_PRICE_ID=price_REPLACE_AFTER_STRIPE_SETUP
ACTIVATION_SECRET=REPLACE_WITH_SECURE_RANDOM_STRING
```

Note: `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` already exist in `.env`.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt app.py .env
git commit -m "feat: add Stripe SDK and config for prepaid subscriptions"
```

---

## Task 3: Update Apple JWT Verification to Accept Web Service ID

**Files:**
- Modify: `app.py:1473-1522` (`_verify_apple_id_token` function)
- Modify: `.env`

The current implementation only accepts a single `APPLE_CLIENT_ID` as the audience. Apple Sign In on the web uses a separate **Service ID** which will have a different value. We need `jwt.decode` to accept both.

- [ ] **Step 1: Update the audience parameter to accept a list**

In `app.py`, modify the `_verify_apple_id_token` function. Replace the audience setup:

```python
    # Get the expected audience (your app's bundle ID or service ID)
    apple_client_id = os.getenv("APPLE_CLIENT_ID")
    if not apple_client_id:
        raise HTTPException(status_code=500, detail="APPLE_CLIENT_ID not configured")
```

With:

```python
    # Accept both iOS app bundle ID and web Service ID as valid audiences
    apple_client_id = os.getenv("APPLE_CLIENT_ID")
    apple_web_service_id = os.getenv("APPLE_WEB_SERVICE_ID")
    if not apple_client_id:
        raise HTTPException(status_code=500, detail="APPLE_CLIENT_ID not configured")

    apple_audiences = [apple_client_id]
    if apple_web_service_id:
        apple_audiences.append(apple_web_service_id)
```

Then update the `jwt.decode` call inside `_sync_verify` to use the list:

```python
            decoded = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=apple_audiences,
                issuer=APPLE_ISSUER,
            )
```

PyJWT's `audience` parameter accepts a list — the token is valid if its `aud` claim matches any value in the list.

- [ ] **Step 2: Add the env var placeholder**

Add to `.env`:

```
APPLE_WEB_SERVICE_ID=REPLACE_AFTER_APPLE_DEVELOPER_SETUP
```

- [ ] **Step 3: Commit**

```bash
git add app.py .env
git commit -m "feat: accept Apple web Service ID in JWT verification"
```

---

## Task 4: Add `POST /checkout/create-session` Endpoint

**Files:**
- Modify: `app.py` (add Pydantic model ~line 1356, add endpoint ~line 2400)

- [ ] **Step 1: Add the Pydantic request model**

Add near the other Pydantic models (around line 1356 in `app.py`):

```python
class CreateCheckoutSessionRequest(BaseModel):
    plan_type: str  # "monthly" or "yearly"
```

- [ ] **Step 2: Add the endpoint**

Add after the existing auth endpoints (around line 2400):

```python
@app.post('/checkout/create-session')
async def create_checkout_session(payload: CreateCheckoutSessionRequest, user=Depends(get_current_user)):
    """Create a Stripe Checkout Session for a pre-order purchase."""
    if payload.plan_type not in ("monthly", "yearly"):
        raise HTTPException(status_code=400, detail="plan_type must be 'monthly' or 'yearly'")

    price_id = STRIPE_MONTHLY_PRICE_ID if payload.plan_type == "monthly" else STRIPE_YEARLY_PRICE_ID
    if not price_id:
        raise HTTPException(status_code=500, detail="Stripe price not configured")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"user_id": user["id"], "plan_type": payload.plan_type},
            success_url="https://dormdish.ai/checkout/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://dormdish.ai",
        )
        return {"success": True, "checkout_url": session.url}
    except stripe.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")
```

- [ ] **Step 3: Verify it compiles**

```bash
python -c "import app; print('OK')"
```

Expected: `OK` (no import errors)

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add POST /checkout/create-session endpoint"
```

---

## Task 5: Add `POST /webhook/stripe` Endpoint

**Files:**
- Modify: `app.py` (add after the create-session endpoint)

- [ ] **Step 1: Add the webhook endpoint**

Add after the `/checkout/create-session` endpoint:

```python
@app.post('/webhook/stripe')
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events for completed checkout sessions."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id")
        plan_type = metadata.get("plan_type")

        if not user_id or not plan_type:
            print(f"[stripe webhook] missing metadata: user_id={user_id} plan_type={plan_type}")
            return {"received": True}

        amount_map = {"monthly": 199, "yearly": 1299}
        amount_cents = amount_map.get(plan_type, 0)

        try:
            supabase.table("prepaid_subscriptions").insert({
                "user_id": user_id,
                "stripe_session_id": session["id"],
                "stripe_payment_intent_id": session.get("payment_intent"),
                "plan_type": plan_type,
                "amount_cents": amount_cents,
                "revenuecat_duration": plan_type,
                "activation_date": ACTIVATION_DATE,
                "status": "pending",
            }).execute()
            print(f"[stripe webhook] prepaid subscription created for user_id={user_id} plan={plan_type}")
        except Exception as e:
            # Unique constraint on stripe_session_id handles duplicate webhooks
            print(f"[stripe webhook] insert error (likely duplicate): {e}")

    return {"received": True}
```

- [ ] **Step 2: Verify it compiles**

```bash
python -c "import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add POST /webhook/stripe for checkout fulfillment"
```

---

## Task 6: Add `POST /admin/activate-prepaid` Endpoint

**Files:**
- Modify: `app.py` (add after the webhook endpoint)

- [ ] **Step 1: Add the activation endpoint**

```python
@app.post('/admin/activate-prepaid')
async def activate_prepaid(authorization: Optional[str] = Header(None)):
    """Activate all pending prepaid subscriptions whose activation date has arrived.

    Protected by ACTIVATION_SECRET — called by GitHub Actions cron.
    """
    token = get_bearer_token(authorization)
    if not token or token != ACTIVATION_SECRET:
        raise HTTPException(status_code=403, detail="Invalid activation secret")

    today = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")

    res = supabase.table("prepaid_subscriptions")\
        .select("*")\
        .eq("status", "pending")\
        .lte("activation_date", today)\
        .execute()

    if not res.data:
        return {"success": True, "activated_count": 0, "message": "No pending subscriptions to activate"}

    activated = 0
    errors = []
    for row in res.data:
        result = await grant_revenuecat_promo(row["user_id"], row["revenuecat_duration"])
        if result.get("success"):
            supabase.table("prepaid_subscriptions")\
                .update({"status": "activated", "activated_at": datetime.now(ZoneInfo("UTC")).isoformat()})\
                .eq("id", row["id"])\
                .execute()
            activated += 1
            print(f"[activate-prepaid] activated user_id={row['user_id']} plan={row['plan_type']}")
        else:
            errors.append({"user_id": row["user_id"], "error": result.get("error")})
            print(f"[activate-prepaid] FAILED user_id={row['user_id']}: {result.get('error')}")

    return {
        "success": True,
        "activated_count": activated,
        "failed_count": len(errors),
        "errors": errors,
    }
```

- [ ] **Step 2: Verify it compiles**

```bash
python -c "import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add POST /admin/activate-prepaid endpoint"
```

---

## Task 7: Add GitHub Actions Activation Workflow

**Files:**
- Create: `.github/workflows/activate-prepaid.yml`

- [ ] **Step 1: Create the workflow file**

Create `.github/workflows/activate-prepaid.yml`:

```yaml
name: Activate prepaid subscriptions

on:
  schedule:
    - cron: '0 7 * * *'  # every day at 07:00 UTC
  workflow_dispatch: {}

jobs:
  activate:
    runs-on: ubuntu-latest
    environment: production
    steps:
      - name: Trigger activation
        env:
          BACKEND_URL: ${{ secrets.BACKEND_URL }}
          ACTIVATION_SECRET: ${{ secrets.ACTIVATION_SECRET }}
        run: |
          response=$(curl -s -w "\n%{http_code}" -X POST \
            "${BACKEND_URL}/admin/activate-prepaid" \
            -H "Authorization: Bearer ${ACTIVATION_SECRET}" \
            -H "Content-Type: application/json")

          http_code=$(echo "$response" | tail -1)
          body=$(echo "$response" | head -n -1)

          echo "Status: $http_code"
          echo "Response: $body"

          if [ "$http_code" -ne 200 ]; then
            echo "::error::Activation failed with status $http_code"
            exit 1
          fi
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/activate-prepaid.yml
git commit -m "feat: add daily cron workflow for prepaid activation"
```

Note: After pushing, add `BACKEND_URL` and `ACTIVATION_SECRET` as GitHub Actions secrets in the repo settings (Settings → Secrets and variables → Actions).

---

## Task 8: End-to-End Smoke Test

**Depends on:** Website track being complete as well.

- [ ] **Step 1: Set up Stripe products**

In the Stripe Dashboard (test mode):
1. Create product "DormDish Pro — Pre-order"
2. Add one-time price: $1.99 → copy price ID → set as `STRIPE_MONTHLY_PRICE_ID` in `.env`
3. Add one-time price: $12.99 → copy price ID → set as `STRIPE_YEARLY_PRICE_ID` in `.env`
4. Add webhook endpoint: `<your-backend-url>/webhook/stripe` → select `checkout.session.completed` event → copy signing secret → verify it matches `STRIPE_WEBHOOK_SECRET` in `.env`

- [ ] **Step 2: Set up Apple Sign In for Web**

In Apple Developer portal:
1. Create a Service ID (Identifiers → Service IDs → Register)
2. Enable "Sign in with Apple" → configure domain (`dormdish.ai`) and return URL (`https://dormdish.ai`)
3. Copy the Service ID identifier → set as `APPLE_WEB_SERVICE_ID` in backend `.env` and `NEXT_PUBLIC_APPLE_CLIENT_ID` in website `.env.local`

- [ ] **Step 3: Test locally with Stripe CLI**

```bash
# Terminal 1: Run backend
uvicorn app:app --reload --port 8000

# Terminal 2: Forward Stripe webhooks
stripe listen --forward-to localhost:8000/webhook/stripe

# Terminal 3: Run website
cd ../dormdish-campus-plan && npm run dev
```

- [ ] **Step 4: Walk through the flow**

1. Open `http://localhost:3000` → find pre-order section
2. Click "Get Started" on a plan → Apple Sign In modal appears
3. Sign in with Apple (requires HTTPS or Apple Developer test account)
4. Redirected to Stripe Checkout → use test card `4242 4242 4242 4242`
5. Redirected to success page → verify confirmation message
6. Check Supabase `prepaid_subscriptions` table → verify row with `status = 'pending'`
7. Manually call activation: `curl -X POST localhost:8000/admin/activate-prepaid -H "Authorization: Bearer YOUR_ACTIVATION_SECRET"`
8. Check `prepaid_subscriptions` table → verify `status = 'activated'`
9. Check RevenueCat Dashboard → verify user has `student_plan` entitlement
