from fastapi import FastAPI, HTTPException, Depends, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from openai import OpenAI
import requests
import json
import os
import re
import time
import secrets
import bcrypt
import requests
import json
import os
import time
import hashlib
import secrets
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from supabase import create_client, Client
from dotenv import load_dotenv
import bcrypt
import csv
from functools import lru_cache
import base64
from fastapi import Request
from uuid import UUID

# New: import FoodPro scraping helpers
try:
    from extract import build_menu_url as fp_build_menu_url, fetch as fp_fetch, parse_menu as fp_parse_menu
except Exception:
    fp_build_menu_url = None  # type: ignore
    fp_fetch = None  # type: ignore
    fp_parse_menu = None  # type: ignore

# Add URL helpers for adjusting FoodPro params (mealName)
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="DormDish API", description="Meal planning API for college students", version="1.0.0")

# Enable CORS for all routes (needed for web frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to OpenAI API (configurable for different providers)
# Default to local model, but allow override via environment variables
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://10.75.176.49:1234/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "lm-studio")

client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

# Initialize Supabase client
# You'll need to set these environment variables or replace with your actual values
SUPABASE_URL = os.getenv("SUPABASE_URL", "your_supabase_url_here")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "your_supabase_service_key_here")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Authentication helper functions
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
        # Get session with user data
        response = supabase.table("user_sessions")\
            .select("*, users(*)")\
            .eq("session_token", token)\
            .gt("expires_at", datetime.now().isoformat())\
            .execute()
        
        if response.data and len(response.data) > 0:
            session = response.data[0]
            # Update last login
            supabase.table("users")\
                .update({"last_login": datetime.now().isoformat()})\
                .eq("id", session["user_id"])\
                .execute()
            return session["users"]
        return None
    except Exception as e:
        print(f"Error verifying session: {e}")
        return None

def calculate_bmr_tdee(age: int, height_feet: int, height_inches: int, weight_lbs: float, 
                      gender: str, activity_level: str) -> Dict[str, float]:
    """Calculate BMR and TDEE based on user stats"""
    # Convert to metric
    height_cm = ((height_feet * 12) + height_inches) * 2.54
    weight_kg = weight_lbs * 0.453592
    
    # Mifflin-St Jeor Equation
    if gender.lower() == 'male':
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    elif gender.lower() == 'female':
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161
    else:
        # Default to male formula
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    
    # Activity multipliers
    activity_multipliers = {
        'sedentary': 1.2,
        'lightly_active': 1.375,
        'moderately_active': 1.55,
        'very_active': 1.725,
        'extremely_active': 1.9,
        'super_active': 2.2
    }
    
    multiplier = activity_multipliers.get(activity_level, 1.55)
    tdee = bmr * multiplier
    
    return {
        'bmr': round(bmr, 2),
        'tdee': round(tdee, 2)
    }

@dataclass
class User:
    """User profile with physical stats and fitness goals"""
    age: int
    height_feet: int
    height_inches: int
    weight_lbs: int
    activity_level: str  # "sedentary", "active", "very_active"
    goal: str  # "bulk", "cut", "maintain"
    preferred_dining_hall: str
    # New: optional dietary preference
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

# ---------------------------
# Brevo (Sendinblue) helpers
# ---------------------------
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


# New: simple Brevo health helpers
def _brevo_get_account() -> Dict:
    try:
        r = requests.get(f"{_brevo_base_url()}/account", headers=_brevo_headers(), timeout=10)
        return {"status": r.status_code, "body": r.json() if r.content else None}
    except Exception as e:
        return {"status": 0, "error": str(e)}


def _brevo_get_template(template_id: int) -> Dict:
    try:
        r = requests.get(f"{_brevo_base_url()}/smtp/templates/{template_id}", headers=_brevo_headers(), timeout=10)
        return {"status": r.status_code, "body": r.json() if r.content else None}
    except Exception as e:
        return {"status": 0, "error": str(e)}


def _guess_name_from_email(email: str) -> Dict[str, str]:
    local = email.split("@")[0]
    # split by common delimiters
    parts = re.split(r"[._-]+", local)
    parts = [p for p in parts if p]
    if not parts:
        return {"first": "", "last": ""}
    if len(parts) == 1:
        return {"first": parts[0].capitalize(), "last": ""}
    return {"first": parts[0].capitalize(), "last": parts[-1].capitalize()}


def upsert_brevo_contact(
    email: str,
    attributes: Optional[Dict[str, str]] = None,
    list_ids: Optional[List[int]] = None,
) -> Dict:
    """Create or update a Brevo contact and optionally add to lists."""
    url = f"{_brevo_base_url()}/contacts"
    payload: Dict = {
        "email": email,
        "updateEnabled": True,
    }
    if attributes:
        payload["attributes"] = attributes
    # If a single list id provided via env, use it by default
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
        payload["listIds"] = list(dict.fromkeys(resolved_list_ids))  # dedupe, preserve order

    try:
        resp = requests.post(url, headers=_brevo_headers(), data=json.dumps(payload), timeout=12)
        # 201 created or 200 updated
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Brevo contact upsert failed: {resp.text}")
        return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Brevo error: {e}")


