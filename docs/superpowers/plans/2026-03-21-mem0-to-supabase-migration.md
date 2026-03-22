# mem0 to Supabase Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all mem0 usage with Supabase `user_preferences` table for storing user food preferences (favorites/dislikes), and remove the mem0 dependency entirely.

**Architecture:** A new `user_preferences` table in Supabase stores favorites and dislikes. `PreferenceManager` is rewritten to use the Supabase client directly. `build_preference_context()` reads user profile data from the `authenticated_user` dict (already populated from the `users` table) instead of mem0. All API response shapes are preserved via a `_row_to_memory_format()` adapter.

**Tech Stack:** Python, FastAPI, Supabase (PostgreSQL), supabase-py client

**Spec:** `docs/superpowers/specs/2026-03-21-mem0-to-supabase-migration-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `memory/preference_manager.py` | Rewrite | All preference CRUD against Supabase + `build_preference_context()` |
| `memory/mem0_client.py` | Delete | Was mem0 REST client — no longer needed |
| `memory/__init__.py` | Update | Update docstring |
| `app.py:1863-1876` | Delete | Remove mem0 memory cleanup from account deletion (ON DELETE CASCADE handles it) |
| `app.py:2599-2607` | Delete | Remove `StoreBMRRequest` and `UpdateBMRRequest` models |
| `app.py:4264-4308` | Modify | Meal plan gen block 1 — use new PreferenceManager |
| `app.py:4784-4810` | Modify | Meal plan gen block 2 — use new PreferenceManager |
| `app.py:5196-5239` | Modify | Meal plan gen block 3 — use new PreferenceManager |
| `app.py:5540-5579` | Modify | Meal plan gen block 4 — use new PreferenceManager |
| `app.py:6504-6505` | Modify | Update section header comment |
| `app.py:6507-6553` | Modify | POST /preferences/favorite — use new PreferenceManager |
| `app.py:6555-6588` | Modify | POST /preferences/dislike — use new PreferenceManager |
| `app.py:6591-6614` | Modify | GET /preferences/favorites — use new PreferenceManager |
| `app.py:6617-6646` | Modify | GET /preferences/all — use new PreferenceManager, populate profile from current_user |
| `app.py:6649-6673` | Modify | GET /preferences/debug — use new PreferenceManager |
| `app.py:6676-6731` | Delete | POST/PUT /preferences/bmr — redundant with onboarding |
| `app.py:6734-6797` | Modify | DELETE /preferences/favorite + dislike — use new PreferenceManager |
| `app.py:7303-7340` | Delete | Remove commented-out mem0 TODO block in calendar endpoint |
| `app.py` (after GET /preferences/favorites) | Add | New `GET /preferences/dislikes` endpoint |
| `requirements.txt:33` | Delete | Remove mem0 comment line |
| `.env:40-41` | Delete | Remove `MEM0_USE_PLATFORM` and `MEM0_API_KEY` |

---

### Task 1: Rewrite PreferenceManager with Supabase

**Files:**
- Rewrite: `memory/preference_manager.py`
- Update: `memory/__init__.py`

- [ ] **Step 1: Write the new `preference_manager.py`**

Replace the entire file with the Supabase-backed implementation:

```python
"""
Preference Manager for DormDish — stores user preferences in Supabase.

Handles favorites, dislikes, and preference context building for meal plan personalization.
"""

from typing import Dict, List, Optional
from supabase import Client


TABLE = "user_preferences"


def _row_to_memory_format(row: dict) -> dict:
    """Transform a Supabase row into the dict shape the frontend expects."""
    category = row["category"]
    item_name = row["item_name"]
    meal_type = row["meal_type"]
    item_type = row["item_type"]

    if category == "favorite":
        memory_text = f"FAVORITE: {item_name} is my favorite {meal_type} {item_type}."
    else:
        memory_text = f"DISLIKE: I do not like {item_name}."
        if row.get("reason"):
            memory_text += f" Reason: {row['reason']}"

    return {
        "id": str(row["id"]),
        "memory": memory_text,
        "metadata": {
            "category": category,
            "item_type": item_type,
            "preference_type": item_type,
            "meal_type": meal_type,
            "item_name": item_name,
            "meal_plan_id": row.get("meal_plan_id"),
            "sentiment": "negative" if category == "dislike" else None,
        },
    }


