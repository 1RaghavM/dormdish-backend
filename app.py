from fastapi import FastAPI, HTTPException, Depends, status, Header, Body, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
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
import secrets as _secrets_for_google  


try:
    from extract import build_menu_url as fp_build_menu_url, fetch as fp_fetch, parse_menu as fp_parse_menu
except Exception:
    fp_build_menu_url = None  
    fp_fetch = None  
    fp_parse_menu = None  

# Added: custom fetch wrapper with retries & UA hardening for FoodPro in deployment
import contextlib
import random
from zoneinfo import ZoneInfo

def _fetch_with_retries(url: str, attempts: int = 3, base_delay: float = 0.6):
    if not fp_fetch:
        raise RuntimeError("FoodPro fetch function unavailable")
    last_err = None
    for i in range(attempts):
        try:
            print(f"[FoodPro] Attempt {i+1}/{attempts} GET {url}")
            return fp_fetch(url)  # fp_fetch already raises on status
        except Exception as e:
            last_err = e
            sleep_for = base_delay * (2 ** i) + random.uniform(0, 0.25)
            print(f"[FoodPro] attempt {i+1} failed: {e} (sleep {sleep_for:.2f}s)")
            time.sleep(sleep_for)
    raise last_err  # propagate after retries


from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


load_dotenv()

app = FastAPI(title="DormDish API", description="Meal planning API for college students", version="1.0.0")


_raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").strip()
_configured_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
_allow_all = any(o in ("*", "*") for o in _configured_origins)
# NEW: optional regex to allow dynamic preview domains
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



OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MEAL_PLAN_MODEL = os.getenv("MEAL_PLAN_MODEL", "openai/gpt-oss-20b")

client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)



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

def calculate_bmr_tdee(age: int, height_feet: int, height_inches: int, weight_lbs: float, 
                      gender: str, activity_level: str) -> Dict[str, float]:
    """Calculate BMR and TDEE based on user stats"""
    
    height_cm = ((height_feet * 12) + height_inches) * 2.54
    weight_kg = weight_lbs * 0.453592
    
    
    if gender.lower() == 'male':
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    elif gender.lower() == 'female':
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161
    else:
        
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    
    
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
        resp = requests.post(url, headers=_brevo_headers(), data=json.dumps(payload), timeout=12)
        
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
        
        fn = (req.firstName or "").strip()
        ln = (req.lastName or "").strip()
        res = send_brevo_template(req.email.strip().lower(), first_name=fn or None, last_name=ln or None)
        return {"success": True, "message": "Subscription successful. Email sent.", **res}
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"⚠️ Subscribe failed: {e}")
        raise HTTPException(status_code=500, detail="Subscription failed")



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


def verify_turnstile_token(token: Optional[str], remoteip: Optional[str] = None) -> bool:
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
        
        
        client_ip = (
            request.headers.get('CF-Connecting-IP')
            or (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() if request.headers.get('X-Forwarded-For') else None)
            or (request.client.host if request.client else None)
        )
        verify_turnstile_token(data.captchaToken, client_ip)

        
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

def _verify_google_id_token(id_token: str) -> Dict[str, any]:
    """Verify Google ID token using Google's tokeninfo endpoint.
    Returns token payload dict or raises HTTPException.
    """
    try:
        resp = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token}, timeout=8)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to verify Google token: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google ID token")
    data = resp.json()
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

        inserted = supabase.table("users").insert(payload).execute()
        if not inserted.data:
            raise HTTPException(status_code=500, detail="Failed to create user")
        user_row = inserted.data[0]

        
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
def auth_google_login(payload: GoogleIdTokenRequest):
    token_data = _verify_google_id_token(payload.id_token)
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
    except Exception:
        pass
    return {
        "success": True,
        "message": "Login successful",
        "user": user_row,
        "session_token": token,
    }

