# DormDish Referral Program Design

**Date:** 2026-02-27
**Status:** Approved
**Model:** Approach A -- "Anyone Refers, Reward on Signup"

---

## Overview

A double-sided referral program where any user (free or premium) can invite friends. Both parties receive 1 free week of premium on signup. Referrers earn a bonus free month when the referred user converts to a paid premium subscription. Apple Sign-In (the only auth method) provides natural anti-fraud protection.

## Goals

- **Primary:** Maximize user growth (virality)
- **Secondary:** Drive premium conversions through try-before-you-buy exposure

---

## User Flow

### Referrer Side

1. User taps "Invite Friends" in the Profile tab
2. Backend returns their unique 6-character code (e.g., `DISH7K`), generating one if it doesn't exist
3. User taps "Share" which opens a native share sheet with a pre-filled message:
   > "Try DormDish with my code DISH7K and we both get free premium!"
4. When someone signs up with their code:
   - Referrer receives push notification: "Your friend joined! You both got a free week of premium"
   - Free week granted via RevenueCat promotional entitlement
5. If the referred user later subscribes to premium:
   - Referrer gets a bonus free month
   - Push notification: "Your referral subscribed! You earned a free month of premium"

### Referred User Side

1. Downloads DormDish, signs in with Apple
2. During onboarding (after Attribution screen, step 13), sees "Have a referral code?" screen
3. Enters the code (or skips)
4. Code is validated server-side during account creation
5. After meeting activation threshold (1 meal log or meal plan generation), both parties receive 1 free week of premium
6. Nudge after free week expires: "Loved your free week? Subscribe to keep premium features"

### Constraints

- A user can only use ONE referral code (at onboarding only, no retroactive entry)
- A referrer can earn up to 12 successful referrals per year
- Referral code is permanent and reusable (same code for life)
- Code is idempotent -- calling generate multiple times returns the same code

---

## Data Model

### New Supabase Tables

#### `referral_codes`

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | UUID (PK) | `gen_random_uuid()` | |
| `user_id` | UUID (FK -> users) | | Owner of this code |
| `code` | VARCHAR(6), UNIQUE | | Shareable code (e.g., `DISH7K`) |
| `is_active` | BOOLEAN | `true` | Deactivated on abuse |
| `successful_referrals` | INT | `0` | Running count |
| `max_referrals` | INT | `12` | Annual cap |
| `created_at` | TIMESTAMPTZ | `now()` | |

#### `referral_events`

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | UUID (PK) | `gen_random_uuid()` | |
| `referral_code_id` | UUID (FK -> referral_codes) | | Which code was used |
| `referrer_user_id` | UUID (FK -> users) | | Who referred |
| `referred_user_id` | UUID (FK -> users) | | Who signed up |
| `status` | TEXT | `pending` | `pending` -> `rewarded` or `expired` |
| `referred_at` | TIMESTAMPTZ | `now()` | When signup happened |
| `rewarded_at` | TIMESTAMPTZ | null | When free week was granted |
| `bonus_granted` | BOOLEAN | `false` | Premium conversion bonus given |
| `bonus_granted_at` | TIMESTAMPTZ | null | When bonus month was granted |
| `flagged` | BOOLEAN | `false` | Suspicious activity flag |
| `flag_reason` | TEXT | null | Why flagged |

#### `referral_rewards` (audit ledger)

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | UUID (PK) | `gen_random_uuid()` | |
| `referral_event_id` | UUID (FK -> referral_events) | | Which referral |
| `user_id` | UUID (FK -> users) | | Reward recipient |
| `reward_type` | TEXT | | `free_week` or `free_month` |
| `granted_via` | TEXT | | `revenuecat_promo` |
| `duration_days` | INT | | 7 or 30 |
| `granted_at` | TIMESTAMPTZ | `now()` | |

### Modification to `users` Table

Add column: `referred_by_code` (VARCHAR, nullable) -- the referral code used at signup.

---

## Anti-Abuse System

### Layer 1: Apple Sign-In (built-in)

- Every Apple ID requires 2FA and a real device
- `realUserStatus` field (from `ASAuthorizationAppleIDCredential`) classifies users as `likelyReal`, `unknown`, or `unsupported`
- **Currently not captured** -- must be added to both frontend (LoginView.swift) and backend (apple auth endpoints)
- Only `likelyReal` users can generate referral codes or count as valid referrals
- `apple_sub` is immutable per Apple ID per developer team -- prevents duplicate accounts