class PreferenceManager:
    """Manages user preferences using Supabase."""

    def __init__(self, supabase: Client):
        self.supabase = supabase

    # ========== FAVORITES ==========

    def add_favorite_meal_plan(
        self,
        user_id: str,
        meal_plan_id: str,
        meal_plan_name: str,
        meal_type: str,
    ) -> Dict:
        try:
            result = (
                self.supabase.table(TABLE)
                .insert(
                    {
                        "user_id": user_id,
                        "category": "favorite",
                        "item_name": meal_plan_name,
                        "item_type": "meal_plan",
                        "meal_type": meal_type,
                        "meal_plan_id": meal_plan_id,
                    }
                )
                .execute()
            )
            return {"status": "success", "data": result.data}
        except Exception as e:
            # Unique constraint violation = already favorited, treat as success
            if "23505" in str(e) or "duplicate" in str(e).lower():
                return {"status": "already_exists"}
            raise

    def add_favorite_food_item(
        self,
        user_id: str,
        food_item_name: str,
        meal_type: str,
    ) -> Dict:
        try:
            result = (
                self.supabase.table(TABLE)
                .insert(
                    {
                        "user_id": user_id,
                        "category": "favorite",
                        "item_name": food_item_name,
                        "item_type": "food_item",
                        "meal_type": meal_type,
                    }
                )
                .execute()
            )
            return {"status": "success", "data": result.data}
        except Exception as e:
            if "23505" in str(e) or "duplicate" in str(e).lower():
                return {"status": "already_exists"}
            raise

    # ========== DISLIKES ==========

    def add_dislike(
        self,
        user_id: str,
        item_name: str,
        item_type: str,
        meal_type: str,
        reason: Optional[str] = None,
    ) -> Dict:
        try:
            result = (
                self.supabase.table(TABLE)
                .insert(
                    {
                        "user_id": user_id,
                        "category": "dislike",
                        "item_name": item_name,
                        "item_type": item_type,
                        "meal_type": meal_type,
                        "reason": reason,
                    }
                )
                .execute()
            )
            return {"status": "success", "data": result.data}
        except Exception as e:
            if "23505" in str(e) or "duplicate" in str(e).lower():
                return {"status": "already_exists"}
            raise

    # ========== RETRIEVAL ==========

    def get_favorites(
        self, user_id: str, meal_type: Optional[str] = None
    ) -> List[Dict]:
        query = (
            self.supabase.table(TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("category", "favorite")
        )
        if meal_type:
            query = query.eq("meal_type", meal_type)
        result = query.order("created_at", desc=True).execute()
        return [_row_to_memory_format(row) for row in (result.data or [])]

    def get_dislikes(
        self, user_id: str, meal_type: Optional[str] = None
    ) -> List[Dict]:
        query = (
            self.supabase.table(TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("category", "dislike")
        )
        if meal_type:
            query = query.eq("meal_type", meal_type)
        result = query.order("created_at", desc=True).execute()
        return [_row_to_memory_format(row) for row in (result.data or [])]

    def get_user_preferences(
        self, user_id: str, categories: Optional[List[str]] = None
    ) -> List[Dict]:
        query = (
            self.supabase.table(TABLE)
            .select("*")
            .eq("user_id", user_id)
        )
        if categories:
            query = query.in_("category", categories)
        result = query.order("created_at", desc=True).execute()
        return [_row_to_memory_format(row) for row in (result.data or [])]

    def get_all_preferences(self, user_id: str) -> List[Dict]:
        result = (
            self.supabase.table(TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [_row_to_memory_format(row) for row in (result.data or [])]

    # ========== DELETION ==========

    def remove_favorite(self, user_id: str, item_name: str) -> Dict:
        result = (
            self.supabase.table(TABLE)
            .delete()
            .eq("user_id", user_id)
            .eq("category", "favorite")
            .ilike("item_name", item_name)
            .execute()
        )
        if result.data:
            return {"status": "deleted"}
        return {"status": "not_found"}

    def remove_dislike(self, user_id: str, item_name: str) -> Dict:
        result = (
            self.supabase.table(TABLE)
            .delete()
            .eq("user_id", user_id)
            .eq("category", "dislike")
            .ilike("item_name", item_name)
            .execute()
        )
        if result.data:
            return {"status": "deleted"}
        return {"status": "not_found"}


def build_preference_context(
    authenticated_user: dict,
    favorites: List[Dict],
    dislikes: List[Dict],
) -> str:
    """Format preferences into LLM context string for meal plan generation."""
    context_parts = []

    context_parts.append("=== USER PREFERENCES (Learn from Past Choices) ===")
    context_parts.append(
        "IMPORTANT: Use these as inspiration for SIMILAR items available in today's menu."
    )
    context_parts.append(
        "Do NOT copy these exact items unless they are available in the current menu."
    )
    context_parts.append(
        "Instead, find similar foods with comparable nutritional profiles and flavors."
    )
    context_parts.append("")

    # User Profile — from authenticated_user (users table)
    bmr = authenticated_user.get("bmr")
    dietary_preference = authenticated_user.get("dietary_preference")
    dietary_restrictions = authenticated_user.get("dietary_restrictions")
    goal = authenticated_user.get("goal")

    if any([bmr is not None, dietary_preference, dietary_restrictions, goal]):
        context_parts.append("USER PROFILE:")
        if bmr is not None:
            context_parts.append(f"• My BMR is {bmr} calories per day.")
        if dietary_preference:
            context_parts.append(
                f"• My dietary preference is: {dietary_preference}"
            )
        if dietary_restrictions:
            restrictions_text = ", ".join(dietary_restrictions)
            context_parts.append(
                f"• My dietary restrictions are: {restrictions_text}"
            )
        if goal:
            context_parts.append(f"• My fitness goal is: {goal}")
        context_parts.append("")

    # Favorites
    if favorites:
        context_parts.append(
            "FAVORITES (Find similar items with these characteristics):"
        )
        for item in favorites:
            memory_text = item.get("memory", "")
            metadata = item.get("metadata", {})
            meal_type = metadata.get("meal_type", "")
            item_name = metadata.get("item_name", "")
            if memory_text:
                meal_label = f"[{meal_type.upper()}] " if meal_type else ""
                context_parts.append(f"• {meal_label}{memory_text}")
                if item_name:
                    context_parts.append(
                        f"  → Look for items similar to: {item_name}"
                    )
        context_parts.append("")

    # Dislikes (CRITICAL — must avoid)
    if dislikes:
        context_parts.append(
            "DISLIKES (NEVER include these or similar items):"
        )
        for dislike in dislikes:
            memory_text = dislike.get("memory", "")
            metadata = dislike.get("metadata", {})
            item_name = metadata.get("item_name", "")
            if memory_text:
                context_parts.append(f"• ❌ {memory_text}")
                if item_name:
                    context_parts.append(
                        f"  → Avoid: {item_name} and similar items"
                    )
        context_parts.append("")

    if len(context_parts) <= 5:  # Only has header
        return ""

    return "\n".join(context_parts)
```

- [ ] **Step 2: Update `memory/__init__.py`**

Replace contents with:

```python
"""Preference storage layer for DormDish using Supabase"""
```

- [ ] **Step 3: Delete `memory/mem0_client.py`**

Run:
```bash
rm memory/mem0_client.py
rm -rf memory/__pycache__
```

- [ ] **Step 4: Commit**

```bash
git add memory/preference_manager.py memory/__init__.py
git rm memory/mem0_client.py
git commit -m "feat: rewrite PreferenceManager to use Supabase instead of mem0"
```

---

### Task 2: Update preference CRUD endpoints in app.py

**Files:**
- Modify: `app.py:6504-6797` (preference endpoints section)

All preference endpoints currently do `from memory.preference_manager import PreferenceManager` then `PreferenceManager()`. They need to change to `PreferenceManager(supabase)`. The methods also change from async to sync (no more `await`).

- [ ] **Step 1: Update section header comment (app.py:6504-6505)**

Change:
```python
# ==================== PREFERENCE MANAGEMENT ENDPOINTS ====================
# These endpoints use mem0 to store and retrieve user preferences, favorites, likes, and dislikes
```
to:
```python
# ==================== PREFERENCE MANAGEMENT ENDPOINTS ====================
# These endpoints use Supabase to store and retrieve user preferences, favorites, and dislikes
```

- [ ] **Step 2: Update POST /preferences/favorite (app.py:6507-6553)**

Change `PreferenceManager()` to `PreferenceManager(supabase)`. Remove all `await` from preference_manager calls. Remove the `[API] mem0 add() returned` debug prints (lines 6535-6539). The endpoint handler stays `async def` (FastAPI requires it for the `Depends`).

- [ ] **Step 3: Update POST /preferences/dislike (app.py:6555-6588)**

Same pattern: `PreferenceManager(supabase)`, remove `await`, remove `[API] mem0 add_dislike()` debug print (line 6576).

- [ ] **Step 4: Update GET /preferences/favorites (app.py:6591-6614)**

Change `PreferenceManager()` to `PreferenceManager(supabase)`, remove `await`.

- [ ] **Step 5: Add GET /preferences/dislikes endpoint**

Add this new endpoint right after the GET /preferences/favorites endpoint (after line ~6614):

```python
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
```

- [ ] **Step 6: Update GET /preferences/all (app.py:6617-6646)**

Replace the entire endpoint body. Use `PreferenceManager(supabase)`, remove `await`, populate `profile` from `current_user`:

```python
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
```

- [ ] **Step 7: Update GET /preferences/debug (app.py:6649-6673)**

Replace the endpoint body. Use `PreferenceManager(supabase)`, replace `get_all_memories()` with `get_all_preferences()`, remove `await`:

```python
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
```

- [ ] **Step 8: Remove POST /preferences/bmr and PUT /preferences/bmr (app.py:6676-6731)**

Delete both endpoints entirely (lines 6676-6731). Also delete the `StoreBMRRequest` and `UpdateBMRRequest` models (app.py:2599-2607).

- [ ] **Step 9: Update DELETE /preferences/favorite/{item_name} (app.py:6734-6764)**

Change `PreferenceManager()` to `PreferenceManager(supabase)`, remove `await`.

- [ ] **Step 10: Update DELETE /preferences/dislike/{item_name} (app.py:6767-6797)**

Change `PreferenceManager()` to `PreferenceManager(supabase)`, remove `await`.

- [ ] **Step 11: Commit**

```bash
git add app.py
git commit -m "feat: update preference endpoints to use Supabase PreferenceManager"
```

---

### Task 3: Update meal plan generation blocks and remove mem0 from account deletion

**Files:**
- Modify: `app.py:1863-1876` (account deletion mem0 cleanup)
- Modify: `app.py:4264-4308` (meal plan gen block 1)
- Modify: `app.py:4784-4810` (meal plan gen block 2)
- Modify: `app.py:5196-5239` (meal plan gen block 3)
- Modify: `app.py:5540-5579` (meal plan gen block 4)
- Delete: `app.py:7303-7340` (commented-out mem0 TODO)

- [ ] **Step 1: Remove mem0 cleanup from account deletion (app.py:1863-1876)**

Delete this entire block:

```python
        # Delete mem0 memories (preferences, favorites, dislikes)
        try:
            from memory.mem0_client import get_memory_client
            memory = get_memory_client()
            # Get all memories for this user
            all_memories = memory.get_all(str(user_id))
            memories_list = all_memories.get("results", []) if isinstance(all_memories, dict) else all_memories
            # Delete each memory
            for mem in memories_list:
                if isinstance(mem, dict) and mem.get("id"):
                    memory.delete(mem["id"])
            print(f"  ✓ Deleted {len(memories_list)} mem0 memories")
        except Exception as e:
            print(f"  ⚠️ Failed to delete mem0 memories: {e}")
```

This is no longer needed because the `user_preferences` table has `ON DELETE CASCADE` — when the user row is deleted (line 1880), all their preferences are automatically deleted.

- [ ] **Step 2: Update meal plan gen block 1 (app.py ~line 4264-4308)**

Replace the preference retrieval block. Change from:

```python
from memory.preference_manager import PreferenceManager, build_preference_context
preference_manager = PreferenceManager()

print(f"[mem0] Meal Plan Generation - User ID from auth: {user_id}")
print(f"[mem0] User email: {authenticated_user.get('email', 'N/A')}")

# Retrieve all preference types in parallel
favorites, dislikes, profile = await asyncio.gather(
    preference_manager.get_favorites(user_id),
    preference_manager.get_dislikes(user_id),
    preference_manager.get_user_profile(user_id),
)
prefs = [profile] if profile else []

# DEBUG: Log what was retrieved
print(f"[mem0] Retrieved {len(favorites)} favorites, {len(dislikes)} dislikes for user {user_id}")
if favorites:
    print(f"[mem0] Favorites: {[f.get('metadata', {}).get('item_name') for f in favorites]}")
if dislikes:
    print(f"[mem0] Dislikes: {[d.get('metadata', {}).get('item_name') for d in dislikes]}")

# Build context string for LLM
user_preferences_context = build_preference_context(prefs, favorites, dislikes)

if user_preferences_context:
    print(f"[mem0] Built preference context ({len(user_preferences_context)} chars)")

preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
```

to:

```python
from memory.preference_manager import PreferenceManager, build_preference_context
preference_manager = PreferenceManager(supabase)

favorites = preference_manager.get_favorites(user_id)
dislikes = preference_manager.get_dislikes(user_id)

user_preferences_context = build_preference_context(authenticated_user, favorites, dislikes)

preferences_snapshot = _build_preferences_snapshot(favorites, dislikes)
```

- [ ] **Step 3: Update block 2 (app.py ~line 4784-4810)**

Apply the same transformation as Step 2.

- [ ] **Step 4: Update block 3 (app.py ~line 5196-5239)**

Apply the same transformation as Step 2.

- [ ] **Step 5: Update block 4 (app.py ~line 5540-5579)**

Apply the same transformation as Step 2.

- [ ] **Step 6: Update error handling in all 4 blocks**

In all 4 blocks, update the except block. Change:
```python
        except Exception as mem_error:
            # Don't fail meal plan generation if mem0 is unavailable
            print(f"[mem0] Warning: Failed to retrieve preferences: {mem_error}")
```
to:
```python
        except Exception as pref_error:
            print(f"Warning: Failed to retrieve preferences: {pref_error}")
```

Also change the "Skipping preferences" print in the `else` branch of each block from:
```python
print(f"[mem0] Skipping preferences for non-premium user {user_id}")
```
to:
```python
print(f"Skipping preferences for non-premium user {user_id}")
```

- [ ] **Step 7: Remove commented-out mem0 TODO block (app.py:7303-7340)**

Delete the entire commented-out block:

```python
        # TODO: MEM0 INTEGRATION - Save meal slots to memory
        # ====================================================
        # Uncomment and implement the following once mem0 integration is ready:
        #
        # from memory.mem0_client import get_mem0_client
        # ... (all commented lines through line 7340)
        # ====================================================
```

- [ ] **Step 8: Commit**

```bash
git add app.py
git commit -m "feat: update meal plan generation to use Supabase preferences and remove mem0 from account deletion"
```

---

### Task 4: Clean up requirements and env references

**Files:**
- Modify: `requirements.txt:33`
- Modify: `.env:40-41`

- [ ] **Step 1: Remove mem0 comment from requirements.txt**

Delete line 33: `# Memory Layer (using Mem0 Platform REST API directly via httpx - no SDK needed)`

Do NOT remove `httpx` — it's used by `app.py`, `subscription.py`, and `refresh_menus.py`.

- [ ] **Step 2: Remove mem0 env vars from .env**

Remove these two lines:
- Line 40: `MEM0_USE_PLATFORM=true`
- Line 41: `MEM0_API_KEY=...`

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: remove mem0 references from requirements and env"
```

---

### Task 5: Verify the migration works end-to-end

**Files:** None (verification only)

- [ ] **Step 1: Check the app module imports without errors**

```bash
python -c "from memory.preference_manager import PreferenceManager, build_preference_context; print('Import OK')"
```

Expected: `Import OK` (no mem0 import errors)

- [ ] **Step 2: Verify no remaining mem0 references in source code**

```bash
grep -rn "mem0\|Mem0\|MEM0" --include="*.py" . | grep -v __pycache__ | grep -v .venv | grep -v docs/
```

Expected: No output (zero references to mem0 in Python source files outside docs)

- [ ] **Step 3: Verify no remaining removed method references**

```bash
grep -rn "get_user_profile\|store_user_bmr\|update_user_bmr\|get_all_memories\|get_memory_client\|mem0_client" --include="*.py" . | grep -v __pycache__ | grep -v .venv | grep -v docs/
```

Expected: No output

- [ ] **Step 4: Verify all preference imports resolve**

```bash
grep -n "from memory" app.py
```

Expected: All lines should show `from memory.preference_manager import PreferenceManager` or `from memory.preference_manager import PreferenceManager, build_preference_context`

- [ ] **Step 5: Verify no remaining `[mem0]` print prefixes**

```bash
grep -n "\[mem0\]" app.py
```

Expected: No output

- [ ] **Step 6: Commit (if any fixups were needed)**

```bash
git add -A && git commit -m "fix: address migration verification issues"
```

Only run if previous steps found issues that required fixes.
