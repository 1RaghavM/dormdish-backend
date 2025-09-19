from flask import Flask, request, jsonify
from openai import OpenAI
import requests
import json
import os
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Connect to LM Studio
client = OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="lm-studio")

# Initialize Supabase client
# You'll need to set these environment variables or replace with your actual values
SUPABASE_URL = os.getenv("SUPABASE_URL", "your_supabase_url_here")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "your_supabase_service_key_here")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

@app.route('/generate-meal-plan', methods=['POST'])
def generate_meal_plan_route():
    """Generate a meal plan for a user"""
    try:
        # Get request data
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['age', 'height_feet', 'height_inches', 'weight_lbs', 
                          'activity_level', 'goal', 'preferred_dining_hall']
        
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Create user object
        user = User(
            age=data['age'],
            height_feet=data['height_feet'],
            height_inches=data['height_inches'],
            weight_lbs=data['weight_lbs'],
            activity_level=data['activity_level'],
            goal=data['goal'],
            preferred_dining_hall=data['preferred_dining_hall']
        )
        
        # Get date (use provided date or today)
        date = data.get('date', datetime.now().strftime("%Y-%m-%d"))
        
        # Fetch menu for user's preferred dining hall
        menu_data = get_dining_hall_menu(user.preferred_dining_hall, date)
        
        if "error" in menu_data and menu_data["error"] is not None:
            return jsonify({"error": f"Failed to fetch menu: {menu_data['error']}"}), 500
        
        # Generate meal plan context
        meal_plan_context = generate_meal_plan(user, menu_data)
        
        # Send to AI model for meal plan generation
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

Ensure daily totals are within ±3% of the user's calorie and protein goals.

If exact matching is impossible, choose the closest possible combination and note the totals.

Prioritize protein targets first, then balance calories.

Avoid repeating the exact same dish within a single day. Variety is preferred.

Always output strictly in JSON format.

Output Format:

{
  "date": "YYYY-MM-DD",
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
        },
        {
          "name": "OATMEAL",
          "calories": 146,
          "protein_g": 5,
          "serving_size": "6 oz"
        },
        {
          "name": "BLUEBERRIES",
          "calories": 32,
          "protein_g": 0,
          "serving_size": "2 oz"
        }
      ]
    },
    "lunch": {
      "description": "Grilled chicken Caesar salad with whole wheat roll",
      "items": [
        {
          "name": "GRILLED CHICKEN CAESAR SALAD",
          "calories": 380,
          "protein_g": 44,
          "serving_size": "1 each"
        }
      ]
    },
    "dinner": {
      "description": "Turkey and cheese sandwich with garden salad",
      "items": [
        {
          "name": "TURKEY BREAST",
          "calories": 76,
          "protein_g": 14,
          "serving_size": "3 oz"
        },
        {
          "name": "SHREDDED CHEDDAR CHEESE",
          "calories": 114,
          "protein_g": 7,
          "serving_size": "1 oz"
        },
        {
          "name": "FRESH GARDEN SALAD",
          "calories": 85,
          "protein_g": 4,
          "serving_size": "1 each"
        }
      ]
    }
  }
}

Additional Rules:

Use menu items in combination to create balanced meals.

Exclude foods where calories or protein are null.

Meals should feel natural, varied, and student-friendly.

JSON must always be valid and parseable.

YOU MUST ONLY USE THE MENU ITEMS FROM THE ATTACHED ITEMS."""
                },
                {
                    "role": "user", 
                    "content": meal_plan_context["ai_prompt"]
                }
            ],
            temperature=0.7
        )
        
        # Return the complete meal plan
        return jsonify({
            "success": True,
            "data": {
                "user_profile": meal_plan_context["user_profile"],
                "nutritional_targets": meal_plan_context["nutritional_targets"],
                "dining_hall": user.preferred_dining_hall,
                "date": date,
                "menu_available": menu_data,
                "ai_meal_plan": response.choices[0].message.content
            }
        })
        
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

@app.route('/dining-halls', methods=['GET'])
def get_supported_dining_halls():
    """Get list of supported dining halls"""
    try:
        response = requests.get("https://dormdish-mcp.vercel.app/universities")
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": f"Failed to fetch dining halls: {str(e)}"}), 500

@app.route('/menu/<dining_hall>/<date>', methods=['GET'])
def get_menu(dining_hall: str, date: str):
    """Get menu for a specific dining hall and date (with Supabase caching)"""
    try:
        menu_data = get_dining_hall_menu(dining_hall, date)
        
        if "error" in menu_data and menu_data["error"] is not None:
            return jsonify({"error": menu_data["error"]}), 500
            
        return jsonify(menu_data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch menu: {str(e)}"}), 500

@app.route('/cache/clear/<dining_hall>/<date>', methods=['DELETE'])
def clear_menu_cache(dining_hall: str, date: str):
    """Clear cached menu for a specific dining hall and date"""
    try:
        university = "hopkins" if "hopkins" in dining_hall or "nolans" in dining_hall or "peabody" in dining_hall else "rutgers"
        menu_id = f"{university}_{dining_hall}_{date}"
        
        response = supabase.table("menus").delete().eq("id", menu_id).execute()
        
        return jsonify({
            "success": True,
            "message": f"Cache cleared for {menu_id}",
            "deleted_count": len(response.data) if response.data else 0
        })
        
    except Exception as e:
        return jsonify({"error": f"Failed to clear cache: {str(e)}"}), 500

@app.route('/cache/status', methods=['GET'])
def cache_status():
    """Get cache statistics"""
    try:
        response = supabase.table("menus").select("id, university, dining_hall, date, created_at").execute()
        
        cached_menus = response.data if response.data else []
        
        return jsonify({
            "total_cached_menus": len(cached_menus),
            "cached_menus": cached_menus
        })
        
    except Exception as e:
        return jsonify({"error": f"Failed to get cache status: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)