import asyncio
import json
import logging
import os
import random
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import bcrypt
import csv
import httpx
import traceback
from difflib import get_close_matches
from dotenv import load_dotenv
from fastapi import Body, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, EmailStr
from supabase import Client, create_client

import secrets as _secrets_for_google

try:
    from extract import build_menu_url as fp_build_menu_url, fetch as fp_fetch, parse_menu as fp_parse_menu
except Exception as exc:
    print(f"⚠️ Failed to import FoodPro scraper helpers: {exc}")
    traceback.print_exc()
    fp_build_menu_url = None  
    fp_fetch = None  
    fp_parse_menu = None  

from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


load_dotenv()

logger = logging.getLogger("dormdish.api")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

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


from meal_plan_agent import generate_meal_plan_with_agent

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

SUPABASE_URL = os.getenv("SUPABASE_URL", "your_supabase_url_here")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "your_supabase_service_key_here")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def generate_session_token() -> str:
    """Generate a secure session token"""
    return secrets.token_urlsafe(32)

def create_user_session(user_id: str, expires_hours: int = 24) -> str:
    """Create a new user session and return the token"""
    token = generate_session_token()
    expires_at = datetime.now() + timedelta(hours=expires_hours)
    
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
        
        response = supabase.table("user_sessions")\
            .select("*, users(*)")\
            .eq("session_token", token)\
            .gt("expires_at", datetime.now().isoformat())\
            .execute()
        
        if response.data and len(response.data) > 0:
            session = response.data[0]
            
            supabase.table("users")\
                .update({"last_login": datetime.now().isoformat()})\
                .eq("id", session["user_id"])\
                .execute()
            return session["users"]
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
    try:
        payload = build_brevo_template_payload(str(email))
        await _send_brevo_email_async(payload)
        return {"success": True, "message": "Test email dispatched."}
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Test email failed: {exc}")




UNIVERSITIES_CSV_PATH = os.path.join(os.path.dirname(__file__), 'us-colleges-and-universities.csv') if os.path.exists(os.path.join(os.path.dirname(__file__), 'us-colleges-and-universities.csv')) else os.path.join(os.getcwd(), 'us-colleges-and-universities.csv')

@lru_cache(maxsize=1)
def load_universities_list() -> List[str]:
    universities: List[str] = []
    try:
        path = UNIVERSITIES_CSV_PATH
        if not os.path.isfile(path):
            print(f"⚠️ Universities CSV not found at {path}")
            return universities
        with open(path, 'r', encoding='utf-8') as f:
            
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;|\t')
                delim = dialect.delimiter
            except Exception:
                
                first_line = sample.splitlines()[0] if sample else ''
                delim = ';' if ';' in first_line else ("\t" if '\t' in first_line else ',')

            reader = csv.DictReader(f, delimiter=delim)
            header_lower = [h.lower() for h in (reader.fieldnames or [])]
            name_key = None
            for key in ['name', 'university', 'school', 'institution']:
                if key in header_lower:
                    name_key = reader.fieldnames[header_lower.index(key)]
                    break
            if name_key:
                for row in reader:
                    val = (row.get(name_key) or '').strip()
                    if val:
                        universities.append(val)
            else:
                
                f.seek(0)
                reader2 = csv.reader(f, delimiter=delim)
                first_row = True
                for row in reader2:
                    if first_row:
                        first_row = False
                        
                        if any((col or '').strip().lower() == 'name' for col in row):
                            continue
                    if row:
                        name = (row[0] or '').strip()
                        if name:
                            universities.append(name)
    except Exception as e:
        print(f"⚠️ Failed to load universities CSV: {e}")
    
    seen = set()
    deduped: List[str] = []
    for u in universities:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped

@app.get('/universities')
def get_universities():
    return {"universities": load_universities_list()}




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
    if aud_expected and data.get("aud") != aud_expected:
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


