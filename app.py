from fastapi import FastAPI, HTTPException, Depends, status
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
from typing import Dict, List, Optional
from supabase import create_client, Client
from dotenv import load_dotenv
import bcrypt
import csv
from functools import lru_cache
import base64

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

@app.post('/waitlist/join')
def join_waitlist(data: WaitlistJoinRequest):
    try:
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

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)