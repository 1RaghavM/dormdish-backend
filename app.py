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
# Mailchimp helpers
# ---------------------------
class SubscribeRequest(BaseModel):
    email: EmailStr
    firstName: Optional[str] = None
    lastName: Optional[str] = None


def _mc_datacenter() -> str:
    dc = os.getenv("MAILCHIMP_SERVER_PREFIX")
    if dc:
        return dc
    key = os.getenv("MAILCHIMP_API_KEY", "")
    return key.split("-")[-1] if "-" in key else "us1"


def _mc_base_url() -> str:
    return f"https://{_mc_datacenter()}.api.mailchimp.com/3.0"


def _mc_headers() -> Dict[str, str]:
    api_key = os.getenv("MAILCHIMP_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="MAILCHIMP_API_KEY not configured")
    token = base64.b64encode(f"anystring:{api_key}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def _subscriber_hash(email: str) -> str:
    return hashlib.md5(email.lower().encode("utf-8")).hexdigest()


def _parse_template_id() -> Optional[int]:
    raw = os.getenv("MAILCHIMP_TEMPLATE_ID", "").strip()
    if not raw:
        return None
    # Accept formats like "244405234_..." or plain integer
    m = re.match(r"(\d+)", raw)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    try:
        return int(raw)
    except Exception:
        return None


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


def upsert_mailchimp_member(email: str, merge_fields: Optional[Dict[str, str]] = None, tags: Optional[List[str]] = None, status_if_new: str = "subscribed") -> Dict:
    list_id = os.getenv("MAILCHIMP_LIST_ID")
    if not list_id:
        raise HTTPException(status_code=500, detail="MAILCHIMP_LIST_ID not configured")

    url = f"{_mc_base_url()}/lists/{list_id}/members/{_subscriber_hash(email)}"
    payload = {
        "email_address": email,
        "status_if_new": status_if_new,
        "merge_fields": merge_fields or {},
    }
    try:
        resp = requests.put(url, headers=_mc_headers(), data=json.dumps(payload), timeout=12)
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Mailchimp member upsert failed: {resp.text}")
        member = resp.json()
        # Apply tags if provided
        if tags:
            tags_url = f"{_mc_base_url()}/lists/{list_id}/members/{_subscriber_hash(email)}/tags"
            tags_payload = {"tags": [{"name": t, "status": "active"} for t in tags]}
            tr = requests.post(tags_url, headers=_mc_headers(), data=json.dumps(tags_payload), timeout=12)
            if tr.status_code >= 400:
                # Log but don't fail subscription on tag error
                print(f"⚠️ Mailchimp tag apply failed: {tr.text}")
        return member
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Mailchimp error: {e}")


def _create_static_segment_for_email(list_id: str, email: str) -> Optional[int]:
    url = f"{_mc_base_url()}/lists/{list_id}/segments"
    name = f"api-single-{int(time.time())}-{_subscriber_hash(email)[:8]}"
    payload = {"name": name, "static_segment": [email]}
    r = requests.post(url, headers=_mc_headers(), data=json.dumps(payload), timeout=12)
    if r.status_code >= 400:
        print(f"⚠️ Failed to create static segment: {r.text}")
        return None
    return r.json().get("id")


def _delete_segment(list_id: str, segment_id: int):
    try:
        url = f"{_mc_base_url()}/lists/{list_id}/segments/{segment_id}"
        requests.delete(url, headers=_mc_headers(), timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to delete static segment {segment_id}: {e}")


def send_mailchimp_template(email: str, first_name: Optional[str] = None, last_name: Optional[str] = None, subject: Optional[str] = None, tags: Optional[List[str]] = None) -> Dict:
    """Send a one-off campaign using a saved Mailchimp template to a single recipient.
    Note: This uses Marketing API via a temporary static segment.
    """
    list_id = os.getenv("MAILCHIMP_LIST_ID")
    from_email = os.getenv("MAILCHIMP_FROM_EMAIL")
    from_name = os.getenv("MAILCHIMP_FROM_NAME", "DormDish")
    template_id = _parse_template_id()

    if not (list_id and from_email and template_id):
        raise HTTPException(status_code=500, detail="Mailchimp env vars missing (LIST_ID, FROM_EMAIL, TEMPLATE_ID)")

    # Ensure contact exists with merge fields
    if not first_name or not last_name:
        guess = _guess_name_from_email(email)
        first_name = first_name or guess["first"]
        last_name = last_name or guess["last"]

    merge_fields = {"FNAME": first_name or "", "LNAME": last_name or ""}
    upsert_mailchimp_member(
        email,
        merge_fields=merge_fields,
        tags=tags or ["newsletter"],
        status_if_new=os.getenv("MAILCHIMP_DOUBLE_OPT_IN", "false").lower() == "true" and "pending" or "subscribed",
    )

    # Create a static segment targeting only this email
    seg_id = _create_static_segment_for_email(list_id, email)
    if not seg_id:
        raise HTTPException(status_code=502, detail="Failed to create Mailchimp segment")

    try:
        # Create a campaign
        camp_url = f"{_mc_base_url()}/campaigns"
        subject = subject or "Welcome to DormDish!"
        payload = {
            "type": "regular",
            "recipients": {"list_id": list_id, "segment_opts": {"saved_segment_id": seg_id}},
            "settings": {
                "subject_line": subject,
                "title": f"API Send to {email}",
                "from_name": from_name,
                "reply_to": from_email,
            },
        }
        cr = requests.post(camp_url, headers=_mc_headers(), data=json.dumps(payload), timeout=12)
        if cr.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Failed to create campaign: {cr.text}")
        campaign = cr.json()
        campaign_id = campaign.get("id")

        # Set campaign content from template
        content_url = f"{_mc_base_url()}/campaigns/{campaign_id}/content"
        content_payload = {"template": {"id": template_id}}
        conr = requests.put(content_url, headers=_mc_headers(), data=json.dumps(content_payload), timeout=12)
        if conr.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Failed to set campaign content: {conr.text}")

        # Send campaign
        send_url = f"{_mc_base_url()}/campaigns/{campaign_id}/actions/send"
        sr = requests.post(send_url, headers=_mc_headers(), timeout=12)
        if sr.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"Failed to send campaign: {sr.text}")

        return {"success": True, "campaign_id": campaign_id}
    finally:
        # Best-effort cleanup
        if seg_id:
            _delete_segment(list_id, seg_id)


@app.post('/subscribe', summary='Subscribe to newsletter and send welcome email')
def subscribe(req: SubscribeRequest):
    try:
        # Upsert member and send template email
        fn = (req.firstName or "").strip()
        ln = (req.lastName or "").strip()
        res = send_mailchimp_template(req.email.strip().lower(), first_name=fn or None, last_name=ln or None)
        return {"success": True, "message": "Subscription successful. Email sent.", **res}
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"⚠️ Subscribe failed: {e}")
        raise HTTPException(status_code=500, detail="Subscription failed")

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

        # Push to Mailchimp if notifications allowed
        mc_status = None
        if bool(data.notifications):
            try:
                merge_fields = {
                    "FNAME": data.firstName.strip(),
                    "LNAME": data.lastName.strip(),
                    "UNIVERSITY": data.university.strip(),
                    "GRADYEAR": (data.graduationYear or ""),
                }
                member = upsert_mailchimp_member(
                    data.email.strip().lower(),
                    merge_fields=merge_fields,
                    tags=["waitlist"],
                    status_if_new=os.getenv("MAILCHIMP_DOUBLE_OPT_IN", "false").lower() == "true" and "pending" or "subscribed",
                )
                mc_status = member.get("status")
                if (mc_status or "").lower() == "subscribed":
                    try:
                        send_mailchimp_template(
                            data.email.strip().lower(),
                            first_name=data.firstName.strip(),
                            last_name=data.lastName.strip(),
                            subject="You're on the DormDish waitlist!",
                            tags=["waitlist"],
                        )
                    except HTTPException:
                        pass
            except HTTPException as e:
                print(f"⚠️ Mailchimp sync failed: {e.detail}")

        # Count people on the same university waitlist
        count_resp = supabase.table('waitlist_users').select('id', count='exact').eq('university', data.university.strip()).execute()
        uni_count = getattr(count_resp, 'count', None)
        return {"success": True, "message": "Successfully joined waitlist", "university_count": uni_count or 0, "mailchimp_status": mc_status}
    except Exception as e:
        print(f"⚠️ Waitlist join failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to join waitlist")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)