@app.post('/auth/register')
def auth_register(data: RegisterRequest):
    try:
        
        existing = supabase.table("users").select("id").eq("email", str(data.email).lower()).execute()
        if existing.data and len(existing.data) > 0:
            raise HTTPException(status_code=400, detail="An account with this email already exists")

        
        pwd_hash = hash_password(data.password)

        
        metrics = calculate_bmr_tdee(
            age=int(data.age),
            height_feet=int(data.height_feet),
            height_inches=int(data.height_inches),
            weight_lbs=float(data.weight_lbs),
            gender=data.gender,
            activity_level=data.activity_level,
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
        }

        # Try inserting the user. If the DB rejects due to a unique constraint on
        # username (common when users share the same display name), automatically
        # attempt a small number of retries by appending a numeric suffix to the
        # username. This avoids returning an internal server error for a
        # username collision while preserving the email-uniqueness check above.
        max_attempts = 5
        base_username = (payload.get("username") or "user").strip()
        if not base_username:
            # Fall back to a name derived from email local-part if username empty
            base_username = str(data.email).split("@")[0]

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
                    raise HTTPException(status_code=500, detail=f"Supabase insert error: {msg}")

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
                raise HTTPException(status_code=500, detail=f"Registration failed: {e}")

        if not user_row:
            # All attempts exhausted
            raise HTTPException(status_code=500, detail="Failed to create user after resolving username conflicts")

        
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        
        
        user_row["session_token"] = token  
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
        metrics = calculate_bmr_tdee(
            age=int(data.age),
            height_feet=int(data.height_feet),
            height_inches=int(data.height_inches),
            weight_lbs=float(data.weight_lbs),
            gender=data.gender,
            activity_level=data.activity_level,
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
        }
        print(f"[google.register] inserting user payload keys={list(payload.keys())}")
        # Same username-collision handling as auth_register: try a few suffixes
        max_attempts = 5
        base_username = (payload.get("username") or "user").strip()
        if not base_username:
            base_username = email.split("@")[0]

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
                    raise HTTPException(status_code=500, detail=f"Supabase insert error: {msg}")
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
                raise HTTPException(status_code=500, detail=f"Google registration failed: {e}")

        if not user_row:
            raise HTTPException(status_code=500, detail="Failed to create user after resolving username conflicts")
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")
        user_row["session_token"] = token
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




class UpdateProfileRequest(BaseModel):
    age: Optional[int] = None
    height_feet: Optional[int] = None
    height_inches: Optional[int] = None
    weight_lbs: Optional[float] = None
    gender: Optional[str] = None
    activity_level: Optional[str] = None
    goal: Optional[str] = None
    university: Optional[str] = None
    
    dietary_preference: Optional[str] = None

class TrackWeightRequest(BaseModel):
    weight_lbs: float
    date: Optional[str] = None  # ISO format date, defaults to today if not provided

class MealPlanRequest(BaseModel):
    dining_hall: str
    date: str

class MealPlanFeedbackRequest(BaseModel):
    generation_id: str
    liked: bool

class SaveMealPlanRequest(BaseModel):
    """Request to save or update a meal plan"""
    dining_hall: str
    date: str
    meal_plan_data: dict  # Contains meals, totals, goals, etc.
    plan_id: Optional[str] = None  # If provided, updates existing plan

class MealPlanItemUpdate(BaseModel):
    """Individual meal item for editing"""
    name: str
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    serving_size: Optional[str] = None
    servings: Optional[float] = 1.0
class TokenData(BaseModel):
    user_id: str

