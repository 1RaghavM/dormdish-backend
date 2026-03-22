import asyncio
import json
import logging
import os
import random
import re
import secrets
import string
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import bcrypt
import httpx
import traceback
from difflib import get_close_matches
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, EmailStr
from supabase import Client, create_client

import secrets as _secrets_for_google

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

from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


load_dotenv()


logger = logging.getLogger("dormdish.api")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

class SchoolRequest(BaseModel):
    name: str
    email: EmailStr
    university: str


# ---------------------------
# Bug Report endpoint
# ---------------------------
class BugReportRequest(BaseModel):
    description: str
    screen_name: str
    app_version: str


def send_bug_report_email(user_email: str, description: str, screen_name: str, app_version: str) -> bool:
    """Send bug report email via Gmail SMTP"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_email = os.getenv("SMTP_EMAIL", "dormdish1@gmail.com")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not smtp_password:
        logger.error("SMTP_PASSWORD environment variable not set")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_email
        msg['To'] = "dormdish1@gmail.com"
        msg['Subject'] = f'[DormDish Bug Report] - {screen_name}'

        body = f"""New Bug Report from DormDish App

Problem Description:
{description}

App Context:
- Screen: {screen_name}
- App Version: {app_version}
- User Email: {user_email}

Reported at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
"""

        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.send_message(msg)

        logger.info(f"Bug report email sent for user {user_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send bug report email: {e}")
        return False


HTTP_CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
HTTP_CLIENT_LIMITS = httpx.Limits(max_connections=40, max_keepalive_connections=20)
HTTP_CLIENT_HEADERS = {
    "User-Agent": "DormDishBackend/1.0 (+https://dormdish.com)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT, limits=HTTP_CLIENT_LIMITS, headers=HTTP_CLIENT_HEADERS)
    app.state.http = client
    try:
        yield
    finally:
        try:
            await client.aclose()
        finally:
            app.state.http = None


app = FastAPI(
    title="DormDish API",
    description="Meal planning API for college students",
    version="1.0.0",
    lifespan=lifespan,
)

# Global exception handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle all unhandled exceptions and return proper JSON responses"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": "An unexpected error occurred. Please try again later.",
            "detail": str(exc) if os.getenv("DEBUG", "false").lower() == "true" else None
        }
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors"""
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation error",
            "message": "Invalid request data",
            "detail": exc.errors()
        }
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "HTTP error",
            "message": exc.detail,
            "status_code": exc.status_code
        }
    )


hi="hi"

def _get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating a fallback client when the
    app lifespan wasn't executed (common in serverless environments).

    The original code expected the shared client to be created in the
    `lifespan` asynccontextmanager. Some serverless adapters don't run
    that lifespan hook, so create and cache a client on-demand to avoid
    a RuntimeError and to ensure outbound HTTP calls (to Google tokeninfo,
    Brevo, Nutrislice, etc.) work.
    """
    client = getattr(app.state, "http", None)
    if client is None:
        
        
        
        
        try:
            client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT, limits=HTTP_CLIENT_LIMITS, headers=HTTP_CLIENT_HEADERS)
            app.state.http = client
            print("[http] lazily initialized AsyncClient on app.state")
        except Exception:
            
            raise RuntimeError("Shared HTTP client not initialized and lazy creation failed")
    return client


async def _fetch_with_retries(url: str, attempts: int = 3, base_delay: float = 0.6) -> str:
    client = _get_http_client()
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            sleep_for = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            logger.warning("http_retry url=%s attempt=%s/%s sleep=%.2fs", url, attempt, attempts, sleep_for)
            await asyncio.sleep(sleep_for)
    if last_exc:
        raise last_exc
    raise RuntimeError("Failed to fetch URL without specific exception")


_raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").strip()
_configured_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
_allow_all = any(o in ("*", "*") for o in _configured_origins)

_cors_regex = os.getenv("CORS_ORIGIN_REGEX", r"https://([a-z0-9-]+\.)?(dormdish\.ai|vercel\.app)") if not _allow_all else None

print("[CORS] raw=", repr(_raw_origins))
print("[CORS] list=", _configured_origins)
print("[CORS] allow_all=", _allow_all)
print("[CORS] regex=", _cors_regex)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else _configured_origins,
    allow_origin_regex=_cors_regex if (not _allow_all and _cors_regex) else None,
    allow_credentials=not _allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "your_openai_api_key_here")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# IMPORTANT: Don't default Supabase env vars to placeholder strings.
# If create_client() is called with an invalid URL/key at import time, the whole
# Vercel function fails to boot and Vercel returns FUNCTION_INVOCATION_FAILED for
# every request.
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

REVENUECAT_SECRET_API_KEY = os.getenv("REVENUECAT_SECRET_API_KEY", "")
REVENUECAT_WEBHOOK_AUTH_KEY = os.getenv("REVENUECAT_WEBHOOK_AUTH_KEY", "")

class _SupabaseNotConfigured:
    def table(self, *_args, **_kwargs):
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured on this server. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.",
        )

try:
    if SUPABASE_URL and SUPABASE_KEY:
        supabase: Client | _SupabaseNotConfigured = create_client(SUPABASE_URL, SUPABASE_KEY)
    else:
        logger.warning("Supabase not configured: missing SUPABASE_URL and/or SUPABASE_SERVICE_KEY")
        supabase = _SupabaseNotConfigured()
except Exception as exc:
    logger.error("Failed to initialize Supabase client: %s", exc)
    supabase = _SupabaseNotConfigured()


# ===================================
# Push Notification Functions
# ===================================

async def send_push_notification(
    expo_push_token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    priority: str = "high"
) -> Dict[str, Any]:
    """
    Send a push notification via Expo's push notification service.
    
    Args:
        expo_push_token: The Expo push token for the device
        title: Notification title
        body: Notification body text
        data: Optional dictionary of data to include with the notification
        priority: Priority level ('default', 'normal', 'high')
    
    Returns:
        Dictionary with the response from Expo's API
    """
    if not expo_push_token or not expo_push_token.startswith('ExponentPushToken['):
        logger.warning(f"Invalid Expo push token format: {expo_push_token}")
        return {"success": False, "error": "Invalid token format"}
    
    message = {
        'to': expo_push_token,
        'sound': 'default',
        'title': title,
        'body': body,
        'data': data or {},
        'priority': priority,
        'channelId': 'default',  # Android channel
    }
    
    try:
        async with httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT) as client:
            response = await client.post(
                'https://exp.host/--/api/v2/push/send',
                json=message,
                headers={
                    'Accept': 'application/json',
                    'Accept-encoding': 'gzip, deflate',
                    'Content-Type': 'application/json',
                }
            )
            result = response.json()
            logger.info(f"Push notification sent to {expo_push_token[:20]}...: {result}")
            return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"Failed to send push notification: {e}")
        return {"success": False, "error": str(e)}


async def get_user_push_token(user_id: str) -> Optional[str]:
    """Get the most recent active push token for a user."""
    try:
        response = supabase.table('push_tokens')\
            .select('token')\
            .eq('user_id', user_id)\
            .eq('active', True)\
            .order('updated_at', desc=True)\
            .limit(1)\
            .execute()
        
        if response.data and len(response.data) > 0:
            return response.data[0]['token']
        return None
    except Exception as e:
        logger.error(f"Error fetching push token for user {user_id}: {e}")
        return None


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

    headers = {
        "Authorization": f"Bearer {REVENUECAT_SECRET_API_KEY}",
        "Content-Type": "application/json",
    }
    subscriber_url = f"https://api.revenuecat.com/v1/subscribers/{user_id}"
    promo_url = f"{subscriber_url}/entitlements/student_plan/promotional"

    try:
        async with httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT) as client:
            # Ensure subscriber exists in RevenueCat (GET auto-creates if missing)
            await client.get(subscriber_url, headers=headers)

            response = await client.post(
                promo_url,
                json={"duration": duration},
                headers=headers,
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


async def check_and_fulfill_referral(user_id: str) -> bool:
    """
    Check if a user has a pending referral event and fulfill it.
    Called after a user's first meal plan generation or food log entry.
    Grants 1 free week of premium to both referrer and referred user.
    Returns True if the referred user's promo was successfully granted, False otherwise.
    """
    try:
        # Find pending referral event for this user
        event_res = supabase.table("referral_events")\
            .select("*")\
            .eq("referred_user_id", user_id)\
            .eq("status", "pending")\
            .execute()

        if not event_res.data:
            return False  # No pending referral

        event = event_res.data[0]
        referrer_id = event["referrer_user_id"]

        # Grant promo to the referred user FIRST — only proceed if it succeeds
        referred_result = await grant_revenuecat_promo(user_id, "weekly")
        if not referred_result.get("success"):
            logger.error(f"[referral] promo failed for referred user {user_id}: {referred_result.get('error')}")
            return False

        # Referred user's promo succeeded — now mark event as rewarded
        update_res = supabase.table("referral_events").update({
            "status": "rewarded",
            "rewarded_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", event["id"]).eq("status", "pending").execute()

        if not update_res.data:
            return True  # Another request already fulfilled this referral, but our promo call succeeded

        # Insert audit record for the referred user
        supabase.table("referral_rewards").insert({
            "referral_event_id": event["id"],
            "user_id": user_id,
            "reward_type": "free_week",
            "granted_via": "revenuecat_promo",
            "duration_days": 7,
        }).execute()

        # Grant promo to the referrer (failure should not block the referred user's reward)
        referrer_result = await grant_revenuecat_promo(referrer_id, "weekly")
        if referrer_result.get("success"):
            supabase.table("referral_rewards").insert({
                "referral_event_id": event["id"],
                "user_id": referrer_id,
                "reward_type": "free_week",
                "granted_via": "revenuecat_promo",
                "duration_days": 7,
            }).execute()
        else:
            logger.error(f"[referral] promo failed for referrer {referrer_id}: {referrer_result.get('error')}")

        # Increment referrer's successful_referrals
        code_res = supabase.table("referral_codes").select("successful_referrals").eq("id", event["referral_code_id"]).execute()
        if code_res.data:
            new_count = code_res.data[0]["successful_referrals"] + 1
            supabase.table("referral_codes").update({"successful_referrals": new_count}).eq("id", event["referral_code_id"]).execute()

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
        return True
    except Exception as e:
        logger.error(f"[referral] check_and_fulfill error for user {user_id}: {e}")
        return False


async def check_user_has_meal_plan_today(user_id: str) -> bool:
    """Check if user has created a meal plan for today."""
    try:
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        
        response = supabase.table('saved_meal_plans')\
            .select('id')\
            .eq('user_id', user_id)\
            .eq('date', today)\
            .limit(1)\
            .execute()
        
        return len(response.data) > 0
    except Exception as e:
        logger.error(f"Error checking meal plan for user {user_id}: {e}")
        return False


async def send_meal_plan_reminder_notification(user_id: str) -> bool:
    """Send a reminder notification to create today's meal plan."""
    try:
        # Check if user already has a meal plan for today
        has_plan = await check_user_has_meal_plan_today(user_id)
        if has_plan:
            logger.info(f"User {user_id} already has meal plan for today, skipping reminder")
            return False
        
        # Get user's push token
        push_token = await get_user_push_token(user_id)
        if not push_token:
            logger.info(f"No push token found for user {user_id}")
            return False
        
        # Send reminder notification
        result = await send_push_notification(
            expo_push_token=push_token,
            title="🍽️ Time to Plan Your Meals!",
            body="Haven't created your meal plan for today yet. Tap here to get started!",
            data={
                "screen": "home",
                "action": "create_meal_plan"
            }
        )
        
        return result.get('success', False)
    except Exception as e:
        logger.error(f"Error sending meal plan reminder to user {user_id}: {e}")
        return False


async def send_meal_plan_ready_notification(user_id: str, date: str) -> bool:
    """Send a notification when meal plan is successfully generated."""
    try:
        # Get user's push token
        push_token = await get_user_push_token(user_id)
        if not push_token:
            logger.info(f"No push token found for user {user_id}")
            return False
        
        # Send ready notification
        result = await send_push_notification(
            expo_push_token=push_token,
            title="✨ Your Meal Plan is Ready!",
            body="Check out your personalized nutrition recommendations for today.",
            data={
                "screen": "meal-plan-detail",
                "date": date
            }
        )
        
        return result.get('success', False)
    except Exception as e:
        logger.error(f"Error sending meal plan ready notification to user {user_id}: {e}")
        return False


def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


# Password Reset Code Generation and Email Sending
def generate_reset_code() -> str:
    """Generate an 8-digit cryptographically secure reset code"""
    return ''.join([str(secrets.choice(range(10))) for _ in range(8)])


def send_password_reset_email(to_email: str, code: str) -> bool:
    """Send password reset code via Gmail SMTP"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_email = os.getenv("SMTP_EMAIL", "dormdish1@gmail.com")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not smtp_password:
        logger.error("SMTP_PASSWORD environment variable not set")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_email
        msg['To'] = to_email
        msg['Subject'] = 'DormDish - Password Reset Code'

        body = f"""
Hello,

You requested to reset your password for DormDish.

Your password reset code is: {code}

This code will expire in 10 minutes.

If you did not request this password reset, please ignore this email.

