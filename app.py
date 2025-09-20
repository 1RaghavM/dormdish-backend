from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from openai import OpenAI
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
        
        # Extract required fields from menu_data or use fallbacks
        hall_slug = menu_data.get("hall_slug", dining_hall)
        fetched_at = menu_data.get("fetched_at", datetime.now().isoformat())
        
        # Check for items in the new structure
        items_count = 0
        if "items" in menu_data:
            items_count = len(menu_data["items"])
        elif "menu" in menu_data and "items" in menu_data["menu"]:
            items_count = len(menu_data["menu"]["items"])
        
        print(f"🔍 Saving menu with {items_count} items to Supabase")
        print(f"🔍 Menu data keys: {list(menu_data.keys())}")
        
        # Prepare the menu data for storage - use the full response
        stored_menu_data = menu_data.get("menu", menu_data)
        
        data_to_insert = {
            "id": menu_id,
            "university": university,
            "dining_hall": dining_hall,
            "hall_slug": hall_slug,
            "date": date,
            "fetched_at": fetched_at,
            "menu_data": stored_menu_data,  # Store the actual menu data
            "created_at": datetime.now().isoformat()
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
    """Fetch dining hall menu - check Supabase first, then fetch directly from Nutrislice API if needed"""
    
    # Map dining hall names to the correct format and university
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
        
        # Rutgers halls - map to correct hall slugs
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
        "cook/douglass": ("rutgers", "neilson-dining-hall")
    }
    
    # Get the university and hall slug
    mapping = dining_hall_mapping.get(dining_hall.lower())
    if not mapping:
        return {"error": f"Unknown dining hall: {dining_hall}"}
    
    university, hall_slug = mapping
    
    # First, check if menu exists in Supabase (use original dining_hall for cache key)
    cached_menu = get_menu_from_supabase(university, dining_hall, date)
    
    if cached_menu is not None:
        print(f"✅ Using cached menu from Supabase")
        return cached_menu
    
    # If not in cache, fetch directly from Nutrislice API
    print(f"🌐 Fetching fresh menu from Nutrislice API for {university} - {hall_slug}")
    
    try:
        menu_data = fetch_nutrislice_menu(university, hall_slug, dining_hall, date)
        
        # Save to Supabase for future use (use original dining_hall for cache key)
        save_result = save_menu_to_supabase(university, dining_hall, date, menu_data)
        print(f"💾 Save to Supabase result: {save_result}")
        
        return menu_data
        
    except Exception as e:
        return {"error": f"Failed to fetch menu: {str(e)}"}


def fetch_nutrislice_menu(university: str, hall_slug: str, dining_hall: str, date: str) -> dict:
    """Fetch menu directly from Nutrislice API"""
    
    # API base URLs for each university
    api_bases = {
        "rutgers": "https://rutgers.api.nutrislice.com/menu/api/weeks/school",
        "hopkins": "https://hopkinsdining.api.nutrislice.com/menu/api/weeks/school"
    }
    
    # Meal slugs for each university
    meal_configs = {
        "rutgers": ["breakfast", "lunch-test", "dinner"],
        "hopkins": {
            "nolans-on-33rd": ["nolans-breakfast", "nolans-dinner-2", "nolans-dinner"],
            "peabody-institute": ["peabody-breakfast", "peabody-lunch", "peabody-dinner"],
            "hopkins-cafe": ["hopkins-cafe-breakfast", "hopkins-cafe-lunch", "hopkins-cafe-dinner"]
        }
    }
    
    api_base = api_bases.get(university)
    if not api_base:
        raise ValueError(f"Unsupported university: {university}")
    
    # Get meal slugs
    if university == "hopkins":
        meal_slugs = meal_configs["hopkins"].get(hall_slug, [])
        if not meal_slugs:
            # Fallback pattern for Hopkins
            prefix = hall_slug.split("-")[0]
            meal_slugs = [f"{prefix}-breakfast", f"{prefix}-lunch", f"{prefix}-dinner"]
    else:
        meal_slugs = meal_configs[university]
    
    all_items = []
    meal_sources = []
    warnings = []
    
    # Fetch items from each meal
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
    
    # Remove duplicates
    seen_items = set()
    unique_items = []
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
        "items": unique_items,  # Put items at top level for compatibility
        "menu": {
            "items": unique_items,
            "meal_sources": meal_sources
        },
        "meta": {
            "warnings": warnings,
            "notes": []
        },
        "error": None
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
        
        # Find the day object for the requested date
        day_obj = None
        candidate_days = []
        
        # Look for days in various possible locations
        if isinstance(data, dict):
            if isinstance(data.get("days"), list):
                candidate_days.extend(data["days"])
            if isinstance(data.get("week"), dict) and isinstance(data["week"].get("days"), list):
                candidate_days.extend(data["week"]["days"])
        
        # Find the matching date
        for d in candidate_days:
            if isinstance(d, dict) and d.get("date") == date:
                day_obj = d
                break
        
        if not day_obj:
            return []
        
        # Extract items from the day object
        items = []
        
        # Check various possible item locations
        for key in ["menu_items", "items", "menuItems"]:
            if isinstance(day_obj.get(key), list):
                for item in day_obj[key]:
                    if isinstance(item, dict):
                        processed_item = process_menu_item(item)
                        if processed_item:
                            items.append(processed_item)
        
        # Check menu groups/categories
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