def send_brevo_template(
    email: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    subject: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict:
    """Send a Brevo transactional email using a stored template to a single recipient."""
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

    # Ensure contact exists (optionally add to default list)
    if not first_name or not last_name:
        guess = _guess_name_from_email(email)
        first_name = first_name or guess["first"]
        last_name = last_name or guess["last"]

    attributes = {
        "FIRSTNAME": first_name or "",
        "LASTNAME": last_name or "",
    }
    upsert_brevo_contact(email, attributes=attributes)

    payload: Dict = {
        "templateId": template_id,
        "to": [{"email": email, "name": f"{first_name or ''} {last_name or ''}".strip()}],
        "sender": {"email": from_email, "name": from_name},
    }
    if subject:
        payload["subject"] = subject
    if tags:
        payload["tags"] = tags

    try:
        url = f"{_brevo_base_url()}/smtp/email"
        r = requests.post(url, headers=_brevo_headers(), data=json.dumps(payload), timeout=12)
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Failed to send Brevo email: {r.text}")
        return {"success": True, **r.json()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Brevo error: {e}")


@app.post('/subscribe', summary='Subscribe to newsletter and send welcome email')
def subscribe(req: SubscribeRequest):
    try:
        # Upsert contact and send template email
        fn = (req.firstName or "").strip()
        ln = (req.lastName or "").strip()
        res = send_brevo_template(req.email.strip().lower(), first_name=fn or None, last_name=ln or None)
        return {"success": True, "message": "Subscription successful. Email sent.", **res}
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"⚠️ Subscribe failed: {e}")
        raise HTTPException(status_code=500, detail="Subscription failed")


# New: Brevo health endpoints (non-breaking additions)
@app.get('/health/brevo', summary='Brevo configuration and template health check')
def brevo_health():
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

        account = _brevo_get_account()
        template_info = _brevo_get_template(template_id) if template_id else None
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
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get('/email/test', summary='Send a Brevo test email using the configured template')
def email_test(email: EmailStr):
    try:
        res = send_brevo_template(str(email))
        return {"success": True, **res}
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Test email failed: {e}")

# ---------------------------
# Universities list endpoint
# ---------------------------
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
            # Detect delimiter (supports ';', ',', '|', tabs)
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;|\t')
                delim = dialect.delimiter
            except Exception:
                # Heuristic fallback
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
                # Fallback: try simple CSV without reliable headers
                f.seek(0)
                reader2 = csv.reader(f, delimiter=delim)
                first_row = True
                for row in reader2:
                    if first_row:
                        first_row = False
                        # Skip header row if it contains a 'name' column
                        if any((col or '').strip().lower() == 'name' for col in row):
                            continue
                    if row:
                        name = (row[0] or '').strip()
                        if name:
                            universities.append(name)
    except Exception as e:
        print(f"⚠️ Failed to load universities CSV: {e}")
    # Deduplicate while preserving order
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

# ---------------------------
# Waitlist endpoints
# ---------------------------
class WaitlistJoinRequest(BaseModel):
    firstName: str
    lastName: str
    email: str
    university: str
    graduationYear: Optional[str] = None
    notifications: Optional[bool] = True
    # New: Turnstile captcha token (optional in dev if server secret not set)
    captchaToken: Optional[str] = None


def verify_turnstile_token(token: Optional[str], remoteip: Optional[str] = None) -> bool:
    """Verify Cloudflare Turnstile token. If TURNSTILE_SECRET_KEY is not set, skip verification (dev mode)."""
    secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    # Dev bypass if no secret configured
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

    try:
        resp = requests.post(url, data=data, timeout=10)
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
def join_waitlist(data: WaitlistJoinRequest, request: Request):
    try:
        # Verify captcha (skipped in dev if no TURNSTILE_SECRET_KEY)
        # Extract client IP from headers or connection
        client_ip = (
            request.headers.get('CF-Connecting-IP')
            or (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() if request.headers.get('X-Forwarded-For') else None)
            or (request.client.host if request.client else None)
        )
        verify_turnstile_token(data.captchaToken, client_ip)

        # Upsert by email (only one per email)
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

        # Push to Brevo if notifications allowed
        brevo_status = None
        if bool(data.notifications):
            try:
                attributes = {
                    "FIRSTNAME": data.firstName.strip(),
                    "LASTNAME": data.lastName.strip(),
                    "UNIVERSITY": data.university.strip(),
                    "GRADYEAR": (data.graduationYear or ""),
                }
                upsert_brevo_contact(
                    data.email.strip().lower(),
                    attributes=attributes,
                )
                # Try sending a confirmation/welcome email
                try:
                    send_brevo_template(
                        data.email.strip().lower(),
                        first_name=data.firstName.strip(),
                        last_name=data.lastName.strip(),
                        subject="You're on the DormDish waitlist!",
                        tags=["waitlist"],
                    )
                    brevo_status = "sent"
                except HTTPException:
                    brevo_status = "queued"
            except HTTPException as e:
                print(f"⚠️ Brevo sync failed: {e.detail}")

        # Count people on the same university waitlist
        count_resp = supabase.table('waitlist_users').select('id', count='exact').eq('university', data.university.strip()).execute()
        uni_count = getattr(count_resp, 'count', None)
        return {"success": True, "message": "Successfully joined waitlist", "university_count": uni_count or 0, "brevo_status": brevo_status}
    except Exception as e:
        print(f"⚠️ Waitlist join failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to join waitlist")

# ---------------------------
# Auth endpoints
# ---------------------------
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
    # New: optional dietary preference
    dietary_preference: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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
        # Check for existing user
        existing = supabase.table("users").select("id").eq("email", str(data.email).lower()).execute()
        if existing.data and len(existing.data) > 0:
            raise HTTPException(status_code=400, detail="An account with this email already exists")

        # Hash password
        pwd_hash = hash_password(data.password)

        # Calculate BMR/TDEE
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
            # New: store dietary preference (nullable)
            "dietary_preference": (data.dietary_preference or None),
            "bmr": metrics.get("bmr"),
            "tdee": metrics.get("tdee"),
            "created_at": datetime.now().isoformat(),
        }

        inserted = supabase.table("users").insert(payload).execute()
        if not inserted.data:
            raise HTTPException(status_code=500, detail="Failed to create user")
        user_row = inserted.data[0]

        # Create session
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        # Return response format expected by frontend
        # Match types in src/services/api.ts: { success, message, user, session_token }
        user_row["session_token"] = token  # not persisted, for response convenience
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
        # Fetch user by email
        res = supabase.table("users").select("*").eq("email", str(data.email).lower()).execute()
        if not res.data:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user_row = res.data[0]

        # Verify password
        hashed = user_row.get("password_hash")
        if not hashed or not verify_password(data.password, hashed):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Create session
        token = create_user_session(user_row.get("id"))
        if not token:
            raise HTTPException(status_code=500, detail="Failed to create session")

        # Update last_login is handled in verify_session_token during profile fetch, but set here too for immediacy
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