@app.get("/health", summary="Health Check")
async def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        supabase.table("users").select("id").limit(1).execute()
        return {
            "status": "healthy",
            "database": "connected",
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


async def save_menu_to_supabase(university: str, dining_hall: str, date: str, menu_data: dict) -> bool:
    """Persist menu data to Supabase without blocking the event loop."""

    def _save() -> bool:
        try:
            menu_id = f"{university}_{dining_hall}_{date}"
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
            print(f"🔍 Saving menu with {items_count} items to Supabase")
            print(f"🔍 Menu data keys: {list(menu_data.keys())}")
            stored_menu_data = menu_data.get("menu", menu_data)
            data_to_insert = {
                "id": menu_id,
                "university": university,
                "dining_hall": dining_hall,
                "hall_slug": hall_slug,
                "date": date,
                "fetched_at": fetched_at,
                "menu_data": stored_menu_data,
                "created_at": datetime.now().isoformat(),
            }
            response = supabase.table("menus").upsert(data_to_insert).execute()
            print(f"💾 Saved menu to Supabase: {menu_id}")
            print(f"💾 Supabase response data length: {len(response.data) if response.data else 0}")
            return True
        except Exception as exc:
            print(f"⚠️ Error saving to Supabase: {exc}")
            print(f"⚠️ Error type: {type(exc)}")
            return False

    return await asyncio.to_thread(_save)


async def get_dining_hall_menu(dining_hall: str, date: str) -> dict:
    """Fetch dining hall menu - cache in Supabase, else fetch from Nutrislice / FoodPro"""
    
    dh_norm = dining_hall.strip().lower()
    if dh_norm == "nielson":  
        dh_norm = "neilson"
    if dh_norm in {"nielson-dining-hall", "nielson dining hall"}:
        dh_norm = "neilson"
    dining_hall = dh_norm

    dining_hall_mapping = {
        
        "hopkins-cafe": ("hopkins", "hopkins-cafe"),
        "hopkins cafe": ("hopkins", "hopkins-cafe"),
        "hopkins": ("hopkins", "hopkins-cafe"),
        "nolans-on-33rd": ("hopkins", "nolans-on-33rd"),
        "nolans": ("hopkins", "nolans-on-33rd"),
        "nolan": ("hopkins", "nolans-on-33rd"),
        "peabody-institute": ("hopkins", "peabody-institute"),
        "peabody": ("hopkins", "peabody-institute"),
        
        "busch": ("rutgers", "busch-dining-hall"),
        "busch-dining-hall": ("rutgers", "busch-dining-hall"),
        "busch dining hall": ("rutgers", "busch-dining-hall"),
        "livingston": ("rutgers", "livingston-dining-commons"),
        "livingston-dining-hall": ("rutgers", "livingston-dining-commons"),
        "livingston dining hall": ("rutgers", "livingston-dining-commons"),
        "livingston dining commons": ("rutgers", "livingston-dining-commons"),
        "brower": ("rutgers", "brower-commons"),
        "brower-commons": ("rutgers", "brower-commons"),
        "brower commons": ("rutgers", "brower-commons"),
        "college-ave": ("rutgers", "brower-commons"),
        "college ave": ("rutgers", "brower-commons"),
        "college avenue": ("rutgers", "brower-commons"),
        
        "neilson": ("rutgers", "neilson-dining-hall"),
        "neilson-dining-hall": ("rutgers", "neilson-dining-hall"),
        "neilson dining hall": ("rutgers", "neilson-dining-hall"),
        "nielson": ("rutgers", "neilson-dining-hall"),
        "nielson-dining-hall": ("rutgers", "neilson-dining-hall"),
        "nielson dining hall": ("rutgers", "neilson-dining-hall"),
        "cook-douglass": ("rutgers", "neilson-dining-hall"),
        "cook douglass": ("rutgers", "neilson-dining-hall"),
        "cook/douglass": ("rutgers", "neilson-dining-hall"),
        
        "the-atrium": ("rutgers", "the-atrium"),
        "the atrium": ("rutgers", "the-atrium"),
        "atrium": ("rutgers", "the-atrium"),
    }
    mapping = dining_hall_mapping.get(dining_hall)
    if not mapping:
        return {"error": f"Unknown dining hall: {dining_hall}"}
    university, hall_slug = mapping

    
    if not date or date.lower() == "today":
        date = _rutgers_today_str() if university == "rutgers" else datetime.utcnow().strftime("%Y-%m-%d")

    cached_menu = await get_menu_from_supabase(university, dining_hall, date)
    if cached_menu is not None:
        print(f"✅ Using cached menu from Supabase")
        return cached_menu

    if university == "rutgers" and hall_slug in {"livingston-dining-commons", "neilson-dining-hall", "the-atrium"}:
        print(f"🌐 Fetching fresh menu from FoodPro for Rutgers - {hall_slug}")
        try:
            menu_data = await fetch_foodpro_menu_rutgers(dining_hall, hall_slug, date)
            if not menu_data.get("items"):
                print(f"⚠️ FoodPro returned no items, skipping cache save")
                return {"error": f"No menu items available for {hall_slug} on {date}"}
            save_result = await save_menu_to_supabase(university, dining_hall, date, menu_data)
            print(f"💾 Save to Supabase result: {save_result}")
            return menu_data
        except Exception as e:
            print(f"❌ FoodPro fetch failed: {e}")
            return {"error": f"FoodPro fetch failed for {hall_slug}: {e}"}

    print(f"🌐 Fetching fresh menu from Nutrislice API for {university} - {hall_slug}")
    try:
        menu_data = await fetch_nutrislice_menu(university, hall_slug, dining_hall, date)
        if not menu_data.get("items"):
            print(f"⚠️ Nutrislice returned no items, skipping cache save")
            return {"error": f"No menu items available for {hall_slug} on {date}"}
        save_result = await save_menu_to_supabase(university, dining_hall, date, menu_data)
        print(f"💾 Save to Supabase result: {save_result}")
        return menu_data
    except Exception as e:
        return {"error": f"Failed to fetch menu: {str(e)}"}


async def fetch_foodpro_menu_rutgers(dining_hall: str, hall_slug: str, date: str) -> dict:
    """Fetch menu from Rutgers FoodPro pickmenu pages and format like Nutrislice output.
    Now aggregates breakfast, lunch, and dinner by invoking the scraper per meal.
    """
    if not (fp_build_menu_url and fp_parse_menu):
        raise RuntimeError("FoodPro scraper not available")

    
    
    location_map = {
        "livingston-dining-commons": {"locationNum": "03", "locationName": "Livingston Dining Commons"},
        "neilson-dining-hall": {"locationNum": "04", "locationName": "Neilson Dining Hall"},
        
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

    def _add_food_obj_from_new_schema(food: dict, items: List[dict]):
        if not isinstance(food, dict):
            return
        name = food.get("name")
        if not name:
            return
        out_item: Dict[str, object] = {"name": name}
        prot = food.get("g_protein")
        if prot is None:
            prot = food.get("protein_g")
        if prot is not None:
            out_item["protein_g"] = prot
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

        for meal in meals_sequence:
            meal_url = _set_meal_in_url(base_url, meal)
            logger.debug("foodpro_meal_request meal=%s url=%s", meal, meal_url)
            try:
                html_text = await _fetch_with_retries(meal_url)
            except Exception as exc:
                warnings.append(f"Failed to fetch {meal}: {exc}")
                continue
            try:
                parsed = fp_parse_menu(html_text, meal_type=meal, date=date, base_url=meal_url)
            except Exception as exc:
                warnings.append(f"Failed to parse {meal}: {exc}")
                continue

            
            items: List[dict] = []
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                
                for it in parsed.get("items", []):
                    if not isinstance(it, dict):
                        continue
                    name = it.get("name")
                    if not name:
                        continue
                    
                    out_item: Dict[str, object] = {"name": name}
                    if it.get("protein_g") is not None:
                        out_item["protein_g"] = it.get("protein_g")
                    if it.get("calories") is not None:
                        out_item["calories"] = it.get("calories")
                    if it.get("serving_size"):
                        out_item["serving_size"] = it.get("serving_size")
                    items.append(out_item)
            else:
                
                if isinstance(parsed, dict):
                    meal_map = parsed.get(meal) or {}
                    blocks = meal_map.get(date) or [] if isinstance(meal_map, dict) else []
                    if isinstance(blocks, list):
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue
                            foods = block.get("foods") or []
                            if isinstance(foods, list):
                                for food in foods:
                                    _add_food_obj_from_new_schema(food, items)

            if items:
                meal_sources.append(meal)
                all_items.extend(items)
            else:
                warnings.append(f"No items found for {meal}")

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

    return {
        "university": "rutgers",
        "hall": dining_hall,
        "hall_slug": hall_slug,
        "date": date,
        "fetched_at": datetime.now().isoformat(),
        "items": unique_items,
        "menu": {"items": unique_items, "meal_sources": meal_sources or ["foodpro"]},
        "meta": {"warnings": warnings, "notes": ["source: FoodPro"]},
        "error": None,
    }


async def fetch_nutrislice_menu(university: str, hall_slug: str, dining_hall: str, date: str) -> dict:
    """Fetch menu directly from Nutrislice API"""
    api_bases = {
        "rutgers": "https://rutgers.api.nutrislice.com/menu/api/weeks/school",
        "hopkins": "https://hopkinsdining.api.nutrislice.com/menu/api/weeks/school",
    }
    meal_configs = {
        "rutgers": ["breakfast", "lunch-test", "dinner"],
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
    else:
        meal_slugs = meal_configs[university]
    all_items: List[dict] = []
    meal_sources: List[str] = []
    warnings: List[str] = []
    for meal_slug in meal_slugs:
        try:
            items = await fetch_meal_items(api_base, hall_slug, meal_slug, date)
            if items:
                all_items.extend(items)
                meal_name = meal_slug.split("-")[-1] if "-" in meal_slug else meal_slug
                if meal_name == "dinner-2":
                    meal_name = "lunch"
                elif meal_name == "lunch-test":
                    meal_name = "lunch"
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
        "university": university,
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
    """Process a menu item and extract nutritional information"""
    name = None
    if isinstance(item.get("food"), dict):
        name = item["food"].get("name")
    if not name:
        name = item.get("name") or item.get("item_name")
    if not name:
        return None
    nutrition: Dict[str, Optional[float]] = {}
    if isinstance(item.get("food"), dict):
        food = item["food"]
        rni = food.get("rounded_nutrition_info") or {}
        serving_info = food.get("serving_size_info") or {}
        nutrition = {
            "protein_g": rni.get("g_protein"),
            "calories": rni.get("calories"),
            "serving_size": f"{serving_info.get('serving_size_amount', '')} {serving_info.get('serving_size_unit', '')}".strip(),
        }
    else:
        nutrition = {
            "protein_g": item.get("g_protein") or item.get("protein"),
            "calories": item.get("calories") or item.get("kcal"),
            "serving_size": item.get("serving_size") or item.get("portion_size"),
        }
    result: Dict[str, object] = {"name": name}
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
        if cal is None or prot is None:
            continue
        try:
            lines.append(f"{nm} | {int(round(cal))} cal | {int(round(prot))} g protein")
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
        ' "totals": { "calories": "<int>", "protein_g": "<int>" },',
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
        '{ "name": "<exact item>", "servings": "<number>", "calories": "<number>", "protein_g": "<number>", "serving_size": "<string|null>" }',
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
        "Calories & protein must be within ±5% of goals.",
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

@app.put("/auth/profile", summary="Update User Profile")
async def update_profile(profile_data: UpdateProfileRequest, current_user=Depends(get_current_user)):
    """Update user profile"""
    try:
        update_data: Dict[str, object] = {}
        
        for field, value in profile_data.model_dump(exclude_unset=True).items():
            # Special-case: allow clearing dietary_preference by explicitly setting it to null/None
            if field == 'dietary_preference' and value is None:
                update_data[field] = None
                continue

            if value is not None:
                if field == 'goal' and value not in ['cut', 'maintain', 'bulk']:
                    raise HTTPException(status_code=400, detail="Invalid goal. Must be 'cut', 'maintain', or 'bulk'")
                if field == 'dietary_preference' and value not in ['vegan', 'vegetarian', 'pescetarian']:
                    raise HTTPException(status_code=400, detail="Invalid dietary_preference. Must be 'vegan', 'vegetarian', or 'pescetarian'")
                update_data[field] = value
        if any(field in update_data for field in ['age', 'height_feet', 'height_inches', 'weight_lbs', 'gender', 'activity_level']):
            current_age = update_data.get('age', current_user['age'])  
            current_height_feet = update_data.get('height_feet', current_user['height_feet'])  
            current_height_inches = update_data.get('height_inches', current_user['height_inches'])  
            current_weight = update_data.get('weight_lbs', current_user['weight_lbs'])  
            current_gender = update_data.get('gender', current_user['gender'])  
            current_activity = update_data.get('activity_level', current_user['activity_level'])  
            bmr_data = calculate_bmr_tdee(
                int(current_age), int(current_height_feet), int(current_height_inches), float(current_weight), str(current_gender), str(current_activity)
            )
            update_data['bmr'] = bmr_data['bmr']
            update_data['tdee'] = bmr_data['tdee']
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
        raise HTTPException(status_code=500, detail=f"Failed to update profile: {str(e)}")


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
        
        # Also update the user's current weight in profile
        profile_update = supabase.table("users")\
            .update({"weight_lbs": weight_data.weight_lbs})\
            .eq("id", current_user["id"])\
            .execute()
        
        # Recalculate BMR and TDEE with new weight
        if all(key in current_user for key in ['age', 'height_feet', 'height_inches', 'gender', 'activity_level']):
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
    current_user=Depends(get_current_user)
):
    """
    Get user's weight history, ordered by date (most recent first).
    Returns weight entries for tracking progress over time.
    """
    try:
        response = supabase.table("weight_history")\
            .select("id, weight_lbs, date, created_at, updated_at")\
            .eq("user_id", current_user["id"])\
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


@app.post("/generate-meal-plan", summary="Generate Meal Plan")
async def generate_meal_plan_route(meal_plan_request: MealPlanRequest, authorization: Optional[str] = Header(None), background_tasks: BackgroundTasks = None):
    """Generate a meal plan for a user (authenticated) with model-provided macros and totals.
    Auto-saves the meal plan to the database. Returns legacy MealPlanResponse wrapper expected by frontend.
    
    Rate limit: One meal plan per day. Meal plans reset at the start of each new day.
    """
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
        metrics = calculate_bmr_tdee(
            age=user.age,
            height_feet=user.height_feet,
            height_inches=user.height_inches,
            weight_lbs=user.weight_lbs,
            gender=authenticated_user.get('gender'),
            activity_level=user.activity_level,
        )
        user_bmr = float(metrics["bmr"])
        user_tdee = float(metrics["tdee"])
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
            serving_size = raw.get("serving_size") or None
            entry = {
                "name": name,
                "calories": float(calories),
                "protein_g": float(protein),
                "serving_size": serving_size,
                "density": (protein / calories) if calories else 0.0,
            }
            key = name.upper()
            if key not in nutrition_lookup:  
                nutrition_lookup[key] = entry
                allowed_for_prompt.append(entry)

        if len(nutrition_lookup) == 0:
            raise HTTPException(status_code=500, detail="No usable menu items after processing")

        
        goal_mode = user.goal.lower()
        if goal_mode == 'cut':
            calorie_target = int(round(user_tdee * 0.85))
        elif goal_mode == 'bulk':
            calorie_target = int(round(user_tdee * 1.10))
        else:
            calorie_target = int(round(user_tdee))
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))

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
        agent_nutrition_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "bmr": user_bmr,
            "tdee": user_tdee,
            "goal": goal_mode,
            "activity_breakdown": selected_activity_metrics or None,
        }

        ai_start = time.time()
        try:
            agent_result, agent_raw_text = await generate_meal_plan_with_agent(
                user_payload=agent_user_payload,
                menu_payload=agent_menu_payload,
                nutrition_targets=agent_nutrition_targets,
            )
        except ValueError as e:
            raise HTTPException(status_code=500, detail=f"Agent execution failed: {e}")
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

                for macro_key in ("calories", "protein_g"):
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

        plan_json.setdefault('goals', {})['calories'] = calorie_target
        plan_json['goals']['protein_g'] = protein_target
        plan_json.setdefault('user_stats', {})['bmr'] = user_bmr
        plan_json['user_stats']['tdee'] = user_tdee
        plan_json['user_stats']['goal'] = goal_mode
        plan_json['user_stats']['dietary_preference'] = user.dietary_preference or None
        plan_json['user_stats']['activity_breakdown'] = selected_activity_metrics or None

        
        totals = plan_json.get('totals')
        if not isinstance(totals, dict):
            raise HTTPException(status_code=500, detail="Model response missing totals block")
        try:
            final_cal = float(totals['calories'])
            final_prot = float(totals['protein_g'])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=500, detail="Model response missing valid totals")
        totals['calories'] = final_cal
        totals['protein_g'] = final_prot
        cal_diff = final_cal - calorie_target
        prot_diff = final_prot - protein_target
        cal_pct = (cal_diff / calorie_target * 100) if calorie_target else 0
        prot_pct = (prot_diff / protein_target * 100) if protein_target else 0
        
        print(f"📊 Final Plan Metrics:")
        print(f"   Calories: {final_cal:.0f} / {calorie_target} ({cal_pct:+.1f}%)")
        print(f"   Protein:  {final_prot:.0f}g / {protein_target}g ({prot_pct:+.1f}%)")

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
            "nutrition_targets": {"calories": calorie_target, "protein_g": protein_target},
            "bmr_tdee": {
                "bmr": user_bmr,
                "tdee": user_tdee,
                "activity_breakdown": selected_activity_metrics or None,
                "bmr_precise": metrics.get("bmr_precise"),
                "tdee_precise": metrics.get("tdee_precise"),
            },
            "available_items": allowed_for_prompt,
            "meal_plan": plan_json,
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
            
            save_response = supabase.table("saved_meal_plans").insert(save_data).execute()
            
            if save_response.data:
                response_payload["saved_plan_id"] = plan_id
                response_payload["auto_saved"] = True
                print(f"✅ Auto-saved meal plan {plan_id} for user {authenticated_user['id']}")
            else:
                print(f"⚠️ Failed to auto-save meal plan, but generation succeeded")
                response_payload["auto_saved"] = False
        except Exception as save_error:
            # Don't fail the entire request if save fails
            print(f"⚠️ Auto-save failed: {save_error}")
            response_payload["auto_saved"] = False
        
        return response_payload

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Meal plan generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Meal plan generation failed: {e}")


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
        
        metrics = calculate_bmr_tdee(
            age=user.age,
            height_feet=user.height_feet,
            height_inches=user.height_inches,
            weight_lbs=user.weight_lbs,
            gender=authenticated_user.get('gender'),
            activity_level=user.activity_level,
        )
        user_bmr = float(metrics["bmr"])
        user_tdee = float(metrics["tdee"])

        # Get menu data
        menu_data = await get_dining_hall_menu(meal_plan_request.dining_hall, meal_plan_request.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        
        items_list = menu_data.get("items", []) or []
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
            serving_size = raw.get("serving_size") or None
            entry = {
                "name": name,
                "calories": float(calories),
                "protein_g": float(protein),
                "serving_size": serving_size,
                "density": (protein / calories) if calories else 0.0,
            }
            key = name.upper()
            if key not in nutrition_lookup:
                nutrition_lookup[key] = entry
                allowed_for_prompt.append(entry)

        # Calculate targets
        goal_mode = user.goal.lower()
        if goal_mode == 'cut':
            calorie_target = int(round(user_tdee * 0.85))
        elif goal_mode == 'bulk':
            calorie_target = int(round(user_tdee * 1.10))
        else:
            calorie_target = int(round(user_tdee))
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))

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
        
        nutrition_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
        }

        async def event_generator():
            """Generator function for Server-Sent Events"""
            try:
                async for chunk in generate_meal_plan_with_streaming(
                    user_payload=user_payload,
                    menu_payload=menu_payload,
                    nutrition_targets=nutrition_targets
                ):
                    # Format as SSE
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as e:
                error_chunk = {"type": "error", "error": str(e)}
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
        raise HTTPException(status_code=500, detail=f"Streaming meal plan generation failed: {e}")


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
async def save_meal_plan(payload: SaveMealPlanRequest, current_user=Depends(get_current_user)):
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
        raise HTTPException(status_code=500, detail=f"Failed to save meal plan: {str(e)}")


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
        
        return {
            "success": True,
            "plans": plans,
            "count": len(plans),
        }
    except Exception as e:
        print(f"Error fetching meal plan history: {e}")
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
        
        # Query for today's meal plan
        response = supabase.table("saved_meal_plans")\
            .select("*")\
            .eq("user_id", current_user["id"])\
            .eq("date", today)\
            .execute()
        
        if not response.data or len(response.data) == 0:
            return {
                "success": True,
                "has_plan": False,
                "meal_plan": None,
                "message": "No meal plan found for today"
            }
        
        meal_plan = response.data[0]
        
        return {
            "success": True,
            "has_plan": True,
            "meal_plan": meal_plan,
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


@app.get("/dining-halls", summary="Get Supported Dining Halls")
async def get_supported_dining_halls():
    """Get list of supported dining halls"""
    fallback_dining_halls = [
            {
                "university": "rutgers",
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
                "university": "hopkins",
                "name": "Johns Hopkins University",
                "dining_halls": [
                    {"id": "hopkins-cafe", "name": "Hopkins Cafe"},
                    {"id": "nolans-on-33rd", "name": "Nolan's on 33rd"},
                    {"id": "peabody-institute", "name": "Peabody Institute"},
                ],
            },
        ]
        
    return fallback_dining_halls


@app.get("/menu/{dining_hall}/{date}", summary="Get Dining Hall Menu")
async def get_menu(dining_hall: str, date: str):
    """Get menu for a specific dining hall and date (with Supabase caching)"""
    try:
        menu_data = await get_dining_hall_menu(dining_hall, date)
        if "error" in menu_data and menu_data["error"] is not None:
            raise HTTPException(status_code=500, detail=menu_data["error"])
        return menu_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch menu: {str(e)}")



import logging
try:
    import stripe  
except Exception:
    stripe = None  


def _stripe_version():
    if not stripe:
        return None
    for attr in ('__version__', 'VERSION'):
        v = getattr(stripe, attr, None)
        if isinstance(v, str):
            return v
    
    try:
        from stripe import _version  
        v = getattr(_version, 'VERSION', None)
        if isinstance(v, str):
            return v
    except Exception:
        pass
    return None


DEBUG_BILLING = os.getenv("DEBUG_BILLING", "0") in ("1", "true", "True")

def _get_stripe_api_key() -> Optional[str]:
    """Return first present Stripe secret key from supported env var names."""
    for k in [
        "STRIPE_API_KEY",
        "STRIPE_SECRET_KEY",
        "STRIPE_SECRET",
        "STRIPE_KEY",
    ]:
        val = os.getenv(k)
        if val and val.strip():
            return val.strip()
    return None

STRIPE_API_KEY = _get_stripe_api_key()
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")  
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")  
if STRIPE_API_KEY and stripe:
    stripe.api_key = STRIPE_API_KEY
    try:
        ver = _stripe_version() or 'unknown'
        logging.info(
            f"Stripe initialized version={ver} key_prefix={STRIPE_API_KEY[:7] if STRIPE_API_KEY else 'None'}"
        )
        
        if ver and ver[0].isdigit():
            try:
                major = int(ver.split('.')[0])
                if major < 10:
                    logging.warning(
                        "Stripe library major version <10 (" + ver + "). Please upgrade (pip install 'stripe>=10,<13')."
                    )
            except Exception:
                pass
        if not STRIPE_PRICE_ID:
            logging.warning("STRIPE_PRICE_ID not configured; checkout will fail until set.")
    except Exception:
        pass

@app.get("/billing/debug")
async def billing_debug():
    return {
        "stripe_imported": stripe is not None,
        "stripe_version": _stripe_version(),
        "api_key_set": bool(STRIPE_API_KEY),
        "api_key_prefix": STRIPE_API_KEY[:7] + '...' if STRIPE_API_KEY else None,
        "api_key_valid_format": bool(STRIPE_API_KEY and STRIPE_API_KEY.startswith('sk_')),
        "debug_flag": DEBUG_BILLING,
        "price_id_set": bool(STRIPE_PRICE_ID),
    }

class CheckoutSessionRequest(BaseModel):
    price_id: Optional[str] = None  
    trial: Optional[bool] = None

stripe_ready = bool(stripe and STRIPE_API_KEY)

def _require_stripe():
    if stripe is None:
        raise HTTPException(status_code=503, detail="Stripe library not installed (pip install stripe)")
    if not STRIPE_API_KEY:
        raise HTTPException(status_code=503, detail="STRIPE_API_KEY missing in environment")
    ver = _stripe_version()
    
    if ver and ver[0].isdigit():
        try:
            major = int(ver.split('.')[0])
            if major < 10:
                raise HTTPException(status_code=503, detail=f"Outdated stripe package ({ver}). Upgrade to >=10.15.0")
        except Exception:
            pass

@app.get("/billing/health")
async def billing_health():
    return {
        "stripe_imported": stripe is not None,
        "api_key_configured": bool(STRIPE_API_KEY),
        "price_id_configured": bool(STRIPE_PRICE_ID),
        "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET),
        "ready": stripe_ready,
    }

@app.post("/billing/create-checkout-session")
async def create_checkout_session(payload: CheckoutSessionRequest = Body(default={}), current_user=Depends(get_current_user)):
    _require_stripe()
    try:
        user_id = current_user.get("id")
        email = current_user.get("email")
        
        stripe_customer_id = current_user.get("stripe_customer_id")
        if not stripe_customer_id:
            customer = stripe.Customer.create(
                email=email,
                metadata={"user_id": user_id}
            )
            stripe_customer_id = customer.id
            try:
                supabase.table("users").update({"stripe_customer_id": stripe_customer_id}).eq("id", user_id).execute()
            except Exception as e:
                logging.warning(f"Could not persist stripe_customer_id: {e}")

        price_id = payload.price_id or STRIPE_PRICE_ID
        if not price_id:
            raise HTTPException(status_code=400, detail="No price specified")

        
        try:
            existing = stripe.Subscription.list(customer=stripe_customer_id, status='active', limit=1)
            if existing and existing.get('data'):
                raise HTTPException(status_code=400, detail="Active subscription already exists")
        except HTTPException:
            raise
        except Exception as sub_list_err:
            logging.warning(f"Could not check existing subscriptions: {sub_list_err}")

        mode = "subscription"
        session_params = {
            "mode": mode,
            "customer": stripe_customer_id,
            "line_items": [
                {"price": price_id, "quantity": 1}
            ],
            "success_url": f"{FRONTEND_URL}/dashboard?upgrade=success",
            "cancel_url": f"{FRONTEND_URL}/pricing?upgrade=cancel",
            "metadata": {"user_id": user_id},
            "subscription_data": {
                "metadata": {"user_id": user_id}
            }
        }
        if payload.trial:
            
            session_params["subscription_data"]["trial_period_days"] = 14

        checkout_session = stripe.checkout.Session.create(**session_params)
        return {"url": checkout_session.url, "id": checkout_session.id}
    except Exception as e:  
        err_type = type(e).__name__
        logging.error(f"Create checkout session failed ({err_type}): {e}")
        if DEBUG_BILLING:
            return JSONResponse(status_code=500, content={
                "error": "checkout_session_failed",
                "error_type": err_type,
                "message": str(e),
                "stripe_version": _stripe_version(),
            })
        raise HTTPException(status_code=500, detail="Failed to create checkout session")


@app.post("/create-checkout-session")
async def legacy_create_checkout(payload: CheckoutSessionRequest = Body(default={}), current_user=Depends(get_current_user)):
    return await create_checkout_session(payload, current_user)  

@app.post("/billing/create-portal-session")
async def create_billing_portal(current_user=Depends(get_current_user)):
    _require_stripe()
    try:
        stripe_customer_id = current_user.get("stripe_customer_id")
        if not stripe_customer_id:
            raise HTTPException(status_code=400, detail="No Stripe customer for user")
        portal = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=f"{FRONTEND_URL}/dashboard"
        )
        return {"url": portal.url}
    except Exception as e:
        logging.error(f"Portal session error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create portal session")