- The DormDish Team
        """

        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.send_message(msg)
        server.quit()

        logger.info(f"Password reset email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset email: {e}")
        return False

def generate_session_token() -> str:
    """Generate a secure session token"""
    return secrets.token_urlsafe(32)

def create_user_session(user_id: str, expires_days: int = 30) -> str:
    """Create a new user session and return the token"""
    token = generate_session_token()
    # Use UTC timezone for consistency with Supabase
    expires_at = datetime.now(ZoneInfo("UTC")) + timedelta(days=expires_days)
    
    try:
        supabase.table("user_sessions").insert({
            "user_id": user_id,
            "session_token": token,
            "expires_at": expires_at.isoformat()
        }).execute()
        return token
    except Exception as e:
        print(f"Error creating session: {e}")
        return None

def verify_session_token(token: str) -> Optional[dict]:
    """Verify a session token and return user data if valid"""
    try:
        # Use UTC timezone for consistency with Supabase
        now_utc = datetime.now(ZoneInfo("UTC")).isoformat()
        response = supabase.table("user_sessions")\
            .select("*, users(*)")\
            .eq("session_token", token)\
            .gt("expires_at", now_utc)\
            .execute()
        
        if response.data and len(response.data) > 0:
            session = response.data[0]
            user = session["users"]
            user.pop('password_hash', None)
            return user
        return None
    except Exception as e:
        print(f"Error verifying session: {e}")
        return None

ACTIVITY_MULTIPLIERS: Dict[str, Tuple[float, str]] = {
    "sedentary": (1.2, "Sedentary: little or no exercise"),
    "lightly_active": (1.375, "Exercise 1-3 times/week"),
    "moderately_active": (1.55, "Exercise 4-5 times/week"),
    "very_active": (1.725, "Daily exercise or intense exercise 3-4 times/week"),
    "extremely_active": (1.9, "Intense exercise 6-7 times/week"),
    "super_active": (2.2, "Very intense exercise daily, or physical job"),
}


def calculate_bmr_tdee(age: int, height_feet: int, height_inches: int, weight_lbs: float,
                      gender: str, activity_level: str) -> Dict[str, object]:
    """Calculate BMR and TDEE using the Mifflin-St Jeor equation, mirroring calculator.net."""

    total_inches = (height_feet * 12) + height_inches
    height_cm = total_inches * 2.54
    weight_kg = weight_lbs * 0.45359237

    gender_normalized = (gender or "").strip().lower()
    base_component = (10 * weight_kg) + (6.25 * height_cm) - (5 * age)

    if gender_normalized in {"male", "man", "m"}:
        bmr_raw = base_component + 5
    elif gender_normalized in {"female", "woman", "f"}:
        bmr_raw = base_component - 161
    else:
        male_estimate = base_component + 5
        female_estimate = base_component - 161
        bmr_raw = (male_estimate + female_estimate) / 2.0

    default_multiplier = ACTIVITY_MULTIPLIERS.get("moderately_active", (1.55, ""))[0]
    multiplier = ACTIVITY_MULTIPLIERS.get(activity_level, (default_multiplier, ""))[0]

    tdee_raw = bmr_raw * multiplier

    activity_breakdown: Dict[str, Dict[str, float]] = {}
    for key, (mult, description) in ACTIVITY_MULTIPLIERS.items():
        activity_breakdown[key] = {
            "label": description,
            "multiplier": mult,
            "calories": int(round(bmr_raw * mult)),
        }

    return {
        "bmr": int(round(bmr_raw)),
        "tdee": int(round(tdee_raw)),
        "bmr_precise": round(bmr_raw, 2),
        "tdee_precise": round(tdee_raw, 2),
        "activity_breakdown": activity_breakdown,
    }


# Weight change speed mapping (lbs per week)
WEIGHT_CHANGE_RATES = {
    "slow": 0.5,
    "recommended": 1.0,
    "fast": 2.0,
}


def calculate_extended_metrics(
    age: int,
    height_feet: int,
    height_inches: int,
    weight_lbs: float,
    gender: str,
    activity_level: str,
    goal: Optional[str] = None,
    goal_weight_lbs: Optional[float] = None,
    weight_change_speed: Optional[str] = None,
) -> Dict[str, object]:
    """Calculate extended user metrics including BMI, daily calorie target, and goal projections."""

    # Get base metrics
    base_metrics = calculate_bmr_tdee(age, height_feet, height_inches, weight_lbs, gender, activity_level)

    total_inches = (height_feet * 12) + height_inches
    height_cm = total_inches * 2.54
    weight_kg = weight_lbs * 0.453592

    # Calculate BMI: (weight_lbs / height_inches^2) * 703
    bmi = (weight_lbs / (total_inches ** 2)) * 703 if total_inches > 0 else None

    # Calculate daily calorie target based on goal and weight change speed
    tdee = base_metrics["tdee"]
    daily_calorie_target = tdee  # Default to maintenance

    weekly_change_rate = WEIGHT_CHANGE_RATES.get(weight_change_speed or "recommended", 1.0)
    # 1 lb of fat = ~3500 calories
    weekly_calorie_adjustment = weekly_change_rate * 3500
    daily_calorie_adjustment = weekly_calorie_adjustment / 7

    goal_normalized = (goal or "").lower()
    if goal_normalized in {"lose", "cut"}:
        daily_calorie_target = tdee - daily_calorie_adjustment
    elif goal_normalized in {"gain", "bulk"}:
        daily_calorie_target = tdee + daily_calorie_adjustment
    # "maintain" keeps tdee as is

    # Calculate estimated weeks to goal and target date
    estimated_weeks_to_goal = None
    target_date = None
    goal_weight_kg = None

    if goal_weight_lbs is not None:
        goal_weight_kg = goal_weight_lbs * 0.453592
        weight_diff = abs(weight_lbs - goal_weight_lbs)
        if weekly_change_rate > 0:
            estimated_weeks_to_goal = weight_diff / weekly_change_rate
            target_date = (datetime.now() + timedelta(weeks=estimated_weeks_to_goal)).date().isoformat()

    return {
        **base_metrics,
        "height_total_inches": total_inches,
        "height_cm": round(height_cm, 2),
        "weight_kg": round(weight_kg, 2),
        "goal_weight_kg": round(goal_weight_kg, 2) if goal_weight_kg else None,
        "bmi": round(bmi, 1) if bmi else None,
        "daily_calorie_target": int(round(daily_calorie_target)),
        "estimated_weeks_to_goal": round(estimated_weeks_to_goal, 1) if estimated_weeks_to_goal else None,
        "target_date": target_date,
    }


@dataclass
class User:
    """User profile with physical stats and fitness goals"""
    age: int
    height_feet: int
    height_inches: int
    weight_lbs: int
    activity_level: str  
    goal: str  
    preferred_dining_hall: str
    
    dietary_preference: Optional[str] = None
    
    @property
    def height_cm(self) -> float:
        """Convert height to centimeters"""
        total_inches = (self.height_feet * 12) + self.height_inches
        return total_inches * 2.54
    
    @property
    def weight_kg(self) -> float:
        """Convert weight to kilograms"""
        return self.weight_lbs * 0.453592




class SubscribeRequest(BaseModel):
    email: EmailStr
    firstName: Optional[str] = None
    lastName: Optional[str] = None


def _brevo_base_url() -> str:
    return "https://api.brevo.com/v3"


def _brevo_headers() -> Dict[str, str]:
    api_key = os.getenv("BREVO_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="BREVO_API_KEY not configured")
    return {
        "api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _brevo_get_account() -> Dict[str, Any]:
    client = _get_http_client()
    try:
        response = await client.get(f"{_brevo_base_url()}/account", headers=_brevo_headers())
        body: Optional[Any] = None
        if response.content:
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text[:200]}
        return {"status": response.status_code, "body": body}
    except Exception as exc:
        return {"status": 0, "error": str(exc)}


async def _brevo_get_template(template_id: int) -> Dict[str, Any]:
    client = _get_http_client()
    try:
        response = await client.get(f"{_brevo_base_url()}/smtp/templates/{template_id}", headers=_brevo_headers())
        body: Optional[Any] = None
        if response.content:
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text[:200]}
        return {"status": response.status_code, "body": body}
    except Exception as exc:
        return {"status": 0, "error": str(exc)}


def _guess_name_from_email(email: str) -> Dict[str, str]:
    local = email.split("@")[0]
    
    parts = re.split(r"[._-]+", local)
    parts = [p for p in parts if p]
    if not parts:
        return {"first": "", "last": ""}
    if len(parts) == 1:
        return {"first": parts[0].capitalize(), "last": ""}
    return {"first": parts[0].capitalize(), "last": parts[-1].capitalize()}
async def upsert_brevo_contact(
    email: str,
    attributes: Optional[Dict[str, str]] = None,
    list_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    client = _get_http_client()
    payload: Dict[str, Any] = {
        "email": email,
        "updateEnabled": True,
    }
    if attributes:
        payload["attributes"] = attributes

    env_list_id = os.getenv("BREVO_LIST_ID")
    resolved_list_ids: List[int] = []
    if env_list_id:
        try:
            resolved_list_ids.append(int(env_list_id))
        except Exception:
            pass
    if list_ids:
        for lid in list_ids:
            if isinstance(lid, int):
                resolved_list_ids.append(lid)
    if resolved_list_ids:
        payload["listIds"] = list(dict.fromkeys(resolved_list_ids))

    try:
        response = await client.post(f"{_brevo_base_url()}/contacts", headers=_brevo_headers(), json=payload)
        if response.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Brevo contact upsert failed: {response.text}")
        return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Brevo error: {exc}")


def build_brevo_template_payload(
    email: str,
    *,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    subject: Optional[str] = None,
    tags: Optional[List[str]] = None,
    contact_attributes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    from_email = os.getenv("BREVO_FROM_EMAIL")
    from_name = os.getenv("BREVO_FROM_NAME", "DormDish")
    template_raw = os.getenv("BREVO_TEMPLATE_ID", "").strip()
    template_id: Optional[int] = None
    if template_raw:
        m = re.match(r"(\d+)", template_raw)
        if m:
            try:
                template_id = int(m.group(1))
            except Exception:
                template_id = None
    if not (from_email and template_id):
        raise HTTPException(status_code=500, detail="Brevo env vars missing (FROM_EMAIL, TEMPLATE_ID)")

    if not first_name or not last_name:
        guess = _guess_name_from_email(email)
        first_name = first_name or guess["first"]
        last_name = last_name or guess["last"]

    payload: Dict[str, Any] = {
        "templateId": template_id,
        "to": [{"email": email, "name": f"{first_name or ''} {last_name or ''}".strip()}],
        "sender": {"email": from_email, "name": from_name},
        "_contact_attributes": {
            "FIRSTNAME": first_name or "",
            "LASTNAME": last_name or "",
        },
    }
    if contact_attributes:
        payload["_contact_attributes"].update(contact_attributes)
    if subject:
        payload["subject"] = subject
    if tags:
        payload["tags"] = tags
    return payload


async def _send_brevo_email_async(body: Dict[str, Any]) -> None:
    client = _get_http_client()
    payload = dict(body)
    attributes = payload.pop("_contact_attributes", None)
    recipient_email: Optional[str] = None
    try:
        to_entries = payload.get("to") or []
        if to_entries and isinstance(to_entries[0], dict):
            recipient_email = to_entries[0].get("email")
        if attributes and recipient_email:
            try:
                await upsert_brevo_contact(recipient_email, attributes=attributes)
            except HTTPException as exc:
                logger.warning("brevo_contact_upsert_failed email=%s detail=%s", recipient_email, exc.detail)
            except Exception as exc:
                logger.warning("brevo_contact_upsert_error email=%s error=%s", recipient_email, exc)
        response = await client.post(f"{_brevo_base_url()}/smtp/email", headers=_brevo_headers(), json=payload)
        if response.status_code not in (200, 201):
            logger.error("brevo_send_failed status=%s body=%s", response.status_code, response.text[:200])
    except Exception as exc:
        logger.exception("brevo_send_exception email=%s error=%s", recipient_email, exc)


@app.post('/subscribe', summary='Subscribe to newsletter and queue welcome email')
async def subscribe(req: SubscribeRequest, background_tasks: BackgroundTasks):
    try:
        fn = (req.firstName or "").strip()
        ln = (req.lastName or "").strip()
        payload = build_brevo_template_payload(
            req.email.strip().lower(),
            first_name=fn or None,
            last_name=ln or None,
        )
        background_tasks.add_task(_send_brevo_email_async, payload)
        return {"success": True, "message": "Subscription successful. Email queued."}
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        logger.warning("subscribe_failed email=%s error=%s", req.email, exc)
        raise HTTPException(status_code=500, detail="Subscription failed")


@app.get('/health/brevo', summary='Brevo configuration and template health check')
async def brevo_health():
    if os.getenv("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
    try:
        from_email = os.getenv("BREVO_FROM_EMAIL")
        from_name = os.getenv("BREVO_FROM_NAME")
        template_raw = os.getenv("BREVO_TEMPLATE_ID", "").strip()
        list_id = os.getenv("BREVO_LIST_ID")
        template_id: Optional[int] = None
        if template_raw:
            m = re.match(r"(\d+)", template_raw)
            if m:
                try:
                    template_id = int(m.group(1))
                except Exception:
                    template_id = None

        account = await _brevo_get_account()
        template_info = await _brevo_get_template(template_id) if template_id else None
        return {
            "ok": bool(account and account.get("status") in (200, 201)),
            "env": {
                "BREVO_FROM_EMAIL": bool(from_email),
                "BREVO_FROM_NAME": bool(from_name),
                "BREVO_TEMPLATE_ID": template_id,
                "BREVO_LIST_ID": list_id,
            },
            "account": account,
            "template": template_info,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get('/email/test', summary='Send a Brevo test email using the configured template')
async def email_test(email: EmailStr):
    if os.getenv("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
    try:
        payload = build_brevo_template_payload(str(email))
        await _send_brevo_email_async(payload)
        return {"success": True, "message": "Test email dispatched."}
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Test email failed")




# Cache for universities loaded from Supabase
_universities_cache: Optional[List[str]] = None
_universities_cache_time: Optional[float] = None
UNIVERSITIES_CACHE_TTL = 3600  # 1 hour cache

def load_universities_from_supabase() -> List[str]:
    """Load universities from Supabase database."""
    global _universities_cache, _universities_cache_time
    
    # Check cache
    current_time = time.time()
    if _universities_cache is not None and _universities_cache_time is not None:
        if current_time - _universities_cache_time < UNIVERSITIES_CACHE_TTL:
            return _universities_cache
    
    universities: List[str] = []
    try:
        # Fetch all universities from Supabase, paginate if needed
        offset = 0
        batch_size = 1000
        
        while True:
            result = supabase.table('universities').select('name').order('name').range(offset, offset + batch_size - 1).execute()
            
            if result.data:
                for row in result.data:
                    name = (row.get('name') or '').strip()
                    if name:
                        universities.append(name)
                
                if len(result.data) < batch_size:
                    break  # No more data
                offset += batch_size
            else:
                break
        
        # Update cache
        _universities_cache = universities
        _universities_cache_time = current_time
        logger.info(f"✅ Loaded {len(universities)} universities from Supabase")
        
    except Exception as e:
        logger.error(f"⚠️ Failed to load universities from Supabase: {e}")
        # Return cached data if available, even if expired
        if _universities_cache is not None:
            return _universities_cache
    
    return universities

@app.get('/universities')
def get_universities():
    return {"universities": load_universities_from_supabase()}


@app.post('/request-school', summary='Request a new school/university to be added')
async def request_school_endpoint(data: SchoolRequest):
    try:
        payload = {
            "name": (data.name or "").strip(),
            "email": (data.email or "").strip().lower(),
            "university": (data.university or "").strip(),
            "created_at": datetime.now().isoformat(),
        }
        # Insert into Supabase (non-blocking wrapper handled elsewhere)
        supabase.table('school_requests').insert(payload).execute()
        return {"success": True, "message": "School request submitted successfully."}
    except Exception as e:
        logger.exception("/request-school failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to submit school request")




class WaitlistJoinRequest(BaseModel):
    firstName: str
    lastName: str
    email: str
    university: str
    graduationYear: Optional[str] = None
    notifications: Optional[bool] = True
    
    captchaToken: Optional[str] = None

async def verify_turnstile_token(token: Optional[str], remoteip: Optional[str] = None) -> bool:
    """Verify Cloudflare Turnstile token. If TURNSTILE_SECRET_KEY is not set, skip verification (dev mode)."""
    secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    
    if not secret:
        print("[Turnstile] No TURNSTILE_SECRET_KEY configured; skipping captcha verification (dev mode)")
        return True

    if not token:
        raise HTTPException(status_code=400, detail="Captcha token missing")

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    data = {
        "secret": secret,
        "response": token,
    }
    if remoteip:
        data["remoteip"] = remoteip
    client = _get_http_client()
    try:
        resp = await client.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Captcha verification error: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Captcha verification failed: HTTP {resp.status_code}")

    try:
        result = resp.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Captcha verification failed: invalid response")

    if not result.get("success"):
        errs = result.get("error-codes") or result.get("error_codes") or []
        raise HTTPException(status_code=400, detail=f"Captcha verification failed: {errs}")

    return True


@app.post('/waitlist/join')
async def join_waitlist(data: WaitlistJoinRequest, request: Request, background_tasks: BackgroundTasks):
    try:
        client_ip = (
            request.headers.get('CF-Connecting-IP')
            or (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() if request.headers.get('X-Forwarded-For') else None)
            or (request.client.host if request.client else None)
        )
        await verify_turnstile_token(data.captchaToken, client_ip)

        
        payload = {
            "first_name": data.firstName.strip(),
            "last_name": data.lastName.strip(),
            "email": data.email.strip().lower(),
            "university": data.university.strip(),
            "graduation_year": data.graduationYear,
            "notifications": bool(data.notifications),
            "updated_at": datetime.now().isoformat()
        }
        supabase.table('waitlist_users').upsert(payload, on_conflict='email').execute()

        
        brevo_status = None
        if bool(data.notifications):
            try:
                contact_attrs = {
                    "UNIVERSITY": data.university.strip(),
                    "GRADYEAR": (data.graduationYear or ""),
                }
                email_payload = build_brevo_template_payload(
                    data.email.strip().lower(),
                    first_name=data.firstName.strip(),
                    last_name=data.lastName.strip(),
                    subject="You're on the DormDish waitlist!",
                    tags=["waitlist"],
                    contact_attributes=contact_attrs,
                )
                background_tasks.add_task(_send_brevo_email_async, email_payload)
                brevo_status = "queued"
            except HTTPException as exc:
                print(f"⚠️ Brevo sync failed: {exc.detail}")

        
        count_resp = supabase.table('waitlist_users').select('id', count='exact').eq('university', data.university.strip()).execute()
        uni_count = getattr(count_resp, 'count', None)
        return {"success": True, "message": "Successfully joined waitlist", "university_count": uni_count or 0, "brevo_status": brevo_status}
    except Exception as e:
        print(f"⚠️ Waitlist join failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to join waitlist")




authorization_header_prefix = "Bearer "

class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    age: int
    height_feet: int
    height_inches: int
    weight_lbs: float
    gender: str
    activity_level: str
    goal: str
    university: str

    dietary_preference: Optional[str] = None
    # New onboarding fields
    birthday: Optional[str] = None  # ISO 8601 date string (YYYY-MM-DD)
    goal_weight_lbs: Optional[float] = None
    weight_change_speed: Optional[str] = None  # slow, recommended, fast
    accomplishments: Optional[List[str]] = None
    obstacles: Optional[List[str]] = None
    school_id: Optional[str] = None
    dining_halls: Optional[List[str]] = None
    dietary_restrictions: Optional[List[str]] = None
    attribution_source: Optional[str] = None
    apple_health_connected: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    auth_provider: Optional[str] = None  # email, apple, google

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleIdTokenRequest(BaseModel):
    id_token: str

class GoogleRegisterRequest(BaseModel):
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
    # New onboarding fields
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


class AppleIdTokenRequest(BaseModel):
    id_token: str
    # Apple may provide user info only on first authorization
    user_data: Optional[Dict[str, Any]] = None
    real_user_status: Optional[int] = None  # 0=unsupported, 1=unknown, 2=likelyReal


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
    # New onboarding fields
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


# ── Referral Models ──

class ApplyReferralCodeRequest(BaseModel):
    code: str

class ReferralCodeResponse(BaseModel):
    success: bool
    code: Optional[str] = None
    has_code: bool = False
    successful_referrals: int = 0
    max_referrals: int = 7
    message: Optional[str] = None


# Password Reset Request Models
class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyResetCodeRequest(BaseModel):
    email: EmailStr
    code: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str


# ── Barcode Lookup Models ──

class BarcodeLookupRequest(BaseModel):
    barcode: str

class ScannedProduct(BaseModel):
    barcode: str
    name: str
    brand: Optional[str] = None
    serving_size: Optional[str] = None
    calories: float = 0.0
    protein_g: float = 0.0
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    image_url: Optional[str] = None
    nutri_score: Optional[str] = None

class BarcodeLookupResponse(BaseModel):
    success: bool
    message: str
    product: Optional[ScannedProduct] = None

# ── Food Log Models ──

class FoodLogAddRequest(BaseModel):
    date: str
    meal_type: str
    barcode: Optional[str] = None
    name: str
    brand: Optional[str] = None
    serving_size: Optional[str] = None
    servings: float = 1.0
    calories: float = 0.0
    protein_g: float = 0.0
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    image_url: Optional[str] = None

class FoodLogEntry(BaseModel):
    id: str
    user_id: str
    date: str
    meal_type: str
    barcode: Optional[str] = None
    name: str
    brand: Optional[str] = None
    serving_size: Optional[str] = None
    servings: float = 1.0
    calories: float = 0.0
    protein_g: float = 0.0
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    image_url: Optional[str] = None
    created_at: Optional[str] = None

class FoodLogResponse(BaseModel):
    success: bool
    message: str
    entries: Optional[list] = None
    entry: Optional[dict] = None


# Cache JWKS clients to avoid fetching keys on every request
_apple_jwks_client = None
_google_jwks_client = None

def _get_apple_jwks_client():
    """Get or create a cached Apple JWKS client."""
    global _apple_jwks_client
    if _apple_jwks_client is None:
        from jwt import PyJWKClient
        # Cache keys for 1 hour (Apple rotates keys infrequently)
        _apple_jwks_client = PyJWKClient(
            "https://appleid.apple.com/auth/keys",
            cache_keys=True,
            lifespan=3600
        )
    return _apple_jwks_client

async def _verify_apple_id_token(id_token: str) -> Dict[str, Any]:
    """Validate an Apple ID token (JWT).

    Apple ID tokens are JWTs signed with Apple's private key. We verify them by:
    1. Fetching Apple's public keys from their JWKS endpoint (cached)
    2. Verifying the JWT signature
    3. Validating standard claims (iss, aud, exp)
    """
    import jwt

    APPLE_ISSUER = "https://appleid.apple.com"

    # Get the expected audience (your app's bundle ID or service ID)
    apple_client_id = os.getenv("APPLE_CLIENT_ID")
    if not apple_client_id:
        raise HTTPException(status_code=500, detail="APPLE_CLIENT_ID not configured")

    try:
        # Use cached JWKS client to avoid fetching keys on every request
        def _sync_verify():
            jwks_client = _get_apple_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)

            # Decode and verify the token
            decoded = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=apple_client_id,
                issuer=APPLE_ISSUER,
            )
            return decoded

        token_data = await asyncio.to_thread(_sync_verify)

        # Apple tokens contain 'sub' (subject) as the unique user identifier
        # and optionally 'email' if the user shared it
        if not token_data.get("sub"):
            raise HTTPException(status_code=401, detail="Apple token missing subject identifier")

        return token_data

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Apple token has expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Apple token audience mismatch")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Apple token issuer mismatch")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Apple ID token: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to verify Apple token: {exc}")


async def _verify_google_id_token(id_token: str) -> Dict[str, Any]:
    """Validate a Google ID token.

    Historically this used the shared AsyncClient which can fail with
    "Event loop is closed" if some other code called ``asyncio.run()`` and
    closed the loop. To make verification robust from both sync and async
    contexts, perform the HTTP request using httpx's synchronous API inside
    ``asyncio.to_thread`` so it doesn't depend on the current event loop.
    """
    url = "https://oauth2.googleapis.com/tokeninfo"
    try:
        def _sync_get():
            
            resp = httpx.get(url, params={"id_token": id_token}, timeout=httpx.Timeout(5.0))
            resp.raise_for_status()
            return resp

        resp = await asyncio.to_thread(_sync_get)
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=401, detail="Invalid Google ID token") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to verify Google token: {exc}") from exc
    aud_expected = os.getenv("GOOGLE_CLIENT_ID")
    if not aud_expected:
        logger.error("GOOGLE_CLIENT_ID env var not set - cannot validate Google tokens")
        raise HTTPException(status_code=500, detail="Server configuration error")
    if data.get("aud") != aud_expected:
        raise HTTPException(status_code=401, detail="Google token audience mismatch")
    if data.get("email_verified") not in ("true", True):
        raise HTTPException(status_code=401, detail="Google email not verified")
    return data

def get_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if authorization.startswith(authorization_header_prefix):
        return authorization[len(authorization_header_prefix):].strip()
    return None


def get_current_user(authorization: Optional[str] = Header(None)):
    token = get_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid authorization header")
    user = verify_session_token(token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    return user


async def require_premium(current_user=Depends(get_current_user)):
    """Dependency that requires an active premium (student_plan) subscription."""
    from subscription import check_subscription_status
    is_premium = await check_subscription_status(str(current_user["id"]))
    if not is_premium:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "premium_required",
                "message": "This feature requires a DormDish Premium subscription.",
            },
        )
    return current_user


@app.post('/bug-report', summary='Submit a bug report')
def submit_bug_report(data: BugReportRequest, background_tasks: BackgroundTasks, current_user=Depends(get_current_user)):
    """
    Submit a bug report. Requires authentication.
    Sends an email to the DormDish team with the bug details.
    """
    try:
        user_email = current_user.get("email", "Unknown")

        # Send email in background
        background_tasks.add_task(
            send_bug_report_email,
            user_email,
            data.description.strip(),
            data.screen_name,
            data.app_version
        )

        return {"success": True, "message": "Bug report submitted successfully. Thank you for your feedback!"}
    except Exception as e:
        logger.error(f"Bug report submission failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit bug report")


@app.post('/auth/register')
def auth_register(data: RegisterRequest):
    try:

        existing = supabase.table("users").select("id").eq("email", str(data.email).lower()).execute()
        if existing.data and len(existing.data) > 0:
            raise HTTPException(status_code=400, detail="An account with this email already exists")


        pwd_hash = hash_password(data.password)

        # Calculate extended metrics (BMR, TDEE, BMI, daily calorie target, etc.)
        metrics = calculate_extended_metrics(
            age=int(data.age),
            height_feet=int(data.height_feet),
            height_inches=int(data.height_inches),
            weight_lbs=float(data.weight_lbs),
            gender=data.gender,
            activity_level=data.activity_level,
            goal=data.goal,
            goal_weight_lbs=float(data.goal_weight_lbs) if data.goal_weight_lbs else None,
            weight_change_speed=data.weight_change_speed,
        )

        payload = {
            "username": data.username,
            "email": str(data.email).lower(),
            "password_hash": pwd_hash,
            "age": int(data.age),
            "height_feet": int(data.height_feet),
            "height_inches": int(data.height_inches),
            "weight_lbs": float(data.weight_lbs),
            "gender": data.gender,
            "activity_level": data.activity_level,
            "goal": data.goal,
            "university": data.university,

            "dietary_preference": (data.dietary_preference or None),
            "bmr": metrics.get("bmr"),
            "tdee": metrics.get("tdee"),
            "created_at": datetime.now().isoformat(),
            # New onboarding fields
            "birthday": data.birthday,
            "goal_weight_lbs": float(data.goal_weight_lbs) if data.goal_weight_lbs else None,
            "weight_change_speed": data.weight_change_speed,
            "accomplishments": data.accomplishments,
            "obstacles": data.obstacles,
            "school_id": data.school_id,
            "dining_halls": data.dining_halls,
            "dietary_restrictions": data.dietary_restrictions,
            "attribution_source": data.attribution_source,
            "apple_health_connected": data.apple_health_connected,
            "notifications_enabled": data.notifications_enabled,
            "auth_provider": data.auth_provider or "email",
            # Extended computed metrics
            "height_cm": metrics.get("height_cm"),
            "weight_kg": metrics.get("weight_kg"),
            "goal_weight_kg": metrics.get("goal_weight_kg"),
            "bmi": metrics.get("bmi"),
            "daily_calorie_target": metrics.get("daily_calorie_target"),
            "estimated_weeks_to_goal": metrics.get("estimated_weeks_to_goal"),
            "target_date": metrics.get("target_date"),
        }

        # Try inserting the user. If the DB rejects due to a unique constraint on
        # username (common when users share the same display name), automatically
        # attempt a small number of retries by appending a numeric suffix to the
        # username. This avoids returning an internal server error for a
        # username collision while preserving the email-uniqueness check above.
        max_attempts = 5
        base_username = (payload.get("username") or "").strip()
        if not base_username:
            # Fall back to a name derived from email local-part if username empty
            base_username = str(data.email).split("@")[0] if data.email else "user"
        #hi
        user_row = None
        for attempt in range(max_attempts):
            try:
                try_username = base_username if attempt == 0 else f"{base_username}-{attempt}"
                payload["username"] = try_username
                inserted = supabase.table("users").insert(payload).execute()

                # supabase client may return an object with .error or raise an Exception.
                insert_error = getattr(inserted, "error", None)
                if insert_error:
                    msg = str(insert_error)
                    # Detect username-unique violations from message text
                    if ("users_username_key" in msg) or ("Key (username)" in msg) or ("duplicate key value" in msg):
                        # Collision on username, try next suffix
                        logger.info("username collision detected for %s (attempt %s): %s", try_username, attempt + 1, msg)
                        continue
                    # If it's a different DB error, surface it
                    raise HTTPException(status_code=500, detail="Registration failed")

                if inserted.data and len(inserted.data) > 0:
                    user_row = inserted.data[0]
                    break
                # If no data was returned but no explicit error, treat as failure and retry once
                logger.warning("Insert returned no data for username=%s on attempt=%s", try_username, attempt + 1)
            except Exception as e:
                emsg = str(e)
                # Psycopg / Postgres unique constraint error code often surfaces as 23505;
                # also check for textual duplicate-key message referencing username.
                if ("23505" in emsg) or ("users_username_key" in emsg) or ("Key (username)" in emsg) or ("duplicate key value" in emsg):
                    logger.info("caught unique-constraint username collision (attempt %s) for username=%s: %s", attempt + 1, payload.get("username"), emsg)
                    continue
                print(f"⚠️ Register insert error (non-username): {e}")
                raise HTTPException(status_code=500, detail="Registration failed")

        if not user_row:
            # All attempts exhausted
            raise HTTPException(status_code=500, detail="Failed to create user after resolving username conflicts")

        
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        
        
        user_row["session_token"] = token
        user_row.pop('password_hash', None)
        return {
            "success": True,
            "message": "Registration successful",
            "user": user_row,
            "session_token": token,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"⚠️ Register failed: {e}")
        raise HTTPException(status_code=500, detail="Registration failed")


@app.post('/auth/login')
def auth_login(data: LoginRequest):
    try:
        
        res = supabase.table("users").select("*").eq("email", str(data.email).lower()).execute()
        if not res.data:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user_row = res.data[0]

        
        hashed = user_row.get("password_hash")
        if not hashed or not verify_password(data.password, hashed):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        
        try:
            supabase.table("users").update({"last_login": datetime.now().isoformat()}).eq("id", user_row.get("id")).execute()
        except Exception:
            pass

        user_row.pop('password_hash', None)
        return {
            "success": True,
            "message": "Login successful",
            "user": user_row,
            "session_token": token,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"⚠️ Login failed: {e}")
        raise HTTPException(status_code=500, detail="Login failed")


@app.get('/auth/profile')
def auth_profile(current_user=Depends(get_current_user)):
    return {"success": True, "user": current_user}


@app.post('/auth/logout')
def auth_logout(authorization: Optional[str] = Header(None)):
    token = get_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid authorization header")
    try:
        supabase.table("user_sessions").delete().eq("session_token", token).execute()
    except Exception as e:
        print(f"⚠️ Logout cleanup failed: {e}")
    return {"success": True, "message": "Logged out"}


@app.delete('/auth/account', summary='Delete user account and all associated data')
async def delete_account(current_user=Depends(get_current_user)):
    """
    Permanently delete user account and all associated data.
    This action cannot be undone.
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID not found")

    print(f"🗑️ Starting account deletion for user {user_id}")

    try:
        # Delete user sessions
        try:
            supabase.table("user_sessions").delete().eq("user_id", user_id).execute()
            print(f"  ✓ Deleted user sessions")
        except Exception as e:
            print(f"  ⚠️ Failed to delete user sessions: {e}")

        # Delete password reset codes
        try:
            supabase.table("password_reset_codes").delete().eq("user_id", user_id).execute()
            print(f"  ✓ Deleted password reset codes")
        except Exception as e:
            print(f"  ⚠️ Failed to delete password reset codes: {e}")

        # Delete saved meal plans
        try:
            supabase.table("saved_meal_plans").delete().eq("user_id", user_id).execute()
            print(f"  ✓ Deleted saved meal plans")
        except Exception as e:
            print(f"  ⚠️ Failed to delete saved meal plans: {e}")

        # Delete weight history
        try:
            supabase.table("weight_history").delete().eq("user_id", user_id).execute()
            print(f"  ✓ Deleted weight history")
        except Exception as e:
            print(f"  ⚠️ Failed to delete weight history: {e}")

        # Delete push tokens
        try:
            supabase.table("push_tokens").delete().eq("user_id", user_id).execute()
            print(f"  ✓ Deleted push tokens")
        except Exception as e:
            print(f"  ⚠️ Failed to delete push tokens: {e}")

        # Delete meal plan generations
        try:
            supabase.table("meal_plan_generations").delete().eq("user_id", user_id).execute()
            print(f"  ✓ Deleted meal plan generations")
        except Exception as e:
            print(f"  ⚠️ Failed to delete meal plan generations: {e}")

        # Finally, delete the user record
        try:
            supabase.table("users").delete().eq("id", user_id).execute()
            print(f"  ✓ Deleted user record")
        except Exception as e:
            print(f"  ❌ Failed to delete user record: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete user account")

        print(f"✅ Account deletion complete for user {user_id}")
        return {"success": True, "message": "Account deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Account deletion failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {str(e)}")