@app.post('/auth/google/register')
def auth_google_register(data: GoogleRegisterRequest):
    """Register a new user via Google ID token.
    Enhanced with detailed logging and clearer error surfaces for frontend debugging.
    """
    try:
        print("[google.register] start")
        token_data = _verify_google_id_token(data.id_token)
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
        inserted = supabase.table("users").insert(payload).execute()
        if getattr(inserted, 'error', None):
            
            raise HTTPException(status_code=500, detail=f"Supabase insert error: {inserted.error}")
        if not inserted.data:
            raise HTTPException(status_code=500, detail="Failed to create user (no data returned)")
        user_row = inserted.data[0]
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
    except Exception as e:
        print(f"⚠️ Error saving to Supabase: {e}")
        print(f"⚠️ Error type: {type(e)}")
        return False


# Patch: ensure Rutgers date uses America/New_York to avoid UTC date drift
def _rutgers_today_str():
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def get_dining_hall_menu(dining_hall: str, date: str) -> dict:
    """Fetch dining hall menu - cache in Supabase, else fetch from Nutrislice / FoodPro"""
    # Normalize common typos / variants early
    dh_norm = dining_hall.strip().lower()
    if dh_norm == "nielson":  # common misspelling
        dh_norm = "neilson"
    if dh_norm in {"nielson-dining-hall", "nielson dining hall"}:
        dh_norm = "neilson"
    dining_hall = dh_norm

    dining_hall_mapping = {
        # Hopkins
        "hopkins-cafe": ("hopkins", "hopkins-cafe"),
        "hopkins cafe": ("hopkins", "hopkins-cafe"),
        "hopkins": ("hopkins", "hopkins-cafe"),
        "nolans-on-33rd": ("hopkins", "nolans-on-33rd"),
        "nolans": ("hopkins", "nolans-on-33rd"),
        "nolan": ("hopkins", "nolans-on-33rd"),
        "peabody-institute": ("hopkins", "peabody-institute"),
        "peabody": ("hopkins", "peabody-institute"),
        # Rutgers (include variants + typos)
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
        # Neilson + common misspellings
        "neilson": ("rutgers", "neilson-dining-hall"),
        "neilson-dining-hall": ("rutgers", "neilson-dining-hall"),
        "neilson dining hall": ("rutgers", "neilson-dining-hall"),
        "nielson": ("rutgers", "neilson-dining-hall"),
        "nielson-dining-hall": ("rutgers", "neilson-dining-hall"),
        "nielson dining hall": ("rutgers", "neilson-dining-hall"),
        "cook-douglass": ("rutgers", "neilson-dining-hall"),
        "cook douglass": ("rutgers", "neilson-dining-hall"),
        "cook/douglass": ("rutgers", "neilson-dining-hall"),
        # Atrium
        "the-atrium": ("rutgers", "the-atrium"),
        "the atrium": ("rutgers", "the-atrium"),
        "atrium": ("rutgers", "the-atrium"),
    }
    mapping = dining_hall_mapping.get(dining_hall)
    if not mapping:
        return {"error": f"Unknown dining hall: {dining_hall}"}
    university, hall_slug = mapping

    # Auto-fix date if empty / 'today'
    if not date or date.lower() == "today":
        date = _rutgers_today_str() if university == "rutgers" else datetime.utcnow().strftime("%Y-%m-%d")

    cached_menu = get_menu_from_supabase(university, dining_hall, date)
    if cached_menu is not None:
        print(f"✅ Using cached menu from Supabase")
        return cached_menu

    if university == "rutgers" and hall_slug in {"livingston-dining-commons", "neilson-dining-hall", "the-atrium"}:
        print(f"🌐 Fetching fresh menu from FoodPro for Rutgers - {hall_slug}")
        try:
            menu_data = fetch_foodpro_menu_rutgers(dining_hall, hall_slug, date)
            if not menu_data.get("items"):
                print(f"⚠️ FoodPro returned no items, skipping cache save")
                return {"error": f"No menu items available for {hall_slug} on {date}"}
            save_result = save_menu_to_supabase(university, dining_hall, date, menu_data)
            print(f"💾 Save to Supabase result: {save_result}")
            return menu_data
        except Exception as e:
            print(f"❌ FoodPro fetch failed: {e}")
            return {"error": f"FoodPro fetch failed for {hall_slug}: {e}"}

    print(f"🌐 Fetching fresh menu from Nutrislice API for {university} - {hall_slug}")
    try:
        menu_data = fetch_nutrislice_menu(university, hall_slug, dining_hall, date)
        if not menu_data.get("items"):
            print(f"⚠️ Nutrislice returned no items, skipping cache save")
            return {"error": f"No menu items available for {hall_slug} on {date}"}
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

    def _attempt_all_meals(location_num: str, location_name: str) -> dict:
        base_url = fp_build_menu_url(date, location_num, location_name)  
        print(f"🔗 FoodPro base URL: {base_url}")
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
            print(f"   ↳ Fetching meal '{meal}' with URL: {meal_url}")
            try:
                soup = _fetch_with_retries(meal_url)
            except Exception as e:
                warnings.append(f"Failed to fetch {meal}: {e}")
                continue
            try:
                parsed = fp_parse_menu(str(soup), meal_type=meal, date=date, base_url=meal_url)  
            except Exception as e:
                warnings.append(f"Failed to parse {meal}: {e}")
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

    attempt = _attempt_all_meals(params["locationNum"], params["locationName"])  

    
    if hall_slug == "the-atrium" and not attempt["items"]:
        for alt_name in [
            "Atrium",  
            "The Atrium at Livingston",  
            "The Atrium at Livingston Campus",
        ]:
            try:
                print(f"🔁 Retrying The Atrium with locationName='{alt_name}'")
                attempt = _attempt_all_meals(params["locationNum"], alt_name)
                if attempt["items"]:
                    break
            except Exception as e:
                print(f"⚠️ Retry failed for Atrium alt name '{alt_name}': {e}")

    
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
    
    # Log if we got no items
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
async def generate_meal_plan_route(meal_plan_request: MealPlanRequest, authorization: Optional[str] = Header(None), background_tasks: BackgroundTasks = None):
    """Generate a meal plan for a user (authenticated) with backend-authoritative macro math."""
    try:
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = verify_session_token(token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required. Guest users not yet supported in FastAPI version.")
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

        #DAILY GENERATION LIMIT (max 3 per UTC day per user)
        # try:
        #     generation_limit = 3
        #     now_utc = datetime.utcnow()
        #     start_of_day = datetime(now_utc.year, now_utc.month, now_utc.day)
        #     end_of_day = start_of_day + timedelta(days=1)
        #     start_iso = start_of_day.isoformat()
        #     end_iso = end_of_day.isoformat()
        #     count_resp = supabase.table("meal_plan_generations") \
        #         .select("id", count="exact") \
        #         .eq("user_id", authenticated_user["id"]) \
        #         .gte("created_at", start_iso) \
        #         .lt("created_at", end_iso) \
        #         .execute()
        #     existing_count = getattr(count_resp, 'count', None)
        #     if existing_count is None:
        #         existing_count = len(count_resp.data or [])
        #     if existing_count >= generation_limit:
        #         raise HTTPException(status_code=429, detail=f"Daily meal plan generation limit reached ({generation_limit}). Try again tomorrow.")
        # except HTTPException:
        #     raise
        # except Exception as _limit_e:
        #     print(f"⚠️ Generation limit check failed: {_limit_e}")

        menu_data = get_dining_hall_menu(meal_plan_request.dining_hall, meal_plan_request.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=500, detail=f"Failed to fetch menu: {menu_data['error']}")
        items_list = menu_data.get("items", []) or []
        if not items_list:
            raise HTTPException(status_code=503, detail=f"No menu items available for {meal_plan_request.dining_hall} on {meal_plan_request.date}.")

        # Build lookup (uppercase name -> nutrition)
        lookup = {}
        for it in items_list:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            key = name.upper()
            try:
                cals = float(it.get("calories")) if it.get("calories") not in (None, "") else None
            except Exception:
                cals = None
            try:
                prot = float(it.get("protein_g") or it.get("protein")) if it.get("protein_g") not in (None, "") else None
            except Exception:
                prot = None
            if cals is None or prot is None:
                # skip unusable items (no nutrient data)
                continue
            lookup[key] = {
                "name": name,
                "calories": cals,
                "protein_g": prot,
                "serving_size": it.get("serving_size") or it.get("portion_size") or it.get("serving") or None,
            }

        if len(lookup) == 0:
            raise HTTPException(status_code=503, detail="No nutritionally usable items available (all missing calories/protein).")

        # Filter lookup by dietary preference if needed
        dietary = user.dietary_preference
        if dietary in ("vegan", "vegetarian"):
            # crude filter: remove items whose name suggests meat (improve later)
            meat_terms = ["CHICKEN", "BEEF", "PORK", "FISH", "SALMON", "TURKEY", "TUNA", "HAM", "BACON"]
            def is_meat(n):
                up = n.upper()
                return any(t in up for t in meat_terms)
            if dietary == "vegetarian":
                filtered = {k:v for k,v in lookup.items() if not is_meat(v['name']) or 'EGG' in v['name'].upper()}
            else:  # vegan - also exclude eggs, cheese, dairy keywords
                dairy_terms = meat_terms + ["EGG", "CHEESE", "MILK", "YOGURT", "BUTTER"]
                def is_animal(n):
                    up = n.upper()
                    return any(t in up for t in dairy_terms)
                filtered = {k:v for k,v in lookup.items() if not is_animal(v['name'])}
            if filtered:
                lookup = filtered
        if not lookup:
            raise HTTPException(status_code=503, detail="Dietary filter removed all items. Cannot build plan.")

        # Compute targets from user goal (protein now fixed at 1.1x bodyweight per updated requirement)
        if user.goal == "bulk":
            target_calories = user_tdee + 400
        elif user.goal == "cut":
            target_calories = user_tdee - 400
        else:
            target_calories = user_tdee
        target_calories = int(round(target_calories))
        target_protein = int(round(user.weight_lbs * 1.1))

        # Prepare menu names for model
        model_menu_listing = "\n".join(sorted(n for n in [v['name'] for v in lookup.values()]))

        user_context = f"Goal: {user.goal}; Target Calories: {target_calories}; Target Protein: {target_protein}g; Dietary: {dietary or 'none'}; Menu Items:\n<<ITEM_LIST>>"

        user_context = user_context.replace("<<ITEM_LIST>>", model_menu_listing)
        ai_start = time.time()
        ai_resp = client.chat.completions.create(
            model=MEAL_PLAN_MODEL,
            messages=[
                #{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_context},
            ],
            temperature=0.6,
        )
        ai_raw = ai_resp.choices[0].message.content.strip()

        # --- Robust JSON extraction / repair ---
        def _robust_parse_ai_json(raw: str):
            import json, re
            original = raw
            # 1. Strip markdown fences
            raw = re.sub(r"^```(?:json|JSON)?", "", raw.strip())
            raw = re.sub(r"```$", "", raw.strip())
            raw = raw.strip()
            # Quick direct parse
            try:
                return json.loads(raw)
            except Exception:
                pass
            # 2. If raw contains multiple lines with commentary, isolate largest balanced brace block
            def _largest_brace_block(s: str):
                best = None
                stack = 0
                start = None
                for i,ch in enumerate(s):
                    if ch == '{':
                        if stack == 0:
                            start = i
                        stack += 1
                    elif ch == '}':
                        if stack > 0:
                            stack -= 1
                            if stack == 0 and start is not None:
                                cand = s[start:i+1]
                                if not best or len(cand) > len(best):
                                    best = cand
                return best
            block = _largest_brace_block(raw)
            if block:
                try:
                    return json.loads(block)
                except Exception:
                    raw = block  # continue attempts on block only
            # 3. Light repairs: remove trailing commas, convert single quotes -> double (naive but constrained)
            repaired = raw
            # Remove trailing commas before } or ]
            repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
            # Replace single-quoted keys/strings that are simple (no embedded quotes)
            repaired = re.sub(r"'([^'\n\r]*)'", lambda m: '"'+m.group(1).replace('"','\\"')+'"', repaired)
            # Ensure numeric tokens ok; attempt parse
            try:
                return json.loads(repaired)
            except Exception:
                pass
            # 4. Last resort: wrap in braces if missing
            if not repaired.strip().startswith('{') and '{' not in repaired:
                repaired = '{' + repaired + '}'
                try:
                    return json.loads(repaired)
                except Exception:
                    pass
            print("[AI JSON Parser] Failed to parse. Raw snippet (first 300 chars):", original[:300])
            raise HTTPException(status_code=500, detail="Failed to parse AI JSON")

        try:
            plan_raw = _robust_parse_ai_json(ai_raw)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to parse AI JSON")

        # Validate skeleton
        if not isinstance(plan_raw, dict) or 'meals' not in plan_raw:
            raise HTTPException(status_code=500, detail="AI response missing meals")
        meals_block = plan_raw.get('meals') or {}
        for meal_name in ['breakfast', 'lunch', 'dinner']:
            if meal_name not in meals_block or not isinstance(meals_block[meal_name], dict):
                raise HTTPException(status_code=500, detail=f"AI missing meal: {meal_name}")

        # Normalize & compute nutrition (track unmatched)
        unmatched_by_meal = {m: [] for m in ['breakfast','lunch','dinner']}
        def compute_meal(meal_obj, meal_label):
            items_out = []
            for it in (meal_obj.get('items') or []):
                if not isinstance(it, dict):
                    continue
                nm = (it.get('name') or '').strip()
                if not nm:
                    continue
                sv_raw = it.get('servings', 1)
                try:
                    sv = float(sv_raw) if sv_raw not in (None, '') else 1.0
                except Exception:
                    sv = 1.0
                if sv <= 0:
                    sv = 1.0
                key = nm.upper()
                base = lookup.get(key)
                if not base:
                    unmatched_by_meal[meal_label].append(nm)
                    continue
                calories = base['calories'] * sv
                protein = base['protein_g'] * sv
                items_out.append({
                    'name': base['name'],
                    'servings': sv,
                    'calories': round(calories),
                    'protein_g': round(protein),
                    'serving_size': base.get('serving_size'),
                })
            # If meal ends up empty, seed with top protein items not yet used
            if not items_out:
                existing_meal_items = []
                for _mn in ['breakfast','lunch','dinner']:
                    if _mn in meals_block and isinstance(meals_block[_mn], dict):
                        for _it in (meals_block[_mn].get('items') or []):
                            if isinstance(_it, dict) and _it.get('name'):
                                existing_meal_items.append({'name': _it.get('name')})
                used_names = { (it.get('name') or '').upper() for it in existing_meal_items if it.get('name') }
                # choose up to 2 highest protein density items
                dens_sorted = sorted(lookup.values(), key=lambda v: (v['protein_g']/max(v['calories'],1)), reverse=True)
                added = 0
                for cand in dens_sorted:
                    if cand['name'].upper() in used_names:
                        continue
                    items_out.append({
                        'name': cand['name'],
                        'servings': 1.0,
                        'calories': round(cand['calories']),
                        'protein_g': round(cand['protein_g']),
                        'serving_size': cand.get('serving_size')
                    })
                    added += 1
                    if added >= 2:
                        break
            return items_out

        meal_items = {m: compute_meal(meals_block[m], m) for m in ['breakfast', 'lunch', 'dinner']}

        def sum_totals(items_all):
            c = sum(it['calories'] for it in items_all)
            p = sum(it['protein_g'] for it in items_all)
            return c, p

        # Adjustment logic
        def adjust_plan():
            # flatten
            for _round in range(8):
                all_items = [*meal_items['breakfast'], *meal_items['lunch'], *meal_items['dinner']]
                calories, protein = sum_totals(all_items)
                cal_low = calories < 0.95 * target_calories
                cal_high = calories > 1.05 * target_calories
                prot_low = protein < target_protein
                if not (cal_low or cal_high or prot_low):
                    return
                # Build density lists
                density = []
                for meal_name in meal_items:
                    for it in meal_items[meal_name]:
                        if it['calories'] > 0:
                            density.append((it['protein_g']/it['calories'], meal_name, it))
                density.sort(reverse=True, key=lambda x: x[0])
                # pool of candidates not yet used or low usage
                usage = {}
                for it in all_items:
                    usage[it['name']] = usage.get(it['name'], 0) + 1
                # functions
                def add_high_protein():
                    # choose best new item
                    existing_names = {it['name'].upper() for it in all_items}
                    candidates = sorted(lookup.values(), key=lambda v: (v['protein_g']/max(v['calories'],1)), reverse=True)
                    for cand in candidates:
                        if cand['name'].upper() in existing_names and usage.get(cand['name'],0) >= 2:
                            continue
                        # add to smallest meal by protein
                        meal_order = sorted(meal_items.keys(), key=lambda mn: sum(i['protein_g'] for i in meal_items[mn]))
                        target_meal = meal_order[0]
                        meal_items[target_meal].append({
                            'name': cand['name'], 'servings': 1.0,
                            'calories': round(cand['calories']), 'protein_g': round(cand['protein_g']),
                            'serving_size': cand.get('serving_size')
                        })
                        return True
                    return False
                def scale_existing(up: bool, protein_first: bool):
                    ordered = density if up else list(reversed(density))
                    for _, meal_name, it in ordered:
                        # limit servings 0.5 - 3
                        new_sv = (it['servings'] + (0.5 if up else -0.5)) if up else (it['servings'] - 0.5)
                        if new_sv < 0.5 or new_sv > 3:
                            continue
                        # find base
                        base = lookup[it['name'].upper()]
                        it['servings'] = round(new_sv,2)
                        it['calories'] = round(base['calories'] * it['servings'])
                        it['protein_g'] = round(base['protein_g'] * it['servings'])
                        return True
                    return False
                if prot_low:
                    # try scaling high-protein first
                    if not scale_existing(True, True):
                        if not add_high_protein():
                            return
                    continue
                # calories fix
                if cal_low:
                    if not scale_existing(True, False):
                        add_high_protein()
                    continue
                if cal_high:
                    scale_existing(False, False)
                    continue
        adjust_plan()

        # Final recompute
        all_items_final = [*meal_items['breakfast'], *meal_items['lunch'], *meal_items['dinner']]
        total_cals, total_pro = sum(it['calories'] for it in all_items_final), sum(it['protein_g'] for it in all_items_final)
        within_5 = abs(total_cals - target_calories) <= 0.05 * max(target_calories,1)

        meal_plan = {
            'user_stats': {
                'bmr': user_bmr,
                'tdee': user_tdee,
                'goal': user.goal,
                'dietary_preference': dietary if dietary in ('vegan','vegetarian') else None,
            },
            'goals': {
                'calories': target_calories,
                'protein_g': target_protein,
            },
            'totals': {
                'calories': int(total_cals),
                'protein_g': int(total_pro),
            },
            'meals': {
                m: {
                    'description': (plan_raw.get('meals', {}).get(m, {}) or {}).get('description') or f'{m.title()} meal',
                    'items': meal_items[m]
                } for m in ['breakfast','lunch','dinner']
            },
            'explanation': plan_raw.get('explanation') or 'Backend computed macros; protein prioritized then calories adjusted.',
            'validation': {
                'calorie_target': target_calories,
                'actual_calories': int(total_cals),
                'within_5_percent': within_5,
                'protein_target': target_protein,
                'protein_met': total_pro >= target_protein,
            },
            'debug': {
                'unmatched_items': unmatched_by_meal
            }
        }

        generation_id = None
        try:
            insert_payload = {
                'user_id': authenticated_user.get('id'),
                'date': meal_plan_request.date,
                'dining_hall': meal_plan_request.dining_hall,
                #'system_prompt': system_prompt,
                'user_prompt': user_context,
                'full_messages': json.dumps([
                    #{'role':'system','content':system_prompt},
                    {'role':'user','content':user_context}
                ]),
                'model_raw_response': ai_raw,
                'meal_plan': meal_plan,
            }
            inserted = supabase.table('meal_plan_generations').insert(insert_payload).execute()
            if inserted.data:
                generation_id = inserted.data[0]['id']
        except Exception as e:
            print(f"⚠️ Failed to insert meal_plan_generation: {e}")

        ai_time = time.time() - ai_start
        if background_tasks and authenticated_user.get('email'):
            try:
                background_tasks.add_task(
                    send_meal_plan_generation_email,
                    authenticated_user,
                    generation_id,
                    meal_plan_request.dining_hall,
                    meal_plan_request.date,
                )
            except Exception as e:
                print(f"[brevo] scheduling meal plan email failed: {e}")
        return {
            'success': True,
            'user_profile': asdict(user),
            'nutrition_targets': {'calories': target_calories, 'protein_grams': target_protein},
            'bmr_tdee': {'bmr': user_bmr, 'tdee': user_tdee},
            'available_items': list(lookup.values()),
            'meal_plan': meal_plan,
            'generation_id': generation_id,
            'ask_feedback': 'Did you like this meal plan?',
            'processing_info': {
                'ai_generation_time_seconds': round(ai_time,2),
            },
            'meta': {
                'dining_hall': meal_plan_request.dining_hall,
                'date': meal_plan_request.date,
                'ai_model': MEAL_PLAN_MODEL,
                'authenticated': authenticated_user is not None,
                'raw_ai_response_length': len(ai_raw),
                'dietary_preference': dietary,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")


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
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://dormdish.com")  
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

BREVO_MEAL_PLAN_TEMPLATE_ID = os.getenv("BREVO_MEAL_PLAN_TEMPLATE_ID", "").strip()  # Template for meal plan generation notification

def send_meal_plan_generation_email(user_row: dict, generation_id: Optional[str], dining_hall: str, date: str):
    """Send Brevo email notifying user their meal plan was generated.
    Uses BREVO_MEAL_PLAN_TEMPLATE_ID. Silently returns if misconfigured.
    """
    try:
        if not user_row or not user_row.get("email"):
            return
        if not BREVO_MEAL_PLAN_TEMPLATE_ID:
            return
        try:
            template_id = int(re.match(r"(\d+)", BREVO_MEAL_PLAN_TEMPLATE_ID).group(1))  # type: ignore
        except Exception:
            print("[brevo] Invalid BREVO_MEAL_PLAN_TEMPLATE_ID value; skipping meal plan email")
            return
        # Ensure contact exists (best effort)
        try:
            guess = _guess_name_from_email(user_row["email"]) if user_row.get("email") else {"first": "", "last": ""}
            upsert_brevo_contact(
                user_row["email"],
                attributes={
                    "FIRSTNAME": (user_row.get("username") or guess.get("first") or ""),
                    "LASTNAME": guess.get("last") or "",
                },
            )
        except Exception as e:
            print(f"[brevo] contact upsert failed (continuing): {e}")
        # Build payload similar to send_brevo_template but with dynamic params
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
        body = {
            "templateId": template_id,
            "to": [{"email": user_row["email"], "name": user_row.get("username") or guess.get("first") or None}],
            "sender": {"email": from_email, "name": from_name},
            "params": params,
            "tags": ["meal_plan_generated"],
        }
        try:
            r = requests.post(f"{_brevo_base_url()}/smtp/email", headers=_brevo_headers(), data=json.dumps(body), timeout=12)
            if r.status_code not in (200, 201):
                print(f"[brevo] meal plan email send failed status={r.status_code} body={r.text[:200]}")
            else:
                print(f"[brevo] meal plan email sent generation_id={generation_id}")
        except Exception as e:
            print(f"[brevo] meal plan email error: {e}")
    except Exception as outer:
        print(f"[brevo] unexpected meal plan email error: {outer}")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)