def process_menu_item(item: dict) -> dict:
    """Process a menu item and extract nutritional information"""
    # Get item name
    name = None
    if isinstance(item.get("food"), dict):
        name = item["food"].get("name")
    if not name:
        name = item.get("name") or item.get("item_name")
    
    if not name:
        return None
    
    # Extract nutritional information
    nutrition = {}
    
    # Check if there's nested food data
    if isinstance(item.get("food"), dict):
        food = item["food"]
        rni = food.get("rounded_nutrition_info") or {}
        serving_info = food.get("serving_size_info") or {}
        
        nutrition = {
            "protein_g": rni.get("g_protein"),
            "calories": rni.get("calories"),
            "serving_size": f"{serving_info.get('serving_size_amount', '')} {serving_info.get('serving_size_unit', '')}".strip()
        }
    else:
        # Direct item data
        nutrition = {
            "protein_g": item.get("g_protein") or item.get("protein"),
            "calories": item.get("calories") or item.get("kcal"),
            "serving_size": item.get("serving_size") or item.get("portion_size")
        }
    
    # Clean up nutrition data
    result = {"name": name}
    
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

def calculate_calorie_needs(user: User) -> Dict[str, float]:
    """Calculate daily calorie and macro needs based on user profile"""
    # Mifflin-St Jeor Equation for BMR (men)
    bmr = (10 * user.weight_kg) + (6.25 * user.height_cm) - (5 * user.age) + 5
    
    # Activity multipliers
    activity_multipliers = {
        "sedentary": 1.2,
        "active": 1.55,
        "very_active": 1.725
    }
    
    # Calculate TDEE
    tdee = bmr * activity_multipliers.get(user.activity_level, 1.55)
    
    # Adjust for goal
    if user.goal == "bulk":
        target_calories = tdee + 300  # Moderate surplus
        protein_per_lb = 1.0  # 1g per lb for bulking
    elif user.goal == "cut":
        target_calories = tdee - 500  # Moderate deficit
        protein_per_lb = 1.2  # Higher protein during cut
    else:  # maintain
        target_calories = tdee
        protein_per_lb = 0.8
    
    # Calculate macros
    protein_grams = user.weight_lbs * protein_per_lb
    protein_calories = protein_grams * 4
    
    # 25-30% fat
    fat_calories = target_calories * 0.27
    fat_grams = fat_calories / 9
    
    # Remaining calories from carbs
    carb_calories = target_calories - protein_calories - fat_calories
    carb_grams = carb_calories / 4
    
    return {
        "calories": round(target_calories),
        "protein_grams": round(protein_grams),
        "carb_grams": round(carb_grams),
        "fat_grams": round(fat_grams)
    }

