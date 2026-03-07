"""
Subscription status checking via RevenueCat API with in-memory TTL cache.
"""

import os
import time
from typing import Dict, Tuple

import httpx

REVENUECAT_SECRET_API_KEY = os.getenv("REVENUECAT_SECRET_API_KEY", "")
ENTITLEMENT_ID = "student_plan"
CACHE_TTL_SECONDS = 300  # 5 minutes

# In-memory cache: user_id -> (is_premium, timestamp)
_subscription_cache: Dict[str, Tuple[bool, float]] = {}


async def check_subscription_status(user_id: str) -> bool:
    """
    Check if a user has an active 'student_plan' entitlement via RevenueCat.
    Results are cached for 5 minutes. On error, defaults to False (safe default).
    """
    now = time.time()

    # Check cache
    if user_id in _subscription_cache:
        is_premium, cached_at = _subscription_cache[user_id]
        if now - cached_at < CACHE_TTL_SECONDS:
            return is_premium

    if not REVENUECAT_SECRET_API_KEY:
        return False

    try:
        headers = {
            "Authorization": f"Bearer {REVENUECAT_SECRET_API_KEY}",
            "Content-Type": "application/json",
        }
        url = f"https://api.revenuecat.com/v1/subscribers/{user_id}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)) as client:
            response = await client.get(url, headers=headers)

        if response.status_code != 200:
            _subscription_cache[user_id] = (False, now)
            return False

        data = response.json()
        entitlements = data.get("subscriber", {}).get("entitlements", {})
        entitlement = entitlements.get(ENTITLEMENT_ID)

        if not entitlement:
            _subscription_cache[user_id] = (False, now)
            return False

        # Check expiration — if expires_date is None, it's a lifetime entitlement
        expires_date = entitlement.get("expires_date")
        if expires_date is None:
            _subscription_cache[user_id] = (True, now)
            return True

        # Parse ISO 8601 expiration and compare to now
        from datetime import datetime, timezone
        expiry = datetime.fromisoformat(expires_date.replace("Z", "+00:00"))
        is_active = expiry > datetime.now(timezone.utc)

        _subscription_cache[user_id] = (is_active, now)
        return is_active

    except Exception as e:
        print(f"[subscription] Error checking status for {user_id}: {e}")
        _subscription_cache[user_id] = (False, now)
        return False


def invalidate_subscription_cache(user_id: str) -> None:
    """Remove a user's cached subscription status (for webhook use)."""
    _subscription_cache.pop(user_id, None)