### Layer 2: Server-side rules

- **Annual cap:** 12 successful referrals per user per year
- **Velocity limit:** Max 3 referrals per 24-hour window; flag if exceeded
- **Self-referral block:** Referrer and referred user cannot share the same `apple_sub`
- **One code per account:** A user can only apply one referral code, only during onboarding

### Layer 3: Activation threshold

- Referred user must complete at least 1 meal log or meal plan generation before the referral "counts"
- Until threshold is met, referral event stays in `pending` status
- Prevents signups that never open the app

### What we intentionally skip

- DeviceCheck/App Attest -- Apple Sign-In already covers device-level fraud
- IP clustering -- unreliable on college campus shared WiFi
- Manual review queues -- not needed at current scale

---

## Reward Mechanics

### RevenueCat Promotional Entitlements

Rewards are granted via RevenueCat's server API:

```
POST https://api.revenuecat.com/v1/subscribers/{app_user_id}/entitlements/student_plan/promotional
```

Body: `{ "duration": "weekly" }` or `{ "duration": "monthly" }`

Headers: `Authorization: Bearer {REVENUECAT_SECRET_API_KEY}`

### Reward Schedule

| Event | Referrer gets | Referred user gets |
|---|---|---|
| Referred user signs up + meets activation threshold | 1 free week of `student_plan` | 1 free week of `student_plan` |
| Referred user subscribes to premium | 1 free month of `student_plan` | Nothing extra (already paying) |

### Behavior

- If referrer is already premium, the promotional entitlement stacks alongside their subscription
- If referrer is free tier, they get temporary premium access for reward duration, then revert
- Rewards appear as `rc_promo` in RevenueCat dashboard (distinct from organic subscriptions)
- The bonus month is triggered by a RevenueCat `INITIAL_PURCHASE` webhook

---

## Backend API Endpoints

All endpoints added to `app.py`.

### `POST /referral/generate-code`

**Auth:** Required (session token)

**Logic:**
1. Verify user's `real_user_status == likelyReal`
2. Check if user already has a code in `referral_codes`
3. If yes, return existing code
4. If no, generate unique 6-char alphanumeric code, insert into `referral_codes`, return it

**Response:** `{ "code": "DISH7K", "successful_referrals": 0, "max_referrals": 12 }`

### `GET /referral/my-code`

**Auth:** Required

**Logic:** Fetch user's code and stats.

**Response:** `{ "code": "DISH7K", "successful_referrals": 3, "max_referrals": 12, "has_code": true }`

### `POST /referral/apply-code`

**Auth:** Required

**Input:** `{ "code": "DISH7K" }`

**Logic:**
1. Validate code exists and `is_active`
2. Verify referrer hasn't hit `max_referrals`
3. Verify user hasn't already used a referral code (`referred_by_code` is null)
4. Verify user isn't the code owner (no self-referral)
5. Check velocity: referrer hasn't had > 3 referrals in last 24h
6. Create `pending` referral event
7. Set `referred_by_code` on user record

**Response:** `{ "success": true, "message": "Referral code applied! Complete your first meal to unlock rewards." }`

### `GET /referral/stats`

**Auth:** Required

**Response:**
```json
{
  "code": "DISH7K",
  "successful_referrals": 3,
  "max_referrals": 12,
  "total_rewards_earned": {
    "free_weeks": 3,
    "free_months": 1
  },
  "referrals": [
    {
      "referred_user": "Jane",
      "status": "rewarded",
      "referred_at": "2026-02-20T...",
      "bonus_granted": true
    }
  ]
}
```

### `POST /referral/webhook/revenuecat`

**Auth:** RevenueCat webhook signature validation

**Logic:**
1. Validate webhook signature
2. On `INITIAL_PURCHASE` event, extract `app_user_id`
3. Look up user, check for `referral_event` where `bonus_granted = false`
4. If found, call RevenueCat promo API to grant referrer 1 free month
5. Update referral event: `bonus_granted = true`, `bonus_granted_at = now()`
6. Insert into `referral_rewards` audit ledger

### Activation Threshold Trigger

Not a separate endpoint. Added as a check in existing endpoints:

- `POST /generate-meal-plan` (and `/generate-meal-plan-stream`)
- `POST /generate-meal` (and `/generate-meal-stream`)
- `POST /food-log/add`