# ---------------------------
# Dining halls, menus, and meal planning (from app real.py)
# ---------------------------
class UpdateProfileRequest(BaseModel):
    age: Optional[int] = None
    height_feet: Optional[int] = None
    height_inches: Optional[int] = None
    weight_lbs: Optional[float] = None
    gender: Optional[str] = None
    activity_level: Optional[str] = None
    goal: Optional[str] = None
    university: Optional[str] = None
    # New: optional dietary preference
    dietary_preference: Optional[str] = None

class MealPlanRequest(BaseModel):
    dining_hall: str
    date: str

class MealPlanFeedbackRequest(BaseModel):
    generation_id: str
    liked: bool

class TokenData(BaseModel):
    user_id: str

@app.get("/health", summary="Health Check")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


def get_menu_from_supabase(university: str, dining_hall: str, date: str) -> Optional[dict]:
    """Check if menu exists in Supabase database"""
    try:
        menu_id = f"{university}_{dining_hall}_{date}"
        response = supabase.table("menus").select("*").eq("id", menu_id).execute()
        if response.data and len(response.data) > 0:
            print(f"📁 Found cached menu for {menu_id}")
            return response.data[0]["menu_data"]
        else:
            print(f"🔍 No cached menu found for {menu_id}")
            return None
    except Exception as e:
        print(f"⚠️ Error checking Supabase: {e}")
        return None


def save_menu_to_supabase(university: str, dining_hall: str, date: str, menu_data: dict) -> bool:
    """Save menu data to Supabase database"""
    try:
        menu_id = f"{university}_{dining_hall}_{date}"
        hall_slug = menu_data.get("hall_slug", dining_hall)
        fetched_at = menu_data.get("fetched_at", datetime.now().isoformat())
        items_count = 0
        if "items" in menu_data:
            items_count = len(menu_data["items"])
        elif "menu" in menu_data and "items" in menu_data["menu"]:
            items_count = len(menu_data["menu"]["items"])
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
    except Exception as e:
        print(f"⚠️ Error saving to Supabase: {e}")
        print(f"⚠️ Error type: {type(e)}")
        return False