def generate_meal_plan(user: User, menu_data: dict) -> dict:
    """Generate a meal plan based on user profile and available menu"""
    nutritional_needs = calculate_calorie_needs(user)
    
    # Create user context for the AI
    user_context = f"""
    User Profile:
    - Age: {user.age}, Height: {user.height_feet}'{user.height_inches}", Weight: {user.weight_lbs}lbs
    - Activity Level: {user.activity_level}
    - Goal: {user.goal}
    
    Nutritional Targets:
    - Calories: {nutritional_needs['calories']}
    - Protein: {nutritional_needs['protein_grams']}g
    - Carbs: {nutritional_needs['carb_grams']}g  
    - Fat: {nutritional_needs['fat_grams']}g
    
    Available Menu: {json.dumps(menu_data, indent=2)}
    
    Please create an optimized meal plan that meets the user's nutritional targets using only items from the available menu. 
    Focus on high-quality protein sources and nutrient-dense foods that support their {user.goal} goal.
    Provide specific portion recommendations and explain how the plan meets their macronutrient needs.
    """
    
    return {
        "user_profile": asdict(user),
        "nutritional_targets": nutritional_needs,
        "menu_data": menu_data,
        "ai_prompt": user_context
    }

def generate_meal_plan_with_bmr(user: User, menu_data: dict, bmr: float, tdee: float) -> dict:
    """Generate a meal plan with BMR/TDEE information for more accurate targeting"""
    # Calculate target calories based on goal and TDEE
    if user.goal == "bulk":
        target_calories = tdee + 400  # Moderate surplus
        protein_per_lb = 1.0
    elif user.goal == "cut":
        target_calories = tdee - 400  # Moderate deficit
        protein_per_lb = 1.2
    else:  # maintain
        target_calories = tdee
        protein_per_lb = 0.8
    
    # Calculate macros
    protein_grams = user.weight_lbs * protein_per_lb
    protein_calories = protein_grams * 4
    
    # 25-30% fat
    fat_calories = target_calories * 0.27
    fat_grams = fat_calories / 9
    
    # Remaining calories from carbs
    carb_calories = target_calories - protein_calories - fat_calories
    carb_grams = carb_calories / 4
    
    nutritional_needs = {
        "calories": round(target_calories),
        "protein_grams": round(protein_grams),
        "carb_grams": round(carb_grams),
        "fat_grams": round(fat_grams),
        "bmr": bmr,
        "tdee": tdee
    }
    
    # Create user context for the AI
    user_context = f"""
    User Profile:
    - Age: {user.age}, Height: {user.height_feet}'{user.height_inches}", Weight: {user.weight_lbs}lbs
    - Activity Level: {user.activity_level}
    - Goal: {user.goal}
    
    Metabolic Information:
    - BMR (Basal Metabolic Rate): {bmr} calories
    - TDEE (Total Daily Energy Expenditure): {tdee} calories
    
    Nutritional Targets (based on TDEE and goal):
    - Target Calories: {nutritional_needs['calories']} (TDEE {'+' if user.goal == 'bulk' else '-' if user.goal == 'cut' else '='} adjustment for {user.goal})
    - Protein: {nutritional_needs['protein_grams']}g
    - Carbs: {nutritional_needs['carb_grams']}g  
    - Fat: {nutritional_needs['fat_grams']}g
    
    Available Menu Items: {len(menu_data.get('items', []))} items available
    {json.dumps(menu_data.get('items', []), indent=2)}
    
    Please create an optimized meal plan that meets the user's nutritional targets using only items from the available menu. 
    The plan should align with their {user.goal} goal and stay within ±5% of the target calories.
    Focus on high-quality protein sources and nutrient-dense foods.
    """
    
    return {
        "user_profile": asdict(user),
        "nutritional_targets": nutritional_needs,
        "menu_data": menu_data,
        "ai_prompt": user_context
    }

# Pydantic models for request/response validation
class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    age: int
    height_feet: int
    height_inches: int
    weight_lbs: float
    gender: str
    activity_level: str
    goal: str
    university: str