After a user completes one of these actions for the first time, check:
1. Does user have a `pending` referral event?
2. If yes, transition to `rewarded`
3. Call RevenueCat promo API for both referrer and referred user (1 week each)
4. Increment referrer's `successful_referrals`
5. Insert 2 records into `referral_rewards`
6. Send push notifications to both users

---

## Frontend Implementation (SwiftUI - dormdish-app-u)

### 1. Capture `realUserStatus` in Apple Sign-In

**File:** `DormDish/Features/Auth/LoginView.swift`

In `handleAppleSignIn()`, add:

```swift
let realUserStatus = appleIDCredential.realUserStatus
// .likelyReal = 2, .unknown = 1, .unsupported = 0
```

Pass this value to the backend in both `/auth/apple/login` and `/auth/apple/register` requests. Store it on the `users` table as `real_user_status`.

### 2. Add Referral Code Screen to Onboarding

**New file:** `DormDish/Features/Onboarding/Screens/ReferralCodeScreen.swift`

**Position:** After AttributionScreen (step 13), before DietaryAllergiesScreen (step 14). Update `OnboardingCoordinator.swift` to insert this step.

**UI:**
- Header: "Have a referral code?"
- Subtext: "Enter a friend's code and you'll both get a free week of premium"
- Text field for 6-character code input (auto-capitalize, max length 6)
- "Apply" button
- "Skip" link at bottom
- Validation feedback (green checkmark or red error)

**State:** Add to `OnboardingStore.swift`:
```swift
@Published var referralCode: String = ""
@Published var referralCodeApplied: Bool = false
```

Pass `referralCode` to the registration API call. The backend applies it during account creation.

### 3. Add Referral Section to Profile

**File:** `DormDish/Features/Profile/ProfileView.swift`

Add a new section between "Preferences" and "Notifications":

```
Invite Friends Section
├── "Invite Friends" row (gift icon, teal)
│   └── Taps → ReferralView (sheet or push)
└── Shows referral count badge: "3/12 referrals"
```

### 4. Create ReferralView

**New file:** `DormDish/Features/Profile/ReferralView.swift`

**UI Layout:**
- Header: "Invite Friends"
- Referral code displayed prominently (large, copyable)
- "Copy Code" button
- "Share" button → native `ShareLink` with pre-filled message
- Stats section:
  - Successful referrals: 3/12
  - Free weeks earned: 3
  - Free months earned: 1
- List of referrals with status indicators

### 5. Add ReferralService

**New file:** `DormDish/Core/Networking/ReferralService.swift`

```swift
actor ReferralService {
    static let shared = ReferralService()

    func generateCode() async throws -> ReferralCodeResponse
    func getMyCode() async throws -> ReferralCodeResponse
    func applyCode(_ code: String) async throws -> ApplyCodeResponse
    func getStats() async throws -> ReferralStatsResponse
}
```

### 6. Share Sheet

Use SwiftUI's native `ShareLink`:

```swift
ShareLink(
    item: "Try DormDish with my code \(code) and we both get free premium!",
    subject: Text("Join DormDish"),
    message: Text("Try DormDish with my code \(code)")
)
```

---

## Environment Variables (Backend)

Add to `.env`:

```
REVENUECAT_SECRET_API_KEY=sk_...  # RevenueCat secret key for promo API
REVENUECAT_WEBHOOK_SECRET=...     # For webhook signature validation
```

---

## Push Notifications

Leverage existing Expo push token infrastructure (`push_tokens` table).

| Event | Recipient | Message |
|---|---|---|
| Referral rewarded | Referrer | "Your friend joined DormDish! You both earned a free week of premium" |
| Referral rewarded | Referred user | "Welcome! Your free week of premium is active" |
| Bonus month earned | Referrer | "Your referral subscribed! You earned a free month of premium" |

---

## Summary

| Aspect | Decision |
|---|---|
| Who can refer | Any user (free or premium) |
| Referral trigger | Signup + activation threshold (1 meal action) |
| Immediate reward | 1 free week premium (both sides) |
| Bonus reward | 1 free month premium (referrer only, on referred user's premium conversion) |
| Anti-cheat | Apple Sign-In `realUserStatus` + annual cap (12) + velocity limit (3/day) + activation threshold |
| Reward delivery | RevenueCat Promotional Entitlements API |
| Code format | 6-char alphanumeric, permanent, reusable |
| Annual cap | 12 referrals per year |
