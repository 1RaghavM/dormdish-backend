# Per-Meal Generation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `POST /generate-meal` and `POST /generate-meal-stream` endpoints that generate a single meal (breakfast, lunch, or dinner) from a user-chosen dining hall, with cross-meal nutrition tracking.

**Architecture:** New endpoints alongside existing ones (no changes to `/generate-meal-plan` or `/generate-meal-plan-stream`). Leverages existing partial plan infrastructure (`exclude_meals`, `build_partial_meals_schema`). Cross-meal tracking via Supabase queries on `saved_meal_plans` with a `meal` field tag.

**Tech Stack:** FastAPI, Pydantic, Supabase, Gemini LLM (via existing `meal_plan_agent.py`)

**Design doc:** `docs/plans/2026-02-22-per-meal-generation-design.md`

---

### Task 1: Add SingleMealRequest model and constants

**Files:**
- Modify: `app.py:2347-2351` (after existing MealPlanRequest)
- Modify: `app.py:2790-2795` (after ATRIUM constants, add MEAL_PROPORTIONS)

**Step 1: Add the SingleMealRequest model**

In `app.py`, after `MealPlanRequest` (line 2351), add:

```python
VALID_MEALS = {"breakfast", "lunch", "dinner"}

class SingleMealRequest(BaseModel):
    meal: str              # "breakfast", "lunch", or "dinner"
    dining_hall: str
    date: str
    restaurant: Optional[str] = None  # For Atrium sub-restaurant
```

**Step 2: Add meal proportion constants**

In `app.py`, after the `ATRIUM_RESTAURANTS` list (around line 2790), add:

```python
MEAL_PROPORTIONS = {"breakfast": 0.30, "lunch": 0.35, "dinner": 0.35}
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add SingleMealRequest model and meal proportion constants"
```

---

### Task 2: Add helper to query existing individual meals

**Files:**
- Modify: `app.py` (add helper function before the generate-meal-plan endpoint, around line 3700)

**Step 1: Add the helper function**

Add this function before the `/generate-meal-plan` route (around line 3700):

```python
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
```

**Step 2: Add target calculation helper**

Add right after the previous function:

```python
def _calculate_meal_targets(
    daily_targets: Dict[str, int],
    already_planned: Dict[str, dict],
    requested_meal: str,
) -> Dict[str, int]:
    """Calculate nutrition targets for a single meal based on daily targets,
    already-planned meals, and proportional allocation."""
    # Subtract already-planned from daily
    remaining = {
        "calories": daily_targets["calories"],
        "protein_g": daily_targets["protein_g"],
        "fat_g": daily_targets["fat_g"],
        "carbs_g": daily_targets["carbs_g"],
    }
    for meal_totals in already_planned.values():
        for key in remaining:
            remaining[key] = max(0, remaining[key] - int(meal_totals.get(key, 0)))

    # Calculate proportional share for the requested meal
    unplanned_meals = [m for m in VALID_MEALS if m not in already_planned and m != requested_meal]
    total_proportion = MEAL_PROPORTIONS[requested_meal] + sum(MEAL_PROPORTIONS[m] for m in unplanned_meals)
    share = MEAL_PROPORTIONS[requested_meal] / total_proportion if total_proportion > 0 else 1.0

    return {
        "calories": max(100, int(remaining["calories"] * share)),
        "protein_g": max(10, int(remaining["protein_g"] * share)),
        "fat_g": max(5, int(remaining["fat_g"] * share)),
        "carbs_g": max(10, int(remaining["carbs_g"] * share)),
    }
```

**Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add helpers for cross-meal tracking and target calculation"
```

---

### Task 3: Add POST /generate-meal endpoint

**Files:**
- Modify: `app.py` (add new endpoint after `/generate-meal-plan` route, around line 4191)

**Step 1: Add the endpoint**

Insert after the `/generate-meal-plan` route's except block (line 4191), before `/generate-meal-plan-stream`:

```python
@app.post("/generate-meal", summary="Generate Single Meal")
async def generate_single_meal_route(
    request: SingleMealRequest,
    authorization: Optional[str] = Header(None),
):
    """Generate a meal plan for a single meal (breakfast, lunch, or dinner).
    Allows choosing a different dining hall per meal. Tracks nutrition across meals."""
    from meal_plan_agent import generate_meal_plan_with_agent
    t_start = time.time()
    try:
        # Validate meal name
        meal_name = request.meal.lower().strip()
        if meal_name not in VALID_MEALS:
            raise HTTPException(status_code=400, detail=f"Invalid meal: '{request.meal}'. Must be breakfast, lunch, or dinner.")

        # Authenticate
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = await asyncio.to_thread(verify_session_token, token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required.")

        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        user_id = str(authenticated_user["id"])

        # Rate limit: check if this specific meal already generated today
        existing_meals = await _get_existing_individual_meals(user_id, today)
        if meal_name in existing_meals:
            raise HTTPException(
                status_code=429,
                detail=f"You already have a {meal_name} plan for today."
            )

        # Calculate user metrics (same as generate-meal-plan)
        user = User(
            age=authenticated_user['age'],
            height_feet=authenticated_user['height_feet'],
            height_inches=authenticated_user['height_inches'],
            weight_lbs=float(authenticated_user['weight_lbs']),
            activity_level=authenticated_user['activity_level'],
            goal=authenticated_user['goal'],
            preferred_dining_hall=request.dining_hall,
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

        # Calculate daily macro targets
        goal_mode = user.goal.lower()
        calorie_target = user_daily_calorie_target
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))
        macro_targets = calculate_macro_targets(
            weight_lbs=user.weight_lbs,
            total_calories=calorie_target,
            protein_g=protein_target,
            goal=goal_mode
        )
        fat_target = macro_targets['fat_g']
        carbs_target = macro_targets['carbs_g']

        daily_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "fat_g": fat_target,
            "carbs_g": carbs_target,
        }

        # Subtract food log entries for today
        try:
            food_log_result = supabase.table("food_log") \
                .select("calories, protein_g, carbs_g, fat_g") \
                .eq("user_id", user_id) \
                .eq("date", request.date) \
                .execute()
            if food_log_result.data:
                logged_cal = sum(e.get("calories", 0) or 0 for e in food_log_result.data)
                logged_pro = sum(e.get("protein_g", 0) or 0 for e in food_log_result.data)
                logged_carb = sum(e.get("carbs_g", 0) or 0 for e in food_log_result.data)
                logged_fat = sum(e.get("fat_g", 0) or 0 for e in food_log_result.data)
                daily_targets["calories"] = max(0, daily_targets["calories"] - int(logged_cal))
                daily_targets["protein_g"] = max(0, daily_targets["protein_g"] - int(logged_pro))
                daily_targets["carbs_g"] = max(0, daily_targets["carbs_g"] - int(logged_carb))
                daily_targets["fat_g"] = max(0, daily_targets["fat_g"] - int(logged_fat))
        except Exception as e:
            logger.warning(f"Failed to fetch food log for target adjustment: {e}")

        # Calculate this meal's share of targets
        meal_targets = _calculate_meal_targets(daily_targets, existing_meals, meal_name)

        # Fetch menu
        menu_data = await get_dining_hall_menu(request.dining_hall, request.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        items_list = menu_data.get("items", []) or []
        if request.restaurant and items_list:
            items_list = [
                item for item in items_list
                if (item.get("restaurant") or "").lower() == request.restaurant.lower()
            ]
            if not items_list:
                raise HTTPException(status_code=404, detail=f"No items for restaurant '{request.restaurant}'")
        if not items_list:
            raise HTTPException(status_code=404, detail="Menu has no items for given hall/date")
        menu_fetch_end = time.time()

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

        # Build payloads for LLM - use meal-level targets, exclude other meals
        exclude_meals = [m for m in VALID_MEALS if m != meal_name]

        agent_menu_payload = {
            "requested_dining_hall": request.dining_hall,
            "requested_date": request.date,
            "source_menu": menu_data,
            "allowed_items": allowed_for_prompt,
        }
        if request.restaurant:
            agent_menu_payload["restaurant"] = request.restaurant
        agent_nutrition_targets = {
            "calories": meal_targets["calories"],
            "protein_g": meal_targets["protein_g"],
            "fat_g": meal_targets["fat_g"],
            "carbs_g": meal_targets["carbs_g"],
            "bmr": user_bmr,
            "tdee": user_tdee,
            "goal": goal_mode,
        }

        # Retrieve user preferences
        user_preferences_context = None
        preferences_snapshot: Dict[str, Any] = {
            "favorites": [], "dislikes": [], "favorite_keys": [], "dislike_keys": [],
        }
        try:
            from memory.preference_manager import PreferenceManager, build_preference_context
            preference_manager = PreferenceManager()
            favorites, dislikes, profile = await asyncio.gather(
                preference_manager.get_favorites(user_id),
                preference_manager.get_dislikes(user_id),
                preference_manager.get_user_profile(user_id),
            )
            prefs = [profile] if profile else []
            user_preferences_context = build_preference_context(prefs, favorites, dislikes)
            preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
        except Exception as mem_error:
            logger.warning(f"[mem0] Failed to retrieve preferences: {mem_error}")

        # Call LLM
        ai_start = time.time()
        try:
            agent_result, agent_raw_text = await generate_meal_plan_with_agent(
                user_payload={
                    "age": user.age,
                    "weight_lbs": user.weight_lbs,
                    "activity_level": user.activity_level,
                    "goal": goal_mode,
                    "dietary_preference": user.dietary_preference,
                },
                menu_payload=agent_menu_payload,
                nutrition_targets=agent_nutrition_targets,
                user_preferences_context=user_preferences_context,
                exclude_meals=exclude_meals,
            )
        except ValueError:
            raise HTTPException(status_code=500, detail="Please try again.")
        ai_end = time.time()

        # Post-process (validate items, fuzzy match, sanitize)
        plan_json = agent_result.model_dump()
        unknown_items: List[str] = []
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
                    if value is not None:
                        try:
                            sanitized_item[macro_key] = float(value)
                        except (TypeError, ValueError):
                            pass
                new_items.append(sanitized_item)
            meal["items"] = new_items

        # Validate totals
        totals = plan_json.get('totals')
        if not isinstance(totals, dict):
            raise HTTPException(status_code=500, detail="Please try again.")
        for key in ("calories", "protein_g", "fat_g", "carbs_g"):
            if key in totals and totals[key] is not None:
                try:
                    totals[key] = float(totals[key])
                except (TypeError, ValueError):
                    totals[key] = 0

        if not plan_json.get('explanation'):
            plan_json['explanation'] = f"Balanced {meal_name} targeting protein and calories."

        t_end = time.time()
        generation_id = str(uuid4())

        response_payload = {
            "success": True,
            "meal": meal_name,
            "meal_plan": plan_json,
            "nutrition_targets": meal_targets,
            "daily_targets": {
                "calories": calorie_target,
                "protein_g": protein_target,
                "fat_g": fat_target,
                "carbs_g": carbs_target,
            },
            "already_planned": existing_meals,
            "available_items": allowed_for_prompt,
            "preferences_snapshot": preferences_snapshot,
            "generation_id": generation_id,
            "processing_info": {
                "menu_fetch_time_seconds": round(menu_fetch_end - t_start, 3),
                "ai_generation_time_seconds": round(ai_end - ai_start, 3),
                "total_time_seconds": round(t_end - t_start, 3),
            },
            "meta": {"dropped_unknown": unknown_items} if unknown_items else {},
        }

        # Auto-save with meal tag
        try:
            plan_id = str(uuid4())
            save_data = {
                "id": plan_id,
                "user_id": authenticated_user["id"],
                "dining_hall": request.dining_hall,
                "date": today,
                "meal_plan_data": {
                    "meal": meal_name,
                    "dining_hall": request.dining_hall,
                    "restaurant": request.restaurant,
                    **response_payload,
                },
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            save_response = supabase.table("saved_meal_plans").insert(save_data).execute()
            if save_response.data:
                response_payload["saved_plan_id"] = plan_id
                response_payload["auto_saved"] = True
            else:
                response_payload["auto_saved"] = False
        except Exception as save_error:
            logger.error(f"[SingleMeal] Auto-save failed: {save_error}", exc_info=True)
            response_payload["auto_saved"] = False

        return response_payload

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Single meal generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Please try again.")
```

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: add POST /generate-meal endpoint for per-meal generation"
```

---

### Task 4: Add POST /generate-meal-stream endpoint

**Files:**
- Modify: `app.py` (add after the `/generate-meal` endpoint added in Task 3)

**Step 1: Add the streaming endpoint**

Insert immediately after the `/generate-meal` endpoint:

```python
@app.post("/generate-meal-stream", summary="Generate Single Meal with Streaming")
async def generate_single_meal_stream_route(
    request: SingleMealRequest,
    authorization: Optional[str] = Header(None),
):
    """Generate a single meal plan with streaming updates."""
    try:
        from meal_plan_agent import generate_meal_plan_with_streaming

        # Validate meal name
        meal_name = request.meal.lower().strip()
        if meal_name not in VALID_MEALS:
            raise HTTPException(status_code=400, detail=f"Invalid meal: '{request.meal}'. Must be breakfast, lunch, or dinner.")

        # Authenticate
        authenticated_user = None
        if authorization and authorization.startswith('Bearer '):
            token = authorization.replace('Bearer ', '')
            authenticated_user = await asyncio.to_thread(verify_session_token, token)
        if not authenticated_user:
            raise HTTPException(status_code=401, detail="Authentication required.")

        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        user_id = str(authenticated_user["id"])

        # Rate limit
        existing_meals = await _get_existing_individual_meals(user_id, today)
        if meal_name in existing_meals:
            raise HTTPException(status_code=429, detail=f"You already have a {meal_name} plan for today.")

        # Calculate user metrics
        user = User(
            age=authenticated_user['age'],
            height_feet=authenticated_user['height_feet'],
            height_inches=authenticated_user['height_inches'],
            weight_lbs=float(authenticated_user['weight_lbs']),
            activity_level=authenticated_user['activity_level'],
            goal=authenticated_user['goal'],
            preferred_dining_hall=request.dining_hall,
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

        goal_mode = user.goal.lower()
        calorie_target = user_daily_calorie_target
        protein_target = int(round(min(max(user.weight_lbs * 0.95, 80), user.weight_lbs * 1.1)))
        macro_targets = calculate_macro_targets(
            weight_lbs=user.weight_lbs,
            total_calories=calorie_target,
            protein_g=protein_target,
            goal=goal_mode
        )
        fat_target = macro_targets['fat_g']
        carbs_target = macro_targets['carbs_g']

        daily_targets = {
            "calories": calorie_target,
            "protein_g": protein_target,
            "fat_g": fat_target,
            "carbs_g": carbs_target,
        }

        # Subtract food log
        try:
            food_log_result = supabase.table("food_log") \
                .select("calories, protein_g, carbs_g, fat_g") \
                .eq("user_id", user_id) \
                .eq("date", request.date) \
                .execute()
            if food_log_result.data:
                logged_cal = sum(e.get("calories", 0) or 0 for e in food_log_result.data)
                logged_pro = sum(e.get("protein_g", 0) or 0 for e in food_log_result.data)
                logged_carb = sum(e.get("carbs_g", 0) or 0 for e in food_log_result.data)
                logged_fat = sum(e.get("fat_g", 0) or 0 for e in food_log_result.data)
                daily_targets["calories"] = max(0, daily_targets["calories"] - int(logged_cal))
                daily_targets["protein_g"] = max(0, daily_targets["protein_g"] - int(logged_pro))
                daily_targets["carbs_g"] = max(0, daily_targets["carbs_g"] - int(logged_carb))
                daily_targets["fat_g"] = max(0, daily_targets["fat_g"] - int(logged_fat))
        except Exception as e:
            logger.warning(f"Failed to fetch food log: {e}")

        # Calculate meal targets
        meal_targets = _calculate_meal_targets(daily_targets, existing_meals, meal_name)

        # Fetch and process menu
        menu_data = await get_dining_hall_menu(request.dining_hall, request.date)
        if menu_data.get("error"):
            raise HTTPException(status_code=400, detail=menu_data.get("error"))
        items_list = menu_data.get("items", []) or []
        if request.restaurant and items_list:
            items_list = [
                item for item in items_list
                if (item.get("restaurant") or "").lower() == request.restaurant.lower()
            ]
            if not items_list:
                raise HTTPException(status_code=404, detail=f"No items for restaurant '{request.restaurant}'")
        if not items_list:
            raise HTTPException(status_code=404, detail="Menu has no items for given hall/date")

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

        if not nutrition_lookup:
            raise HTTPException(status_code=500, detail="No usable menu items after processing")

        exclude_meals = [m for m in VALID_MEALS if m != meal_name]

        user_payload = {
            "age": user.age,
            "weight_lbs": user.weight_lbs,
            "activity_level": user.activity_level,
            "goal": goal_mode,
            "dietary_preference": user.dietary_preference,
        }
        menu_payload = {
            "requested_dining_hall": request.dining_hall,
            "requested_date": request.date,
            "allowed_items": allowed_for_prompt,
            "source_menu": menu_data,
        }
        if request.restaurant:
            menu_payload["restaurant"] = request.restaurant
        nutrition_targets = {
            "calories": meal_targets["calories"],
            "protein_g": meal_targets["protein_g"],
            "fat_g": meal_targets["fat_g"],
            "carbs_g": meal_targets["carbs_g"],
        }

        # Preferences
        user_preferences_context = None
        preferences_snapshot: Dict[str, Any] = {
            "favorites": [], "dislikes": [], "favorite_keys": [], "dislike_keys": [],
        }
        try:
            from memory.preference_manager import PreferenceManager, build_preference_context
            preference_manager = PreferenceManager()
            favorites, dislikes, profile = await asyncio.gather(
                preference_manager.get_favorites(user_id),
                preference_manager.get_dislikes(user_id),
                preference_manager.get_user_profile(user_id),
            )
            prefs = [profile] if profile else []
            user_preferences_context = build_preference_context(prefs, favorites, dislikes)
            preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
        except Exception:
            pass

        # Streaming generator
        async def event_stream():
            thinking_messages = [
                f"Analyzing menu items for {meal_name}...",
                f"Calculating your {meal_name} nutrition targets...",
                f"Selecting optimal {meal_name} options...",
                f"Finalizing your {meal_name} plan...",
            ]
            for i, msg in enumerate(thinking_messages[:2]):
                yield f"data: {json.dumps({'type': 'thinking', 'content': msg})}\n\n"
                await asyncio.sleep(0.3)

            try:
                async for chunk in generate_meal_plan_with_streaming(
                    user_payload=user_payload,
                    menu_payload=menu_payload,
                    nutrition_targets=nutrition_targets,
                    user_preferences_context=user_preferences_context,
                    exclude_meals=exclude_meals,
                ):
                    if chunk.get("type") == "thinking":
                        yield f"data: {json.dumps(chunk)}\n\n"
                    elif chunk.get("type") == "complete":
                        plan_json = chunk.get("meal_plan", {})

                        # Post-process the single meal
                        meal_data = plan_json.get("meals", {}).get(meal_name)
                        if isinstance(meal_data, dict):
                            new_items = []
                            for item in meal_data.get("items", []) or []:
                                if not isinstance(item, dict):
                                    continue
                                nm = (item.get("name") or "").strip()
                                if not nm:
                                    continue
                                key_match = _fuzzy_lookup(nm, nutrition_lookup)
                                if not key_match:
                                    continue
                                sanitized_item = {**item}
                                sanitized_item["name"] = nutrition_lookup[key_match]["name"]
                                servings = sanitized_item.get("servings") or 1
                                try:
                                    sanitized_item["servings"] = round(float(servings), 2)
                                except Exception:
                                    sanitized_item["servings"] = 1.0
                                if not sanitized_item.get("serving_size"):
                                    sanitized_item["serving_size"] = nutrition_lookup[key_match].get("serving_size") or "1 serving"
                                for mk in ("calories", "protein_g", "fat_g", "carbs_g"):
                                    v = sanitized_item.get(mk)
                                    if v is not None:
                                        try:
                                            sanitized_item[mk] = float(v)
                                        except (TypeError, ValueError):
                                            pass
                                new_items.append(sanitized_item)
                            meal_data["items"] = new_items

                        generation_id = str(uuid4())
                        response_payload = {
                            "success": True,
                            "meal": meal_name,
                            "meal_plan": plan_json,
                            "nutrition_targets": meal_targets,
                            "daily_targets": {
                                "calories": calorie_target,
                                "protein_g": protein_target,
                                "fat_g": fat_target,
                                "carbs_g": carbs_target,
                            },
                            "already_planned": existing_meals,
                            "preferences_snapshot": preferences_snapshot,
                            "generation_id": generation_id,
                        }

                        # Auto-save
                        try:
                            plan_id = str(uuid4())
                            save_data = {
                                "id": plan_id,
                                "user_id": authenticated_user["id"],
                                "dining_hall": request.dining_hall,
                                "date": today,
                                "meal_plan_data": {
                                    "meal": meal_name,
                                    "dining_hall": request.dining_hall,
                                    "restaurant": request.restaurant,
                                    **response_payload,
                                },
                                "created_at": datetime.now().isoformat(),
                                "updated_at": datetime.now().isoformat(),
                            }
                            save_response = supabase.table("saved_meal_plans").insert(save_data).execute()
                            if save_response.data:
                                response_payload["saved_plan_id"] = plan_id
                                response_payload["auto_saved"] = True
                            else:
                                response_payload["auto_saved"] = False
                        except Exception:
                            response_payload["auto_saved"] = False

                        yield f"data: {json.dumps({'type': 'complete', 'meal_plan': response_payload})}\n\n"
                    elif chunk.get("type") == "error":
                        yield f"data: {json.dumps(chunk)}\n\n"

            except Exception as e:
                logger.error(f"Streaming single meal error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'error': 'Please try again.'})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Single meal stream setup error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Please try again.")
```

**Step 2: Commit**

```bash
git add app.py
git commit -m "feat: add POST /generate-meal-stream endpoint for streaming per-meal generation"
```

---

### Task 5: Verify syntax and test

**Step 1: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app.py').read()); print('app.py: syntax OK')"
```

Expected: `app.py: syntax OK`

**Step 2: Verify endpoints are registered**

```bash
python3 -c "
from app import app
routes = [r.path for r in app.routes if hasattr(r, 'path')]
assert '/generate-meal' in routes, 'Missing /generate-meal'
assert '/generate-meal-stream' in routes, 'Missing /generate-meal-stream'
assert '/generate-meal-plan' in routes, 'Old endpoint still present'
assert '/generate-meal-plan-stream' in routes, 'Old streaming endpoint still present'
print('All 4 endpoints registered')
"
```

Expected: `All 4 endpoints registered`

**Step 3: Verify old endpoints unchanged**

Confirm that `/generate-meal-plan` and `/generate-meal-plan-stream` still use `MealPlanRequest` (not `SingleMealRequest`).

```bash
python3 -c "
import inspect, app as a
sig = inspect.signature(a.generate_meal_plan_route)
param_type = sig.parameters['meal_plan_request'].annotation.__name__
assert param_type == 'MealPlanRequest', f'Expected MealPlanRequest, got {param_type}'
print('Old endpoint unchanged: uses MealPlanRequest')
"
```

Expected: `Old endpoint unchanged: uses MealPlanRequest`

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: per-meal generation - complete implementation"
```