class LoginRequest(BaseModel):
    email: str
    password: str

class UpdateProfileRequest(BaseModel):
    age: Optional[int] = None
    height_feet: Optional[int] = None
    height_inches: Optional[int] = None
    weight_lbs: Optional[float] = None
    gender: Optional[str] = None
    activity_level: Optional[str] = None
    goal: Optional[str] = None
    university: Optional[str] = None

class MealPlanRequest(BaseModel):
    dining_hall: str
    date: str

class TokenData(BaseModel):
    user_id: str

# Dependency for authentication
from fastapi import Header

async def get_current_user(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization token required")
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    
    token = authorization.replace("Bearer ", "")
    user_data = verify_session_token(token)
    
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    return user_data

# @app.get("/", summary="API Information")
# async def api_info():
#     """Base route - API information"""
#     return {
#         "name": "DormDish Backend API",
#         "version": "1.0.0",
#         "description": "AI-powered meal planning API for university dining halls",
#         "status": "active",
#         "endpoints": {
#             "health": {
#                 "path": "/health",
#                 "method": "GET",
#                 "description": "Health check endpoint"
#             },
#             "auth": {
#                 "register": {
#                     "path": "/auth/register",
#                     "method": "POST",
#                     "description": "Register a new user account"
#                 },
#                 "login": {
#                     "path": "/auth/login",
#                     "method": "POST",
#                     "description": "User login"
#                 },
#                 "logout": {
#                     "path": "/auth/logout",
#                     "method": "POST",
#                     "description": "User logout"
#                 },
#                 "profile": {
#                     "path": "/auth/profile",
#                     "methods": ["GET", "PUT"],
#                     "description": "Get or update user profile"
#                 }
#             },
#             "meal_planning": {
#                 "generate": {
#                     "path": "/generate-meal-plan",
#                     "method": "POST",
#                     "description": "Generate AI-powered meal plan (supports both authenticated users and guests)"
#                 }
#             },
#             "dining": {
#                 "halls": {
#                     "path": "/dining-halls",
#                     "method": "GET",
#                     "description": "Get list of supported dining halls"
#                 },
#                 "menu": {
#                     "path": "/menu/<dining_hall>/<date>",
#                     "method": "GET",
#                     "description": "Get menu for specific dining hall and date"
#                 }
#             },
#             "cache": {
#                 "status": {
#                     "path": "/cache/status",
#                     "method": "GET",
#                     "description": "Get cache status"
#                 },
#                 "clear": {
#                     "path": "/cache/clear/<dining_hall>/<date>",
#                     "method": "DELETE",
#                     "description": "Clear specific cache entry"
#                 }
#             }
#         },
#         "supported_universities": [
#             "Rutgers University",
#             "Johns Hopkins University"
#         ],
#         "features": [
#             "AI-powered meal planning",
#             "User authentication and profiles",
#             "Nutritional targeting based on BMR/TDEE",
#             "Real-time dining hall menus",
#             "Guest and authenticated user support",
#             "Menu caching for performance"
#         ],
#         "timestamp": datetime.now().isoformat()
#     }

# @app.get("/", summary="Root Endpoint")
# async def root():
#     """Root endpoint for the DormDish API"""
#     return {
#         "message": "Welcome to DormDish API",
#         "version": "1.0.0",
#         "docs": "/docs",
#         "health": "/health"
#     }

@app.get("/health", summary="Health Check")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# Authentication Routes
@app.post("/auth/register", summary="Register New User")
async def register(user_data: RegisterRequest):
    """Register a new user"""
    try:
        # Validate activity level
        valid_activity_levels = [
            'sedentary', 'lightly_active', 'moderately_active', 
            'very_active', 'extremely_active', 'super_active'
        ]
        
        if user_data.activity_level not in valid_activity_levels:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid activity level. Must be one of: {', '.join(valid_activity_levels)}"
            )
        
        # Validate goal
        valid_goals = ['cut', 'maintain', 'bulk']
        if user_data.goal not in valid_goals:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid goal. Must be one of: {', '.join(valid_goals)}"
            )
        
        # Validate gender
        if user_data.gender.lower() not in ['male', 'female', 'other']:
            raise HTTPException(status_code=400, detail="Gender must be 'male', 'female', or 'other'")
        
        # Check if user already exists
        existing_user = supabase.table("users")\
            .select("id")\
            .or_(f"email.eq.{user_data.email},username.eq.{user_data.username}")\
            .execute()
        
        if existing_user.data:
            raise HTTPException(status_code=409, detail="User with this email or username already exists")
        
        # Hash password
        password_hash = hash_password(user_data.password)
        
        # Calculate BMR and TDEE
        bmr_data = calculate_bmr_tdee(
            user_data.age, user_data.height_feet, user_data.height_inches,
            user_data.weight_lbs, user_data.gender, user_data.activity_level
        )
        
        # Create user
        db_user_data = {
            'username': user_data.username,
            'email': user_data.email,
            'password_hash': password_hash,
            'age': user_data.age,
            'height_feet': user_data.height_feet,
            'height_inches': user_data.height_inches,
            'weight_lbs': user_data.weight_lbs,
            'gender': user_data.gender.lower(),
            'activity_level': user_data.activity_level,
            'goal': user_data.goal,
            'university': user_data.university,
            'bmr': bmr_data['bmr'],
            'tdee': bmr_data['tdee']
        }
        
        response = supabase.table("users").insert(db_user_data).execute()
        
        if response.data:
            user = response.data[0]
            # Create session token
            session_token = create_user_session(user['id'])
            
            # Remove sensitive data from response
            user.pop('password_hash', None)
            
            return {
                "success": True,
                "message": "User registered successfully",
                "user": user,
                "session_token": session_token
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to create user")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")