@app.post("/create-portal-session")
async def legacy_portal(current_user=Depends(get_current_user)):
    return await create_billing_portal(current_user)  

@app.post("/webhooks/stripe", response_class=PlainTextResponse)
async def stripe_webhook(request: Request):
    if not (stripe and STRIPE_API_KEY):
        return PlainTextResponse("stripe disabled", status_code=200)

    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    event = None
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET
            )
        else:
            event = stripe.Event.construct_from(json.loads(payload.decode("utf-8")), stripe.api_key)
    except Exception as e:
        logging.error(f"Webhook signature/parse error: {e}")
        return PlainTextResponse("invalid payload", status_code=400)

    etype = event.get("type")
    data_object = event.get("data", {}).get("object", {})

    try:
        if etype == "checkout.session.completed":
            
            subscription_id = data_object.get("subscription")
            customer_id = data_object.get("customer")
            if subscription_id and customer_id:
                try:
                    sub = stripe.Subscription.retrieve(subscription_id)
                    user_id = (sub.get("metadata", {}) or {}).get("user_id") or (data_object.get("metadata", {}) or {}).get("user_id")
                    current_period_end = sub.get("current_period_end")
                    update_payload = {
                        "stripe_customer_id": customer_id,
                        "stripe_subscription_id": subscription_id,
                        "subscription_status": sub.get("status"),
                        "subscription_current_period_end": datetime.utcfromtimestamp(current_period_end).isoformat() if current_period_end else None,
                    }
                    if user_id:
                        try:
                            supabase.table("users").update(update_payload).eq("id", user_id).execute()
                        except Exception as db_err:
                            logging.warning(f"Failed to update user subscription columns (create them if missing): {db_err}")
                except Exception as sub_err:
                    logging.error(f"Failed to process subscription: {sub_err}")
        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            subscription_id = data_object.get("id")
            customer_id = data_object.get("customer")
            status_val = data_object.get("status")
            user_id = (data_object.get("metadata", {}) or {}).get("user_id")
            current_period_end = data_object.get("current_period_end")
            update_payload = {
                "subscription_status": status_val,
                "subscription_current_period_end": datetime.utcfromtimestamp(current_period_end).isoformat() if current_period_end else None,
            }
            if user_id:
                try:
                    supabase.table("users").update(update_payload).eq("id", user_id).execute()
                except Exception as db_err:
                    logging.warning(f"Failed to update user subscription on lifecycle event: {db_err}")
            else:
                
                try:
                    res = supabase.table("users").select("id").eq("stripe_customer_id", customer_id).execute()
                    if res.data:
                        uid = res.data[0]["id"]
                        supabase.table("users").update(update_payload).eq("id", uid).execute()
                except Exception as db_err:
                    logging.warning(f"Fallback update failed: {db_err}")
    except Exception as e:
        logging.error(f"Webhook handling error: {e}")

    return PlainTextResponse("ok", status_code=200)

BREVO_MEAL_PLAN_TEMPLATE_ID = os.getenv("BREVO_MEAL_PLAN_TEMPLATE_ID", "").strip()


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

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)