def get_dining_hall_menu(dining_hall: str, date: str) -> dict:
    """Fetch dining hall menu - cache in Supabase, else fetch from Nutrislice"""
    dining_hall_mapping = {
        # Hopkins halls
        "hopkins-cafe": ("hopkins", "hopkins-cafe"),
        "hopkins cafe": ("hopkins", "hopkins-cafe"),
        "hopkins": ("hopkins", "hopkins-cafe"),
        "nolans-on-33rd": ("hopkins", "nolans-on-33rd"),
        "nolans": ("hopkins", "nolans-on-33rd"),
        "nolan": ("hopkins", "nolans-on-33rd"),
        "peabody-institute": ("hopkins", "peabody-institute"),
        "peabody": ("hopkins", "peabody-institute"),
        # Rutgers halls
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
        "cook-douglass": ("rutgers", "neilson-dining-hall"),
        "cook douglass": ("rutgers", "neilson-dining-hall"),
        "cook/douglass": ("rutgers", "neilson-dining-hall"),
        # New: The Atrium (Rutgers Livingston)
        "the-atrium": ("rutgers", "the-atrium"),
        "the atrium": ("rutgers", "the-atrium"),
        "atrium": ("rutgers", "the-atrium"),
    }
    mapping = dining_hall_mapping.get(dining_hall.lower())
    if not mapping:
        return {"error": f"Unknown dining hall: {dining_hall}"}
    university, hall_slug = mapping
    cached_menu = get_menu_from_supabase(university, dining_hall, date)
    if cached_menu is not None:
        print(f"✅ Using cached menu from Supabase")
        return cached_menu
    # Route Rutgers halls to FoodPro when applicable
    if university == "rutgers" and hall_slug in {"livingston-dining-commons", "neilson-dining-hall", "the-atrium"}:
        print(f"🌐 Fetching fresh menu from FoodPro for Rutgers - {hall_slug}")
        try:
            menu_data = fetch_foodpro_menu_rutgers(dining_hall, hall_slug, date)
            save_result = save_menu_to_supabase(university, dining_hall, date, menu_data)
            print(f"💾 Save to Supabase result: {save_result}")
            return menu_data
        except Exception as e:
            print(f"❌ FoodPro fetch failed, falling back to Nutrislice if available: {e}")
            # Fallback to Nutrislice path
    print(f"🌐 Fetching fresh menu from Nutrislice API for {university} - {hall_slug}")
    try:
        menu_data = fetch_nutrislice_menu(university, hall_slug, dining_hall, date)
        save_result = save_menu_to_supabase(university, dining_hall, date, menu_data)
        print(f"💾 Save to Supabase result: {save_result}")
        return menu_data
    except Exception as e:
        return {"error": f"Failed to fetch menu: {str(e)}"}