@app.post("/auth/login", summary="User Login")
async def login(login_data: LoginRequest):
    """User login"""
    try:
        # Get user by email
        response = supabase.table("users")\
            .select("*")\
            .eq("email", login_data.email)\
            .execute()
        
        if not response.data:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        user = response.data[0]
        
        # Verify password
        if not verify_password(login_data.password, user['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        # Create session token
        session_token = create_user_session(user['id'])
        
        if not session_token:
            raise HTTPException(status_code=500, detail="Failed to create session")
        
        # Remove sensitive data
        user.pop('password_hash', None)
        
        return {
            "success": True,
            "message": "Login successful",
            "user": user,
            "session_token": session_token
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")

@app.post("/auth/logout", summary="User Logout")
async def logout(authorization: str = Header(...)):
    """User logout"""
    try:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Invalid authorization format")
        
        token = authorization.replace("Bearer ", "")
        
        # Delete session
        supabase.table("user_sessions")\
            .delete()\
            .eq("session_token", token)\
            .execute()
        
        return {"success": True, "message": "Logged out successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Logout failed: {str(e)}")

@app.get("/auth/profile", summary="Get User Profile")
async def get_profile(current_user: dict = Depends(get_current_user)):
    """Get user profile"""
    try:
        # Remove sensitive data
        current_user.pop('password_hash', None)
        
        return {"success": True, "user": current_user}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get profile: {str(e)}")

@app.put("/auth/profile", summary="Update User Profile")
async def update_profile(profile_data: UpdateProfileRequest, current_user: dict = Depends(get_current_user)):
    """Update user profile"""
    try:
        # Build update data from provided fields
        update_data = {}
        
        for field, value in profile_data.dict(exclude_unset=True).items():
            if value is not None:
                # Validate goal if being updated
                if field == 'goal' and value not in ['cut', 'maintain', 'bulk']:
                    raise HTTPException(status_code=400, detail="Invalid goal. Must be 'cut', 'maintain', or 'bulk'")
                update_data[field] = value
        
        # Recalculate BMR/TDEE if physical stats changed
        if any(field in update_data for field in ['age', 'height_feet', 'height_inches', 'weight_lbs', 'gender', 'activity_level']):
            # Get current values or use provided ones
            current_age = update_data.get('age', current_user['age'])
            current_height_feet = update_data.get('height_feet', current_user['height_feet'])
            current_height_inches = update_data.get('height_inches', current_user['height_inches'])
            current_weight = update_data.get('weight_lbs', current_user['weight_lbs'])
            current_gender = update_data.get('gender', current_user['gender'])
            current_activity = update_data.get('activity_level', current_user['activity_level'])
            
            bmr_data = calculate_bmr_tdee(
                current_age, current_height_feet, current_height_inches,
                current_weight, current_gender, current_activity
            )
            
            update_data['bmr'] = bmr_data['bmr']
            update_data['tdee'] = bmr_data['tdee']
        
        if update_data:
            response = supabase.table("users")\
                .update(update_data)\
                .eq("id", current_user['id'])\
                .execute()
            
            if response.data:
                updated_user = response.data[0]
                updated_user.pop('password_hash', None)
                
                return {
                    "success": True,
                    "message": "Profile updated successfully",
                    "user": updated_user
                }
            else:
                raise HTTPException(status_code=500, detail="Failed to update profile")
        else:
            raise HTTPException(status_code=400, detail="No valid fields to update")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update profile: {str(e)}")

@app.post("/generate-meal-plan", summary="Generate Meal Plan")
async def generate_meal_plan_route(
    meal_plan_request: MealPlanRequest,
    authorization: str = Header(None)
):
    """Generate a meal plan for a user (authenticated or guest)"""
    try:
        # Check for authentication token
        authenticated_user = None
        
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = verify_session_token(token)
        
        # If authenticated, use user profile; otherwise require all fields in request body
        if authenticated_user:
            # Use authenticated user's profile
            user = User(
                age=authenticated_user['age'],
                height_feet=authenticated_user['height_feet'],
                height_inches=authenticated_user['height_inches'],
                weight_lbs=float(authenticated_user['weight_lbs']),
                activity_level=authenticated_user['activity_level'],
                goal=authenticated_user['goal'],
                preferred_dining_hall=meal_plan_request.dining_hall
            )
            
            # Include BMR/TDEE from user profile
            user_bmr = float(authenticated_user['bmr'])
            user_tdee = float(authenticated_user['tdee'])
            
        else:
            # Guest user - use default values (this would need to be expanded for guest support)
            # For now, return error requiring authentication
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Guest users not yet supported in FastAPI version."
            )
        
        # Fetch menu for user's preferred dining hall
        menu_data = get_dining_hall_menu(meal_plan_request.dining_hall, meal_plan_request.date)
        
        if "error" in menu_data and menu_data["error"] is not None:
            raise HTTPException(status_code=500, detail=f"Failed to fetch menu: {menu_data['error']}")
        
        # Generate meal plan context with BMR/TDEE information
        meal_plan_context = generate_meal_plan_with_bmr(user, menu_data, user_bmr, user_tdee)
        
        # Send to AI model for meal plan generation
        # Track timing for AI generation
        ai_start_time = time.time()
        
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system", 
                    "content": """You are an AI meal planner. Your role is to generate complete daily meal plans using the provided dataset of foods.

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

Always output strictly in JSON format WITHOUT any markdown code blocks or additional text.

Output Format:

{
  "date": "YYYY-MM-DD",
  "user_stats": {
    "bmr": <float>,
    "tdee": <float>,
    "goal": "<goal>"
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
  "explanation": "Brief explanation of how the plan meets nutritional targets based on BMR/TDEE"
}

Additional Rules:

Use menu items in combination to create balanced meals.
Exclude foods where calories or protein are null.
Meals should feel natural, varied, and student-friendly.
JSON must always be valid and parseable.
YOU MUST ONLY USE THE MENU ITEMS FROM THE ATTACHED ITEMS.
IMPORTANT: Return ONLY the JSON object, no markdown formatting, no code blocks, no additional text."""
                },
                {
                    "role": "user", 
                    "content": meal_plan_context["ai_prompt"]
                }
            ],
            temperature=0.7
        )
        
        ai_end_time = time.time()
        
        # Extract AI response content
        ai_response_raw = response.choices[0].message.content.strip()
        
        # Parse the AI response (handle potential markdown wrapping)
        def extract_json_from_response(content: str) -> dict:
            """Extract JSON from AI response, handling markdown code blocks"""
            try:
                # First, try to parse as direct JSON
                return json.loads(content)
            except json.JSONDecodeError:
                # If that fails, look for JSON within markdown code blocks
                import re
                
                # Look for ```json...``` or ```...``` patterns
                json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
                    try:
                        return json.loads(json_content)
                    except json.JSONDecodeError:
                        pass
                
                # Look for content between { and } (fallback)
                brace_match = re.search(r'\{.*\}', content, re.DOTALL)
                if brace_match:
                    json_content = brace_match.group(0)
                    try:
                        return json.loads(json_content)
                    except json.JSONDecodeError:
                        pass
                
                # If all parsing fails, return raw content with error
                return {
                    "error": "Failed to parse AI response as JSON",
                    "raw_response": content
                }
        
        # Parse the meal plan
        meal_plan = extract_json_from_response(ai_response_raw)
        
        # Calculate processing times
        menu_fetch_time = getattr(menu_data, 'fetch_time', 0)
        ai_generation_time = ai_end_time - ai_start_time
        total_time = time.time() - ai_start_time
        
        # Return the complete meal plan
        return {
            "success": True,
            "user_profile": meal_plan_context["user_profile"],
            "nutrition_targets": meal_plan_context["nutritional_targets"],
            "bmr_tdee": {
                "bmr": user_bmr,
                "tdee": user_tdee
            },
            "available_items": menu_data.get("items", []),
            "meal_plan": meal_plan,
            "processing_info": {
                "menu_fetch_time_seconds": round(menu_fetch_time, 2),
                "ai_generation_time_seconds": round(ai_generation_time, 2),
                "total_time_seconds": round(total_time, 2)
            },
            "meta": {
                "dining_hall": meal_plan_request.dining_hall,
                "date": meal_plan_request.date,
                "ai_model": "openai/gpt-oss-20b",
                "authenticated": authenticated_user is not None,
                "raw_ai_response_length": len(ai_response_raw)
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/dining-halls", summary="Get Supported Dining Halls")
async def get_supported_dining_halls():
    """Get list of supported dining halls"""
    try:
        response = requests.get("https://dormdish-mcp.vercel.app/universities", timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        # Fallback to hardcoded list if external service is down
        fallback_dining_halls = [
            {
                "university": "rutgers",
                "name": "Rutgers University",
                "dining_halls": [
                    {"id": "busch", "name": "Busch Dining Hall"},
                    {"id": "livingston", "name": "Livingston Dining Commons"},
                    {"id": "brower", "name": "Brower Commons"},
                    {"id": "neilson", "name": "Neilson Dining Hall"}
                ]
            },
            {
                "university": "hopkins",
                "name": "Johns Hopkins University", 
                "dining_halls": [
                    {"id": "hopkins-cafe", "name": "Hopkins Cafe"},
                    {"id": "nolans-on-33rd", "name": "Nolan's on 33rd"},
                    {"id": "peabody-institute", "name": "Peabody Institute"}
                ]
            }
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
        university = "hopkins" if "hopkins" in dining_hall or "nolans" in dining_hall or "peabody" in dining_hall else "rutgers"
        menu_id = f"{university}_{dining_hall}_{date}"
        
        response = supabase.table("menus").delete().eq("id", menu_id).execute()
        
        return {
            "success": True,
            "message": f"Cache cleared for {menu_id}",
            "deleted_count": len(response.data) if response.data else 0
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear cache: {str(e)}")

@app.get("/cache/status", summary="Get Cache Status")
async def cache_status():
    """Get cache statistics"""
    try:
        response = supabase.table("menus").select("id, university, dining_hall, date, created_at").execute()
        
        cached_menus = response.data if response.data else []
        
        return {
            "total_cached_menus": len(cached_menus),
            "cached_menus": cached_menus
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get cache status: {str(e)}")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)