# Password Reset Endpoints
@app.post('/auth/forgot-password', summary='Request password reset code')
def auth_forgot_password(data: ForgotPasswordRequest, background_tasks: BackgroundTasks):
    """
    Request a password reset code. If the email exists, an 8-digit code
    is sent to the email address, valid for 10 minutes.
    """
    try:
        email = str(data.email).lower()

        # Check if user exists
        res = supabase.table("users").select("id, email").eq("email", email).execute()
        if not res.data:
            # Don't reveal whether email exists - return success anyway
            return {"success": True, "message": "If this email is registered, you will receive a reset code."}

        user_row = res.data[0]
        user_id = user_row.get("id")

        # Generate 6-digit code
        code = generate_reset_code()
        expires_at = datetime.now() + timedelta(minutes=10)

        # Delete any existing reset codes for this user
        try:
            supabase.table("password_reset_codes").delete().eq("user_id", user_id).execute()
        except Exception:
            pass  # Table might not exist yet or no existing codes

        # Store the reset code
        try:
            supabase.table("password_reset_codes").insert({
                "user_id": user_id,
                "email": email,
                "code": code,
                "expires_at": expires_at.isoformat(),
                "created_at": datetime.now().isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Failed to store reset code: {e}")
            raise HTTPException(status_code=500, detail="Failed to create reset code")

        # Send email in background
        background_tasks.add_task(send_password_reset_email, email, code)

        return {"success": True, "message": "If this email is registered, you will receive a reset code."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Forgot password error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process password reset request")


@app.post('/auth/verify-reset-code', summary='Verify password reset code')
def auth_verify_reset_code(data: VerifyResetCodeRequest):
    """
    Verify a password reset code. Returns success if the code is valid and not expired.
    """
    try:
        email = str(data.email).lower()
        code = data.code.strip()

        # Find the reset code
        res = supabase.table("password_reset_codes")\
            .select("*")\
            .eq("email", email)\
            .eq("code", code)\
            .gt("expires_at", datetime.now().isoformat())\
            .execute()

        if not res.data:
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        return {"success": True, "message": "Code verified successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Verify reset code error: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify reset code")


@app.post('/auth/reset-password', summary='Reset password with verified code')
def auth_reset_password(data: ResetPasswordRequest):
    """
    Reset the user's password using a verified code.
    """
    try:
        email = str(data.email).lower()
        code = data.code.strip()
        new_password = data.new_password

        # Validate new password
        if len(new_password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

        # Verify the code again
        res = supabase.table("password_reset_codes")\
            .select("*, users!inner(id)")\
            .eq("email", email)\
            .eq("code", code)\
            .gt("expires_at", datetime.now().isoformat())\
            .execute()

        if not res.data:
            raise HTTPException(status_code=400, detail="Invalid or expired reset code")

        reset_record = res.data[0]
        user_id = reset_record.get("user_id")

        # Hash the new password
        pwd_hash = hash_password(new_password)

        # Update the user's password
        supabase.table("users").update({
            "password_hash": pwd_hash,
            "updated_at": datetime.now().isoformat()
        }).eq("id", user_id).execute()

        # Delete the used reset code
        supabase.table("password_reset_codes").delete().eq("email", email).execute()

        # Invalidate all existing sessions for this user (security measure)
        try:
            supabase.table("user_sessions").delete().eq("user_id", user_id).execute()
        except Exception:
            pass  # Non-critical

        return {"success": True, "message": "Password reset successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reset password error: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset password")


@app.post('/auth/google/login')
async def auth_google_login(payload: GoogleIdTokenRequest):
    try:
        token_data = await _verify_google_id_token(payload.id_token)
        
        email = token_data.get('email', '').lower()
        given_name = token_data.get('given_name') or ''
        family_name = token_data.get('family_name') or ''
        
        if not email:
            raise HTTPException(status_code=400, detail="Google token missing email")

        res = supabase.table("users").select("*").eq("email", email).execute()
        if not res.data:
            
            return {
                "success": True,
                "message": "Google account not registered",
                "needs_registration": True,
                "email": email,
                "given_name": given_name,
                "family_name": family_name,
            }
        user_row = res.data[0]

        token = create_user_session(user_row.get("id"))
        if not token:
            
            raise HTTPException(status_code=500, detail="Failed to create session")
        try:
            supabase.table("users").update({"last_login": datetime.now().isoformat()}).eq("id", user_row.get("id")).execute()
        except Exception as e:
            print(f"[auth.google.login] warning: failed to update last_login: {e}")
        user_row.pop('password_hash', None)
        return {
                "success": True,
                "message": "Login successful",
                "user": user_row,
                "session_token": token,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        
        
        debug = os.getenv("DEBUG", "0") in ("1", "true", "True")
        if debug:
            raise HTTPException(status_code=500, detail=f"Google login failed: {e}")
        else:
            raise HTTPException(status_code=500, detail="Google login failed")

@app.post('/auth/google/register')
async def auth_google_register(data: GoogleRegisterRequest):
    """Register a new user via Google ID token.
    Enhanced with detailed logging and clearer error surfaces for frontend debugging.
    """
    try:
        print("[google.register] start")
        token_data = await _verify_google_id_token(data.id_token)
        print(f"[google.register] token aud={token_data.get('aud')} email={token_data.get('email')} sub={token_data.get('sub')}")
        email = token_data.get('email', '').lower()
        if email != str(data.email).lower():
            raise HTTPException(status_code=400, detail="Email mismatch between Google token and registration data")
        existing = supabase.table("users").select("id").eq("email", email).execute()
        if existing.data:
            raise HTTPException(status_code=400, detail="An account with this email already exists")
        random_secret = _secrets_for_google.token_urlsafe(32)
        pwd_hash = hash_password(random_secret)
        metrics = calculate_extended_metrics(
            age=int(data.age),
            height_feet=int(data.height_feet),
            height_inches=int(data.height_inches),
            weight_lbs=float(data.weight_lbs),
            gender=data.gender,
            activity_level=data.activity_level,
            goal=data.goal,
            goal_weight_lbs=float(data.goal_weight_lbs) if data.goal_weight_lbs else None,
            weight_change_speed=data.weight_change_speed,
        )
        payload = {
            "username": data.username,
            "email": email,
            "password_hash": pwd_hash,
            "age": int(data.age),
            "height_feet": int(data.height_feet),
            "height_inches": int(data.height_inches),
            "weight_lbs": float(data.weight_lbs),
            "gender": data.gender,
            "activity_level": data.activity_level,
            "goal": data.goal,
            "university": data.university,
            "dietary_preference": (data.dietary_preference or None),
            "bmr": metrics.get("bmr"),
            "tdee": metrics.get("tdee"),
            "created_at": datetime.now().isoformat(),
            # New onboarding fields
            "birthday": data.birthday,
            "goal_weight_lbs": float(data.goal_weight_lbs) if data.goal_weight_lbs else None,
            "weight_change_speed": data.weight_change_speed,
            "accomplishments": data.accomplishments,
            "obstacles": data.obstacles,
            "school_id": data.school_id,
            "dining_halls": data.dining_halls,
            "dietary_restrictions": data.dietary_restrictions,
            "attribution_source": data.attribution_source,
            "apple_health_connected": data.apple_health_connected,
            "notifications_enabled": data.notifications_enabled,
            "auth_provider": "google",
            # Extended computed metrics
            "height_cm": metrics.get("height_cm"),
            "weight_kg": metrics.get("weight_kg"),
            "goal_weight_kg": metrics.get("goal_weight_kg"),
            "bmi": metrics.get("bmi"),
            "daily_calorie_target": metrics.get("daily_calorie_target"),
            "estimated_weeks_to_goal": metrics.get("estimated_weeks_to_goal"),
            "target_date": metrics.get("target_date"),
        }
        print(f"[google.register] inserting user payload keys={list(payload.keys())}")
        # Same username-collision handling as auth_register: try a few suffixes
        max_attempts = 5
        base_username = (payload.get("username") or "").strip()
        if not base_username:
            base_username = email.split("@")[0] if email else "user"

        user_row = None
        for attempt in range(max_attempts):
            try:
                try_username = base_username if attempt == 0 else f"{base_username}-{attempt}"
                payload["username"] = try_username
                inserted = supabase.table("users").insert(payload).execute()
                insert_error = getattr(inserted, "error", None)
                if insert_error:
                    msg = str(insert_error)
                    if ("users_username_key" in msg) or ("Key (username)" in msg) or ("duplicate key value" in msg):
                        logger.info("[google.register] username collision for %s attempt=%s: %s", try_username, attempt + 1, msg)
                        continue
                    raise HTTPException(status_code=500, detail="Registration failed")
                if inserted.data and len(inserted.data) > 0:
                    user_row = inserted.data[0]
                    break
                logger.warning("[google.register] insert returned no data for username=%s on attempt=%s", try_username, attempt + 1)
            except Exception as e:
                emsg = str(e)
                if ("23505" in emsg) or ("users_username_key" in emsg) or ("Key (username)" in emsg) or ("duplicate key value" in emsg):
                    logger.info("[google.register] caught unique-constraint username collision attempt=%s username=%s: %s", attempt + 1, payload.get("username"), emsg)
                    continue
                print(f"[google.register] insert error (non-username): {e}")
                raise HTTPException(status_code=500, detail="Google registration failed")

        if not user_row:
            raise HTTPException(status_code=500, detail="Failed to create user after resolving username conflicts")
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")
        user_row["session_token"] = token
        user_row.pop('password_hash', None)
        print(f"[google.register] success user_id={user_row.get('id')}")
        return {
            "success": True,
            "message": "Registration successful",
            "user": user_row,
            "session_token": token,
        }
    except HTTPException as he:
        print(f"[google.register] http_error status={he.status_code} detail={he.detail}")
        raise
    except Exception as e:
        print(f"[google.register] unhandled_error: {e}")
        raise HTTPException(status_code=500, detail="Google registration failed")


@app.post('/auth/apple/login')
async def auth_apple_login(payload: AppleIdTokenRequest):
    """Login with Apple ID token.

    Verifies the Apple identity token and either:
    - Returns the user with a session token if they exist
    - Returns needs_registration: true if the user needs to register
    """
    try:
        print("[apple.login] start")
        token_data = await _verify_apple_id_token(payload.id_token)
        print(f"[apple.login] token sub={token_data.get('sub')} email={token_data.get('email')}")

        # Apple uses 'sub' as the unique identifier
        apple_sub = token_data.get('sub')
        # Email may or may not be present (user can choose to hide it)
        email = token_data.get('email', '').lower() if token_data.get('email') else None

        # Apple may provide user info in payload.user_data only on first authorization
        user_data = payload.user_data or {}
        given_name = user_data.get('name', {}).get('firstName', '') if user_data else ''
        family_name = user_data.get('name', {}).get('lastName', '') if user_data else ''

        if not apple_sub:
            raise HTTPException(status_code=400, detail="Apple token missing subject identifier")

        # First, try to find user by apple_sub (unique Apple identifier)
        res = supabase.table("users").select("*").eq("apple_sub", apple_sub).execute()

        # If not found by apple_sub but email is provided, try finding by email
        if not res.data and email:
            res = supabase.table("users").select("*").eq("email", email).execute()

        if not res.data:
            # User not found - needs registration
            return {
                "success": True,
                "message": "Apple account not registered",
                "needs_registration": True,
                "email": email,
                "apple_sub": apple_sub,
                "given_name": given_name,
                "family_name": family_name,
            }

        user_row = res.data[0]

        # If user exists but doesn't have apple_sub linked, link it now
        if not user_row.get("apple_sub") and apple_sub:
            try:
                supabase.table("users").update({"apple_sub": apple_sub}).eq("id", user_row.get("id")).execute()
                user_row["apple_sub"] = apple_sub
            except Exception as e:
                print(f"[apple.login] warning: failed to link apple_sub: {e}")

        # Store real_user_status if provided (only sent on first auth)
        if payload.real_user_status is not None and not user_row.get("real_user_status"):
            try:
                real_status_map = {0: "unsupported", 1: "unknown", 2: "likelyReal"}
                status_str = real_status_map.get(payload.real_user_status, "unknown")
                supabase.table("users").update({"real_user_status": status_str}).eq("id", user_row.get("id")).execute()
                user_row["real_user_status"] = status_str
            except Exception as e:
                print(f"[apple.login] warning: failed to store real_user_status: {e}")

        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        try:
            supabase.table("users").update({"last_login": datetime.now().isoformat()}).eq("id", user_row.get("id")).execute()
        except Exception as e:
            print(f"[apple.login] warning: failed to update last_login: {e}")

        print(f"[apple.login] success user_id={user_row.get('id')}")
        user_row.pop('password_hash', None)
        return {
            "success": True,
            "message": "Login successful",
            "user": user_row,
            "session_token": token,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        debug = os.getenv("DEBUG", "0") in ("1", "true", "True")
        if debug:
            raise HTTPException(status_code=500, detail=f"Apple login failed: {e}")
        else:
            raise HTTPException(status_code=500, detail="Apple login failed")


@app.post('/auth/apple/register')
async def auth_apple_register(data: AppleRegisterRequest):
    """Register a new user via Apple ID token.

    Creates a new user account linked to the Apple identifier.
    """
    try:
        print("[apple.register] start")
        token_data = await _verify_apple_id_token(data.id_token)
        print(f"[apple.register] token sub={token_data.get('sub')} email={token_data.get('email')}")

        apple_sub = token_data.get('sub')
        token_email = token_data.get('email', '').lower() if token_data.get('email') else None

        if not apple_sub:
            raise HTTPException(status_code=400, detail="Apple token missing subject identifier")

        # Validate email - if token has email, it should match the provided email
        # If token doesn't have email (user chose to hide), trust the provided email
        if token_email and token_email != str(data.email).lower():
            raise HTTPException(status_code=400, detail="Email mismatch between Apple token and registration data")

        email = str(data.email).lower()

        # Check if user already exists by apple_sub or email
        existing_by_sub = supabase.table("users").select("id").eq("apple_sub", apple_sub).execute()
        if existing_by_sub.data:
            raise HTTPException(status_code=400, detail="An account with this Apple ID already exists")

        existing_by_email = supabase.table("users").select("id").eq("email", email).execute()
        if existing_by_email.data:
            raise HTTPException(status_code=400, detail="An account with this email already exists")

        # Generate a random password hash (user will authenticate via Apple)
        random_secret = _secrets_for_google.token_urlsafe(32)
        pwd_hash = hash_password(random_secret)

        metrics = calculate_extended_metrics(
            age=int(data.age),
            height_feet=int(data.height_feet),
            height_inches=int(data.height_inches),
            weight_lbs=float(data.weight_lbs),
            gender=data.gender,
            activity_level=data.activity_level,
            goal=data.goal,
            goal_weight_lbs=float(data.goal_weight_lbs) if data.goal_weight_lbs else None,
            weight_change_speed=data.weight_change_speed,
        )

        payload = {
            "username": data.username,
            "email": email,
            "password_hash": pwd_hash,
            "apple_sub": apple_sub,  # Link Apple identifier
            "age": int(data.age),
            "height_feet": int(data.height_feet),
            "height_inches": int(data.height_inches),
            "weight_lbs": float(data.weight_lbs),
            "gender": data.gender,
            "activity_level": data.activity_level,
            "goal": data.goal,
            "university": data.university,
            "dietary_preference": (data.dietary_preference or None),
            "bmr": metrics.get("bmr"),
            "tdee": metrics.get("tdee"),
            "created_at": datetime.now().isoformat(),
            # New onboarding fields
            "birthday": data.birthday,
            "goal_weight_lbs": float(data.goal_weight_lbs) if data.goal_weight_lbs else None,
            "weight_change_speed": data.weight_change_speed,
            "accomplishments": data.accomplishments,
            "obstacles": data.obstacles,
            "school_id": data.school_id,
            "dining_halls": data.dining_halls,
            "dietary_restrictions": data.dietary_restrictions,
            "attribution_source": data.attribution_source,
            "apple_health_connected": data.apple_health_connected,
            "notifications_enabled": data.notifications_enabled,
            "real_user_status": {0: "unsupported", 1: "unknown", 2: "likelyReal"}.get(data.real_user_status, "unknown") if data.real_user_status is not None else None,
            "referred_by_code": data.referral_code,
            "auth_provider": "apple",
            # Extended computed metrics
            "height_cm": metrics.get("height_cm"),
            "weight_kg": metrics.get("weight_kg"),
            "goal_weight_kg": metrics.get("goal_weight_kg"),
            "bmi": metrics.get("bmi"),
            "daily_calorie_target": metrics.get("daily_calorie_target"),
            "estimated_weeks_to_goal": metrics.get("estimated_weeks_to_goal"),
            "target_date": metrics.get("target_date"),
        }

        print(f"[apple.register] inserting user payload keys={list(payload.keys())}")

        # Username collision handling (same as Google/email registration)
        max_attempts = 5
        base_username = (payload.get("username") or "").strip()
        if not base_username:
            base_username = email.split("@")[0] if email else "user"

        user_row = None
        for attempt in range(max_attempts):
            try:
                try_username = base_username if attempt == 0 else f"{base_username}-{attempt}"
                payload["username"] = try_username
                inserted = supabase.table("users").insert(payload).execute()
                insert_error = getattr(inserted, "error", None)
                if insert_error:
                    msg = str(insert_error)
                    if ("users_username_key" in msg) or ("Key (username)" in msg) or ("duplicate key value" in msg):
                        logger.info("[apple.register] username collision for %s attempt=%s: %s", try_username, attempt + 1, msg)
                        continue
                    raise HTTPException(status_code=500, detail="Registration failed")
                if inserted.data and len(inserted.data) > 0:
                    user_row = inserted.data[0]
                    break
                logger.warning("[apple.register] insert returned no data for username=%s on attempt=%s", try_username, attempt + 1)
            except Exception as e:
                emsg = str(e)
                if ("23505" in emsg) or ("users_username_key" in emsg) or ("Key (username)" in emsg) or ("duplicate key value" in emsg):
                    logger.info("[apple.register] caught unique-constraint username collision attempt=%s username=%s: %s", attempt + 1, payload.get("username"), emsg)
                    continue
                print(f"[apple.register] insert error (non-username): {e}")
                raise HTTPException(status_code=500, detail="Apple registration failed")

        if not user_row:
            raise HTTPException(status_code=500, detail="Failed to create user after resolving username conflicts")

        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        user_row["session_token"] = token
        user_row.pop('password_hash', None)
        print(f"[apple.register] success user_id={user_row.get('id')}")

        # If a referral code was provided, create a pending referral event
        referral_reward_granted = False
        if data.referral_code:
            try:
                code_res = supabase.table("referral_codes").select("*").eq("code", data.referral_code.upper()).eq("is_active", True).execute()
                if code_res.data:
                    code_row = code_res.data[0]
                    referrer_id = code_row["user_id"]
                    new_user_id = user_row["id"]
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
                            # Grant reward immediately instead of waiting for first meal plan
                            reward_ok = await check_and_fulfill_referral(str(new_user_id))
                            referral_reward_granted = reward_ok is True
            except Exception as e:
                print(f"[apple.register] warning: failed to process referral code: {e}")

        return {
            "success": True,
            "message": "Registration successful",
            "user": user_row,
            "session_token": token,
            "referral_reward_granted": referral_reward_granted,
        }
    except HTTPException as he:
        print(f"[apple.register] http_error status={he.status_code} detail={he.detail}")
        raise
    except Exception as e:
        print(f"[apple.register] unhandled_error: {e}")
        raise HTTPException(status_code=500, detail="Apple registration failed")


@app.post('/auth/google/initiate', summary='Initiate Google OAuth flow for sign-in/sign-up')
async def initiate_google_auth():
    """
    Start Google OAuth flow for authentication.
    Returns authorization URL for user to visit.
    This is similar to the calendar OAuth flow but for user authentication.
    """
    try:
        # Get Google OAuth client credentials
        google_client_id = os.getenv("GOOGLE_CLIENT_ID")
        google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        redirect_uri = os.getenv("GOOGLE_AUTH_REDIRECT_URI") or os.getenv("GOOGLE_REDIRECT_URI")
        
        if not google_client_id or not google_client_secret:
            logger.error("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET")
            raise HTTPException(status_code=500, detail="Server configuration error")
        
        if not redirect_uri:
            # Default to deployed backend callback URL
            redirect_uri = "https://dormdish-backend-git-test-raghav-mehtas-projects-9e41f86c.vercel.app/auth/google/callback"
        
        # Generate a random state for CSRF protection
        import secrets as sec
        state = sec.token_urlsafe(32)
        
        # Build authorization URL
        from urllib.parse import urlencode
        
        auth_params = {
            'client_id': google_client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'openid profile email',
            'access_type': 'offline',
            'prompt': 'consent',
            'state': state
        }
        
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(auth_params)}"
        
        logger.info(f"[auth.google.initiate] Created auth URL with state: {state[:20]}...")
        
        return {
            "success": True,
            "authorization_url": auth_url,
            "state": state
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to initiate Google auth: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to initiate authorization")


hi = "hi"
class UpdateProfileRequest(BaseModel):
    username: Optional[str] = None
    age: Optional[int] = None
    height_feet: Optional[int] = None
    height_inches: Optional[int] = None
    weight_lbs: Optional[float] = None
    gender: Optional[str] = None
    activity_level: Optional[str] = None
    goal: Optional[str] = None
    university: Optional[str] = None

    dietary_preference: Optional[str] = None
    # New onboarding fields
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

class TrackWeightRequest(BaseModel):
    weight_lbs: float
    date: Optional[str] = None  # ISO format date, defaults to today if not provided

class MealPlanRequest(BaseModel):
    dining_hall: str
    date: str
    exclude_meals: Optional[List[str]] = None
    restaurant: Optional[str] = None  # For Atrium sub-restaurant filtering

VALID_MEALS = {"breakfast", "lunch", "dinner"}

class SingleMealRequest(BaseModel):
    meal: str              # "breakfast", "lunch", or "dinner"
    dining_hall: str
    date: str
    restaurant: Optional[str] = None  # For Atrium sub-restaurant

class MealPlanFeedbackRequest(BaseModel):
    generation_id: str
    liked: bool

class SaveMealPlanRequest(BaseModel):
    """Request to save or update a meal plan"""
    dining_hall: str
    date: str
    meal_plan_data: dict  # Contains meals, totals, goals, etc.
    plan_id: Optional[str] = None  # If provided, updates existing plan

# ==================== PREFERENCE MANAGEMENT MODELS ====================

class FavoriteRequest(BaseModel):
    item_name: str
    item_type: str  # "meal_plan" or "food_item"
    meal_type: str  # "breakfast", "lunch", "dinner"
    meal_plan_id: Optional[str] = None

class DislikeRequest(BaseModel):
    item_name: str
    item_type: str  # "meal_plan" or "food_item"
    meal_type: str
    reason: Optional[str] = None

class TokenData(BaseModel):
    user_id: str

class MealPlanItemUpdate(BaseModel):
    """Individual meal item for editing"""
    name: str
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    serving_size: Optional[str] = None
    servings: Optional[float] = 1.0

class PushTokenRequest(BaseModel):
    """Request model for registering push notification tokens"""
    token: str
    user_id: str
    platform: str  # 'mobile', 'ios', 'android'

@app.get("/health", summary="Health Check")
async def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        supabase.table("users").select("id").limit(1).execute()
        return {
            "status": "healthy",
            "database": "connected",
            "revenuecat_configured": bool(REVENUECAT_SECRET_API_KEY),
            "revenuecat_webhook_configured": bool(REVENUECAT_WEBHOOK_AUTH_KEY),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }
        )


@app.get("/loaderio-e4d6abce9b7ba70cd20b89ccdd7d82c4.txt", summary="Loader.io verification")
async def loaderio_verification():
    """Loader.io verification endpoint"""
    return PlainTextResponse(content="loaderio-e4d6abce9b7ba70cd20b89ccdd7d82c4")


# ==================== CRON JOBS ====================

@app.post("/cron/refresh-menus", summary="Refresh all dining hall menus (cron job)")
@app.get("/cron/refresh-menus", summary="Refresh all dining hall menus (cron job)")
async def cron_refresh_menus(
    request: Request,
    secret: Optional[str] = Query(None),  # Allow ?secret=xxx for easy testing
    x_cron_secret: Optional[str] = Header(None, alias="X-Cron-Secret")
):
    """
    Pre-fetch and cache all dining hall menus for today.
    Called by Vercel Cron at 5 AM ET daily.

    Security: Allows Vercel cron calls, or manual calls with secret.
    """
    # Check if this is a Vercel cron call (has special header)
    is_vercel_cron = request.headers.get("x-vercel-cron") is not None

    # Verify: either Vercel cron, or valid secret in header/query
    if not is_vercel_cron:
        cron_secret = os.getenv("CRON_SECRET")
        if not cron_secret:
            logger.error("CRON_SECRET env var not set - rejecting cron request")
            raise HTTPException(status_code=500, detail="Server configuration error")
        secret_valid = (x_cron_secret == cron_secret) or (secret == cron_secret)
        if not secret_valid:
            raise HTTPException(status_code=401, detail="Unauthorized - pass ?secret=YOUR_CRON_SECRET")

    print("🕐 [CRON] Starting daily menu refresh...")
    start_time = time.time()

    # Get today's date for each timezone
    rutgers_date = _rutgers_today_str()
    hopkins_date = datetime.utcnow().strftime("%Y-%m-%d")

    results = []
    success_count = 0
    fail_count = 0

    for university, hall_slug, is_foodpro in ALL_DINING_HALLS:
        date = rutgers_date if university == "rutgers" else hopkins_date
        result = await fetch_and_cache_menu(university, hall_slug, date, is_foodpro)
        results.append(result)
        if result.get("success"):
            success_count += 1
        else:
            fail_count += 1

    elapsed = time.time() - start_time
    print(f"✅ [CRON] Menu refresh complete: {success_count} success, {fail_count} failed in {elapsed:.1f}s")

    # Create Gemini context caches for refreshed halls
    cache_summary = {"created": 0, "failed": 0, "cleaned_up": 0}
    try:
        from menu_cache import create_caches_for_date
        refreshed_halls = [
            (university, hall_slug)
            for idx, (university, hall_slug, _is_foodpro) in enumerate(ALL_DINING_HALLS)
            if idx < len(results) and results[idx].get("success")
        ]
        if refreshed_halls:
            cache_summary = await create_caches_for_date(rutgers_date, refreshed_halls)
            print(f"[CRON] Gemini caches: {cache_summary['created']} created, {cache_summary['failed']} failed, {cache_summary['cleaned_up']} stale cleaned")
    except Exception as e:
        print(f"[CRON] Gemini cache creation failed (non-fatal): {e}")

    return {
        "success": True,
        "message": f"Refreshed {success_count}/{len(ALL_DINING_HALLS)} menus",
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
        "cache_summary": cache_summary,
    }


@app.post('/api/register-push-token', summary='Register device push notification token')
async def register_push_token(data: PushTokenRequest, current_user=Depends(get_current_user)):
    """
    Register or update a user's push notification token.
    This endpoint stores the Expo push token for sending notifications.
    """
    # Verify the caller owns this user_id
    if str(data.user_id) != str(current_user.get("id")):
        raise HTTPException(status_code=403, detail="Cannot register push token for another user")
    try:
        # Validate token format
        if not data.token or not data.token.startswith('ExponentPushToken['):
            raise HTTPException(status_code=400, detail="Invalid Expo push token format")
        
        # Check if token already exists for this user
        existing = supabase.table('push_tokens')\
            .select('id')\
            .eq('user_id', data.user_id)\
            .eq('token', data.token)\
            .execute()
        
        now = datetime.now().isoformat()
        
        if existing.data and len(existing.data) > 0:
            # Update existing token
            supabase.table('push_tokens')\
                .update({
                    'active': True,
                    'updated_at': now,
                    'platform': data.platform
                })\
                .eq('id', existing.data[0]['id'])\
                .execute()
            
            logger.info(f"Updated push token for user {data.user_id}")
        else:
            # Deactivate old tokens for this user (keep only one active)
            supabase.table('push_tokens')\
                .update({'active': False})\
                .eq('user_id', data.user_id)\
                .execute()
            
            # Insert new token
            supabase.table('push_tokens').insert({
                'user_id': data.user_id,
                'token': data.token,
                'platform': data.platform,
                'active': True,
                'created_at': now,
                'updated_at': now
            }).execute()
            
            logger.info(f"Registered new push token for user {data.user_id}")
        
        return {"success": True, "message": "Push token registered successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to register push token: {e}")
        raise HTTPException(status_code=500, detail="Failed to register push token")


@app.api_route('/api/send-daily-reminders', methods=['GET', 'POST'], summary='Send daily meal plan reminders (admin/cron only)')
async def send_daily_reminders(
    request: Request,
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="X-Cron-Secret")
):
    """
    Send reminder notifications to all users who haven't created their meal plan today.
    This endpoint should be called by a cron job or scheduled task.
    Accepts both GET and POST for Vercel Cron compatibility.
    """
    # Verify: either Vercel cron, or valid secret
    is_vercel_cron = request.headers.get("x-vercel-cron") is not None
    if not is_vercel_cron:
        cron_secret = os.getenv("CRON_SECRET")
        if not cron_secret:
            logger.error("CRON_SECRET env var not set - rejecting cron request")
            raise HTTPException(status_code=500, detail="Server configuration error")
        secret_valid = (x_cron_secret == cron_secret) or (secret == cron_secret)
        if not secret_valid:
            raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        # Get all users with active push tokens
        response = supabase.table('push_tokens')\
            .select('user_id')\
            .eq('active', True)\
            .execute()
        
        if not response.data:
            return {"success": True, "message": "No active push tokens found", "sent": 0}
        
        # Get unique user IDs
        user_ids = list(set(token['user_id'] for token in response.data))
        
        sent_count = 0
        failed_count = 0
        
        # Send reminders to each user
        for user_id in user_ids:
            try:
                success = await send_meal_plan_reminder_notification(user_id)
                if success:
                    sent_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(f"Failed to send reminder to user {user_id}: {e}")
                failed_count += 1
        
        logger.info(f"Daily reminders sent: {sent_count} successful, {failed_count} failed/skipped")
        
        return {
            "success": True,
            "message": "Daily reminders processed",
            "sent": sent_count,
            "failed": failed_count,
            "total_users": len(user_ids)
        }
        
    except Exception as e:
        logger.error(f"Failed to send daily reminders: {e}")
        raise HTTPException(status_code=500, detail="Failed to send daily reminders")


def _rutgers_today_str():
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


async def get_menu_from_supabase(university: str, dining_hall: str, date: str) -> Optional[dict]:
    """Check if menu exists in Supabase database without blocking the event loop."""

    def _fetch() -> Optional[dict]:
        try:
            menu_id = f"{university}_{dining_hall}_{date}"
            response = supabase.table("menus").select("*").eq("id", menu_id).execute()
            if response.data and len(response.data) > 0:
                print(f"📁 Found cached menu for {menu_id}")
                return response.data[0]["menu_data"]
            print(f"🔍 No cached menu found for {menu_id}")
            return None
        except Exception as exc:
            print(f"⚠️ Error checking Supabase: {exc}")
            return None

    return await asyncio.to_thread(_fetch)


async def save_menu_to_supabase(university: str, dining_hall: str, date: str, menu_data: dict, meal: str = None) -> bool:
    """Persist menu data to Supabase without blocking the event loop."""

    def _save() -> bool:
        try:
            menu_id = f"{university}_{dining_hall}_{date}_{meal}" if meal else f"{university}_{dining_hall}_{date}"
            hall_slug = menu_data.get("hall_slug", dining_hall)
            fetched_at = menu_data.get("fetched_at", datetime.now().isoformat())
            items_count = 0
            if "items" in menu_data:
                items_count = len(menu_data["items"])
            elif "menu" in menu_data and "items" in menu_data["menu"]:
                items_count = len(menu_data["menu"]["items"])
            if items_count == 0:
                print(f"⚠️ Skipping Supabase save for empty menu {menu_id}")
                return False
            print(f"� Saving menu with {items_count} items to Supabase")
            print(f"� Menu data keys: {list(menu_data.keys())}")
            stored_menu_data = menu_data.get("menu", menu_data)
            
            # DEBUG: Check if fat_g and carbs_g are in the items being saved
            if "items" in stored_menu_data and len(stored_menu_data["items"]) > 0:
                sample_item = stored_menu_data["items"][0]
                print(f"� SAMPLE ITEM BEFORE SAVE: {sample_item.get('name')}")
                print(f"� SAMPLE MACROS: protein={sample_item.get('protein_g')}, fat={sample_item.get('fat_g')}, carbs={sample_item.get('carbs_g')}, cal={sample_item.get('calories')}")
                
                # Count items with complete macros
                complete_count = sum(1 for item in stored_menu_data["items"] 
                                   if item.get('fat_g') is not None and item.get('carbs_g') is not None)
                print(f"💾 Items with fat_g and carbs_g: {complete_count}/{len(stored_menu_data['items'])}")
            else:
                print(f"⚠️ No items found in stored_menu_data")
                
            data_to_insert = {
                "id": menu_id,
                "university": UNIVERSITY_DISPLAY_NAMES.get(university, university),
                "dining_hall": dining_hall,
                "hall_slug": hall_slug,
                "date": date,
                "fetched_at": fetched_at,
                "menu_data": stored_menu_data,
                "created_at": datetime.now().isoformat(),
            }
            if meal:
                data_to_insert["meal"] = meal
            response = supabase.table("menus").upsert(data_to_insert).execute()
            print(f"✅ Saved menu to Supabase: {menu_id}")
            print(f"✅ Supabase response data length: {len(response.data) if response.data else 0}")
            return True
        except Exception as exc:
            print(f"⚠️ Error saving to Supabase: {exc}")
            print(f"⚠️ Error type: {type(exc)}")
            import traceback
            traceback.print_exc()
            return False

    return await asyncio.to_thread(_save)


# Dining hall mapping - shared between get_dining_hall_menu and cron jobs
DINING_HALL_MAPPING = {
    # Hopkins University
    "hopkins-cafe": ("hopkins", "hopkins-cafe"),
    "hopkins cafe": ("hopkins", "hopkins-cafe"),
    "hopkins": ("hopkins", "hopkins-cafe"),
    "nolans-on-33rd": ("hopkins", "nolans-on-33rd"),
    "nolans": ("hopkins", "nolans-on-33rd"),
    "nolan": ("hopkins", "nolans-on-33rd"),
    "peabody-institute": ("hopkins", "peabody-institute"),
    "peabody": ("hopkins", "peabody-institute"),

    # Rutgers University - FoodPro halls
    "busch": ("rutgers", "busch-dining-hall"),
    "busch-dining-hall": ("rutgers", "busch-dining-hall"),
    "busch dining hall": ("rutgers", "busch-dining-hall"),
    "neilson": ("rutgers", "neilson-dining-hall"),
    "neilson-dining-hall": ("rutgers", "neilson-dining-hall"),
    "neilson dining hall": ("rutgers", "neilson-dining-hall"),
    "nielson": ("rutgers", "neilson-dining-hall"),
    "nielson-dining-hall": ("rutgers", "neilson-dining-hall"),
    "nielson dining hall": ("rutgers", "neilson-dining-hall"),
    "cook-douglass": ("rutgers", "neilson-dining-hall"),
    "cook douglass": ("rutgers", "neilson-dining-hall"),
    "cook/douglass": ("rutgers", "neilson-dining-hall"),
    "livingston": ("rutgers", "livingston-dining-commons"),
    "livingston-dining-hall": ("rutgers", "livingston-dining-commons"),
    "livingston dining hall": ("rutgers", "livingston-dining-commons"),
    "livingston dining commons": ("rutgers", "livingston-dining-commons"),
    "the-atrium": ("rutgers", "the-atrium"),
    "the atrium": ("rutgers", "the-atrium"),
    "atrium": ("rutgers", "the-atrium"),

    # Rutgers University - Nutrislice halls
    "brower": ("rutgers", "brower-commons"),
    "brower-commons": ("rutgers", "brower-commons"),
    "brower commons": ("rutgers", "brower-commons"),
    "college-ave": ("rutgers", "brower-commons"),
    "college ave": ("rutgers", "brower-commons"),
    "college avenue": ("rutgers", "brower-commons"),
}

# Slug -> display name mapping for database storage
UNIVERSITY_DISPLAY_NAMES = {
    "rutgers": "Rutgers University",
    "hopkins": "Johns Hopkins University",
}

# FoodPro halls (Rutgers halls that use FoodPro scraper)
FOODPRO_HALLS = {"livingston-dining-commons", "busch-dining-hall", "neilson-dining-hall", "the-atrium"}

# All halls to refresh daily (university, hall_slug, is_foodpro)
ALL_DINING_HALLS = [
    ("rutgers", "busch-dining-hall", True),
    ("rutgers", "livingston-dining-commons", True),
    ("rutgers", "neilson-dining-hall", True),
    ("rutgers", "the-atrium", True),
    # Hopkins - commented out until deployment
    # ("hopkins", "hopkins-cafe", False),
    # ("hopkins", "nolans-on-33rd", False),
    # ("hopkins", "peabody-institute", False),
]

ATRIUM_CATEGORY_TO_RESTAURANT = {
    "pi pizza": "PI Pizza",
    "pi pizza toppings": "PI Pizza",
    "mezze proteins": "Mezze",
    "mezze bases": "Mezze",
    "mezze toppings and sides": "Mezze",
    "scarlet ginger": "Scarlet Ginger",
    "scarlet ginger noodle bowl": "Scarlet Ginger",
    "kings hawaiian": "Kings Hawaiian",
    "three chilies": "Three Chilies",
    "salad bar": "Salad Bar",
    "soups": "Soups",
    "grab and go": "Grab and Go",
}

ATRIUM_RESTAURANTS = [
    "PI Pizza", "Mezze", "Scarlet Ginger", "Kings Hawaiian",
    "Three Chilies", "Salad Bar", "Soups", "Grab and Go",
]

MEAL_PROPORTIONS = {"breakfast": 0.30, "lunch": 0.35, "dinner": 0.35}


async def get_dining_hall_menu(dining_hall: str, date: str) -> dict:
    """Get dining hall menu from cache only. Menus are pre-fetched by cron job."""

    dh_norm = dining_hall.strip().lower()
    if dh_norm == "nielson":
        dh_norm = "neilson"
    if dh_norm in {"nielson-dining-hall", "nielson dining hall"}:
        dh_norm = "neilson"
    dining_hall = dh_norm

    mapping = DINING_HALL_MAPPING.get(dining_hall)
    if not mapping:
        return {"error": f"Unknown dining hall: {dining_hall}"}
    university, hall_slug = mapping

    if not date or date.lower() == "today":
        date = _rutgers_today_str() if university == "rutgers" else datetime.utcnow().strftime("%Y-%m-%d")

    # Only use cached menu - no on-demand fetching to avoid timeouts
    cached_menu = await get_menu_from_supabase(university, hall_slug, date)
    if cached_menu is not None:
        print(f"✅ Using cached menu from Supabase for {hall_slug}")
        return cached_menu

    # No cached menu - return error (menus are pre-fetched by cron job)
    print(f"⚠️ No cached menu for {hall_slug} on {date} - waiting for cron refresh")
    return {
        "error": f"Menu not yet available for {hall_slug} on {date}. Menus are refreshed daily at 5 AM ET.",
        "retry_later": True
    }


async def fetch_and_cache_menu(university: str, hall_slug: str, date: str, is_foodpro: bool) -> dict:
    """Fetch menu from source and cache in Supabase. Used by cron job."""
    try:
        if is_foodpro:
            print(f"🌐 [CRON] Fetching FoodPro menu for {university}/{hall_slug}")
            menu_data = await fetch_foodpro_menu_rutgers(hall_slug, hall_slug, date)
        else:
            print(f"🌐 [CRON] Fetching Nutrislice menu for {university}/{hall_slug}")
            menu_data = await fetch_nutrislice_menu(university, hall_slug, hall_slug, date)

        if not menu_data.get("items"):
            print(f"⚠️ [CRON] No items for {hall_slug}, skipping cache")
            return {"success": False, "hall": hall_slug, "error": "No items returned"}

        save_result = await save_menu_to_supabase(university, hall_slug, date, menu_data)
        item_count = len(menu_data.get("items", []))
        print(f"✅ [CRON] Cached {item_count} items for {hall_slug}")

        # Save per-meal rows
        meal_sources = (menu_data.get("menu") or {}).get("meal_sources", [])
        all_items = menu_data.get("items", [])
        for meal_name in meal_sources:
            meal_items = [item for item in all_items if item.get("meal") == meal_name]
            if not meal_items:
                continue
            meal_menu_data = {
                **menu_data,
                "items": meal_items,
                "menu": {"items": meal_items, "meal_sources": [meal_name]},
            }
            await save_menu_to_supabase(university, hall_slug, date, meal_menu_data, meal=meal_name)
            print(f"✅ [CRON] Cached {len(meal_items)} items for {hall_slug}/{meal_name}")

        return {"success": True, "hall": hall_slug, "items": item_count}
    except Exception as e:
        print(f"❌ [CRON] Failed to fetch {hall_slug}: {e}")
        return {"success": False, "hall": hall_slug, "error": str(e)}


async def fetch_foodpro_menu_rutgers(dining_hall: str, hall_slug: str, date: str) -> dict:
    """Fetch menu from Rutgers FoodPro pickmenu pages and format like Nutrislice output.
    Now aggregates breakfast, lunch, and dinner by invoking the scraper per meal.
    """
    from extract import build_menu_url as fp_build_menu_url, fetch as fp_fetch, parse_menu as fp_parse_menu
    if not (fp_build_menu_url and fp_parse_menu):
        raise RuntimeError("FoodPro scraper not available")

    
    
    location_map = {
        "livingston-dining-commons": {"locationNum": "03", "locationName": "Livingston Dining Commons"},
        "busch-dining-hall": {"locationNum": "04", "locationName": "Busch Dining Hall"},
        "neilson-dining-hall": {"locationNum": "05", "locationName": "Neilson Dining Hall"},
        "the-atrium": {"locationNum": "07", "locationName": "The Atrium"},
    }
    params = location_map.get(hall_slug)
    if not params:
        raise ValueError(f"Unsupported Rutgers FoodPro hall: {hall_slug}")

    def _set_meal_in_url(url: str, meal: str) -> str:
        
        try:
            u = urlparse(url)
            qs = parse_qs(u.query)
            qs["mealName"] = [meal.capitalize()]
            new_q = urlencode(qs, doseq=True)
            return urlunparse(u._replace(query=new_q))
        except Exception:
            return url

    def _add_food_obj_from_new_schema(food: dict, items: List[dict], category: str = None):
        if not isinstance(food, dict):
            return
        name = food.get("name")
        if not name:
            return
        out_item: Dict[str, object] = {"name": title_case_name(name)}
        if category:
            out_item["category"] = category
        prot = food.get("g_protein")
        if prot is None:
            prot = food.get("protein_g")
        if prot is not None:
            out_item["protein_g"] = prot
        # include fat and carbs if present in new FoodPro schema
        fat = food.get("g_fat") or food.get("fat_g")
        if fat is not None:
            out_item["fat_g"] = fat
        carbs = food.get("g_carbs") or food.get("carbs_g") or food.get("carbohydrates")
        if carbs is not None:
            out_item["carbs_g"] = carbs
        cals = food.get("calories")
        if cals is not None:
            out_item["calories"] = cals
        sv = food.get("serving_size")
        if isinstance(sv, dict):
            amt = sv.get("serving_size_amount")
            unit = sv.get("serving_size_unit")
            parts = []
            if amt not in (None, ""):
                parts.append(str(amt))
            if unit not in (None, ""):
                parts.append(str(unit))
            if parts:
                out_item["serving_size"] = " ".join(parts)
        elif isinstance(sv, str) and sv:
            out_item["serving_size"] = sv
        items.append(out_item)

    async def _attempt_all_meals(location_num: str, location_name: str) -> dict:
        base_url = fp_build_menu_url(date, location_num, location_name)
        logger.debug("foodpro_base_url=%s", base_url)
        all_items: List[dict] = []
        meal_sources: List[str] = []
        warnings: List[str] = []

        
        try:
            weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
        except Exception:
            weekday = None
        if weekday is not None and weekday >= 5:
            meals_sequence = ("brunch", "dinner", "breakfast", "lunch")
        else:
            meals_sequence = ("breakfast", "brunch", "lunch", "dinner")

        # Helper function to fetch and parse a single meal
        async def _fetch_and_parse_meal(meal: str) -> tuple[str, List[dict], str | None]:
            """Fetch and parse a single meal, returns (meal_name, items, warning_or_none)"""
            meal_url = _set_meal_in_url(base_url, meal)
            logger.debug("foodpro_meal_request meal=%s url=%s", meal, meal_url)
            try:
                html_text = await _fetch_with_retries(meal_url)
            except Exception as exc:
                return (meal, [], f"Failed to fetch {meal}: {exc}")
            try:
                # Run synchronous parse_menu in thread pool to not block event loop
                # (parse_menu now uses ThreadPoolExecutor internally for parallel nutrition fetching)
                parsed = await asyncio.to_thread(fp_parse_menu, html_text, meal, date, meal_url)
            except Exception as exc:
                return (meal, [], f"Failed to parse {meal}: {exc}")

            items: List[dict] = []
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                for it in parsed.get("items", []):
                    if not isinstance(it, dict):
                        continue
                    name = it.get("name")
                    if not name:
                        continue

                    out_item: Dict[str, object] = {"name": title_case_name(name)}
                    if it.get("protein_g") is not None:
                        out_item["protein_g"] = it.get("protein_g")
                    if it.get("calories") is not None:
                        out_item["calories"] = it.get("calories")
                    if it.get("fat_g") is not None:
                        out_item["fat_g"] = it.get("fat_g")
                    if it.get("carbs_g") is not None:
                        out_item["carbs_g"] = it.get("carbs_g")
                    if it.get("serving_size"):
                        out_item["serving_size"] = it.get("serving_size")
                    if it.get("category"):
                        out_item["category"] = it["category"]
                    items.append(out_item)
            else:
                if isinstance(parsed, dict):
                    meal_map = parsed.get(meal) or {}
                    blocks = meal_map.get(date) or [] if isinstance(meal_map, dict) else []
                    if isinstance(blocks, list):
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue
                            block_category = block.get("category")
                            foods = block.get("foods") or []
                            if isinstance(foods, list):
                                for food in foods:
                                    _add_food_obj_from_new_schema(food, items, category=block_category)

            if items:
                return (meal, items, None)
            else:
                return (meal, [], f"No items found for {meal}")

        # Fetch all meals in parallel using asyncio.gather
        logger.info(f"[FoodPro] Fetching {len(meals_sequence)} meals in parallel: {meals_sequence}")
        results = await asyncio.gather(*[_fetch_and_parse_meal(meal) for meal in meals_sequence])

        # Process results and maintain meal order
        for meal, items, warning in results:
            if warning:
                warnings.append(warning)
            if items:
                meal_sources.append(meal)
                for item in items:
                    item["meal"] = meal
                all_items.extend(items)
                # DEBUG: Log first item with macros
                if items:
                    first = items[0]
                    print(f"🔍 FIRST ITEM from {meal}: {first.get('name')}")
                    print(f"🔍 MACROS: protein={first.get('protein_g')}, fat={first.get('fat_g')}, carbs={first.get('carbs_g')}, cal={first.get('calories')}")

        return {"items": all_items, "meal_sources": meal_sources, "warnings": warnings, "url": base_url}

    attempt = await _attempt_all_meals(params["locationNum"], params["locationName"])

    
    if hall_slug == "the-atrium" and not attempt["items"]:
        for alt_name in [
            "Atrium",  
            "The Atrium at Livingston",  
            "The Atrium at Livingston Campus",
        ]:
            try:
                logger.info("foodpro_atrium_retry alt_name=%s", alt_name)
                attempt = await _attempt_all_meals(params["locationNum"], alt_name)
                if attempt["items"]:
                    break
            except Exception as exc:
                logger.warning("foodpro_atrium_retry_failed alt_name=%s error=%s", alt_name, exc)

    
    all_items = attempt["items"]
    seen = set()
    unique_items: List[dict] = []
    for it in all_items:
        nm = (it.get("name") or '').strip()
        if nm and nm not in seen:
            seen.add(nm)
            unique_items.append(it)

    warnings: List[str] = attempt.get("warnings", [])
    if not unique_items:
        warnings.append("No items parsed from FoodPro")
        raise RuntimeError("FoodPro returned zero items")

    meal_sources = attempt.get("meal_sources", [])

    # Map categories to restaurants for Atrium
    present_restaurants: List[str] = []
    if hall_slug == "the-atrium":
        seen_restaurants: set = set()
        for item in unique_items:
            cat = (item.get("category") or "").strip().lower()
            restaurant = ATRIUM_CATEGORY_TO_RESTAURANT.get(cat)
            if restaurant:
                item["restaurant"] = restaurant
                if restaurant not in seen_restaurants:
                    seen_restaurants.add(restaurant)
                    present_restaurants.append(restaurant)

    # Strip category from items — only used for Atrium mapping above
    for item in unique_items:
        item.pop("category", None)

    result = {
        "university": "Rutgers University",
        "hall": dining_hall,
        "hall_slug": hall_slug,
        "date": date,
        "fetched_at": datetime.now().isoformat(),
        "items": unique_items,
        "menu": {"items": unique_items, "meal_sources": meal_sources or ["foodpro"]},
        "meta": {"warnings": warnings, "notes": ["source: FoodPro"]},
        "error": None,
    }
    if present_restaurants:
        result["restaurants"] = present_restaurants
        result["menu"]["restaurants"] = present_restaurants
    return result


async def fetch_nutrislice_menu(university: str, hall_slug: str, dining_hall: str, date: str) -> dict:
    """Fetch menu directly from Nutrislice API"""
    api_bases = {
        "rutgers": "https://rutgers.api.nutrislice.com/menu/api/weeks/school",
        "hopkins": "https://hopkinsdining.api.nutrislice.com/menu/api/weeks/school",
    }
    
    # Configure meal slugs per university and hall
    meal_configs = {
        "rutgers": {
            # Default meals for most Rutgers halls
            "default": ["breakfast", "lunch-test", "dinner"],
            # Neilson/Cook-Douglass specific meals
            "neilson-dining-hall": ["breakfast", "lunch-test", "dinner"],
            # Busch specific meals  
            "busch-dining-hall": ["breakfast", "lunch-test", "dinner"],
            # Brower specific meals
            "brower-commons": ["breakfast", "lunch-test", "dinner"],
        },
        "hopkins": {
            "nolans-on-33rd": ["nolans-breakfast", "nolans-dinner-2", "nolans-dinner"],
            "peabody-institute": ["peabody-breakfast", "peabody-lunch", "peabody-dinner"],
            "hopkins-cafe": ["hopkins-cafe-breakfast", "hopkins-cafe-lunch", "hopkins-cafe-dinner"],
        },
    }
    
    api_base = api_bases.get(university)
    if not api_base:
        raise ValueError(f"Unsupported university: {university}")
    
    if university == "hopkins":
        meal_slugs = meal_configs["hopkins"].get(hall_slug, [])
        if not meal_slugs:
            prefix = hall_slug.split("-")[0]
            meal_slugs = [f"{prefix}-breakfast", f"{prefix}-lunch", f"{prefix}-dinner"]
    elif university == "rutgers":
        # Use hall-specific config if available, otherwise use default
        meal_slugs = meal_configs["rutgers"].get(hall_slug, meal_configs["rutgers"]["default"])
    else:
        # Fallback for any other university
        meal_slugs = ["breakfast", "lunch", "dinner"]
    
    all_items: List[dict] = []
    meal_sources: List[str] = []
    warnings: List[str] = []
    for meal_slug in meal_slugs:
        try:
            items = await fetch_meal_items(api_base, hall_slug, meal_slug, date)
            if items:
                meal_name = meal_slug.split("-")[-1] if "-" in meal_slug else meal_slug
                if meal_name == "dinner-2":
                    meal_name = "lunch"
                elif meal_name == "lunch-test":
                    meal_name = "lunch"
                for item in items:
                    item["meal"] = meal_name
                all_items.extend(items)
                if meal_name not in meal_sources:
                    meal_sources.append(meal_name)
            else:
                warnings.append(f"No items found for {meal_slug}")
        except Exception as e:
            warnings.append(f"Failed to fetch {meal_slug}: {str(e)}")
    seen_items = set()
    unique_items: List[dict] = []
    for item in all_items:
        item_name = item.get("name", "")
        if item_name and item_name not in seen_items:
            seen_items.add(item_name)
            unique_items.append(item)
    
    
    if not unique_items:
        print(f"⚠️ WARNING: fetch_nutrislice_menu returned ZERO items for {university}/{hall_slug} on {date}")
        print(f"⚠️ Warnings: {warnings}")
        print(f"⚠️ Meal sources attempted: {meal_slugs}")
    else:
        print(f"✅ fetch_nutrislice_menu got {len(unique_items)} unique items")
    
    return {
        "university": UNIVERSITY_DISPLAY_NAMES.get(university, university),
        "hall": dining_hall,
        "hall_slug": hall_slug,
        "date": date,
        "fetched_at": datetime.now().isoformat(),
        "items": unique_items,
        "menu": {"items": unique_items, "meal_sources": meal_sources},
        "meta": {"warnings": warnings, "notes": []},
        "error": None,
    }


async def fetch_meal_items(api_base: str, hall_slug: str, meal_slug: str, date: str) -> list:
    """Fetch items for a specific meal from Nutrislice API"""
    year, month, day = date.split("-")
    url = f"{api_base}/{hall_slug}/menu-type/{meal_slug}/{year}/{month}/{day}/"
    logger.debug("nutrislice_fetch url=%s", url)
    try:
        client = _get_http_client()
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
        day_obj = None
        candidate_days = []
        if isinstance(data, dict):
            if isinstance(data.get("days"), list):
                candidate_days.extend(data["days"])
            if isinstance(data.get("week"), dict) and isinstance(data["week"].get("days"), list):
                candidate_days.extend(data["week"]["days"])
        for d in candidate_days:
            if isinstance(d, dict) and d.get("date") == date:
                day_obj = d
                break
        if not day_obj:
            return []
        items: List[dict] = []
        for key in ["menu_items", "items", "menuItems"]:
            if isinstance(day_obj.get(key), list):
                for item in day_obj[key]:
                    if isinstance(item, dict):
                        processed_item = process_menu_item(item)
                        if processed_item:
                            items.append(processed_item)
        for cat_key in ["menu_groups", "groups", "categories", "menuCategories", "stations"]:
            cats = day_obj.get(cat_key)
            if isinstance(cats, list):
                for cat in cats:
                    if not isinstance(cat, dict):
                        continue
                    for key in ["items", "menu_items", "menuItems"]:
                        if isinstance(cat.get(key), list):
                            for item in cat[key]:
                                if isinstance(item, dict):
                                    processed_item = process_menu_item(item)
                                    if processed_item:
                                        items.append(processed_item)
        return items
    except Exception as e:
        logger.warning("nutrislice_fetch_failed url=%s error=%s", url, e)
        return []


def process_menu_item(item: dict) -> Optional[dict]:
    """Process a menu item and extract nutritional information including fats and carbs"""
    name = None
    if isinstance(item.get("food"), dict):
        name = item["food"].get("name")
    if not name:
        name = item.get("name") or item.get("item_name")
    if not name:
        return None
    
    nutrition: Dict[str, Optional[float]] = {}
    
    # Extract nutrition from nested 'food' object (Nutrislice format)
    if isinstance(item.get("food"), dict):
        food = item["food"]
        rni = food.get("rounded_nutrition_info") or {}
        serving_info = food.get("serving_size_info") or {}
        nutrition = {
            "protein_g": rni.get("g_protein"),
            "calories": rni.get("calories"),
            "fat_g": rni.get("g_fat"),  # Extract fat
            "carbs_g": rni.get("g_carbs"),  # Extract carbs
            "serving_size": f"{serving_info.get('serving_size_amount', '')} {serving_info.get('serving_size_unit', '')}".strip(),
        }
    else:
        # Extract from flat structure (alternative format)
        nutrition = {
            "protein_g": item.get("g_protein") or item.get("protein"),
            "calories": item.get("calories") or item.get("kcal"),
            "fat_g": item.get("g_fat") or item.get("fat"),  # Extract fat
            "carbs_g": item.get("g_carbs") or item.get("carbs") or item.get("carbohydrates"),  # Extract carbs
            "serving_size": item.get("serving_size") or item.get("portion_size"),
        }
    
    result: Dict[str, object] = {"name": title_case_name(name)}
    
    # Add required macros (protein and calories)
    if nutrition.get("protein_g") is not None:
        try:
            result["protein_g"] = float(nutrition["protein_g"])
        except (ValueError, TypeError):
            pass
    if nutrition.get("calories") is not None:
        try:
            result["calories"] = float(nutrition["calories"])
        except (ValueError, TypeError):
            pass
    if nutrition.get("serving_size"):
        result["serving_size"] = str(nutrition["serving_size"])
    
    # Add optional macros (fats and carbs) - IMPORTANT for complete nutrition tracking
    if nutrition.get("fat_g") is not None:
        try:
            result["fat_g"] = float(nutrition.get("fat_g"))
        except (ValueError, TypeError):
            pass

    if nutrition.get("carbs_g") is not None:
        try:
            result["carbs_g"] = float(nutrition.get("carbs_g"))
        except (ValueError, TypeError):
            pass
    
    return result





def build_meal_plan_system_prompt(allowed_items_with_macros: List[dict]) -> str:
    """Construct the system prompt for the meal plan model.
    Emphasizes a balanced, realistic nutritionist approach (main + sides + sensible pairings)
    while keeping the JSON schema EXACTLY the same. Less rigid wording, still enforces
    internal numeric consistency and allowed item usage.
    """
    lines: List[str] = []
    for it in allowed_items_with_macros:
        nm = (it.get("name") or "").strip()
        if not nm:
            continue
        cal = it.get("calories")
        prot = it.get("protein_g")
        fat = it.get("fat_g")
        carbs = it.get("carbs_g")
        if cal is None or prot is None:
            continue
        try:
            # Include fats and carbs if available
            parts = [f"{nm}", f"{int(round(cal))} cal", f"{int(round(prot))}g protein"]
            if fat is not None:
                parts.append(f"{int(round(fat))}g fat")
            if carbs is not None:
                parts.append(f"{int(round(carbs))}g carbs")
            lines.append(" | ".join(parts))
        except Exception:
            continue
    allowed_block = "\n".join(lines) if lines else "(none)"

    prompt_lines: List[str] = [
        "You are a pragmatic, encouraging university dining hall nutritionist & meal designer.",
        "Create ONE daily meal plan (breakfast, lunch, dinner) using ONLY the allowed items. Think in terms of a plate: a main + sides. If an item requires a companion (pasta → sauce, sandwich → bread + protein, salad → dressing), include it. Avoid absurd pairings.",
        "",
        "STRICT OUTPUT: Return a single JSON object with these exact top-level keys (no extras, no markdown):",
        "",
        "{",
        ' "totals": { "calories": "<int>", "protein_g": "<int>", "fat_g": "<int>", "carbs_g": "<int>" },',
        ' "meals": {',
        '   "breakfast": { "description": "<string>", "items": [ "<meal_item>", ... ] },',
        '   "lunch": { "description": "<string>", "items": [ "<meal_item>", ... ] },',
        '   "dinner": { "description": "<string>", "items": [ "<meal_item>", ... ] }',
        ' },',
        ' "explanation": "<string>"',
        "}",
        "",
        "Meal item schema:",
        "",
        '{ "name": "<exact item>", "servings": "<number>", "calories": "<number>", "protein_g": "<number>", "fat_g": "<number>", "carbs_g": "<number>", "serving_size": "<string|null>" }',
        "",
        "Rules",
        "",
        "3 meals only. Each: short description + 2–4 items (main protein + carb/veg/sauce). A single larger item is OK if realistic.",
        "",
        "Variety: avoid repeating exact items more than once/day.",
        "",
        "Don’t stack multiple meats in one meal. Plant proteins may complement sensibly.",
        "",
        "If pairing item missing from list, proceed without fabrication (note in explanation).",
        "",
        "Respect dietary prefs (vegan/vegetarian).",
        "",
        "Serving sizes can be fractional (0.5 increments).",
        "",
        "Nutrition Targets",
        "",
        "Calories & protein must be within ±5% of goals. Track fats and carbs when available.",
        "",
        "Prioritize protein target first, then adjust calories.",
        "",
        "Meals must be realistic, balanced, appetizing.",
        "",
        "Explanation field: 1–3 sentences on overall balance and any unavoidable shortfall/excess.",
        "",
        "Allowed items list:",
        allowed_block,
    ]

    return "\n".join(prompt_lines).strip()

def _estimate_missing_macros(calories: Optional[float], protein: Optional[float]) -> Tuple[float, float]:
    """Estimate missing macros with sensible defaults based on available data."""
    if calories is not None and protein is not None:
        return float(calories), float(protein)
    if protein is not None and calories is None:
        
        calories = protein * 4 * 1.25
    elif calories is not None and protein is None:
        
        protein = max(1.0, (calories * 0.12) / 4.0)
    else:
        
        calories = 50.0
        protein = 2.0
    return float(calories), float(protein)


def calculate_macro_targets(weight_lbs: float, total_calories: int, protein_g: int, goal: str) -> dict:
    """
    Calculate fat and carbs targets based on calories, protein, weight, and fitness goal.
    
    Standard approach:
    - Fat: 0.4-0.5g per lb bodyweight (varies by goal)
    - Carbs: Remaining calories after protein and fat are allocated
    
    Calorie conversion:
    - Protein: 4 cal/g
    - Fat: 9 cal/g
    - Carbs: 4 cal/g
    
    Args:
        weight_lbs: User's body weight in pounds
        total_calories: Daily calorie target
        protein_g: Daily protein target in grams
        goal: 'cut', 'maintain', or 'bulk'
    
    Returns:
        dict with 'fat_g' and 'carbs_g' targets
    """
    goal_lower = goal.lower()
    
    # Calculate fat target based on goal
    # Cut: 0.4g/lb (lower fat to maximize deficit)
    # Maintain: 0.45g/lb (moderate fat)
    # Bulk: 0.5g/lb (higher fat for surplus)
    if goal_lower == 'cut':
        fat_per_lb = 0.4
    elif goal_lower == 'bulk':
        fat_per_lb = 0.5
    else:  # maintain
        fat_per_lb = 0.45
    
    fat_target = weight_lbs * fat_per_lb
    
    # Calculate calories from protein and fat
    protein_calories = protein_g * 4  # 4 cal per gram of protein
    fat_calories = fat_target * 9  # 9 cal per gram of fat
    
    # Remaining calories go to carbs
    carbs_calories = total_calories - protein_calories - fat_calories
    
    # Ensure carbs don't go negative (minimum 50g for brain function)
    carbs_calories = max(carbs_calories, 200)  # 50g * 4 cal/g = 200 cal minimum
    carbs_target = carbs_calories / 4  # 4 cal per gram of carbs
    
    return {
        'fat_g': int(round(fat_target)),
        'carbs_g': int(round(carbs_target))
    }


def _fuzzy_lookup(name: str, lookup: Dict[str, dict]) -> Optional[str]:
    """Fuzzy lookup for item names in nutrition database."""
    if name.upper() in lookup:
        return name.upper()
    lower_map = {k.lower(): k for k in lookup.keys()}
    if name.lower() in lower_map:
        return lower_map[name.lower()]
    candidates = get_close_matches(name.lower(), lower_map.keys(), n=1, cutoff=0.85)
    if candidates:
        return lower_map[candidates[0]]
    return None


def _normalize_preference_key(metadata: Dict[str, Any]) -> Optional[str]:
    """Normalize metadata into a front-end friendly favorite/dislike key."""
    item_name = (metadata.get("item_name") or metadata.get("food_item") or "").strip().lower()
    if not item_name:
        return None
    item_type = metadata.get("preference_type") or metadata.get("item_type") or "food_item"
    if item_type not in ("meal_plan", "food_item"):
        item_type = "food_item"
    return f"{item_type}:{item_name}"


def _build_preferences_snapshot(
    favorites: List[Dict[str, Any]],
    dislikes: List[Dict[str, Any]]
) -> Dict[str, Any]:
    favorite_keys: List[str] = []
    for fav in favorites or []:
        metadata = fav.get("metadata") or {}
        key = _normalize_preference_key(metadata)
        if key and key not in favorite_keys:
            favorite_keys.append(key)

    dislike_keys: List[str] = []
    for dislike in dislikes or []:
        metadata = dislike.get("metadata") or {}
        key = _normalize_preference_key(metadata)
        if key and key not in dislike_keys:
            dislike_keys.append(key)

    return {
        "favorites": favorites or [],
        "dislikes": dislikes or [],
        "favorite_keys": favorite_keys,
        "dislike_keys": dislike_keys,
    }

@app.put("/auth/profile", summary="Update User Profile")
async def update_profile(profile_data: UpdateProfileRequest, current_user=Depends(get_current_user)):
    """Update user profile"""
    try:
        update_data: Dict[str, object] = {}

        # Fields that trigger extended metrics recalculation
        metrics_fields = {'age', 'height_feet', 'height_inches', 'weight_lbs', 'gender', 'activity_level', 'goal', 'goal_weight_lbs', 'weight_change_speed'}

        for field, value in profile_data.model_dump(exclude_unset=True).items():
            # Special-case: allow clearing dietary_preference by explicitly setting it to null/None
            if field == 'dietary_preference' and value is None:
                update_data[field] = None
                continue

            if value is not None:
                if field == 'username':
                    value = value.strip()
                    if not value or len(value) > 100:
                        raise HTTPException(status_code=400, detail="Username must be between 1 and 100 characters")
                    update_data[field] = value
                    continue
                if field == 'goal' and value not in ['cut', 'maintain', 'bulk', 'lose', 'gain']:
                    raise HTTPException(status_code=400, detail="Invalid goal. Must be 'cut', 'maintain', 'bulk', 'lose', or 'gain'")
                if field == 'dietary_preference' and value not in ['vegan', 'vegetarian', 'pescetarian', 'keto', 'gluten_free', 'dairy_free']:
                    raise HTTPException(status_code=400, detail="Invalid dietary_preference. Must be 'vegan', 'vegetarian', 'pescetarian', 'keto', 'gluten_free', or 'dairy_free'")
                if field == 'weight_change_speed' and value not in ['slow', 'recommended', 'fast']:
                    raise HTTPException(status_code=400, detail="Invalid weight_change_speed. Must be 'slow', 'recommended', or 'fast'")
                update_data[field] = value

        # Recalculate extended metrics if any relevant field changed
        if any(field in update_data for field in metrics_fields):
            current_age = update_data.get('age', current_user.get('age'))
            current_height_feet = update_data.get('height_feet', current_user.get('height_feet'))
            current_height_inches = update_data.get('height_inches', current_user.get('height_inches'))
            current_weight = update_data.get('weight_lbs', current_user.get('weight_lbs'))
            current_gender = update_data.get('gender', current_user.get('gender'))
            current_activity = update_data.get('activity_level', current_user.get('activity_level'))
            current_goal = update_data.get('goal', current_user.get('goal'))
            current_goal_weight = update_data.get('goal_weight_lbs', current_user.get('goal_weight_lbs'))
            current_weight_change_speed = update_data.get('weight_change_speed', current_user.get('weight_change_speed'))

            if all(v is not None for v in [current_age, current_height_feet, current_height_inches, current_weight, current_gender, current_activity]):
                metrics = calculate_extended_metrics(
                    age=int(current_age),
                    height_feet=int(current_height_feet),
                    height_inches=int(current_height_inches),
                    weight_lbs=float(current_weight),
                    gender=str(current_gender),
                    activity_level=str(current_activity),
                    goal=current_goal,
                    goal_weight_lbs=float(current_goal_weight) if current_goal_weight else None,
                    weight_change_speed=current_weight_change_speed,
                )
                update_data['bmr'] = metrics['bmr']
                update_data['tdee'] = metrics['tdee']
                update_data['height_cm'] = metrics.get('height_cm')
                update_data['weight_kg'] = metrics.get('weight_kg')
                update_data['goal_weight_kg'] = metrics.get('goal_weight_kg')
                update_data['bmi'] = metrics.get('bmi')
                update_data['daily_calorie_target'] = metrics.get('daily_calorie_target')
                update_data['estimated_weeks_to_goal'] = metrics.get('estimated_weeks_to_goal')
                update_data['target_date'] = metrics.get('target_date')
        if update_data:
            response = supabase.table("users").update(update_data).eq("id", current_user['id']).execute()
            
            if getattr(response, 'error', None):
                err_text = str(response.error)
                
                if 'invalid input value' in err_text or 'invalid input syntax for enum' in err_text or 'cannot insert' in err_text:
                    
                    guidance = (
                        "The database rejected the new dietary preference value. "
                        "This often means the `dietary_preference` column is a Postgres enum that doesn't include 'pescetarian'.\n"
                        "To inspect the type of the column run:\n"
                        "  SELECT column_name, data_type, udt_name FROM information_schema.columns\n"
                        "  WHERE table_name='users' AND column_name='dietary_preference';\n\n"
                        "If the column is an enum (udt_name shows the enum type), add the new value with (run as a DB admin):\n"
                        "  ALTER TYPE <enum_type> ADD VALUE 'pescetarian';\n\n"
                        "To find the enum_type replace <enum_type> with the udt_name from the query above. "
                        "If you're using Supabase, run those SQL statements in the SQL editor."
                    )
                    raise HTTPException(status_code=500, detail=f"Failed to update profile: DB rejected value. Postgres error: {err_text}.\n{guidance}")
                
                raise HTTPException(status_code=500, detail=f"Failed to update profile: {err_text}")
            if response.data:
                updated_user = response.data[0]
                updated_user.pop('password_hash', None)
                return {"success": True, "message": "Profile updated successfully", "user": updated_user}
            else:
                raise HTTPException(status_code=500, detail="Failed to update profile")
        else:
            raise HTTPException(status_code=400, detail="No valid fields to update")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to update profile")


@app.post("/auth/weight", summary="Track user weight")
async def track_weight(weight_data: TrackWeightRequest, current_user=Depends(get_current_user)):
    """
    Track user weight for a specific date.
    Creates or updates weight entry for the given date.
    Also updates the user's profile weight_lbs field.
    """
    try:
        # Use provided date or default to today
        weight_date = weight_data.date or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        
        # Validate weight value
        if weight_data.weight_lbs <= 0 or weight_data.weight_lbs > 1000:
            raise HTTPException(status_code=400, detail="Weight must be between 0 and 1000 lbs")
        
        # Check if weight entry already exists for this date
        existing = supabase.table("weight_history")\
            .select("id")\
            .eq("user_id", current_user["id"])\
            .eq("date", weight_date)\
            .execute()
        
        if existing.data and len(existing.data) > 0:
            # Update existing entry
            response = supabase.table("weight_history")\
                .update({
                    "weight_lbs": weight_data.weight_lbs,
                    "updated_at": datetime.now(ZoneInfo("America/New_York")).isoformat()
                })\
                .eq("user_id", current_user["id"])\
                .eq("date", weight_date)\
                .execute()
        else:
            # Create new entry
            response = supabase.table("weight_history")\
                .insert({
                    "user_id": current_user["id"],
                    "weight_lbs": weight_data.weight_lbs,
                    "date": weight_date,
                    "created_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                    "updated_at": datetime.now(ZoneInfo("America/New_York")).isoformat()
                })\
                .execute()
        
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to save weight data")
        
        # Only update the user's profile weight if this entry is for today
        # (prevents HealthKit historical sync from overwriting the current weight)
        today_str = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if weight_date == today_str:
            profile_update = supabase.table("users")\
                .update({"weight_lbs": weight_data.weight_lbs})\
                .eq("id", current_user["id"])\
                .execute()
        
        # Recalculate BMR and TDEE only when profile weight was updated (today's entry)
        if weight_date == today_str and all(key in current_user for key in ['age', 'height_feet', 'height_inches', 'gender', 'activity_level']):
            bmr_data = calculate_bmr_tdee(
                current_user['age'],
                current_user['height_feet'],
                current_user['height_inches'],
                weight_data.weight_lbs,
                current_user['gender'],
                current_user['activity_level']
            )
            supabase.table("users")\
                .update({"bmr": bmr_data['bmr'], "tdee": bmr_data['tdee']})\
                .eq("id", current_user["id"])\
                .execute()
        
        return {
            "success": True,
            "message": "Weight tracked successfully",
            "weight": response.data[0]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error tracking weight: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to track weight: {str(e)}")


@app.get("/auth/weight/history", summary="Get user weight history")
async def get_weight_history(
    limit: int = 30,
    offset: int = 0,
    days: Optional[int] = None,  # Optional: get entries from last N days
    current_user=Depends(get_current_user)
):
    """
    Get user's weight history, ordered by date (most recent first).
    Returns weight entries for tracking progress over time.
    
    If 'days' parameter is provided, returns entries from the last N days.
    Otherwise, returns the last N entries (limited by 'limit').
    """
    try:
        query = supabase.table("weight_history")\
            .select("id, weight_lbs, date, created_at, updated_at")\
            .eq("user_id", current_user["id"])
        
        # If 'days' parameter is provided, filter by date range
        if days is not None:
            today = datetime.now(ZoneInfo("America/New_York")).date()
            start_date = (today - timedelta(days=days - 1)).isoformat()  # days - 1 to include today
            query = query.gte("date", start_date)
        
        response = query\
            .order("date", desc=True)\
            .range(offset, offset + limit - 1)\
            .execute()
        
        weights = response.data if response.data else []
        
        return {
            "success": True,
            "weights": weights,
            "count": len(weights)
        }
    except Exception as e:
        print(f"Error fetching weight history: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch weight history: {str(e)}")


async def _get_existing_individual_meals(user_id: str, date: str) -> Dict[str, dict]:
    """Query saved_meal_plans for individual meal entries for this user+date.
    Returns dict like {"breakfast": {"calories": 600, "protein_g": 45, ...}}
    """
    def _fetch():
        try:
            response = supabase.table("saved_meal_plans")\
                .select("meal_plan_data")\
                .eq("user_id", user_id)\
                .eq("date", date)\
                .execute()
            result = {}
            for row in (response.data or []):
                mpd = row.get("meal_plan_data") or {}
                meal_name = mpd.get("meal")
                if not meal_name:
                    continue  # Skip full-day plans (no meal field)
                totals = (mpd.get("meal_plan") or {}).get("totals") or {}
                result[meal_name] = {
                    "calories": float(totals.get("calories", 0)),
                    "protein_g": float(totals.get("protein_g", 0)),
                    "fat_g": float(totals.get("fat_g", 0)),
                    "carbs_g": float(totals.get("carbs_g", 0)),
                }
            return result
        except Exception as exc:
            logger.warning("Failed to fetch existing individual meals: %s", exc)
            return {}
    return await asyncio.to_thread(_fetch)


def _calculate_meal_targets(
    daily_targets: Dict[str, int],
    already_planned: Dict[str, dict],
    requested_meal: str,
) -> Dict[str, int]:
    """Calculate nutrition targets for a single meal based on daily targets,
    already-planned meals, and proportional allocation."""
    remaining = {
        "calories": daily_targets["calories"],
        "protein_g": daily_targets["protein_g"],
        "fat_g": daily_targets["fat_g"],
        "carbs_g": daily_targets["carbs_g"],
    }
    for meal_totals in already_planned.values():
        for key in remaining:
            remaining[key] = max(0, remaining[key] - int(meal_totals.get(key, 0)))

    unplanned_meals = [m for m in VALID_MEALS if m not in already_planned and m != requested_meal]
    total_proportion = MEAL_PROPORTIONS[requested_meal] + sum(MEAL_PROPORTIONS[m] for m in unplanned_meals)
    share = MEAL_PROPORTIONS[requested_meal] / total_proportion if total_proportion > 0 else 1.0

    return {
        "calories": max(100, int(remaining["calories"] * share)),
        "protein_g": max(10, int(remaining["protein_g"] * share)),
        "fat_g": max(5, int(remaining["fat_g"] * share)),
        "carbs_g": max(10, int(remaining["carbs_g"] * share)),
    }


@app.post("/generate-meal-plan", summary="Generate Meal Plan")
async def generate_meal_plan_route(meal_plan_request: MealPlanRequest, authorization: Optional[str] = Header(None), background_tasks: BackgroundTasks = None):
    """Generate a meal plan for a user (authenticated) with model-provided macros and totals.
    Auto-saves the meal plan to the database. Returns legacy MealPlanResponse wrapper expected by frontend.

    Rate limit: One meal plan per day. Meal plans reset at the start of each new day.
    """
    from meal_plan_agent import generate_meal_plan_with_agent
    t_start = time.time()
    menu_fetch_start = t_start
    try:
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = await asyncio.to_thread(verify_session_token, token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required. Guest users not supported.")

        
        # Check if user already has a meal plan for today (rate limiting)
        # NOTE: Disabled for testing so multiple meal plans per day can be created.
        # Keep `today` defined because it's used later when auto-saving the plan
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        # existing_plan_check = supabase.table("saved_meal_plans")\
        #     .select("id, created_at")\
        #     .eq("user_id", authenticated_user["id"])\
        #     .eq("date", today)\
        #     .execute()
        
        # if existing_plan_check.data and len(existing_plan_check.data) > 0:
        #     existing = existing_plan_check.data[0]
        #     raise HTTPException(
        #         status_code=409,
        #         detail=f"You already have a meal plan for today. You can generate a new one at the start of tomorrow or use the existing plan."
        #     )

        
        # Check if user already has a meal plan for today (rate limiting)
        
        
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        existing_plan_check = supabase.table("saved_meal_plans")\
            .select("id, created_at")\
            .eq("user_id", authenticated_user["id"])\
            .eq("date", today)\
            .execute()
        
        if existing_plan_check.data and len(existing_plan_check.data) > 0:
            existing = existing_plan_check.data[0]
            raise HTTPException(
                status_code=409,
                detail=f"You already have a meal plan for today. You can generate a new one at the start of tomorrow or use the existing plan."
            )
        user = User(
            age=authenticated_user['age'],
            height_feet=authenticated_user['height_feet'],
            height_inches=authenticated_user['height_inches'],
            weight_lbs=float(authenticated_user['weight_lbs']),
            activity_level=authenticated_user['activity_level'],
            goal=authenticated_user['goal'],
            preferred_dining_hall=meal_plan_request.dining_hall,
            dietary_preference=authenticated_user.get('dietary_preference'),
        )
        metrics = calculate_extended_metrics(
            age=user.age,
            height_feet=user.height_feet,
            height_inches=user.height_inches,
            weight_lbs=user.weight_lbs,
            gender=authenticated_user.get('gender'),
            activity_level=user.activity_level,
            goal=user.goal,
            goal_weight_lbs=float(authenticated_user['goal_weight_lbs']) if authenticated_user.get('goal_weight_lbs') else None,
            weight_change_speed=authenticated_user.get('weight_change_speed'),
        )
        user_bmr = float(metrics["bmr"])
        user_tdee = float(metrics["tdee"])
        user_daily_calorie_target = int(metrics["daily_calorie_target"])
        user_act_breakdown = metrics.get("activity_breakdown", {})

        stored_bmr = authenticated_user.get("bmr")
        stored_tdee = authenticated_user.get("tdee")
        try:
            stored_bmr_val = float(stored_bmr) if stored_bmr is not None else None
            stored_tdee_val = float(stored_tdee) if stored_tdee is not None else None
        except (TypeError, ValueError):
            stored_bmr_val, stored_tdee_val = None, None

        if stored_bmr_val != user_bmr or stored_tdee_val != user_tdee:
            try:
                await asyncio.to_thread(
                    lambda: supabase.table("users").update({
                        "bmr": user_bmr,
                        "tdee": user_tdee,
                    }).eq("id", authenticated_user['id']).execute()
                )
                authenticated_user['bmr'] = user_bmr
                authenticated_user['tdee'] = user_tdee
            except Exception as update_exc:
                print(f"⚠️ Failed to persist refreshed BMR/TDEE: {update_exc}")

        menu_data = await get_dining_hall_menu(meal_plan_request.dining_hall, meal_plan_request.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        items_list = menu_data.get("items", []) or []
        if meal_plan_request.restaurant and items_list:
            items_list = [
                item for item in items_list
                if (item.get("restaurant") or "").lower() == meal_plan_request.restaurant.lower()
            ]
            if not items_list:
                raise HTTPException(status_code=404, detail=f"No items for restaurant '{meal_plan_request.restaurant}'")
        if not items_list:
            raise HTTPException(status_code=404, detail="Menu has no items for given hall/date")
        menu_fetch_end = time.time()


        nutrition_lookup: Dict[str, dict] = {}
        allowed_for_prompt: List[dict] = []
        for raw in items_list:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            calories = raw.get("calories")
            protein = raw.get("protein_g") or raw.get("protein") or raw.get("g_protein")
            calories, protein = _estimate_missing_macros(calories, protein)
            fat = raw.get("fat_g") or raw.get("g_fat")
            carbs = raw.get("carbs_g") or raw.get("g_carbs") or raw.get("carbohydrates")
            serving_size = raw.get("serving_size") or None
            entry = {
                "name": name,
                "calories": float(calories),
                "protein_g": float(protein),
                "serving_size": serving_size,
                "density": (protein / calories) if calories else 0.0,
            }
            # Add optional macros if available
            if fat is not None:
                try:
                    entry["fat_g"] = float(fat)
                except (TypeError, ValueError):
                    pass
            if carbs is not None:
                try:
                    entry["carbs_g"] = float(carbs)
                except (TypeError, ValueError):
                    pass
            key = name.upper()
            if key not in nutrition_lookup:  
                nutrition_lookup[key] = entry
                allowed_for_prompt.append(entry)

        if len(nutrition_lookup) == 0:
            raise HTTPException(status_code=500, detail="No usable menu items after processing")

        
        goal_mode = user.goal.lower()
        calorie_target = user_daily_calorie_target
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))

        # Calculate fat and carbs targets
        macro_targets = calculate_macro_targets(
            weight_lbs=user.weight_lbs,
            total_calories=calorie_target,
            protein_g=protein_target,
            goal=goal_mode
        )
        fat_target = macro_targets['fat_g']
        carbs_target = macro_targets['carbs_g']

        # Subtract any food log entries for today from targets
        try:
            food_log_result = supabase.table("food_log") \
                .select("calories, protein_g, carbs_g, fat_g") \
                .eq("user_id", str(authenticated_user["id"])) \
                .eq("date", meal_plan_request.date) \
                .execute()
            if food_log_result.data:
                logged_cal = sum(e.get("calories", 0) or 0 for e in food_log_result.data)
                logged_pro = sum(e.get("protein_g", 0) or 0 for e in food_log_result.data)
                logged_carb = sum(e.get("carbs_g", 0) or 0 for e in food_log_result.data)
                logged_fat = sum(e.get("fat_g", 0) or 0 for e in food_log_result.data)
                calorie_target = max(0, calorie_target - int(logged_cal))
                protein_target = max(0, protein_target - int(logged_pro))
                carbs_target = max(0, carbs_target - int(logged_carb))
                fat_target = max(0, fat_target - int(logged_fat))
                logger.info(f"Adjusted targets after food log: cal={calorie_target}, pro={protein_target}, carb={carbs_target}, fat={fat_target}")
        except Exception as e:
            logger.warning(f"Failed to fetch food log for target adjustment: {e}")

        selected_activity_metrics: Dict[str, Any] = {}
        if isinstance(user_act_breakdown, dict):
            selected_entry = user_act_breakdown.get(user.activity_level)
            if isinstance(selected_entry, dict):
                selected_activity_metrics = {user.activity_level: selected_entry}

        agent_user_payload = {
            "age": user.age,
            "height_feet": user.height_feet,
            "height_inches": user.height_inches,
            "weight_lbs": user.weight_lbs,
            "activity_level": user.activity_level,
            "goal": goal_mode,
            "dietary_preference": user.dietary_preference,
            "bmr": user_bmr,
            "tdee": user_tdee,
            "activity_breakdown": selected_activity_metrics or None,
        }
        agent_menu_payload = {
            "requested_dining_hall": meal_plan_request.dining_hall,
            "requested_date": meal_plan_request.date,
            "source_menu": menu_data,
            "allowed_items": allowed_for_prompt,
        }
        if meal_plan_request.restaurant:
            agent_menu_payload["restaurant"] = meal_plan_request.restaurant
        agent_nutrition_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "fat_g": fat_target,
            "carbs_g": carbs_target,
            "bmr": user_bmr,
            "tdee": user_tdee,
            "goal": goal_mode,
            "activity_breakdown": selected_activity_metrics or None,
        }

        # Retrieve user preferences (if available, premium only)
        user_preferences_context = None
        preferences_snapshot: Dict[str, Any] = {
            "favorites": [],
            "dislikes": [],
            "favorite_keys": [],
            "dislike_keys": [],
        }
        try:
            user_id = str(authenticated_user["id"])
            from subscription import check_subscription_status
            is_premium = await check_subscription_status(user_id)
            if is_premium:
                from memory.preference_manager import PreferenceManager, build_preference_context
                preference_manager = PreferenceManager(supabase)

                favorites = preference_manager.get_favorites(user_id)
                dislikes = preference_manager.get_dislikes(user_id)

                user_preferences_context = build_preference_context(authenticated_user, favorites, dislikes)

                preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
            else:
                print(f"Skipping preferences for non-premium user {user_id}")
        except Exception as pref_error:
            print(f"Warning: Failed to retrieve preferences: {pref_error}")
            user_preferences_context = None
            preferences_snapshot = {
                "favorites": [],
                "dislikes": [],
                "favorite_keys": [],
                "dislike_keys": [],
            }

        # Look up Gemini cache handle
        cache_name = None
        try:
            from menu_cache import get_cache_name
            _mp_hall = (meal_plan_request.dining_hall or "").strip().lower()
            _mapping = DINING_HALL_MAPPING.get(_mp_hall)
            if _mapping:
                _mp_univ, _mp_slug = _mapping
                cache_name = await get_cache_name(
                    dining_hall=_mp_slug,
                    university=_mp_univ,
                    date=meal_plan_request.date or today,
                    use_case="meal_plan",
                )
        except Exception as e:
            logger.debug(f"Meal plan cache lookup skipped: {e}")

        ai_start = time.time()
        try:
            agent_result, agent_raw_text = await generate_meal_plan_with_agent(
                user_payload=agent_user_payload,
                menu_payload=agent_menu_payload,
                nutrition_targets=agent_nutrition_targets,
                user_preferences_context=user_preferences_context,
                exclude_meals=meal_plan_request.exclude_meals,
                cached_content=cache_name,
            )
        except ValueError as e:
            raise HTTPException(status_code=500, detail="Please try again.")
        ai_end = time.time()

        plan_json = agent_result.model_dump()

        unknown_items: List[str] = []
        used_names_global = set()
        for meal_key in ["breakfast", "lunch", "dinner"]:
            meal = plan_json.get("meals", {}).get(meal_key)
            if not isinstance(meal, dict):
                continue
            new_items = []
            for item in meal.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                nm = (item.get("name") or "").strip()
                if not nm:
                    continue
                key_match = _fuzzy_lookup(nm, nutrition_lookup)
                if not key_match:
                    unknown_items.append(nm)
                    continue
                if key_match in used_names_global:
                    continue
                used_names_global.add(key_match)
                servings = item.get("servings") or 1
                try:
                    servings = float(servings)
                    if servings <= 0:
                        servings = 1.0
                except Exception:
                    servings = 1.0

                sanitized_item = {**item}
                sanitized_item["name"] = nutrition_lookup[key_match]["name"]
                sanitized_item["servings"] = round(servings, 2)
                # Strict schema requires serving_size to be a string, not None
                if not sanitized_item.get("serving_size"):
                    sanitized_item["serving_size"] = nutrition_lookup[key_match].get("serving_size") or "1 serving"

                for macro_key in ("calories", "protein_g", "fat_g", "carbs_g"):
                    value = sanitized_item.get(macro_key)
                    if value is None:
                        continue
                    try:
                        sanitized_item[macro_key] = float(value)
                    except (TypeError, ValueError):
                        sanitized_item[macro_key] = value

                new_items.append(sanitized_item)
            meal["items"] = new_items
        
        # Store unknown_items in separate meta dict (not part of strict schema)
        dropped_items_meta = {}
        if unknown_items:
            dropped_items_meta['dropped_unknown'] = unknown_items

        plan_json["preferences_snapshot"] = preferences_snapshot

        plan_json.setdefault('goals', {})['calories'] = calorie_target
        plan_json['goals']['protein_g'] = protein_target
        plan_json['goals']['fat_g'] = fat_target
        plan_json['goals']['carbs_g'] = carbs_target
        plan_json.setdefault('user_stats', {})['bmr'] = user_bmr
        plan_json['user_stats']['tdee'] = user_tdee
        plan_json['user_stats']['goal'] = goal_mode
        plan_json['user_stats']['dietary_preference'] = user.dietary_preference or None
        plan_json['user_stats']['activity_breakdown'] = selected_activity_metrics or None

        
        totals = plan_json.get('totals')
        if not isinstance(totals, dict):
            raise HTTPException(status_code=500, detail="Please try again.")
        try:
            final_cal = float(totals['calories'])
            final_prot = float(totals['protein_g'])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=500, detail="Please try again.")
        totals['calories'] = final_cal
        totals['protein_g'] = final_prot
        
        # Handle optional fat_g and carbs_g in totals
        if 'fat_g' in totals and totals['fat_g'] is not None:
            try:
                totals['fat_g'] = float(totals['fat_g'])
            except (TypeError, ValueError):
                totals['fat_g'] = 0
        
        if 'carbs_g' in totals and totals['carbs_g'] is not None:
            try:
                totals['carbs_g'] = float(totals['carbs_g'])
            except (TypeError, ValueError):
                totals['carbs_g'] = 0
        
        cal_diff = final_cal - calorie_target
        prot_diff = final_prot - protein_target
        cal_pct = (cal_diff / calorie_target * 100) if calorie_target else 0
        prot_pct = (prot_diff / protein_target * 100) if protein_target else 0
        
        print(f"📊 Final Plan Metrics:")
        print(f"   Calories: {final_cal:.0f} / {calorie_target} ({cal_pct:+.1f}%)")
        print(f"   Protein:  {final_prot:.0f}g / {protein_target}g ({prot_pct:+.1f}%)")
        if totals.get('fat_g'):
            print(f"   Fat:      {totals['fat_g']:.0f}g")
        if totals.get('carbs_g'):
            print(f"   Carbs:    {totals['carbs_g']:.0f}g")

        if not plan_json.get('explanation'):
            plan_json['explanation'] = "Balanced plan targeting protein and calories with available items."

        t_end = time.time()
        menu_fetch_time = menu_fetch_end - menu_fetch_start
        ai_time = ai_end - ai_start
        total_time = t_end - t_start

        
        safe_user = {k: authenticated_user[k] for k in authenticated_user.keys() if k != 'password_hash'}
        safe_user['bmr'] = user_bmr
        safe_user['tdee'] = user_tdee
        safe_user['activity_breakdown'] = selected_activity_metrics or None

        generation_id = str(uuid4())
        # Build meta payload separately (not part of strict schema)
        meta_payload = {
            'agent_raw_response': agent_raw_text,
            **dropped_items_meta  # Include dropped items if any
        }

        response_payload = {
            "success": True,
            "user_profile": safe_user,
            "nutrition_targets": {
                "calories": calorie_target,
                "protein_g": protein_target,
                "fat_g": fat_target,
                "carbs_g": carbs_target,
            },
            "bmr_tdee": {
                "bmr": user_bmr,
                "tdee": user_tdee,
                "activity_breakdown": selected_activity_metrics or None,
                "bmr_precise": metrics.get("bmr_precise"),
                "tdee_precise": metrics.get("tdee_precise"),
            },
            "available_items": allowed_for_prompt,
            "meal_plan": plan_json,
            "preferences_snapshot": preferences_snapshot,
            "generation_id": generation_id,
            "ask_feedback": "Please rate this meal plan",  
            "processing_info": {
                "menu_fetch_time_seconds": round(menu_fetch_time, 3),
                "ai_generation_time_seconds": round(ai_time, 3),
                "total_time_seconds": round(total_time, 3),
            },
            "meta": meta_payload,
        }

        logger.info(
            "meal_plan_timing menu=%.2fs ai=%.2fs total=%.2fs allowed_items=%d",
            menu_fetch_time,
            ai_time,
            total_time,
            len(allowed_for_prompt),
        )
        
        # Auto-save the generated meal plan to database
        try:
            plan_id = str(uuid4())
            save_data = {
                "id": plan_id,
                "user_id": authenticated_user["id"],
                "dining_hall": meal_plan_request.dining_hall,
                "date": today,
                "meal_plan_data": response_payload,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

            logger.info(f"[MealPlan] Attempting to auto-save meal plan for user {authenticated_user['id']} on {today}")
            save_response = supabase.table("saved_meal_plans").insert(save_data).execute()

            if save_response.data:
                response_payload["saved_plan_id"] = plan_id
                response_payload["auto_saved"] = True
                logger.info(f"[MealPlan] ✅ Auto-saved meal plan {plan_id} for user {authenticated_user['id']}")
            else:
                error_msg = getattr(save_response, 'error', 'Unknown error')
                logger.error(f"[MealPlan] ⚠️ Failed to auto-save meal plan - no data returned. Error: {error_msg}")
                response_payload["auto_saved"] = False
        except Exception as save_error:
            # Don't fail the entire request if save fails
            logger.error(f"[MealPlan] ⚠️ Auto-save failed with exception: {save_error}", exc_info=True)
            response_payload["auto_saved"] = False

        # Check referral activation threshold
        try:
            await check_and_fulfill_referral(str(authenticated_user["id"]))
        except Exception as e:
            logger.error(f"[referral] activation check failed: {e}")

        return response_payload

    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        print(f"❌ Meal plan generation error: {err_msg}")
        # Don't expose error details to users
        raise HTTPException(status_code=500, detail="Please try again.")


@app.post("/generate-meal", summary="Generate Single Meal")
async def generate_single_meal_route(req: SingleMealRequest, authorization: Optional[str] = Header(None)):
    """Generate a meal plan for a single meal (breakfast, lunch, or dinner).

    Rate limit: One generation per meal per day.  Already-planned meals are
    taken into account so that the calorie / macro budget is proportional.
    Auto-saves the result to saved_meal_plans with a ``meal`` key.
    """
    from meal_plan_agent import generate_meal_plan_with_agent
    t_start = time.time()
    menu_fetch_start = t_start
    try:
        # --- Validate meal name ---
        meal_name = req.meal.lower().strip()
        if meal_name not in VALID_MEALS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid meal '{req.meal}'. Must be one of: {', '.join(sorted(VALID_MEALS))}",
            )

        # --- Authenticate ---
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = await asyncio.to_thread(verify_session_token, token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required. Guest users not supported.")

        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

        # --- Rate limit: check if this specific meal was already generated ---
        already_planned = await _get_existing_individual_meals(str(authenticated_user["id"]), req.date)
        if meal_name in already_planned:
            raise HTTPException(
                status_code=429,
                detail=f"You already have a generated {meal_name} for {req.date}. Wait until tomorrow or delete the existing entry.",
            )

        # --- Build User model & calculate metrics ---
        user = User(
            age=authenticated_user['age'],
            height_feet=authenticated_user['height_feet'],
            height_inches=authenticated_user['height_inches'],
            weight_lbs=float(authenticated_user['weight_lbs']),
            activity_level=authenticated_user['activity_level'],
            goal=authenticated_user['goal'],
            preferred_dining_hall=req.dining_hall,
            dietary_preference=authenticated_user.get('dietary_preference'),
        )
        metrics = calculate_extended_metrics(
            age=user.age,
            height_feet=user.height_feet,
            height_inches=user.height_inches,
            weight_lbs=user.weight_lbs,
            gender=authenticated_user.get('gender'),
            activity_level=user.activity_level,
            goal=user.goal,
            goal_weight_lbs=float(authenticated_user['goal_weight_lbs']) if authenticated_user.get('goal_weight_lbs') else None,
            weight_change_speed=authenticated_user.get('weight_change_speed'),
        )
        user_bmr = float(metrics["bmr"])
        user_tdee = float(metrics["tdee"])
        user_daily_calorie_target = int(metrics["daily_calorie_target"])
        user_act_breakdown = metrics.get("activity_breakdown", {})

        # Persist refreshed BMR/TDEE if changed
        stored_bmr = authenticated_user.get("bmr")
        stored_tdee = authenticated_user.get("tdee")
        try:
            stored_bmr_val = float(stored_bmr) if stored_bmr is not None else None
            stored_tdee_val = float(stored_tdee) if stored_tdee is not None else None
        except (TypeError, ValueError):
            stored_bmr_val, stored_tdee_val = None, None

        if stored_bmr_val != user_bmr or stored_tdee_val != user_tdee:
            try:
                await asyncio.to_thread(
                    lambda: supabase.table("users").update({
                        "bmr": user_bmr,
                        "tdee": user_tdee,
                    }).eq("id", authenticated_user['id']).execute()
                )
                authenticated_user['bmr'] = user_bmr
                authenticated_user['tdee'] = user_tdee
            except Exception as update_exc:
                print(f"⚠️ Failed to persist refreshed BMR/TDEE: {update_exc}")

        # --- Daily macro targets ---
        goal_mode = user.goal.lower()
        calorie_target = user_daily_calorie_target
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))

        macro_targets = calculate_macro_targets(
            weight_lbs=user.weight_lbs,
            total_calories=calorie_target,
            protein_g=protein_target,
            goal=goal_mode,
        )
        fat_target = macro_targets['fat_g']
        carbs_target = macro_targets['carbs_g']

        # Subtract food log entries for the requested date
        try:
            food_log_result = supabase.table("food_log") \
                .select("calories, protein_g, carbs_g, fat_g") \
                .eq("user_id", str(authenticated_user["id"])) \
                .eq("date", req.date) \
                .execute()
            if food_log_result.data:
                logged_cal = sum(e.get("calories", 0) or 0 for e in food_log_result.data)
                logged_pro = sum(e.get("protein_g", 0) or 0 for e in food_log_result.data)
                logged_carb = sum(e.get("carbs_g", 0) or 0 for e in food_log_result.data)
                logged_fat = sum(e.get("fat_g", 0) or 0 for e in food_log_result.data)
                calorie_target = max(0, calorie_target - int(logged_cal))
                protein_target = max(0, protein_target - int(logged_pro))
                carbs_target = max(0, carbs_target - int(logged_carb))
                fat_target = max(0, fat_target - int(logged_fat))
                logger.info(f"Adjusted targets after food log: cal={calorie_target}, pro={protein_target}, carb={carbs_target}, fat={fat_target}")
        except Exception as e:
            logger.warning(f"Failed to fetch food log for target adjustment: {e}")

        daily_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "fat_g": fat_target,
            "carbs_g": carbs_target,
        }

        # --- Calculate proportional targets for this meal ---
        meal_targets = _calculate_meal_targets(daily_targets, already_planned, meal_name)

        # --- Fetch menu ---
        menu_data = await get_dining_hall_menu(req.dining_hall, req.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        items_list = menu_data.get("items", []) or []
        if req.restaurant and items_list:
            items_list = [
                item for item in items_list
                if (item.get("restaurant") or "").lower() == req.restaurant.lower()
            ]
            if not items_list:
                raise HTTPException(status_code=404, detail=f"No items for restaurant '{req.restaurant}'")
        # Filter to only this meal's items (fall back to full menu if no tagged items)
        meal_filtered = [item for item in items_list if item.get("meal") == meal_name]
        if meal_filtered:
            items_list = meal_filtered
        if not items_list:
            raise HTTPException(status_code=404, detail="Menu has no items for given hall/date")
        menu_fetch_end = time.time()

        # --- Build nutrition lookup ---
        nutrition_lookup: Dict[str, dict] = {}
        allowed_for_prompt: List[dict] = []
        for raw in items_list:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            calories = raw.get("calories")
            protein = raw.get("protein_g") or raw.get("protein") or raw.get("g_protein")
            calories, protein = _estimate_missing_macros(calories, protein)
            fat = raw.get("fat_g") or raw.get("g_fat")
            carbs = raw.get("carbs_g") or raw.get("g_carbs") or raw.get("carbohydrates")
            serving_size = raw.get("serving_size") or None
            entry = {
                "name": name,
                "calories": float(calories),
                "protein_g": float(protein),
                "serving_size": serving_size,
                "density": (protein / calories) if calories else 0.0,
            }
            if fat is not None:
                try:
                    entry["fat_g"] = float(fat)
                except (TypeError, ValueError):
                    pass
            if carbs is not None:
                try:
                    entry["carbs_g"] = float(carbs)
                except (TypeError, ValueError):
                    pass
            key = name.upper()
            if key not in nutrition_lookup:
                nutrition_lookup[key] = entry
                allowed_for_prompt.append(entry)

        if len(nutrition_lookup) == 0:
            raise HTTPException(status_code=500, detail="No usable menu items after processing")

        # --- Prepare agent payloads (use meal-level targets) ---
        selected_activity_metrics: Dict[str, Any] = {}
        if isinstance(user_act_breakdown, dict):
            selected_entry = user_act_breakdown.get(user.activity_level)
            if isinstance(selected_entry, dict):
                selected_activity_metrics = {user.activity_level: selected_entry}

        agent_user_payload = {
            "age": user.age,
            "height_feet": user.height_feet,
            "height_inches": user.height_inches,
            "weight_lbs": user.weight_lbs,
            "activity_level": user.activity_level,
            "goal": goal_mode,
            "dietary_preference": user.dietary_preference,
            "bmr": user_bmr,
            "tdee": user_tdee,
            "activity_breakdown": selected_activity_metrics or None,
        }
        agent_menu_payload = {
            "requested_dining_hall": req.dining_hall,
            "requested_date": req.date,
            "source_menu": menu_data,
            "allowed_items": allowed_for_prompt,
        }
        if req.restaurant:
            agent_menu_payload["restaurant"] = req.restaurant
        agent_nutrition_targets = {
            "calories": meal_targets["calories"],
            "protein_g": meal_targets["protein_g"],
            "fat_g": meal_targets["fat_g"],
            "carbs_g": meal_targets["carbs_g"],
            "bmr": user_bmr,
            "tdee": user_tdee,
            "goal": goal_mode,
            "activity_breakdown": selected_activity_metrics or None,
        }

        # --- Retrieve user preferences (premium only) ---
        user_preferences_context = None
        preferences_snapshot: Dict[str, Any] = {
            "favorites": [],
            "dislikes": [],
            "favorite_keys": [],
            "dislike_keys": [],
        }
        try:
            user_id = str(authenticated_user["id"])
            from subscription import check_subscription_status
            is_premium = await check_subscription_status(user_id)
            if is_premium:
                from memory.preference_manager import PreferenceManager, build_preference_context
                preference_manager = PreferenceManager(supabase)

                favorites = preference_manager.get_favorites(user_id)
                dislikes = preference_manager.get_dislikes(user_id)

                user_preferences_context = build_preference_context(authenticated_user, favorites, dislikes)

                preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
            else:
                print(f"Skipping preferences for non-premium user {user_id}")
        except Exception as pref_error:
            print(f"Warning: Failed to retrieve preferences: {pref_error}")
            user_preferences_context = None
            preferences_snapshot = {
                "favorites": [],
                "dislikes": [],
                "favorite_keys": [],
                "dislike_keys": [],
            }

        # --- Call agent (exclude the other two meals) ---
        exclude_meals = sorted(VALID_MEALS - {meal_name})

        # Look up Gemini cache handle
        cache_name = None
        try:
            from menu_cache import get_cache_name
            _mp_hall = (req.dining_hall or "").strip().lower()
            _mapping = DINING_HALL_MAPPING.get(_mp_hall)
            if _mapping:
                _mp_univ, _mp_slug = _mapping
                cache_name = await get_cache_name(
                    dining_hall=_mp_slug,
                    university=_mp_univ,
                    date=req.date or today,
                    use_case="meal_plan",
                )
        except Exception as e:
            logger.debug(f"Meal plan cache lookup skipped: {e}")

        ai_start = time.time()
        try:
            agent_result, agent_raw_text = await generate_meal_plan_with_agent(
                user_payload=agent_user_payload,
                menu_payload=agent_menu_payload,
                nutrition_targets=agent_nutrition_targets,
                user_preferences_context=user_preferences_context,
                exclude_meals=exclude_meals,
                cached_content=cache_name,
            )
        except ValueError as e:
            raise HTTPException(status_code=500, detail="Please try again.")
        ai_end = time.time()

        plan_json = agent_result.model_dump()

        # --- Post-process: fuzzy match items (only for the requested meal) ---
        unknown_items: List[str] = []
        used_names_global = set()
        meal = plan_json.get("meals", {}).get(meal_name)
        if isinstance(meal, dict):
            new_items = []
            for item in meal.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                nm = (item.get("name") or "").strip()
                if not nm:
                    continue
                key_match = _fuzzy_lookup(nm, nutrition_lookup)
                if not key_match:
                    unknown_items.append(nm)
                    continue
                if key_match in used_names_global:
                    continue
                used_names_global.add(key_match)
                servings = item.get("servings") or 1
                try:
                    servings = float(servings)
                    if servings <= 0:
                        servings = 1.0
                except Exception:
                    servings = 1.0

                sanitized_item = {**item}
                sanitized_item["name"] = nutrition_lookup[key_match]["name"]
                sanitized_item["servings"] = round(servings, 2)
                if not sanitized_item.get("serving_size"):
                    sanitized_item["serving_size"] = nutrition_lookup[key_match].get("serving_size") or "1 serving"

                for macro_key in ("calories", "protein_g", "fat_g", "carbs_g"):
                    value = sanitized_item.get(macro_key)
                    if value is None:
                        continue
                    try:
                        sanitized_item[macro_key] = float(value)
                    except (TypeError, ValueError):
                        sanitized_item[macro_key] = value

                new_items.append(sanitized_item)
            meal["items"] = new_items

        dropped_items_meta = {}
        if unknown_items:
            dropped_items_meta['dropped_unknown'] = unknown_items

        plan_json["preferences_snapshot"] = preferences_snapshot

        plan_json.setdefault('goals', {})['calories'] = meal_targets["calories"]
        plan_json['goals']['protein_g'] = meal_targets["protein_g"]
        plan_json['goals']['fat_g'] = meal_targets["fat_g"]
        plan_json['goals']['carbs_g'] = meal_targets["carbs_g"]
        plan_json.setdefault('user_stats', {})['bmr'] = user_bmr
        plan_json['user_stats']['tdee'] = user_tdee
        plan_json['user_stats']['goal'] = goal_mode
        plan_json['user_stats']['dietary_preference'] = user.dietary_preference or None
        plan_json['user_stats']['activity_breakdown'] = selected_activity_metrics or None

        # --- Sanitize totals ---
        totals = plan_json.get('totals')
        if not isinstance(totals, dict):
            raise HTTPException(status_code=500, detail="Please try again.")
        try:
            final_cal = float(totals['calories'])
            final_prot = float(totals['protein_g'])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=500, detail="Please try again.")
        totals['calories'] = final_cal
        totals['protein_g'] = final_prot

        if 'fat_g' in totals and totals['fat_g'] is not None:
            try:
                totals['fat_g'] = float(totals['fat_g'])
            except (TypeError, ValueError):
                totals['fat_g'] = 0

        if 'carbs_g' in totals and totals['carbs_g'] is not None:
            try:
                totals['carbs_g'] = float(totals['carbs_g'])
            except (TypeError, ValueError):
                totals['carbs_g'] = 0

        cal_diff = final_cal - meal_targets["calories"]
        prot_diff = final_prot - meal_targets["protein_g"]
        cal_pct = (cal_diff / meal_targets["calories"] * 100) if meal_targets["calories"] else 0
        prot_pct = (prot_diff / meal_targets["protein_g"] * 100) if meal_targets["protein_g"] else 0

        print(f"📊 Single Meal ({meal_name}) Metrics:")
        print(f"   Calories: {final_cal:.0f} / {meal_targets['calories']} ({cal_pct:+.1f}%)")
        print(f"   Protein:  {final_prot:.0f}g / {meal_targets['protein_g']}g ({prot_pct:+.1f}%)")
        if totals.get('fat_g'):
            print(f"   Fat:      {totals['fat_g']:.0f}g")
        if totals.get('carbs_g'):
            print(f"   Carbs:    {totals['carbs_g']:.0f}g")

        if not plan_json.get('explanation'):
            plan_json['explanation'] = f"Balanced {meal_name} targeting protein and calories with available items."

        t_end = time.time()
        menu_fetch_time = menu_fetch_end - menu_fetch_start
        ai_time = ai_end - ai_start
        total_time = t_end - t_start

        generation_id = str(uuid4())
        meta_payload = {
            'agent_raw_response': agent_raw_text,
            **dropped_items_meta,
        }

        response_payload = {
            "success": True,
            "meal": meal_name,
            "meal_plan": plan_json,
            "nutrition_targets": {
                "calories": meal_targets["calories"],
                "protein_g": meal_targets["protein_g"],
                "fat_g": meal_targets["fat_g"],
                "carbs_g": meal_targets["carbs_g"],
            },
            "daily_targets": daily_targets,
            "already_planned": already_planned,
            "available_items": allowed_for_prompt,
            "preferences_snapshot": preferences_snapshot,
            "generation_id": generation_id,
            "processing_info": {
                "menu_fetch_time_seconds": round(menu_fetch_time, 3),
                "ai_generation_time_seconds": round(ai_time, 3),
                "total_time_seconds": round(total_time, 3),
            },
            "meta": meta_payload,
        }

        logger.info(
            "single_meal_timing meal=%s menu=%.2fs ai=%.2fs total=%.2fs allowed_items=%d",
            meal_name,
            menu_fetch_time,
            ai_time,
            total_time,
            len(allowed_for_prompt),
        )

        # --- Auto-save with "meal" key in meal_plan_data ---
        try:
            plan_id = str(uuid4())
            save_data = {
                "id": plan_id,
                "user_id": authenticated_user["id"],
                "dining_hall": req.dining_hall,
                "date": today,
                "meal_plan_data": {**response_payload, "meal": meal_name},
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

            logger.info(f"[SingleMeal] Attempting to auto-save {meal_name} for user {authenticated_user['id']} on {today}")
            save_response = supabase.table("saved_meal_plans").insert(save_data).execute()

            if save_response.data:
                response_payload["saved_plan_id"] = plan_id
                response_payload["auto_saved"] = True
                logger.info(f"[SingleMeal] Auto-saved {meal_name} plan {plan_id} for user {authenticated_user['id']}")
            else:
                error_msg = getattr(save_response, 'error', 'Unknown error')
                logger.error(f"[SingleMeal] Failed to auto-save - no data returned. Error: {error_msg}")
                response_payload["auto_saved"] = False
        except Exception as save_error:
            logger.error(f"[SingleMeal] Auto-save failed with exception: {save_error}", exc_info=True)
            response_payload["auto_saved"] = False

        # Check referral activation threshold
        try:
            await check_and_fulfill_referral(str(authenticated_user["id"]))
        except Exception as e:
            logger.error(f"[referral] activation check failed: {e}")

        return response_payload

    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        print(f"❌ Single meal generation error: {err_msg}")
        raise HTTPException(status_code=500, detail="Please try again.")


@app.post("/generate-meal-plan-stream", summary="Generate Meal Plan with Streaming")
async def generate_meal_plan_stream_route(meal_plan_request: MealPlanRequest, authorization: Optional[str] = Header(None)):
    """Generate a meal plan with streaming updates. Streams thinking and response generation in real-time."""
    try:
        from meal_plan_agent import generate_meal_plan_with_streaming
        
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = await asyncio.to_thread(verify_session_token, token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required.")

        # Get user data
        user = User(
            age=authenticated_user['age'],
            height_feet=authenticated_user['height_feet'],
            height_inches=authenticated_user['height_inches'],
            weight_lbs=float(authenticated_user['weight_lbs']),
            activity_level=authenticated_user['activity_level'],
            goal=authenticated_user['goal'],
            preferred_dining_hall=meal_plan_request.dining_hall,
            dietary_preference=authenticated_user.get('dietary_preference'),
        )
        
        metrics = calculate_extended_metrics(
            age=user.age,
            height_feet=user.height_feet,
            height_inches=user.height_inches,
            weight_lbs=user.weight_lbs,
            gender=authenticated_user.get('gender'),
            activity_level=user.activity_level,
            goal=user.goal,
            goal_weight_lbs=float(authenticated_user['goal_weight_lbs']) if authenticated_user.get('goal_weight_lbs') else None,
            weight_change_speed=authenticated_user.get('weight_change_speed'),
        )
        user_bmr = float(metrics["bmr"])
        user_tdee = float(metrics["tdee"])
        user_daily_calorie_target = int(metrics["daily_calorie_target"])

        # Get menu data
        menu_data = await get_dining_hall_menu(meal_plan_request.dining_hall, meal_plan_request.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        
        items_list = menu_data.get("items", []) or []
        if meal_plan_request.restaurant and items_list:
            items_list = [
                item for item in items_list
                if (item.get("restaurant") or "").lower() == meal_plan_request.restaurant.lower()
            ]
            if not items_list:
                raise HTTPException(status_code=404, detail=f"No items for restaurant '{meal_plan_request.restaurant}'")
        if not items_list:
            raise HTTPException(status_code=404, detail="Menu has no items for given hall/date")

        # Process nutrition data
        nutrition_lookup: Dict[str, dict] = {}
        allowed_for_prompt: List[dict] = []
        for raw in items_list:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            calories = raw.get("calories")
            protein = raw.get("protein_g") or raw.get("protein") or raw.get("g_protein")
            calories, protein = _estimate_missing_macros(calories, protein)
            fat = raw.get("fat_g") or raw.get("g_fat")
            carbs = raw.get("carbs_g") or raw.get("g_carbs") or raw.get("carbohydrates")
            serving_size = raw.get("serving_size") or None
            entry = {
                "name": name,
                "calories": float(calories),
                "protein_g": float(protein),
                "serving_size": serving_size,
                "density": (protein / calories) if calories else 0.0,
            }
            # Add optional macros if available
            if fat is not None:
                try:
                    entry["fat_g"] = float(fat)
                except (TypeError, ValueError):
                    pass
            if carbs is not None:
                try:
                    entry["carbs_g"] = float(carbs)
                except (TypeError, ValueError):
                    pass
            key = name.upper()
            if key not in nutrition_lookup:
                nutrition_lookup[key] = entry
                allowed_for_prompt.append(entry)

        # Calculate targets
        goal_mode = user.goal.lower()
        calorie_target = user_daily_calorie_target
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))
        
        # Calculate fat and carbs targets
        macro_targets = calculate_macro_targets(
            weight_lbs=user.weight_lbs,
            total_calories=calorie_target,
            protein_g=protein_target,
            goal=goal_mode
        )
        fat_target = macro_targets['fat_g']
        carbs_target = macro_targets['carbs_g']

        # Subtract any food log entries for today from targets
        try:
            food_log_result = supabase.table("food_log") \
                .select("calories, protein_g, carbs_g, fat_g") \
                .eq("user_id", str(authenticated_user["id"])) \
                .eq("date", meal_plan_request.date) \
                .execute()
            if food_log_result.data:
                logged_cal = sum(e.get("calories", 0) or 0 for e in food_log_result.data)
                logged_pro = sum(e.get("protein_g", 0) or 0 for e in food_log_result.data)
                logged_carb = sum(e.get("carbs_g", 0) or 0 for e in food_log_result.data)
                logged_fat = sum(e.get("fat_g", 0) or 0 for e in food_log_result.data)
                calorie_target = max(0, calorie_target - int(logged_cal))
                protein_target = max(0, protein_target - int(logged_pro))
                carbs_target = max(0, carbs_target - int(logged_carb))
                fat_target = max(0, fat_target - int(logged_fat))
                logger.info(f"Adjusted targets (stream) after food log: cal={calorie_target}, pro={protein_target}, carb={carbs_target}, fat={fat_target}")
        except Exception as e:
            logger.warning(f"Failed to fetch food log for target adjustment: {e}")

        # Prepare payloads for streaming agent
        user_payload = {
            "age": user.age,
            "weight_lbs": user.weight_lbs,
            "activity_level": user.activity_level,
            "goal": user.goal,
            "dietary_preference": user.dietary_preference,
        }
        
        menu_payload = {
            "requested_dining_hall": meal_plan_request.dining_hall,
            "requested_date": meal_plan_request.date,
            "allowed_items": allowed_for_prompt,
            "source_menu": menu_data,
        }
        if meal_plan_request.restaurant:
            menu_payload["restaurant"] = meal_plan_request.restaurant
        
        nutrition_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "fat_g": fat_target,
            "carbs_g": carbs_target,
        }

        # Retrieve user preferences (if available, premium only)
        user_preferences_context = None
        preferences_snapshot: Dict[str, Any] = {
            "favorites": [],
            "dislikes": [],
            "favorite_keys": [],
            "dislike_keys": [],
        }
        try:
            user_id = str(authenticated_user["id"])
            from subscription import check_subscription_status
            is_premium = await check_subscription_status(user_id)
            if is_premium:
                from memory.preference_manager import PreferenceManager, build_preference_context
                preference_manager = PreferenceManager(supabase)

                favorites = preference_manager.get_favorites(user_id)
                dislikes = preference_manager.get_dislikes(user_id)

                user_preferences_context = build_preference_context(authenticated_user, favorites, dislikes)

                preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
            else:
                print(f"Skipping preferences for non-premium user {user_id}")
        except Exception as pref_error:
            print(f"Warning: Failed to retrieve preferences: {pref_error}")
            user_preferences_context = None
            preferences_snapshot = {
                "favorites": [],
                "dislikes": [],
                "favorite_keys": [],
                "dislike_keys": [],
            }

        # Get today's date for auto-save (same timezone as other endpoints)
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

        # Look up Gemini cache handle
        cache_name = None
        try:
            from menu_cache import get_cache_name
            _mp_hall = (meal_plan_request.dining_hall or "").strip().lower()
            _mapping = DINING_HALL_MAPPING.get(_mp_hall)
            if _mapping:
                _mp_univ, _mp_slug = _mapping
                cache_name = await get_cache_name(
                    dining_hall=_mp_slug,
                    university=_mp_univ,
                    date=meal_plan_request.date or today,
                    use_case="meal_plan",
                )
        except Exception as e:
            logger.debug(f"Meal plan cache lookup skipped: {e}")

        async def event_generator():
            """Generator function for Server-Sent Events"""
            try:
                async for chunk in generate_meal_plan_with_streaming(
                    user_payload=user_payload,
                    menu_payload=menu_payload,
                    nutrition_targets=nutrition_targets,
                    user_preferences_context=user_preferences_context,
                    exclude_meals=meal_plan_request.exclude_meals,
                    cached_content=cache_name,
                ):
                    if isinstance(chunk, dict) and chunk.get("type") == "complete":
                        chunk["preferences_snapshot"] = preferences_snapshot

                        # Auto-save the completed meal plan to database
                        try:
                            plan_id = str(uuid4())
                            # Build the response payload similar to non-streaming endpoint
                            response_payload = {
                                "success": True,
                                "meal_plan": chunk.get("meal_plan"),
                                "nutrition_targets": nutrition_targets,
                                "dining_hall": meal_plan_request.dining_hall,
                                "date": today,
                            }
                            save_data = {
                                "id": plan_id,
                                "user_id": authenticated_user["id"],
                                "dining_hall": meal_plan_request.dining_hall,
                                "date": today,
                                "meal_plan_data": response_payload,
                                "created_at": datetime.now().isoformat(),
                                "updated_at": datetime.now().isoformat(),
                            }

                            logger.info(f"[MealPlan-Stream] Attempting to auto-save meal plan for user {authenticated_user['id']} on {today}")
                            save_response = await asyncio.to_thread(
                                lambda: supabase.table("saved_meal_plans").insert(save_data).execute()
                            )

                            if save_response.data:
                                chunk["saved_plan_id"] = plan_id
                                chunk["auto_saved"] = True
                                logger.info(f"[MealPlan-Stream] ✅ Auto-saved meal plan {plan_id} for user {authenticated_user['id']}")

                                # Check referral activation threshold
                                try:
                                    await check_and_fulfill_referral(str(authenticated_user["id"]))
                                except Exception as ref_err:
                                    logger.error(f"[referral] stream activation check failed: {ref_err}")
                            else:
                                chunk["auto_saved"] = False
                                logger.error(f"[MealPlan-Stream] ⚠️ Failed to auto-save meal plan - no data returned")
                        except Exception as save_error:
                            chunk["auto_saved"] = False
                            logger.error(f"[MealPlan-Stream] ⚠️ Auto-save failed: {save_error}")

                    # Format as SSE
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as e:
                print(f"❌ Streaming meal plan generation error: {e}")
                error_chunk = {"type": "error", "error": "Please try again."}
                yield f"data: {json.dumps(error_chunk)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable buffering for nginx
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Streaming meal plan generation error: {e}")
        raise HTTPException(status_code=500, detail="Please try again.")


@app.post("/generate-meal-stream", summary="Generate Single Meal with Streaming")
async def generate_single_meal_stream_route(req: SingleMealRequest, authorization: Optional[str] = Header(None)):
    """Generate a single meal (breakfast, lunch, or dinner) with streaming updates.

    Same validation, auth, rate-limiting, and auto-save as the non-streaming
    ``/generate-meal`` endpoint, but returns Server-Sent Events so the client
    can show real-time progress.
    """
    try:
        from meal_plan_agent import generate_meal_plan_with_streaming

        # --- Validate meal name ---
        meal_name = req.meal.lower().strip()
        if meal_name not in VALID_MEALS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid meal '{req.meal}'. Must be one of: {', '.join(sorted(VALID_MEALS))}",
            )

        # --- Authenticate ---
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = await asyncio.to_thread(verify_session_token, token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required.")

        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

        # --- Rate limit: check if this specific meal was already generated ---
        already_planned = await _get_existing_individual_meals(str(authenticated_user["id"]), req.date)
        if meal_name in already_planned:
            raise HTTPException(
                status_code=429,
                detail=f"You already have a generated {meal_name} for {req.date}. Wait until tomorrow or delete the existing entry.",
            )

        # --- Build User model & calculate metrics ---
        user = User(
            age=authenticated_user['age'],
            height_feet=authenticated_user['height_feet'],
            height_inches=authenticated_user['height_inches'],
            weight_lbs=float(authenticated_user['weight_lbs']),
            activity_level=authenticated_user['activity_level'],
            goal=authenticated_user['goal'],
            preferred_dining_hall=req.dining_hall,
            dietary_preference=authenticated_user.get('dietary_preference'),
        )
        metrics = calculate_extended_metrics(
            age=user.age,
            height_feet=user.height_feet,
            height_inches=user.height_inches,
            weight_lbs=user.weight_lbs,
            gender=authenticated_user.get('gender'),
            activity_level=user.activity_level,
            goal=user.goal,
            goal_weight_lbs=float(authenticated_user['goal_weight_lbs']) if authenticated_user.get('goal_weight_lbs') else None,
            weight_change_speed=authenticated_user.get('weight_change_speed'),
        )
        user_bmr = float(metrics["bmr"])
        user_tdee = float(metrics["tdee"])
        user_daily_calorie_target = int(metrics["daily_calorie_target"])

        # --- Daily macro targets ---
        goal_mode = user.goal.lower()
        calorie_target = user_daily_calorie_target
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))

        macro_targets = calculate_macro_targets(
            weight_lbs=user.weight_lbs,
            total_calories=calorie_target,
            protein_g=protein_target,
            goal=goal_mode,
        )
        fat_target = macro_targets['fat_g']
        carbs_target = macro_targets['carbs_g']

        # Subtract food log entries for the requested date
        try:
            food_log_result = supabase.table("food_log") \
                .select("calories, protein_g, carbs_g, fat_g") \
                .eq("user_id", str(authenticated_user["id"])) \
                .eq("date", req.date) \
                .execute()
            if food_log_result.data:
                logged_cal = sum(e.get("calories", 0) or 0 for e in food_log_result.data)
                logged_pro = sum(e.get("protein_g", 0) or 0 for e in food_log_result.data)
                logged_carb = sum(e.get("carbs_g", 0) or 0 for e in food_log_result.data)
                logged_fat = sum(e.get("fat_g", 0) or 0 for e in food_log_result.data)
                calorie_target = max(0, calorie_target - int(logged_cal))
                protein_target = max(0, protein_target - int(logged_pro))
                carbs_target = max(0, carbs_target - int(logged_carb))
                fat_target = max(0, fat_target - int(logged_fat))
                logger.info(f"Adjusted targets (meal-stream) after food log: cal={calorie_target}, pro={protein_target}, carb={carbs_target}, fat={fat_target}")
        except Exception as e:
            logger.warning(f"Failed to fetch food log for target adjustment: {e}")

        daily_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "fat_g": fat_target,
            "carbs_g": carbs_target,
        }

        # --- Calculate proportional targets for this meal ---
        meal_targets = _calculate_meal_targets(daily_targets, already_planned, meal_name)

        # --- Fetch menu ---
        menu_data = await get_dining_hall_menu(req.dining_hall, req.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        items_list = menu_data.get("items", []) or []
        if req.restaurant and items_list:
            items_list = [
                item for item in items_list
                if (item.get("restaurant") or "").lower() == req.restaurant.lower()
            ]
            if not items_list:
                raise HTTPException(status_code=404, detail=f"No items for restaurant '{req.restaurant}'")
        # Filter to only this meal's items (fall back to full menu if no tagged items)
        meal_filtered = [item for item in items_list if item.get("meal") == meal_name]
        if meal_filtered:
            items_list = meal_filtered
        if not items_list:
            raise HTTPException(status_code=404, detail="Menu has no items for given hall/date")

        # --- Build nutrition lookup ---
        nutrition_lookup: Dict[str, dict] = {}
        allowed_for_prompt: List[dict] = []
        for raw in items_list:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            calories = raw.get("calories")
            protein = raw.get("protein_g") or raw.get("protein") or raw.get("g_protein")
            calories, protein = _estimate_missing_macros(calories, protein)
            fat = raw.get("fat_g") or raw.get("g_fat")
            carbs = raw.get("carbs_g") or raw.get("g_carbs") or raw.get("carbohydrates")
            serving_size = raw.get("serving_size") or None
            entry = {
                "name": name,
                "calories": float(calories),
                "protein_g": float(protein),
                "serving_size": serving_size,
                "density": (protein / calories) if calories else 0.0,
            }
            if fat is not None:
                try:
                    entry["fat_g"] = float(fat)
                except (TypeError, ValueError):
                    pass
            if carbs is not None:
                try:
                    entry["carbs_g"] = float(carbs)
                except (TypeError, ValueError):
                    pass
            key = name.upper()
            if key not in nutrition_lookup:
                nutrition_lookup[key] = entry
                allowed_for_prompt.append(entry)

        if len(nutrition_lookup) == 0:
            raise HTTPException(status_code=500, detail="No usable menu items after processing")

        # --- Prepare agent payloads (use meal-level targets) ---
        user_payload = {
            "age": user.age,
            "weight_lbs": user.weight_lbs,
            "activity_level": user.activity_level,
            "goal": goal_mode,
            "dietary_preference": user.dietary_preference,
        }

        menu_payload = {
            "requested_dining_hall": req.dining_hall,
            "requested_date": req.date,
            "allowed_items": allowed_for_prompt,
            "source_menu": menu_data,
        }
        if req.restaurant:
            menu_payload["restaurant"] = req.restaurant

        nutrition_targets = {
            "calories": meal_targets["calories"],
            "protein_g": meal_targets["protein_g"],
            "fat_g": meal_targets["fat_g"],
            "carbs_g": meal_targets["carbs_g"],
        }

        # --- Retrieve user preferences (premium only) ---
        user_preferences_context = None
        preferences_snapshot: Dict[str, Any] = {
            "favorites": [],
            "dislikes": [],
            "favorite_keys": [],
            "dislike_keys": [],
        }
        try:
            user_id = str(authenticated_user["id"])
            from subscription import check_subscription_status
            is_premium = await check_subscription_status(user_id)
            if is_premium:
                from memory.preference_manager import PreferenceManager, build_preference_context
                preference_manager = PreferenceManager(supabase)

                favorites = preference_manager.get_favorites(user_id)
                dislikes = preference_manager.get_dislikes(user_id)

                user_preferences_context = build_preference_context(authenticated_user, favorites, dislikes)

                preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
            else:
                print(f"Skipping preferences for non-premium user {user_id}")
        except Exception as pref_error:
            print(f"Warning: Failed to retrieve preferences: {pref_error}")
            user_preferences_context = None
            preferences_snapshot = {
                "favorites": [],
                "dislikes": [],
                "favorite_keys": [],
                "dislike_keys": [],
            }

        # Exclude the other two meals so the agent only generates the requested one
        exclude_meals = sorted(VALID_MEALS - {meal_name})

        # Look up Gemini cache handle
        cache_name = None
        try:
            from menu_cache import get_cache_name
            _mp_hall = (req.dining_hall or "").strip().lower()
            _mapping = DINING_HALL_MAPPING.get(_mp_hall)
            if _mapping:
                _mp_univ, _mp_slug = _mapping
                cache_name = await get_cache_name(
                    dining_hall=_mp_slug,
                    university=_mp_univ,
                    date=req.date or today,
                    use_case="meal_plan",
                )
        except Exception as e:
            logger.debug(f"Meal plan cache lookup skipped: {e}")

        async def event_generator():
            """Generator function for Server-Sent Events"""
            try:
                # Yield a meal-specific thinking message first
                thinking_msg = {"type": "thinking", "content": f"Analyzing menu items for {meal_name}..."}
                yield f"data: {json.dumps(thinking_msg)}\n\n"

                async for chunk in generate_meal_plan_with_streaming(
                    user_payload=user_payload,
                    menu_payload=menu_payload,
                    nutrition_targets=nutrition_targets,
                    user_preferences_context=user_preferences_context,
                    exclude_meals=exclude_meals,
                    cached_content=cache_name,
                ):
                    if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                        # Make thinking messages meal-specific
                        content = chunk.get("content", "")
                        if "menu items" in content.lower() and meal_name not in content.lower():
                            chunk["content"] = f"Processing {meal_name} menu items..."
                        yield f"data: {json.dumps(chunk)}\n\n"
                        continue

                    if isinstance(chunk, dict) and chunk.get("type") == "complete":
                        # --- Post-process: fuzzy match items (only for the requested meal) ---
                        plan_data = chunk.get("meal_plan") or {}
                        if isinstance(plan_data, dict):
                            unknown_items: List[str] = []
                            used_names_global = set()
                            meal_obj = plan_data.get("meals", {}).get(meal_name)
                            if isinstance(meal_obj, dict):
                                new_items = []
                                for item in meal_obj.get("items", []) or []:
                                    if not isinstance(item, dict):
                                        continue
                                    nm = (item.get("name") or "").strip()
                                    if not nm:
                                        continue
                                    key_match = _fuzzy_lookup(nm, nutrition_lookup)
                                    if not key_match:
                                        unknown_items.append(nm)
                                        continue
                                    if key_match in used_names_global:
                                        continue
                                    used_names_global.add(key_match)
                                    servings = item.get("servings") or 1
                                    try:
                                        servings = float(servings)
                                        if servings <= 0:
                                            servings = 1.0
                                    except Exception:
                                        servings = 1.0

                                    sanitized_item = {**item}
                                    sanitized_item["name"] = nutrition_lookup[key_match]["name"]
                                    sanitized_item["servings"] = round(servings, 2)
                                    if not sanitized_item.get("serving_size"):
                                        sanitized_item["serving_size"] = nutrition_lookup[key_match].get("serving_size") or "1 serving"

                                    for macro_key in ("calories", "protein_g", "fat_g", "carbs_g"):
                                        value = sanitized_item.get(macro_key)
                                        if value is None:
                                            continue
                                        try:
                                            sanitized_item[macro_key] = float(value)
                                        except (TypeError, ValueError):
                                            sanitized_item[macro_key] = value

                                    new_items.append(sanitized_item)
                                meal_obj["items"] = new_items

                            if unknown_items:
                                plan_data.setdefault("meta", {})["dropped_unknown"] = unknown_items

                            # Set goals on the plan data
                            plan_data.setdefault("goals", {})["calories"] = meal_targets["calories"]
                            plan_data["goals"]["protein_g"] = meal_targets["protein_g"]
                            plan_data["goals"]["fat_g"] = meal_targets["fat_g"]
                            plan_data["goals"]["carbs_g"] = meal_targets["carbs_g"]

                        chunk["meal"] = meal_name
                        chunk["preferences_snapshot"] = preferences_snapshot
                        chunk["nutrition_targets"] = nutrition_targets
                        chunk["daily_targets"] = daily_targets
                        chunk["already_planned"] = already_planned

                        # --- Auto-save with "meal" key in meal_plan_data ---
                        try:
                            plan_id = str(uuid4())
                            response_payload = {
                                "success": True,
                                "meal": meal_name,
                                "meal_plan": chunk.get("meal_plan"),
                                "nutrition_targets": nutrition_targets,
                                "daily_targets": daily_targets,
                                "dining_hall": req.dining_hall,
                                "date": today,
                            }
                            save_data = {
                                "id": plan_id,
                                "user_id": authenticated_user["id"],
                                "dining_hall": req.dining_hall,
                                "date": today,
                                "meal_plan_data": {**response_payload, "meal": meal_name},
                                "created_at": datetime.now().isoformat(),
                                "updated_at": datetime.now().isoformat(),
                            }

                            logger.info(f"[SingleMeal-Stream] Attempting to auto-save {meal_name} for user {authenticated_user['id']} on {today}")
                            save_response = await asyncio.to_thread(
                                lambda: supabase.table("saved_meal_plans").insert(save_data).execute()
                            )

                            if save_response.data:
                                chunk["saved_plan_id"] = plan_id
                                chunk["auto_saved"] = True
                                logger.info(f"[SingleMeal-Stream] Auto-saved {meal_name} plan {plan_id} for user {authenticated_user['id']}")

                                # Check referral activation threshold
                                try:
                                    await check_and_fulfill_referral(str(authenticated_user["id"]))
                                except Exception as ref_err:
                                    logger.error(f"[referral] stream activation check failed: {ref_err}")
                            else:
                                chunk["auto_saved"] = False
                                logger.error(f"[SingleMeal-Stream] Failed to auto-save {meal_name} - no data returned")
                        except Exception as save_error:
                            chunk["auto_saved"] = False
                            logger.error(f"[SingleMeal-Stream] Auto-save failed: {save_error}")

                    # Format as SSE
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as e:
                print(f"Streaming single meal generation error: {e}")
                error_chunk = {"type": "error", "error": "Please try again."}
                yield f"data: {json.dumps(error_chunk)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable buffering for nginx
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Streaming single meal generation error: {e}")
        raise HTTPException(status_code=500, detail="Please try again.")


@app.post("/meal-plan/feedback", summary="Record user feedback (like/dislike) for a meal plan generation")
async def meal_plan_feedback(payload: MealPlanFeedbackRequest, current_user=Depends(get_current_user)):
    try:
        
        gen_id = payload.generation_id
        try:
            _ = UUID(gen_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid generation_id")
        
        existing = supabase.table("meal_plan_generations").select("id, user_id, liked").eq("id", gen_id).eq("user_id", current_user["id"]).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Generation not found")
        update_data = {"liked": payload.liked, "feedback_at": datetime.now().isoformat()}
        updated = supabase.table("meal_plan_generations").update(update_data).eq("id", gen_id).execute()
        return {"success": True, "generation_id": gen_id, "liked": payload.liked}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to record feedback: {e}")


@app.post("/meal-plan/save", summary="Save or update a meal plan")
async def save_meal_plan(
    payload: SaveMealPlanRequest, 
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user)
):
    """Save a new meal plan or update an existing one. Enforces one meal plan per day limit."""
    try:
        # Check if this is a new plan or an update
        is_update = payload.plan_id is not None
        
        if not is_update:
            # For new plans, check if a meal plan already exists for this date
            # NOTE: Disabled for testing so multiple meal plans per day can be created.
            # existing_plan = supabase.table("saved_meal_plans")\
            #     .select("id, dining_hall")\
            #     .eq("user_id", current_user["id"])\
            #     .eq("date", payload.date)\
            #     .execute()
            # 
            # if existing_plan.data and len(existing_plan.data) > 0:
            #     existing = existing_plan.data[0]
            #     raise HTTPException(
            #         status_code=409,
            #         detail=f"You already have a meal plan for {payload.date} at {existing['dining_hall']}. Please update or delete it first."
            #     )
            pass
        
        plan_id = payload.plan_id or str(uuid4())
        
        # Prepare data to save
        save_data = {
            "id": plan_id,
            "user_id": current_user["id"],
            "dining_hall": payload.dining_hall,
            "date": payload.date,
            "meal_plan_data": payload.meal_plan_data,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        
        # Use upsert to either create or update
        response = supabase.table("saved_meal_plans").upsert(save_data).execute()
        
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to save meal plan")
        
        # Send push notification when meal plan is created (not on updates)
        if not is_update:
            background_tasks.add_task(
                send_meal_plan_ready_notification,
                user_id=str(current_user["id"]),
                date=payload.date
            )
            logger.info(f"Scheduled push notification for user {current_user['id']} meal plan on {payload.date}")
        
        return {
            "success": True,
            "plan_id": plan_id,
            "message": "Meal plan saved successfully",
            "meal_plan": response.data[0]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error saving meal plan: {e}")
        raise HTTPException(status_code=500, detail="Failed to save meal plan")


@app.get("/meal-plan/history", summary="Get user's saved meal plans")
async def get_meal_plan_history(
    limit: int = 10,
    offset: int = 0,
    current_user=Depends(get_current_user)
):
    """Get list of user's saved meal plans, ordered by date (most recent first)"""
    try:
        response = supabase.table("saved_meal_plans")\
            .select("id, dining_hall, date, created_at, updated_at, meal_plan_data")\
            .eq("user_id", current_user["id"])\
            .order("date", desc=True)\
            .range(offset, offset + limit - 1)\
            .execute()
        
        plans = response.data if response.data else []
        
        logger.info(f"[MealPlanHistory] Found {len(plans)} meal plans for user {current_user['id']}")
        if plans:
            # Log dates of all plans found
            dates = [p.get('date') for p in plans]
            logger.info(f"[MealPlanHistory] Plan dates: {dates}")
            
            # Log structure of first plan for debugging
            first_plan = plans[0]
            has_meal_plan_data = 'meal_plan_data' in first_plan
            if has_meal_plan_data:
                meal_plan_data_keys = list(first_plan['meal_plan_data'].keys()) if first_plan['meal_plan_data'] else []
                has_nested_meal_plan = 'meal_plan' in (first_plan['meal_plan_data'] or {})
                has_totals = 'totals' in (first_plan['meal_plan_data'].get('meal_plan', {}) if first_plan['meal_plan_data'] else {})
                logger.info(f"[MealPlanHistory] First plan structure - has_meal_plan_data: {has_meal_plan_data}, keys: {meal_plan_data_keys}, has_nested_meal_plan: {has_nested_meal_plan}, has_totals: {has_totals}")
            else:
                logger.warning(f"[MealPlanHistory] First plan missing meal_plan_data field!")
        else:
            logger.warning(f"[MealPlanHistory] No meal plans found for user {current_user['id']}")
        
        return {
            "success": True,
            "plans": plans,
            "count": len(plans),
        }
    except Exception as e:
        logger.error(f"[MealPlanHistory] Error fetching meal plan history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch meal plans: {str(e)}")


@app.get("/meal-plan/today", summary="Get today's saved meal plan")
async def get_todays_meal_plan(current_user=Depends(get_current_user)):
    """Get the saved meal plan for today if it exists.
    
    This endpoint checks if the user has already generated a meal plan for today.
    Meal plans reset at the start of each new day (based on the date field).
    Automatically cleans up meal plans from previous days.
    """
    try:
        # Get today's date in the user's timezone (assuming US Eastern for Rutgers)
        # In production, you might want to store user timezone preference
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        
        # Auto-cleanup: Delete meal plans older than 7 days (background operation)
        try:
            cutoff_date = (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=7)).isoformat()
            cleanup_response = supabase.table("saved_meal_plans")\
                .delete()\
                .eq("user_id", current_user["id"])\
                .lt("date", cutoff_date)\
                .execute()
            deleted_count = len(cleanup_response.data) if cleanup_response.data else 0
            if deleted_count > 0:
                print(f"🧹 Auto-cleaned {deleted_count} meal plan(s) older than 7 days for user {current_user['id']}")
        except Exception as cleanup_error:
            # Don't fail the request if cleanup fails
            print(f"⚠️ Auto-cleanup failed: {cleanup_error}")
        
        # Query for today's meal plans (may include full-day + individual meals)
        response = supabase.table("saved_meal_plans")\
            .select("*")\
            .eq("user_id", current_user["id"])\
            .eq("date", today)\
            .order("created_at", desc=True)\
            .execute()

        if not response.data or len(response.data) == 0:
            return {
                "success": True,
                "has_plan": False,
                "meal_plan": None,
                "individual_meal_plans": None,
                "message": "No meal plan found for today"
            }

        # Separate full-day plans from individual meal plans
        full_day_plans = []
        individual_meals = []
        for plan in response.data:
            mpd = plan.get("meal_plan_data") or {}
            if mpd.get("meal"):
                individual_meals.append(plan)
            else:
                full_day_plans.append(plan)

        # Deduplicate individual meals: keep only the latest per meal type
        if individual_meals:
            seen_meals = {}
            for plan in individual_meals:
                meal_key = (plan.get("meal_plan_data") or {}).get("meal")
                if meal_key and meal_key not in seen_meals:
                    seen_meals[meal_key] = plan  # First seen = most recent (ordered desc)
            individual_meals = list(seen_meals.values())

        return {
            "success": True,
            "has_plan": bool(full_day_plans) or bool(individual_meals),
            "meal_plan": full_day_plans[0] if full_day_plans else None,
            "individual_meal_plans": individual_meals if individual_meals else None,
            "date": today
        }
    except Exception as e:
        print(f"Error fetching today's meal plan: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch today's meal plan: {str(e)}")


@app.get("/meal-plan/{plan_id}", summary="Get a specific saved meal plan")
async def get_meal_plan(plan_id: str, current_user=Depends(get_current_user)):
    """Get a specific meal plan by ID"""
    try:
        response = supabase.table("saved_meal_plans")\
            .select("*")\
            .eq("id", plan_id)\
            .eq("user_id", current_user["id"])\
            .execute()
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(status_code=404, detail="Meal plan not found")
        
        return {
            "success": True,
            "meal_plan": response.data[0]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching meal plan: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch meal plan: {str(e)}")


@app.put("/meal-plan/{plan_id}", summary="Update an existing meal plan")
async def update_meal_plan(
    plan_id: str,
    payload: SaveMealPlanRequest,
    current_user=Depends(get_current_user)
):
    """Update an existing meal plan"""
    try:
        # Verify the plan exists and belongs to the user
        existing = supabase.table("saved_meal_plans")\
            .select("id")\
            .eq("id", plan_id)\
            .eq("user_id", current_user["id"])\
            .execute()
        
        if not existing.data:
            raise HTTPException(status_code=404, detail="Meal plan not found")
        
        # Update the plan
        update_data = {
            "dining_hall": payload.dining_hall,
            "date": payload.date,
            "meal_plan_data": payload.meal_plan_data,
            "updated_at": datetime.now().isoformat(),
        }
        
        response = supabase.table("saved_meal_plans")\
            .update(update_data)\
            .eq("id", plan_id)\
            .execute()
        
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to update meal plan")
        
        return {
            "success": True,
            "message": "Meal plan updated successfully",
            "meal_plan": response.data[0]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating meal plan: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update meal plan: {str(e)}")


@app.delete("/meal-plan/{plan_id}", summary="Delete a saved meal plan")
async def delete_meal_plan(plan_id: str, current_user=Depends(get_current_user)):
    """Delete a saved meal plan"""
    try:
        response = supabase.table("saved_meal_plans")\
            .delete()\
            .eq("id", plan_id)\
            .eq("user_id", current_user["id"])\
            .execute()
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(status_code=404, detail="Meal plan not found")
        
        return {
            "success": True,
            "message": "Meal plan deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting meal plan: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete meal plan: {str(e)}")


@app.get("/meal-plan/debug", summary="Debug meal plan database")
async def debug_meal_plan_db(current_user=Depends(get_current_user)):
    """Debug endpoint to check meal plan database state."""
    if os.getenv("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
    try:
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        user_id = current_user["id"]

        # Check all meal plans for this user
        all_plans = supabase.table("saved_meal_plans")\
            .select("id, date, dining_hall, created_at")\
            .eq("user_id", user_id)\
            .execute()

        # Check today's meal plan specifically
        today_plan = supabase.table("saved_meal_plans")\
            .select("id, date")\
            .eq("user_id", user_id)\
            .eq("date", today)\
            .execute()

        # Test insert capability with a dummy record
        test_id = str(uuid4())
        test_result = None
        try:
            test_insert = supabase.table("saved_meal_plans").insert({
                "id": test_id,
                "user_id": user_id,
                "dining_hall": "test",
                "date": "1999-01-01",  # Use old date that won't conflict
                "meal_plan_data": {"test": True},
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }).execute()
            test_result = "INSERT_SUCCESS" if test_insert.data else "INSERT_FAILED_NO_DATA"
            # Clean up test record
            supabase.table("saved_meal_plans").delete().eq("id", test_id).execute()
        except Exception as e:
            test_result = f"INSERT_ERROR: {type(e).__name__}: {str(e)}"

        return {
            "success": True,
            "user_id": user_id,
            "user_id_type": type(user_id).__name__,
            "today": today,
            "all_plans_count": len(all_plans.data) if all_plans.data else 0,
            "all_plans": [{"id": p["id"], "date": p["date"], "dining_hall": p["dining_hall"]} for p in (all_plans.data or [])],
            "today_plan_found": bool(today_plan.data and len(today_plan.data) > 0),
            "today_plan": today_plan.data[0] if today_plan.data else None,
            "test_insert_result": test_result,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {str(e)}",
        }


@app.post("/meal-plan/cleanup-old", summary="Cleanup meal plans from previous days")
async def cleanup_old_meal_plans(current_user=Depends(get_current_user)):
    """Delete meal plans from previous days for the current user.

    This endpoint is automatically called when fetching today's meal plan,
    but can also be called manually to clean up old data.
    """
    try:
        # Calculate cutoff date (yesterday)
        cutoff_date = (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)).isoformat()
        
        # Delete all meal plans before today
        response = supabase.table("saved_meal_plans")\
            .delete()\
            .eq("user_id", current_user["id"])\
            .lt("date", cutoff_date)\
            .execute()
        
        deleted_count = len(response.data) if response.data else 0
        
        return {
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Cleaned up {deleted_count} old meal plan(s)"
        }
    except Exception as e:
        print(f"Error cleaning up old meal plans: {e}")
        # Don't fail hard on cleanup errors
        return {
            "success": False,
            "deleted_count": 0,
            "message": f"Cleanup failed: {str(e)}"
        }


@lru_cache(maxsize=1)
def load_universities_config() -> List[dict]:
    """Load universities and dining halls from config file"""
    config_path = os.path.join(os.path.dirname(__file__), 'universities_config.json')
    fallback_dining_halls = [
        {
            "id": "rutgers",
            "university": "Rutgers University",
            "name": "Rutgers University",
            "dining_halls": [
                {"id": "busch", "name": "Busch Dining Hall"},
                {"id": "livingston", "name": "Livingston Dining Commons"},
                {"id": "brower", "name": "Brower Commons"},
                {"id": "neilson", "name": "Neilson Dining Hall"},
                {"id": "the-atrium", "name": "The Atrium"},
            ],
        },
        {
            "id": "hopkins",
            "university": "Johns Hopkins University",
            "name": "Johns Hopkins University",
            "dining_halls": [
                {"id": "hopkins-cafe", "name": "Hopkins Cafe"},
                {"id": "nolans-on-33rd", "name": "Nolan's on 33rd"},
                {"id": "peabody-institute", "name": "Peabody Institute"},
            ],
        },
    ]
    
    try:
        if os.path.isfile(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                universities = config.get('universities', [])
                if universities:
                    logger.info(f"✅ Loaded {len(universities)} universities from config file")
                    return universities
                else:
                    logger.warning("⚠️ No universities found in config file, using fallback")
        else:
            logger.warning(f"⚠️ Universities config file not found at {config_path}, using fallback")
    except Exception as e:
        logger.error(f"⚠️ Failed to load universities config: {e}, using fallback")
    
    return fallback_dining_halls

@app.get("/dining-halls", summary="Get Supported Dining Halls")
async def get_supported_dining_halls():
    """Get list of supported dining halls from configuration"""
    return load_universities_config()


@app.get("/menu/{dining_hall}/{date}", summary="Get Dining Hall Menu")
async def get_menu(dining_hall: str, date: str, restaurant: Optional[str] = None, meal: Optional[str] = None):
    """Get menu for a specific dining hall and date (with Supabase caching)"""
    try:
        menu_data = await get_dining_hall_menu(dining_hall, date)
        if "error" in menu_data and menu_data["error"] is not None:
            raise HTTPException(status_code=500, detail=menu_data["error"])
        if restaurant and menu_data.get("items"):
            menu_data = dict(menu_data)
            menu_data["items"] = [
                item for item in menu_data.get("items", [])
                if (item.get("restaurant") or "").lower() == restaurant.lower()
            ]
        if meal and menu_data.get("items"):
            items_with_meal = [item for item in menu_data["items"] if item.get("meal")]
            if items_with_meal:
                menu_data = dict(menu_data) if not isinstance(menu_data, dict) else menu_data
                menu_data["items"] = [
                    item for item in menu_data["items"]
                    if (item.get("meal") or "").lower() == meal.lower()
                ]
        return menu_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch menu: {str(e)}")


@app.get("/menu/{dining_hall}/{date}/restaurants", summary="List restaurants in a dining hall")
async def get_menu_restaurants(dining_hall: str, date: str):
    """Get list of restaurants available at a dining hall for a given date."""
    try:
        menu_data = await get_dining_hall_menu(dining_hall, date)
        if "error" in menu_data and menu_data["error"] is not None:
            raise HTTPException(status_code=500, detail=menu_data["error"])
        # Use cached restaurants list if present
        restaurants = menu_data.get("restaurants")
        if restaurants is None:
            # Derive from item restaurant fields as fallback
            seen: set = set()
            restaurants = []
            for item in menu_data.get("items", []):
                r = item.get("restaurant")
                if r and r not in seen:
                    seen.add(r)
                    restaurants.append(r)
        return {"restaurants": restaurants}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch restaurants: {str(e)}")


# ==================== BARCODE LOOKUP ENDPOINT ====================

@app.post("/barcode/lookup", summary="Look up nutrition info by food barcode")
async def barcode_lookup(
    req: BarcodeLookupRequest,
    user: dict = Depends(get_current_user),
):
    """
    Look up a food product by barcode (EAN-13, UPC-A, etc.).
    Checks local cache first, then falls back to Open Food Facts API.
    """
    barcode = req.barcode.strip()
    if not barcode:
        raise HTTPException(status_code=400, detail="Barcode is required")

    # 1. Check cache
    try:
        cached = supabase.table("barcode_cache") \
            .select("product_data, created_at") \
            .eq("barcode", barcode) \
            .execute()

        if cached.data and len(cached.data) > 0:
            row = cached.data[0]
            # Check if cache is fresh (< 30 days)
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if datetime.now(ZoneInfo("UTC")) - created < timedelta(days=30):
                logger.info(f"Barcode cache hit: {barcode}")
                return BarcodeLookupResponse(
                    success=True,
                    message="Product found (cached)",
                    product=ScannedProduct(**row["product_data"]),
                )
    except Exception as e:
        logger.warning(f"Cache lookup failed for {barcode}: {e}")

    # 2. Call Open Food Facts API
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}"
            params = {
                "fields": "product_name,brands,nutriments,nutrition_grades,"
                          "image_front_url,serving_size,serving_quantity"
            }
            headers = {"User-Agent": "DormDish/1.0 (contact@dormdish.com)"}
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"Open Food Facts request failed for {barcode}: {e}")
        return BarcodeLookupResponse(
            success=False,
            message="Could not reach nutrition database. Please try again.",
        )

    if data.get("status") != 1 or not data.get("product"):
        return BarcodeLookupResponse(
            success=False,
            message="Product not found. Try scanning again or check the barcode.",
        )

    # 3. Normalize nutrition data
    p = data["product"]
    n = p.get("nutriments", {})

    # Prefer per-serving values; fall back to per-100g
    serving_size = p.get("serving_size") or "100g"
    serving_qty = p.get("serving_quantity")

    # If per-serving values exist, use them directly
    if n.get("energy-kcal_serving") is not None:
        cal = n.get("energy-kcal_serving", 0)
        pro = n.get("proteins_serving", 0)
        carb = n.get("carbohydrates_serving")
        fat = n.get("fat_serving")
        fib = n.get("fiber_serving")
        sug = n.get("sugars_serving")
    elif serving_qty and serving_qty > 0:
        # Convert per-100g to per-serving
        factor = serving_qty / 100.0
        cal = (n.get("energy-kcal_100g") or 0) * factor
        pro = (n.get("proteins_100g") or 0) * factor
        carb = (n.get("carbohydrates_100g") or 0) * factor if n.get("carbohydrates_100g") is not None else None
        fat = (n.get("fat_100g") or 0) * factor if n.get("fat_100g") is not None else None
        fib = (n.get("fiber_100g") or 0) * factor if n.get("fiber_100g") is not None else None
        sug = (n.get("sugars_100g") or 0) * factor if n.get("sugars_100g") is not None else None
    else:
        # Use per-100g as-is
        cal = n.get("energy-kcal_100g", 0)
        pro = n.get("proteins_100g", 0)
        carb = n.get("carbohydrates_100g")
        fat = n.get("fat_100g")
        fib = n.get("fiber_100g")
        sug = n.get("sugars_100g")

    product = ScannedProduct(
        barcode=barcode,
        name=p.get("product_name") or "Unknown Product",
        brand=p.get("brands"),
        serving_size=serving_size,
        calories=round(cal or 0, 1),
        protein_g=round(pro or 0, 1),
        carbs_g=round(carb, 1) if carb is not None else None,
        fat_g=round(fat, 1) if fat is not None else None,
        fiber_g=round(fib, 1) if fib is not None else None,
        sugar_g=round(sug, 1) if sug is not None else None,
        image_url=p.get("image_front_url"),
        nutri_score=p.get("nutrition_grades"),
    )

    # 4. Cache the result
    try:
        supabase.table("barcode_cache").upsert({
            "barcode": barcode,
            "product_data": product.model_dump(),
            "source": "openfoodfacts",
            "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to cache barcode {barcode}: {e}")

    return BarcodeLookupResponse(
        success=True,
        message="Product found",
        product=product,
    )


# ==================== FOOD LOG ENDPOINTS ====================

@app.post("/food-log/add", summary="Add item to food log")
async def add_food_log_entry(
    req: FoodLogAddRequest,
    user: dict = Depends(get_current_user),
):
    """Add a scanned or manually entered food item to the daily food log."""
    try:
        entry_data = {
            "user_id": str(user["id"]),
            "date": req.date,
            "meal_type": req.meal_type,
            "barcode": req.barcode,
            "name": req.name,
            "brand": req.brand,
            "serving_size": req.serving_size,
            "servings": req.servings,
            "calories": req.calories,
            "protein_g": req.protein_g,
            "carbs_g": req.carbs_g,
            "fat_g": req.fat_g,
            "fiber_g": req.fiber_g,
            "sugar_g": req.sugar_g,
            "image_url": req.image_url,
        }
        result = supabase.table("food_log").insert(entry_data).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to save food log entry")

        # Check referral activation threshold
        try:
            await check_and_fulfill_referral(str(user["id"]))
        except Exception as e:
            logger.error(f"[referral] activation check failed: {e}")

        return {"success": True, "message": "Item logged", "entry": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add food log entry: {e}")
        raise HTTPException(status_code=500, detail="Failed to log food item")


@app.get("/food-log/{date}", summary="Get food log entries for a date")
async def get_food_log(
    date: str,
    user: dict = Depends(get_current_user),
):
    """Get all food log entries for a specific date."""
    try:
        result = supabase.table("food_log") \
            .select("*") \
            .eq("user_id", str(user["id"])) \
            .eq("date", date) \
            .order("created_at") \
            .execute()

        return {
            "success": True,
            "message": "Food log retrieved",
            "entries": result.data or [],
        }
    except Exception as e:
        logger.error(f"Failed to get food log: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get food log: {str(e)}")


@app.delete("/food-log/{entry_id}", summary="Delete a food log entry")
async def delete_food_log_entry(
    entry_id: str,
    user: dict = Depends(get_current_user),
):
    """Delete a food log entry by ID."""
    try:
        result = supabase.table("food_log") \
            .delete() \
            .eq("id", entry_id) \
            .eq("user_id", str(user["id"])) \
            .execute()

        return {"success": True, "message": "Entry deleted"}
    except Exception as e:
        logger.error(f"Failed to delete food log entry: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete entry: {str(e)}")


# ==================== FOOD SCAN ENDPOINTS ====================

from food_scan_models import FoodScanFeedbackRequest


@app.post('/food-scan/analyze', summary='Analyze a food photo using AI')
async def food_scan_analyze(
    image: UploadFile = File(...),
    dining_hall: str = Form(...),
    date: str = Form(...),
    description: Optional[str] = Form(None),
    current_user=Depends(get_current_user),
):
    from food_scan_endpoints import handle_food_scan_analyze
    return await handle_food_scan_analyze(image, dining_hall, date, description, current_user)


@app.post('/food-scan/feedback', summary='Submit feedback on food scan accuracy')
async def food_scan_feedback(
    data: FoodScanFeedbackRequest,
    current_user=Depends(get_current_user),
):
    from food_scan_endpoints import handle_food_scan_feedback
    return await handle_food_scan_feedback(data, current_user, supabase)


# ==================== PREFERENCE MANAGEMENT ENDPOINTS ====================
# These endpoints use Supabase to store and retrieve user preferences, favorites, and dislikes

@app.post("/preferences/favorite", summary="Add item to favorites")
async def add_favorite(
    favorite_data: FavoriteRequest,
    current_user=Depends(require_premium)
):
    """Add a meal plan or food item to user's favorites"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])
        print(f"[API] /preferences/favorite - User ID: {user_id}, Email: {current_user.get('email', 'N/A')}")
        print(f"[API] Item: {favorite_data.item_name}, Type: {favorite_data.item_type}")

        if favorite_data.item_type == "meal_plan":
            result = preference_manager.add_favorite_meal_plan(
                user_id=user_id,
                meal_plan_id=favorite_data.meal_plan_id or "",
                meal_plan_name=favorite_data.item_name,
                meal_type=favorite_data.meal_type
            )
        else:
            result = preference_manager.add_favorite_food_item(
                user_id=user_id,
                food_item_name=favorite_data.item_name,
                meal_type=favorite_data.meal_type
            )

        # Only return essential info, not all memories
        return {
            "status": "success",
            "message": f"Added {favorite_data.item_name} to favorites",
            "item_name": favorite_data.item_name,
            "item_type": favorite_data.item_type,
            "meal_type": favorite_data.meal_type
        }
    except Exception as e:
        logger.error(f"Failed to add favorite: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to add favorite: {str(e)}")


@app.post("/preferences/dislike", summary="Dislike a meal plan or food item")
async def dislike_item(
    dislike_data: DislikeRequest,
    current_user=Depends(require_premium)
):
    """Record that user dislikes a meal plan or food item"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        preference_manager.add_dislike(
            user_id=user_id,
            item_name=dislike_data.item_name,
            item_type=dislike_data.item_type,
            meal_type=dislike_data.meal_type,
            reason=dislike_data.reason
        )

        # Only return essential info, not all memories
        return {
            "status": "success",
            "message": f"Recorded dislike for {dislike_data.item_name}",
            "item_name": dislike_data.item_name,
            "item_type": dislike_data.item_type,
            "meal_type": dislike_data.meal_type
        }
    except Exception as e:
        logger.error(f"Failed to record dislike: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to record dislike: {str(e)}")


@app.get("/preferences/favorites", summary="Get user's favorites")
async def get_favorites(
    meal_type: Optional[str] = None,
    current_user=Depends(require_premium)
):
    """Get all favorited items for a user"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        favorites = preference_manager.get_favorites(user_id, meal_type)

        return {
            "status": "success",
            "count": len(favorites),
            "favorites": favorites
        }
    except Exception as e:
        logger.error(f"Failed to retrieve favorites: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve favorites: {str(e)}")


@app.get("/preferences/dislikes", summary="Get user's dislikes")
async def get_dislikes_endpoint(
    meal_type: Optional[str] = None,
    current_user=Depends(require_premium)
):
    """Get all disliked items for a user"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        dislikes = preference_manager.get_dislikes(user_id, meal_type)

        return {
            "status": "success",
            "count": len(dislikes),
            "dislikes": dislikes
        }
    except Exception as e:
        logger.error(f"Failed to retrieve dislikes: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve dislikes: {str(e)}")


@app.get("/preferences/all", summary="Get all user preferences")
async def get_all_preferences(
    current_user=Depends(require_premium)
):
    """Get all preferences (favorites, dislikes, profile) for a user"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        favorites = preference_manager.get_favorites(user_id)
        dislikes = preference_manager.get_dislikes(user_id)

        # Profile data from users table (already in current_user)
        profile = {
            "bmr": current_user.get("bmr"),
            "dietary_preference": current_user.get("dietary_preference"),
            "dietary_restrictions": current_user.get("dietary_restrictions"),
            "goal": current_user.get("goal"),
        }

        return {
            "status": "success",
            "user_id": user_id,
            "favorites": favorites,
            "dislikes": dislikes,
            "profile": profile,
            "counts": {
                "favorites": len(favorites),
                "dislikes": len(dislikes)
            }
        }
    except Exception as e:
        logger.error(f"Failed to retrieve preferences: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve preferences: {str(e)}")


@app.get("/preferences/debug", summary="Debug: Get ALL raw preferences")
async def debug_all_preferences(
    current_user=Depends(require_premium)
):
    """Get ALL raw preferences for debugging"""
    if os.getenv("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        all_prefs = preference_manager.get_all_preferences(user_id)

        return {
            "status": "success",
            "user_id": user_id,
            "total_preferences": len(all_prefs),
            "preferences": all_prefs
        }
    except Exception as e:
        logger.error(f"Failed to retrieve debug preferences: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve debug preferences: {str(e)}")


@app.delete("/preferences/favorite/{item_name}", summary="Remove from favorites")
async def remove_favorite(
    item_name: str,
    current_user=Depends(require_premium)
):
    """Remove an item from user's favorites"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        result = preference_manager.remove_favorite(user_id, item_name)

        # Return success even if not found (idempotent operation)
        # This handles cases where item was already removed or never existed
        if result.get("status") == "not_found":
            return {
                "status": "success",
                "message": f"{item_name} not found in favorites (may have been already removed)",
                "was_found": False
            }

        return {
            "status": "success",
            "message": f"Removed {item_name} from favorites",
            "was_found": True
        }
    except Exception as e:
        logger.error(f"Failed to remove favorite: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to remove favorite: {str(e)}")


@app.delete("/preferences/dislike/{item_name}", summary="Remove from dislikes")
async def remove_dislike(
    item_name: str,
    current_user=Depends(require_premium)
):
    """Remove an item from user's dislikes"""
    try:
        from memory.preference_manager import PreferenceManager
        preference_manager = PreferenceManager(supabase)

        user_id = str(current_user["id"])

        result = preference_manager.remove_dislike(user_id, item_name)

        # Return success even if not found (idempotent operation)
        # This handles cases where item was already removed or never existed
        if result.get("status") == "not_found":
            return {
                "status": "success",
                "message": f"{item_name} not found in dislikes (may have been already removed)",
                "was_found": False
            }

        return {
            "status": "success",
            "message": f"Removed {item_name} from dislikes",
            "was_found": True
        }
    except Exception as e:
        logger.error(f"Failed to remove dislike: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to remove dislike: {str(e)}")




async def _send_meal_plan_generation_email_async(user_row: dict, generation_id: Optional[str], dining_hall: str, date: str) -> None:
    if not user_row or not user_row.get("email"):
        return
    if not BREVO_MEAL_PLAN_TEMPLATE_ID:
        return
    try:
        template_id = int(re.match(r"(\d+)", BREVO_MEAL_PLAN_TEMPLATE_ID).group(1))
    except Exception:
        print("[brevo] Invalid BREVO_MEAL_PLAN_TEMPLATE_ID value; skipping meal plan email")
        return

    try:
        guess = _guess_name_from_email(user_row.get("email") or "")
        await upsert_brevo_contact(
            user_row["email"],
            attributes={
                "FIRSTNAME": (user_row.get("username") or guess.get("first") or ""),
                "LASTNAME": guess.get("last") or "",
            },
        )
    except Exception as exc:
        print(f"[brevo] contact upsert failed (continuing): {exc}")

    from_email = os.getenv("BREVO_FROM_EMAIL")
    from_name = os.getenv("BREVO_FROM_NAME", "DormDish")
    if not from_email:
        print("[brevo] BREVO_FROM_EMAIL not set; skipping meal plan email")
        return

    params = {
        "USERNAME": user_row.get("username") or guess.get("first") or "",
        "GENERATION_ID": generation_id or "",
        "DINING_HALL": dining_hall,
        "DATE": date,
    }
    payload = {
        "templateId": template_id,
        "to": [{"email": user_row["email"], "name": user_row.get("username") or guess.get("first") or None}],
        "sender": {"email": from_email, "name": from_name},
        "params": params,
        "tags": ["meal_plan_generated"],
    }
    client = _get_http_client()
    try:
        response = await client.post(f"{_brevo_base_url()}/smtp/email", headers=_brevo_headers(), json=payload)
        if response.status_code not in (200, 201):
            print(f"[brevo] meal plan email send failed status={response.status_code} body={response.text[:200]}")
        else:
            print(f"[brevo] meal plan email sent generation_id={generation_id}")
    except Exception as exc:
        print(f"[brevo] meal plan email error: {exc}")


def send_meal_plan_generation_email(user_row: dict, generation_id: Optional[str], dining_hall: str, date: str):
    """Dispatch background task to send meal plan generation email."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_send_meal_plan_generation_email_async(user_row, generation_id, dining_hall, date))
    else:
        loop.create_task(_send_meal_plan_generation_email_async(user_row, generation_id, dining_hall, date))


# ============================================================================
# Google Calendar Integration Endpoints
# ============================================================================

# Google Calendar service loaded lazily inside calendar endpoints
_calendar_service = None

def _get_calendar_service():
    global _calendar_service
    if _calendar_service is None:
        from google_calendar_service import GoogleCalendarService
        _calendar_service = GoogleCalendarService()
    return _calendar_service

class GoogleCalendarAuthRequest(BaseModel):
    """Request to initiate Google Calendar OAuth"""
    pass

class GoogleCalendarCallbackRequest(BaseModel):
    """OAuth callback data"""
    code: str
    state: str

class GoogleCalendarEventsRequest(BaseModel):
    """Request to get calendar events for a specific date"""
    date: str  # ISO format YYYY-MM-DD

class ExportMealPlanRequest(BaseModel):
    """Request to export meal plan to Google Calendar"""
    plan_id: str
    date: str  # ISO format YYYY-MM-DD


@app.post('/calendar/auth/initiate', summary='Initiate Google Calendar OAuth flow')
async def initiate_google_calendar_auth(current_user=Depends(get_current_user)):
    """
    Start Google Calendar OAuth flow
    Returns authorization URL for user to visit
    """
    try:
        # Generate a random state for CSRF protection
        import secrets as sec
        state = sec.token_urlsafe(32)
        
        # Store state in session (you may want to use Redis or similar)
        # For now, we'll include user_id in the state
        state_with_user = f"{current_user['id']}:{state}"
        
        # Create authorization URL
        auth_url = _get_calendar_service().create_authorization_url(state_with_user)
        
        return {
            "success": True,
            "authorization_url": auth_url,
            "state": state_with_user
        }
    except Exception as e:
        logger.error(f"Failed to initiate Google Calendar auth: {e}")
        raise HTTPException(status_code=500, detail="Failed to initiate authorization")


@app.get('/calendar/auth/callback', summary='Handle Google Calendar OAuth callback (GET redirect)')
async def google_calendar_callback_get(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None)
):
    """
    Handle OAuth callback from Google (GET request with query params).
    Google redirects here after user authorization. Processes callback and redirects to app.
    """
    if error:
        # User denied access or error occurred
        logger.warning(f"OAuth callback error: {error}")
        redirect_url = f"dormdish://calendar-callback?error={error}"
        return RedirectResponse(url=redirect_url)
    
    if not code or not state:
        logger.error("OAuth callback missing code or state")
        redirect_url = "dormdish://calendar-callback?error=missing_params"
        return RedirectResponse(url=redirect_url)
    
    try:
        # URL decode state in case Google encoded it
        from urllib.parse import unquote
        
        # Log received state for debugging
        logger.info(f"[OAuth] Received state: {state}")
        logger.info(f"[OAuth] Received code: {code[:20] if code else None}...")
        
        # Try to decode if it looks URL-encoded
        decoded_state = state
        try:
            decoded_state = unquote(state)
            if decoded_state != state:
                logger.info(f"[OAuth] State was URL-encoded, decoded: {decoded_state[:50]}...")
                state = decoded_state
        except Exception:
            # If unquote fails, use original state
            pass
        
        # State should be in format: "user_id:random_token"
        # Check if it's valid before splitting
        if not state or not isinstance(state, str):
            logger.error(f"[OAuth] State is None or invalid type: {type(state)}")
            redirect_url = "dormdish://calendar-callback?error=invalid_state"
            return RedirectResponse(url=redirect_url)
        
        # Extract user_id from state
        if ':' not in state:
            logger.error(f"[OAuth] Invalid state format - no colon separator: {state}")
            logger.error(f"[OAuth] State length: {len(state)}, first 50 chars: {state[:50]}")
            redirect_url = "dormdish://calendar-callback?error=invalid_state"
            return RedirectResponse(url=redirect_url)
        
        state_parts = state.split(':', 1)
        if len(state_parts) != 2:
            logger.error(f"[OAuth] Invalid state format - wrong split result: {state_parts}")
            redirect_url = "dormdish://calendar-callback?error=invalid_state"
            return RedirectResponse(url=redirect_url)
        
        user_id = state_parts[0].strip()
        state_token = state_parts[1].strip()
        
        # Verify user_id is not empty
        if not user_id:
            logger.error(f"[OAuth] Empty user_id in state: {state}")
            redirect_url = "dormdish://calendar-callback?error=invalid_state"
            return RedirectResponse(url=redirect_url)
        
        logger.info(f"[OAuth] Extracted user_id: {user_id}, token_length: {len(state_token)}")
        
        # Exchange code for credentials (pass the ORIGINAL state as received from Google)
        # But use the decoded state for the exchange
        creds_dict = _get_calendar_service().exchange_code_for_credentials(code, state)
        
        # Store credentials in database
        try:
            supabase.table("users").update({
                "google_calendar_credentials": creds_dict,
                "google_calendar_connected": True,
                "google_calendar_connected_at": datetime.now().isoformat()
            }).eq("id", user_id).execute()
            
            logger.info(f"Stored Google Calendar credentials for user {user_id}")
        except Exception as db_err:
            logger.error(f"Failed to store credentials: {db_err}")
            redirect_url = "dormdish://calendar-callback?error=save_failed"
            return RedirectResponse(url=redirect_url)
        
        # Redirect to app with success
        redirect_url = "dormdish://calendar-callback?success=true"
        return RedirectResponse(url=redirect_url)
        
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        import traceback
        traceback.print_exc()
        redirect_url = "dormdish://calendar-callback?error=callback_failed"
        return RedirectResponse(url=redirect_url)


@app.post('/calendar/auth/callback', summary='Handle Google Calendar OAuth callback (POST)')
async def google_calendar_callback_post(payload: GoogleCalendarCallbackRequest):
    """
    Handle OAuth callback via POST (for manual API calls if needed)
    """
    try:
        # Extract user_id from state
        state_parts = payload.state.split(':', 1)
        if len(state_parts) != 2:
            raise HTTPException(status_code=400, detail="Invalid state parameter")
        
        user_id = state_parts[0]
        
        # Exchange code for credentials
        creds_dict = _get_calendar_service().exchange_code_for_credentials(
            payload.code,
            payload.state
        )
        
        # Store credentials in database
        try:
            # Update user with Google Calendar credentials
            supabase.table("users").update({
                "google_calendar_credentials": creds_dict,
                "google_calendar_connected": True,
                "google_calendar_connected_at": datetime.now().isoformat()
            }).eq("id", user_id).execute()
            
            logger.info(f"Stored Google Calendar credentials for user {user_id}")
        except Exception as db_err:
            logger.error(f"Failed to store credentials: {db_err}")
            raise HTTPException(status_code=500, detail="Failed to save credentials")
        
        return {
            "success": True,
            "message": "Google Calendar connected successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to complete authorization")


@app.get('/auth/google/callback', summary='Handle Google OAuth callback for sign-in/sign-up')
async def auth_google_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None)
):
    """
    Handle OAuth callback from Google for user authentication.
    Exchanges authorization code for ID token and redirects to mobile app.
    """
    if error:
        # User denied access or error occurred
        logger.warning(f"Google OAuth callback error: {error}")
        redirect_url = f"dormdish://auth-callback?error={error}"
        return RedirectResponse(url=redirect_url)
    
    if not code:
        logger.error("OAuth callback missing authorization code")
        redirect_url = "dormdish://auth-callback?error=missing_code"
        return RedirectResponse(url=redirect_url)
    
    try:
        # Exchange authorization code for ID token
        google_client_id = os.getenv("GOOGLE_CLIENT_ID")
        google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        redirect_uri = os.getenv("GOOGLE_AUTH_REDIRECT_URI") or os.getenv("GOOGLE_REDIRECT_URI")
        
        if not google_client_id or not google_client_secret:
            logger.error("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET")
            redirect_url = "dormdish://auth-callback?error=server_config_error"
            return RedirectResponse(url=redirect_url)
        
        if not redirect_uri:
            # Default to deployed backend callback URL
            redirect_uri = "https://dormdish-backend-git-test-raghav-mehtas-projects-9e41f86c.vercel.app/auth/google/callback"
            logger.info(f"[auth.google.callback] Using default redirect URI: {redirect_uri}")
        else:
            logger.info(f"[auth.google.callback] Using redirect URI from env: {redirect_uri}")
        
        logger.info(f"[auth.google.callback] Received code={code[:20] if code else None}..., state={state[:20] if state else None}...")
        
        # Exchange code for tokens
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "code": code,
            "client_id": google_client_id,
            "client_secret": google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
        
        def _exchange_token():
            """Synchronous function to exchange authorization code for tokens"""
            try:
                resp = httpx.post(token_url, data=token_data, timeout=10.0)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text if e.response else str(e)
                logger.error(f"Token exchange HTTP error: {e.response.status_code} - {error_detail}")
                logger.error(f"Token exchange request data: client_id={google_client_id[:20]}..., redirect_uri={redirect_uri}")
                raise
            except Exception as e:
                logger.error(f"Token exchange exception: {type(e).__name__}: {e}")
                raise
        
        token_response = await asyncio.to_thread(_exchange_token)
        
        # Extract ID token from response
        id_token = token_response.get("id_token")
        if not id_token:
            logger.error("No ID token in token exchange response")
            redirect_url = "dormdish://auth-callback?error=no_id_token"
            return RedirectResponse(url=redirect_url)
        
        # Verify the ID token
        try:
            token_data = await _verify_google_id_token(id_token)
            email = token_data.get('email', '').lower()
            given_name = token_data.get('given_name') or ''
            family_name = token_data.get('family_name') or ''
            
            if not email:
                raise HTTPException(status_code=400, detail="Google token missing email")
        except Exception as e:
            logger.error(f"Failed to verify ID token: {e}")
            redirect_url = f"dormdish://auth-callback?error=token_verification_failed"
            return RedirectResponse(url=redirect_url)
        
        # Encode the ID token and user info for the app
        from urllib.parse import urlencode
        params = urlencode({
            'id_token': id_token,
            'email': email,
            'given_name': given_name,
            'family_name': family_name,
        })
        
        # Redirect to app with ID token
        redirect_url = f"dormdish://auth-callback?{params}"
        logger.info(f"Redirecting to app with Google auth data for {email}")
        return RedirectResponse(url=redirect_url)
        
    except httpx.HTTPStatusError as e:
        logger.error(f"Token exchange failed: {e.response.text}")
        redirect_url = "dormdish://auth-callback?error=token_exchange_failed"
        return RedirectResponse(url=redirect_url)
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        import traceback
        traceback.print_exc()
        redirect_url = "dormdish://auth-callback?error=callback_failed"
        return RedirectResponse(url=redirect_url)


@app.get('/calendar/status', summary='Check Google Calendar connection status')
async def check_calendar_status(current_user=Depends(get_current_user)):
    """Check if user has connected their Google Calendar"""
    is_connected = current_user.get('google_calendar_connected', False)
    connected_at = current_user.get('google_calendar_connected_at')
    
    return {
        "success": True,
        "connected": is_connected,
        "connected_at": connected_at
    }


@app.post('/calendar/events/day', summary='Get calendar events for a specific day')
async def get_calendar_events_for_day(
    payload: GoogleCalendarEventsRequest,
    current_user=Depends(get_current_user)
):
    """
    Get all calendar events for a specific day
    """
    try:
        # Check if user has connected calendar
        creds_dict = current_user.get('google_calendar_credentials')
        if not creds_dict:
            raise HTTPException(
                status_code=400,
                detail="Google Calendar not connected. Please connect your calendar first."
            )
        
        # Refresh credentials if needed
        refreshed_creds = _get_calendar_service().refresh_credentials_if_needed(creds_dict)
        if refreshed_creds:
            try:
                supabase.table("users").update({
                    "google_calendar_credentials": refreshed_creds
                }).eq("id", current_user['id']).execute()
                creds_dict = refreshed_creds
                logger.info(f"[Calendar] Credentials refreshed for user {current_user['id']}")
            except Exception as e:
                logger.error(f"[Calendar] Failed to save refreshed credentials: {e}")
        
        # Parse date
        try:
            date_obj = datetime.fromisoformat(payload.date).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        # Get events
        events = _get_calendar_service().get_events_for_day(
            creds_dict,
            date_obj,
            timezone="America/New_York"  # TODO: Get from user preferences
        )
        
        return {
            "success": True,
            "date": payload.date,
            "events": events,
            "count": len(events)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get calendar events: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch calendar events")


@app.post('/calendar/meal-slots/find', summary='Find optimal meal time slots')
async def find_meal_time_slots(
    payload: GoogleCalendarEventsRequest,
    current_user=Depends(get_current_user)
):
    """
    Analyze user's schedule and find optimal meal time slots
    """
    try:
        # Check if user has connected calendar
        creds_dict = current_user.get('google_calendar_credentials')
        if not creds_dict:
            raise HTTPException(
                status_code=400,
                detail="Google Calendar not connected. Please connect your calendar first."
            )
        
        # Refresh credentials if needed
        refreshed_creds = _get_calendar_service().refresh_credentials_if_needed(creds_dict)
        if refreshed_creds:
            try:
                supabase.table("users").update({
                    "google_calendar_credentials": refreshed_creds
                }).eq("id", current_user['id']).execute()
                creds_dict = refreshed_creds
                logger.info(f"[Calendar] Credentials refreshed for user {current_user['id']}")
            except Exception as e:
                logger.error(f"[Calendar] Failed to save refreshed credentials: {e}")
        
        # Parse date
        try:
            date_obj = datetime.fromisoformat(payload.date).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        # Get events for the day
        events = _get_calendar_service().get_events_for_day(
            creds_dict,
            date_obj,
            timezone="America/New_York"
        )
        
        # Find meal slots
        meal_slots = _get_calendar_service().find_meal_time_slots(
            events,
            date_obj,
            timezone="America/New_York"
        )
        
        return {
            "success": True,
            "date": payload.date,
            "events_count": len(events),
            "meal_slots": meal_slots,
            "events": events  # Include events for context
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to find meal slots: {e}")
        raise HTTPException(status_code=500, detail="Failed to find meal time slots")


@app.post('/calendar/export-meal-plan', summary='Export meal plan to Google Calendar')
async def export_meal_plan_to_calendar(
    payload: ExportMealPlanRequest,
    current_user=Depends(get_current_user)
):
    """
    Export a meal plan to Google Calendar
    Creates calendar events for each meal at optimal times
    """
    try:
        # Check if user has connected calendar
        creds_dict = current_user.get('google_calendar_credentials')
        if not creds_dict:
            raise HTTPException(
                status_code=400,
                detail="Google Calendar not connected. Please connect your calendar first."
            )
        
        # ✅ FIX: Refresh credentials if needed and save back to database
        refreshed_creds = _get_calendar_service().refresh_credentials_if_needed(creds_dict)
        if refreshed_creds:
            logger.info(f"[Calendar] Credentials were expired, refreshing for user {current_user['id']}")
            # Always use refreshed credentials for this request (even if DB save fails)
            creds_dict = refreshed_creds
            try:
                # Update the database with new credentials
                update_result = supabase.table("users").update({
                    "google_calendar_credentials": refreshed_creds
                }).eq("id", current_user['id']).execute()
                
                if update_result.data:
                    logger.info(f"[Calendar] ✅ Credentials refreshed and saved to database")
                else:
                    logger.warning(f"[Calendar] Failed to save refreshed credentials to database (will use in-memory)")
            except Exception as update_error:
                logger.error(f"[Calendar] Error updating credentials in database: {update_error}")
                # Continue with the request using refreshed creds in memory
        
        # Parse date
        try:
            date_obj = datetime.fromisoformat(payload.date).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        # Get the meal plan from database
        try:
            plan_result = supabase.table("saved_meal_plans")\
                .select("*")\
                .eq("id", payload.plan_id)\
                .eq("user_id", current_user['id'])\
                .execute()
            
            if not plan_result.data:
                raise HTTPException(status_code=404, detail="Meal plan not found")
            
            saved_plan = plan_result.data[0]
            meal_plan_data = saved_plan.get('meal_plan_data', {})
            
            # Extract meal plan (handle nested structure)
            meal_plan = meal_plan_data.get('meal_plan', meal_plan_data)
            dining_hall = saved_plan.get('dining_hall', 'Dining Hall')
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch meal plan: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch meal plan")
        
        # Get events for the day
        events = _get_calendar_service().get_events_for_day(
            creds_dict,
            date_obj,
            timezone="America/New_York"
        )
        
        # Find meal slots
        meal_slots = _get_calendar_service().find_meal_time_slots(
            events,
            date_obj,
            timezone="America/New_York"
        )
        
        # Export to calendar
        created_events = _get_calendar_service().export_meal_plan_to_calendar(
            creds_dict,
            meal_plan,
            meal_slots,
            date_obj,
            dining_hall
        )
        
        return {
            "success": True,
            "message": "Meal plan exported to Google Calendar",
            "events_created": created_events,
            "meal_slots": meal_slots
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export meal plan: {e}", exc_info=True)
        # Return actual error message to help debugging
        error_detail = str(e) if str(e) else "Failed to export meal plan to calendar"
        raise HTTPException(status_code=500, detail="Calendar export failed")

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
                        "max_referrals": 7,
                    }
            except Exception:
                continue  # Code collision, retry

        raise HTTPException(status_code=500, detail="Failed to generate referral code")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[referral.generate] error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate referral code")


@app.get("/referral/validate-code/{code}", summary="Validate a referral code (public)")
async def validate_referral_code(code: str):
    """Public endpoint to check if a referral code is valid and active.
    Used by onboarding screen before user is authenticated."""
    try:
        code_upper = code.strip().upper()
        if len(code_upper) != 6:
            return {"valid": False, "message": "Code must be 6 characters"}

        code_res = supabase.table("referral_codes").select("id, is_active, successful_referrals, max_referrals").eq("code", code_upper).execute()

        if not code_res.data:
            return {"valid": False, "message": "Invalid referral code"}

        row = code_res.data[0]
        if not row["is_active"]:
            return {"valid": False, "message": "This referral code is no longer active"}

        if row["successful_referrals"] >= row["max_referrals"]:
            return {"valid": False, "message": "This referral code has reached its limit"}

        return {"valid": True, "message": "Valid referral code"}
    except Exception as e:
        logger.error(f"[referral.validate] error: {e}")
        return {"valid": False, "message": "Unable to validate code"}


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
                "max_referrals": 7,
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
                "max_referrals": 7,
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


@app.post("/referral/webhook/revenuecat", summary="RevenueCat webhook for referral bonus")
async def revenuecat_webhook(request: Request):
    """Handle RevenueCat webhook events.
    Grants referrer a bonus free month when a referred user makes their first purchase.
    """
    try:
        # Validate webhook authorization
        auth_header = request.headers.get("Authorization")
        if not REVENUECAT_WEBHOOK_AUTH_KEY:
            logger.error("[referral.webhook] REVENUECAT_WEBHOOK_AUTH_KEY not configured")
            raise HTTPException(status_code=503, detail="Webhook not configured")
        if auth_header != f"Bearer {REVENUECAT_WEBHOOK_AUTH_KEY}":
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
            "bonus_granted_at": datetime.now(timezone.utc).isoformat(),
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


# ==================== ANNOUNCEMENT ENDPOINTS ====================

ANNOUNCEMENT_ADMIN_KEY = os.getenv("ANNOUNCEMENT_ADMIN_KEY", "dormdish-admin-2026")


def require_admin_key(x_admin_key: str = Header(None)):
    """Verify the admin API key for announcement management endpoints."""
    if not x_admin_key or x_admin_key != ANNOUNCEMENT_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")


def _parse_version_tuple(version_str: str):
    """Parse a version string like '1.2.3' into a comparable tuple."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


@app.get("/api/announcements")
async def get_announcements(
    platform: str = Query("ios"),
    app_version: str = Query("1.0.0"),
    university_id: str = Query(None),
    user: dict = Depends(get_current_user),
):
    """Fetch active announcements filtered by targeting rules."""
    now = datetime.utcnow().isoformat()

    query = supabase.table("announcements") \
        .select("*") \
        .eq("is_active", True) \
        .lte("start_date", now)

    result = query.execute()

    filtered = []
    for ann in result.data:
        # Filter expired
        if ann.get("end_date") and ann["end_date"] < now:
            continue

        # Filter by platform
        platforms = ann.get("target_platforms") or ["ios"]
        if platform not in platforms:
            continue

        # Filter by min app version
        min_version = ann.get("target_min_app_version")
        if min_version:
            if _parse_version_tuple(app_version) < _parse_version_tuple(min_version):
                continue

        # Filter by university
        target_unis = ann.get("target_university_ids")
        if target_unis and university_id and university_id not in target_unis:
            continue

        # Map to response format
        filtered.append({
            "id": ann["id"],
            "type": ann["type"],
            "title": ann["title"],
            "body": ann.get("body"),
            "icon_name": ann.get("icon_name"),
            "image_url": ann.get("image_url"),
            "cta_text": ann.get("cta_text"),
            "cta_deeplink": ann.get("cta_deeplink"),
            "bg_color": ann.get("bg_color"),
            "priority": ann.get("priority", 80),
            "source": "server",
            "max_impressions": ann.get("max_impressions_per_user", 1),
            "auto_dismiss_seconds": ann.get("auto_dismiss_seconds"),
            "start_date": ann.get("start_date"),
            "end_date": ann.get("end_date"),
            "screen": ann.get("screen", "home"),
        })

    filtered.sort(key=lambda x: x["priority"], reverse=True)
    return {"announcements": filtered}


@app.post("/api/announcements")
async def create_announcement(
    payload: dict = Body(...),
    _: None = Depends(require_admin_key),
):
    """Create a new announcement (admin only — requires X-Admin-Key header)."""
    result = supabase.table("announcements").insert(payload).execute()
    return {"success": True, "announcement": result.data[0] if result.data else None}


@app.put("/api/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: str,
    payload: dict = Body(...),
    _: None = Depends(require_admin_key),
):
    """Update an existing announcement (admin only — requires X-Admin-Key header)."""
    payload["updated_at"] = datetime.utcnow().isoformat()
    result = supabase.table("announcements").update(payload).eq("id", announcement_id).execute()
    return {"success": True, "announcement": result.data[0] if result.data else None}


@app.delete("/api/announcements/{announcement_id}")
async def delete_announcement(
    announcement_id: str,
    _: None = Depends(require_admin_key),
):
    """Delete an announcement (admin only — requires X-Admin-Key header)."""
    supabase.table("announcements").delete().eq("id", announcement_id).execute()
    return {"success": True, "message": "Announcement deleted"}


@app.post("/api/announcements/{announcement_id}/interact")
async def track_announcement_interaction(
    announcement_id: str,
    payload: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """Track an announcement interaction (impression, tap, dismiss)."""
    interaction_type = payload.get("interaction_type")
    if interaction_type not in ("impression", "tap", "dismiss"):
        raise HTTPException(status_code=400, detail="Invalid interaction type")

    supabase.table("announcement_interactions").insert({
        "announcement_id": announcement_id,
        "user_id": user["id"],
        "interaction_type": interaction_type,
    }).execute()

    return {"success": True}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)