def fetch_foodpro_menu_rutgers(dining_hall: str, hall_slug: str, date: str) -> dict:
    """Fetch menu from Rutgers FoodPro pickmenu pages and format like Nutrislice output.
    Now aggregates breakfast, lunch, and dinner by invoking the scraper per meal.
    """
    if not (fp_build_menu_url and fp_fetch and fp_parse_menu):
        raise RuntimeError("FoodPro scraper not available")

    # Map hall slug to FoodPro location params
    # Known/assumed mappings: Livingston=03, Neilson=04. Atrium best-effort mapping using location name.
    location_map = {
        "livingston-dining-commons": {"locationNum": "03", "locationName": "Livingston Dining Commons"},
        "neilson-dining-hall": {"locationNum": "04", "locationName": "Neilson Dining Hall"},
        # Atrium is a retail at Livingston; try common FoodPro name
        "the-atrium": {"locationNum": "07", "locationName": "The Atrium"},
    }
    params = location_map.get(hall_slug)
    if not params:
        raise ValueError(f"Unsupported Rutgers FoodPro hall: {hall_slug}")

    def _set_meal_in_url(url: str, meal: str) -> str:
        # FoodPro typically accepts mealName=Breakfast|Lunch|Dinner
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

    def _attempt_all_meals(location_num: str, location_name: str) -> dict:
        base_url = fp_build_menu_url(date, location_num, location_name)  # type: ignore
        print(f"🔗 FoodPro base URL: {base_url}")
        all_items: List[dict] = []
        meal_sources: List[str] = []
        warnings: List[str] = []

        for meal in ("breakfast", "lunch", "dinner"):
            meal_url = _set_meal_in_url(base_url, meal)
            print(f"   ↳ Fetching meal '{meal}' with URL: {meal_url}")
            try:
                soup = fp_fetch(meal_url)  # type: ignore
            except Exception as e:
                warnings.append(f"Failed to fetch {meal}: {e}")
                continue
            try:
                parsed = fp_parse_menu(str(soup), meal_type=meal, date=date, base_url=meal_url)  # type: ignore
            except Exception as e:
                warnings.append(f"Failed to parse {meal}: {e}")
                continue

            # Collect items from either old or new schema
            items: List[dict] = []
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                # Old shape
                for it in parsed.get("items", []):
                    if not isinstance(it, dict):
                        continue
                    name = it.get("name")
                    if not name:
                        continue
                    # Fixed type annotation syntax
                    out_item: Dict[str, object] = {"name": name}
                    if it.get("protein_g") is not None:
                        out_item["protein_g"] = it.get("protein_g")
                    if it.get("calories") is not None:
                        out_item["calories"] = it.get("calories")
                    if it.get("serving_size"):
                        out_item["serving_size"] = it.get("serving_size")
                    items.append(out_item)
            else:
                # New meals_trimmed-style
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

    attempt = _attempt_all_meals(params["locationNum"], params["locationName"])  # type: ignore

    # If Atrium returned no items across all meals, try alternative naming variants
    if hall_slug == "the-atrium" and not attempt["items"]:
        for alt_name in [
            "Atrium",  # shorter variant
            "The Atrium at Livingston",  # descriptive variant
            "The Atrium at Livingston Campus",
        ]:
            try:
                print(f"🔁 Retrying The Atrium with locationName='{alt_name}'")
                attempt = _attempt_all_meals(params["locationNum"], alt_name)
                if attempt["items"]:
                    break
            except Exception as e:
                print(f"⚠️ Retry failed for Atrium alt name '{alt_name}': {e}")

    # Deduplicate by name to mirror Nutrislice behavior
    all_items = attempt["items"]
    seen = set()
    unique_items: List[dict] = []
    for it in all_items:
        nm = (it.get("name") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            unique_items.append(it)

    warnings: List[str] = attempt.get("warnings", [])
    if not unique_items:
        warnings.append("No items parsed from FoodPro")

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


def fetch_nutrislice_menu(university: str, hall_slug: str, dining_hall: str, date: str) -> dict:
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
            items = fetch_meal_items(api_base, hall_slug, meal_slug, date)
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


def fetch_meal_items(api_base: str, hall_slug: str, meal_slug: str, date: str) -> list:
    """Fetch items for a specific meal from Nutrislice API"""
    year, month, day = date.split("-")
    url = f"{api_base}/{hall_slug}/menu-type/{meal_slug}/{year}/{month}/{day}/"
    print(f"🔗 Fetching: {url}")
    try:
        response = requests.get(url, timeout=12)
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
        print(f"❌ Error fetching meal items from {url}: {e}")
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
            result["protein_g"] = float(nutrition["protein_g"])  # type: ignore
        except (ValueError, TypeError):
            pass
    if nutrition.get("calories") is not None:
        try:
            result["calories"] = float(nutrition["calories"])  # type: ignore
        except (ValueError, TypeError):
            pass
    if nutrition.get("serving_size"):
        result["serving_size"] = str(nutrition["serving_size"])  # type: ignore
    return result


def calculate_calorie_needs(user: User) -> Dict[str, float]:
    """Calculate daily calorie and macro needs based on user profile"""
    bmr = (10 * user.weight_kg) + (6.25 * user.height_cm) - (5 * user.age) + 5
    activity_multipliers = {
        "sedentary": 1.2,
        "active": 1.55,
        "very_active": 1.725,
    }
    tdee = bmr * activity_multipliers.get(user.activity_level, 1.55)
    if user.goal == "bulk":
        target_calories = tdee + 300
        protein_per_lb = 1.0
    elif user.goal == "cut":
        target_calories = tdee - 500
        protein_per_lb = 1.2
    else:
        target_calories = tdee
        protein_per_lb = 0.8
    protein_grams = user.weight_lbs * protein_per_lb
    protein_calories = protein_grams * 4
    fat_calories = target_calories * 0.27
    fat_grams = fat_calories / 9
    carb_calories = target_calories - protein_calories - fat_calories
    carb_grams = carb_calories / 4
    return {
        "calories": round(target_calories),
        "protein_grams": round(protein_grams),
        "carb_grams": round(carb_grams),
        "fat_grams": round(fat_grams),
    }


def generate_meal_plan(user: User, menu_data: dict) -> dict:
    """Generate a meal plan based on user profile and available menu"""
    nutritional_needs = calculate_calorie_needs(user)
    user_context = f"""
    User Profile:
    - Age: {user.age}, Height: {user.height_feet}'{user.height_inches}", Weight: {user.weight_lbs}lbs
    - Activity Level: {user.activity_level}
    - Goal: {user.goal}
    {f"- Dietary Preference: {user.dietary_preference}" if user.dietary_preference else ""}

    Nutritional Targets:
    - Calories: {nutritional_needs['calories']}
    - Protein: {nutritional_needs['protein_grams']}g

    Available Menu: {json.dumps(menu_data, indent=2)}

    Please create an optimized meal plan that meets the user's nutritional targets using only items from the available menu. 
    Focus on high-quality protein sources and nutrient-dense foods that support their {user.goal} goal.
    Provide specific portion recommendations and explain how the plan meets their macronutrient needs.
    {"Additionally, strictly ensure all items comply with the user's dietary preference (use only vegan items if vegan; only vegetarian items if vegetarian)." if user.dietary_preference else ""}
    """
    return {
        "user_profile": asdict(user),
        "nutritional_targets": nutritional_needs,
        "menu_data": menu_data,
        "ai_prompt": user_context,
    }


def generate_meal_plan_with_bmr(user: User, menu_data: dict, bmr: float, tdee: float) -> dict:
    """Generate a meal plan with BMR/TDEE information for more accurate targeting"""
    if user.goal == "bulk":
        target_calories = tdee + 400
        protein_per_lb = 1.0
    elif user.goal == "cut":
        target_calories = tdee - 400
        protein_per_lb = 1.2
    else:
        target_calories = tdee
        protein_per_lb = 0.8
    protein_grams = user.weight_lbs * protein_per_lb
    protein_calories = protein_grams * 4
    fat_calories = target_calories * 0.27
    fat_grams = fat_calories / 9
    carb_calories = target_calories - protein_calories - fat_calories
    carb_grams = carb_calories / 4
    nutritional_needs = {
        "calories": round(target_calories),
        "protein_grams": round(protein_grams),
        "bmr": bmr,
        "tdee": tdee,
    }
    user_context = f"""
    User Profile:
    - Age: {user.age}, Height: {user.height_feet}'{user.height_inches}", Weight: {user.weight_lbs}lbs
    - Activity Level: {user.activity_level}
    - Goal: {user.goal}
    {f"- Dietary Preference: {user.dietary_preference}" if user.dietary_preference else ""}

    Metabolic Information:
    - BMR (Basal Metabolic Rate): {bmr} calories
    - TDEE (Total Daily Energy Expenditure): {tdee} calories

    Nutritional Targets (based on TDEE and goal):
    - Target Calories: {nutritional_needs['calories']} (TDEE {'+' if user.goal == 'bulk' else '-' if user.goal == 'cut' else '='} adjustment for {user.goal})
    - Protein: {nutritional_needs['protein_grams']}g


    Available Menu Items: {len(menu_data.get('items', []))} items available
    {json.dumps(menu_data.get('items', []), indent=2)}

    Please create an optimized meal plan that meets the user's nutritional targets using only items from the available menu. 
    The plan should align with their {user.goal} goal and stay within ±5% of the target calories.
    Focus on high-quality protein sources and nutrient-dense foods.
    {"Additionally, strictly ensure all items comply with the user's dietary preference (use only vegan items if vegan; only vegetarian items if vegetarian)." if user.dietary_preference else ""}
    """
    return {
        "user_profile": asdict(user),
        "nutritional_targets": nutritional_needs,
        "menu_data": menu_data,
        "ai_prompt": user_context,
    }


# ---- Server-side nutrition math to avoid model hallucinations ----

def _to_number(val) -> Optional[float]:
    """Best-effort convert to float. Accepts numbers or numeric strings like '141', '141 kcal'."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip()
        # Extract the first numeric token (supports decimals, optional minus)
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None
    return None


def _sum_items(items: List[dict]) -> Tuple[float, float]:
    calories_sum = 0.0
    protein_sum = 0.0
    for it in items or []:
        c = _to_number(it.get("calories"))
        p = _to_number(it.get("protein_g"))
        # Skip items where numbers are missing, mirroring the instruction to exclude nulls
        if c is not None:
            calories_sum += c
        if p is not None:
            protein_sum += p
    return calories_sum, protein_sum


def recompute_meal_plan_totals(meal_plan: dict) -> dict:
    """Compute per-meal and total calories/protein from the AI-assembled items."""
    meals = (meal_plan or {}).get("meals", {}) or {}
    per_meal = {}
    total_cals = 0.0
    total_pro = 0.0
    for meal_name in ("breakfast", "lunch", "dinner"):
        section = meals.get(meal_name) or {}
        items = section.get("items") or []
        cals, prot = _sum_items(items)
        per_meal[meal_name] = {
            "calories": int(round(cals)),
            "protein_g": int(round(prot)),
        }
        total_cals += cals
        total_pro += prot
    return {
        "totals": {"calories": int(round(total_cals)), "protein_g": int(round(total_pro))},
        "per_meal": per_meal,
    }


def build_user_stats_obj(bmr: float, tdee: float, goal: str, dietary: Optional[str]) -> dict:
    return {
        "bmr": float(bmr),
        "tdee": float(tdee),
        "goal": str(goal),
        "dietary_preference": (dietary if dietary in ("vegan", "vegetarian") else None),
    }


def patch_meal_plan_numbers(meal_plan: dict, user_bmr: float, user_tdee: float, user_goal: str, dietary: Optional[str], targets: dict) -> dict:
    """Override model-provided numbers with server-calculated values and add validation."""
    recomputed = recompute_meal_plan_totals(meal_plan)
    server_totals = recomputed["totals"]
    server_goals = {
        "calories": int(round(targets.get("calories") or 0)),
        "protein_g": int(round(targets.get("protein_grams") or 0)),
    }
    # Update/override fields
    meal_plan["user_stats"] = build_user_stats_obj(user_bmr, user_tdee, user_goal, dietary)
    meal_plan["goals"] = server_goals
    meal_plan["totals"] = server_totals
    # Optional: attach per-meal computed sums for transparency (non-breaking addition)
    meal_plan.setdefault("meta", {})
    meal_plan["meta"]["per_meal_computed"] = recomputed.get("per_meal")
    # Validation: within ±5% of calorie target
    tgt = max(server_goals["calories"], 1)
    delta = server_totals["calories"] - server_goals["calories"]
    within = abs(delta) <= 0.05 * tgt
    meal_plan["validation"] = {
        "calorie_target": server_goals["calories"],
        "actual_calories": server_totals["calories"],
        "delta": delta,
        "within_5_percent": within,
    }
    return meal_plan


@app.put("/auth/profile", summary="Update User Profile")
async def update_profile(profile_data: UpdateProfileRequest, current_user=Depends(get_current_user)):
    """Update user profile"""
    try:
        update_data: Dict[str, object] = {}
        for field, value in profile_data.dict(exclude_unset=True).items():
            if value is not None:
                if field == 'goal' and value not in ['cut', 'maintain', 'bulk']:
                    raise HTTPException(status_code=400, detail="Invalid goal. Must be 'cut', 'maintain', or 'bulk'")
                if field == 'dietary_preference' and value not in ['vegan', 'vegetarian']:
                    raise HTTPException(status_code=400, detail="Invalid dietary_preference. Must be 'vegan' or 'vegetarian'")
                update_data[field] = value
        if any(field in update_data for field in ['age', 'height_feet', 'height_inches', 'weight_lbs', 'gender', 'activity_level']):
            current_age = update_data.get('age', current_user['age'])  # type: ignore
            current_height_feet = update_data.get('height_feet', current_user['height_feet'])  # type: ignore
            current_height_inches = update_data.get('height_inches', current_user['height_inches'])  # type: ignore
            current_weight = update_data.get('weight_lbs', current_user['weight_lbs'])  # type: ignore
            current_gender = update_data.get('gender', current_user['gender'])  # type: ignore
            current_activity = update_data.get('activity_level', current_user['activity_level'])  # type: ignore
            bmr_data = calculate_bmr_tdee(
                int(current_age), int(current_height_feet), int(current_height_inches), float(current_weight), str(current_gender), str(current_activity)
            )
            update_data['bmr'] = bmr_data['bmr']
            update_data['tdee'] = bmr_data['tdee']
        if update_data:
            response = supabase.table("users").update(update_data).eq("id", current_user['id']).execute()
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


@app.post("/generate-meal-plan", summary="Generate Meal Plan")
async def generate_meal_plan_route(meal_plan_request: MealPlanRequest, authorization: Optional[str] = Header(None)):
    """Generate a meal plan for a user (authenticated or guest)"""
    try:
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = verify_session_token(token)
        if authenticated_user:
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
            user_bmr = float(authenticated_user['bmr'])
            user_tdee = float(authenticated_user['tdee'])
        else:
            raise HTTPException(status_code=401, detail="Authentication required. Guest users not yet supported in FastAPI version.")
        menu_data = get_dining_hall_menu(meal_plan_request.dining_hall, meal_plan_request.date)
        if "error" in menu_data and menu_data["error"] is not None:
            raise HTTPException(status_code=500, detail=f"Failed to fetch menu: {menu_data['error']}")
        meal_plan_context = generate_meal_plan_with_bmr(user, menu_data, user_bmr, user_tdee)
        ai_start_time = time.time()
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": """You are an AI meal planner. Your role is to generate complete daily meal plans using the provided dataset of foods.

Core Instructions:

Always return three meals: breakfast, lunch, and dinner.

Build structured meals, not just item lists:

Combine menu items into proper dishes (e.g., cereal with milk, sandwich with bread + filling, eggs with toast and fruit).

Do not give standalone components (e.g., just "bun" or "slice of cheese"). Always assemble them into realistic meal components.

Select foods only from the dataset. Never invent foods not present in the menu.

Use the user's BMR and TDEE to determine appropriate calorie targets:
- For bulking: aim for TDEE + 300-500 calories
- For cutting: aim for TDEE - 300-500 calories  
- For maintaining: aim for TDEE

Ensure daily totals are within ±5% of the calculated calorie target.

Prioritize protein targets first, then balance calories.

Avoid repeating the exact same dish within a single day. Variety is preferred.

Respect dietary constraints when provided: if the user is vegan or vegetarian, only select items compliant with that diet.

Always output strictly in JSON format WITHOUT any markdown code blocks or additional text.

Output Format:

{
  "user_stats": {
    "bmr": <float>,
    "tdee": <float>,
    "goal": "<goal>",
    "dietary_preference": "<vegan|vegetarian|null>"
  },
  "goals": {
    "calories": <int>,
    "protein_g": <int>
  },
  "totals": {
    "calories": <int>,
    "protein_g": <int>
  },
  "meals": {
    "breakfast": {
      "description": "Scrambled eggs with toast, oatmeal, and blueberries",
      "items": [
        {
          "name": "SCRAMBLED EGGS",
          "calories": 141,
          "protein_g": 11,
          "serving_size": "4 oz"
        }
      ]
    },
    "lunch": { ... },
    "dinner": { ... }
  },
  "explanation": "Brief explanation of how the plan meets nutritional targets based on BMR/TDEE wihtout saying the amount of calories and protein. Just explain how this meal plan makes sense for users goal."
}

Additional Rules:

Use menu items in combination to create balanced meals.
Exclude foods where calories or protein are null.
Meals should feel natural, varied, and student-friendly.
JSON must always be valid and parseable.
YOU MUST ONLY USE THE MENU ITEMS FROM THE ATTACHED ITEMS.
YOU ARE ALLOWED TO RETURN MORE THAN THE STANDARD SERVIING SIZE IF NEEDED TO FIT THE MACRO TARGETS.
IF THE DIET TYPE IS VEGETARIAN OR VEGAN, THERE IS ADDED LEEWAY TO USE ITEMS THAT FIT THE DIET TYPE. YOU MUST RETURN A MEAL PLAN REGARDLESS OF THE GOAL REQUIREMENTS.
IMPORTANT: Return ONLY the JSON object, no markdown formatting, no code blocks, no additional text."""},
                {"role": "user", "content": meal_plan_context["ai_prompt"]},
            ],
            temperature=0.7,
        )
        # Capture prompts for storage
        system_prompt = "You are an AI meal planner. Your role is to generate complete daily meal plans using the provided dataset of foods. (truncated)"
        user_prompt = meal_plan_context["ai_prompt"]
        ai_end_time = time.time()
        ai_response_raw = response.choices[0].message.content.strip()

        def extract_json_from_response(content: str) -> dict:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                import re as _re
                json_match = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, _re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
                    try:
                        return json.loads(json_content)
                    except json.JSONDecodeError:
                        pass
                brace_match = _re.search(r"\{.*\}", content, _re.DOTALL)
                if brace_match:
                    json_content = brace_match.group(0)
                    try:
                        return json.loads(json_content)
                    except json.JSONDecodeError:
                        pass
                return {"error": "Failed to parse AI response as JSON", "raw_response": content}

        meal_plan = extract_json_from_response(ai_response_raw)
        # NEW: deterministically recompute numbers and override model outputs
        try:
            meal_plan = patch_meal_plan_numbers(
                meal_plan if isinstance(meal_plan, dict) else {},
                user_bmr,
                user_tdee,
                user.goal,
                user.dietary_preference,
                meal_plan_context["nutritional_targets"],
            )
        except Exception as _e:
            # Non-fatal; attach error for debugging
            if isinstance(meal_plan, dict):
                meal_plan.setdefault("meta", {})
                meal_plan["meta"]["server_totals_error"] = str(_e)

        # Store generation in meal_plan_generations
        generation_id = None
        try:
            insert_payload = {
                "user_id": authenticated_user.get("id"),
                "date": meal_plan_request.date,
                "dining_hall": meal_plan_request.dining_hall,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "full_messages": json.dumps([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]),
                "model_raw_response": ai_response_raw,
                "meal_plan": meal_plan,
                # Removed calories/protein_g columns (not in table). Totals remain inside meal_plan JSON.
            }
            inserted = supabase.table("meal_plan_generations").insert(insert_payload).execute()
            if inserted.data:
                generation_id = inserted.data[0]["id"]
        except Exception as e:
            print(f"⚠️ Failed to insert meal_plan_generation: {e}")

        menu_fetch_time = getattr(menu_data, 'fetch_time', 0)
        ai_generation_time = ai_end_time - ai_start_time
        total_time = time.time() - ai_start_time
        return {
            "success": True,
            "user_profile": meal_plan_context["user_profile"],
            "nutrition_targets": meal_plan_context["nutritional_targets"],
            "bmr_tdee": {"bmr": user_bmr, "tdee": user_tdee},
            "available_items": menu_data.get("items", []),
            "meal_plan": meal_plan,
            "generation_id": generation_id,
            "ask_feedback": "Did you like this meal plan?",
            "processing_info": {
                "menu_fetch_time_seconds": round(menu_fetch_time, 2),
                "ai_generation_time_seconds": round(ai_generation_time, 2),
                "total_time_seconds": round(total_time, 2),
            },
            "meta": {
                "dining_hall": meal_plan_request.dining_hall,
                "date": meal_plan_request.date,
                "ai_model": "openai/gpt-oss-20b",
                "authenticated": authenticated_user is not None,
                "raw_ai_response_length": len(ai_response_raw),
                "dietary_preference": user.dietary_preference,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/meal-plan/feedback", summary="Record user feedback (like/dislike) for a meal plan generation")
async def meal_plan_feedback(payload: MealPlanFeedbackRequest, current_user=Depends(get_current_user)):
    try:
        # Validate UUID format
        gen_id = payload.generation_id
        try:
            _ = UUID(gen_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid generation_id")
        # Ensure record belongs to user
        existing = supabase.table("meal_plan_generations").select("id, user_id, liked").eq("id", gen_id).eq("user_id", current_user["id"]).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Generation not found")
        update_data = {"liked": payload.liked, "feedback_at": datetime.now().isoformat()}
        updated = supabase.table("meal_plan_generations").update(update_data).eq("id", gen_id).execute()
        return {"success": True, "generation_id": gen_id, "liked": payload.liked}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to record feedback: {e}")


@app.get("/dining-halls", summary="Get Supported Dining Halls")
async def get_supported_dining_halls():
    """Get list of supported dining halls"""
    try:
        response = requests.get("https://dormdish-mcp.vercel.app/universities", timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
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
        print(f"⚠️ External dining halls service unavailable, using fallback: {str(e)}")
        return fallback_dining_halls


@app.get("/menu/{dining_hall}/{date}", summary="Get Dining Hall Menu")
async def get_menu(dining_hall: str, date: str):
    """Get menu for a specific dining hall and date (with Supabase caching)"""
    try:
        menu_data = get_dining_hall_menu(dining_hall, date)
        if "error" in menu_data and menu_data["error"] is not None:
            raise HTTPException(status_code=500, detail=menu_data["error"])
        return menu_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch menu: {str(e)}")


@app.delete("/cache/clear/{dining_hall}/{date}", summary="Clear Menu Cache")
async def clear_menu_cache(dining_hall: str, date: str):
    """Clear cached menu for a specific dining hall and date"""
    try:
        university = "hopkins" if ("hopkins" in dining_hall or "nolans" in dining_hall or "peabody" in dining_hall) else "rutgers"
        menu_id = f"{university}_{dining_hall}_{date}"
        response = supabase.table("menus").delete().eq("id", menu_id).execute()
        return {
            "success": True,
            "message": f"Cache cleared for {menu_id}",
            "deleted_count": len(response.data) if response.data else 0,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear cache: {str(e)}")


@app.get("/cache/status", summary="Get Cache Status")
async def cache_status():
    """Get cache statistics"""
    try:
        response = supabase.table("menus").select("id, university, dining_hall, date, created_at").execute()
        cached_menus = response.data if response.data else []
        return {"total_cached_menus": len(cached_menus), "cached_menus": cached_menus}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get cache status: {str(e)}")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)