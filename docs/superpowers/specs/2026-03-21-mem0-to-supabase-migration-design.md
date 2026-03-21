# Replace mem0 with Supabase for User Preferences

**Date:** 2026-03-21
**Status:** Draft

## Problem

User food preferences (favorites, dislikes) are stored in mem0, a third-party AI memory API. This adds latency (~200-500ms per call), cost (paid API), and a runtime dependency for what is fundamentally structured, exact-match data. The BMR/profile data stored in mem0 is redundant with what's already in the Supabase `users` table.

## Solution

Replace all mem0 usage with a single `user_preferences` Supabase table for favorites/dislikes. Remove redundant BMR/profile mem0 storage. Fully remove the mem0 dependency.

## Database Schema

```sql
create table user_preferences (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  category text not null check (category in ('favorite', 'dislike')),
  item_name text not null,
  item_type text not null check (item_type in ('meal_plan', 'food_item')),
  meal_type text not null check (meal_type in ('breakfast', 'lunch', 'dinner')),
  meal_plan_id text,
  reason text,
  created_at timestamptz not null default now()
);

-- Prevent duplicate preferences per user+category+item+meal_type
create unique index idx_user_preferences_unique
  on user_preferences(user_id, category, lower(item_name), meal_type);

-- Fast lookups by user + category
create index idx_user_preferences_lookup
  on user_preferences(user_id, category);
```

Key decisions:
- Case-insensitive uniqueness via `lower(item_name)` on the unique index
- `meal_type` included in unique index so the same item can be favorited for different meals (e.g. "Greek Yogurt" for breakfast AND lunch)
- Cascade delete on user removal
- `meal_plan_id` nullable (only used for meal_plan favorites)
- `reason` nullable (only used for dislikes)
- **Category values use singular** (`'favorite'`, `'dislike'`) — the old mem0 metadata used plural (`"favorites"`, `"dislikes"`), but these values are only consumed via `_row_to_memory_format()` which outputs them in the `metadata` dict. No frontend code depends on the raw category string.

### Upsert strategy

Since the unique index uses a functional expression (`lower(item_name)`), Supabase's `.upsert()` cannot target it directly. Instead, inserts will use a try/except pattern: attempt INSERT, catch unique violation (409/23505), and return success (idempotent). This is simpler than adding a generated column.

## Rewritten PreferenceManager

`memory/preference_manager.py` is rewritten to use the Supabase client instead of mem0.

### Constructor

Takes the Supabase `Client` instance (already available globally in `app.py`) instead of initializing a mem0 client.

### Method mapping

| Current (mem0) | New (Supabase) | Notes |
|---|---|---|
| `add_favorite_meal_plan(user_id, meal_plan_id, meal_plan_name, meal_type)` | INSERT row with `category='favorite'`, `item_type='meal_plan'` | Idempotent: catches duplicate constraint violation |
| `add_favorite_food_item(user_id, food_item_name, meal_type)` | INSERT row with `category='favorite'`, `item_type='food_item'` | Idempotent: catches duplicate constraint violation |
| `add_dislike(user_id, item_name, item_type, meal_type, reason?)` | INSERT row with `category='dislike'` | Idempotent: catches duplicate constraint violation |
| `get_favorites(user_id, meal_type?)` | SELECT WHERE `category='favorite'` + optional `meal_type` filter | No more fuzzy text filtering |
| `get_dislikes(user_id, meal_type?)` | SELECT WHERE `category='dislike'` + optional `meal_type` filter | No more fuzzy text filtering |
| `remove_favorite(user_id, item_name)` | DELETE WHERE `category='favorite'` AND `item_name` ilike match | Case-insensitive |
| `remove_dislike(user_id, item_name)` | DELETE WHERE `category='dislike'` AND `item_name` ilike match | Case-insensitive |
| `get_user_profile(user_id)` | **Removed** | Data already in `users` table |
| `store_user_bmr(...)` | **Removed** | Data stored during onboarding |
| `update_user_bmr(...)` | **Removed** | Data stored during onboarding |
| `get_all_memories(user_id)` | SELECT all rows for user | For debug endpoint |
| `get_user_preferences(user_id, categories?)` | SELECT with optional category filter | For general retrieval |

### Response format compatibility

Each Supabase row is transformed to match the current mem0 response shape so the frontend doesn't need changes:

```python
def _row_to_memory_format(row: dict) -> dict:
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
        }
    }
```

Note: The redundant `"type"` key from the old mem0 metadata is dropped. `_normalize_preference_key()` and `_build_preferences_snapshot()` only read `"preference_type"` and `"item_type"`, both of which are present above.

## build_preference_context() changes

The function signature changes to accept an `authenticated_user` dict instead of a `prefs` list:

```python
def build_preference_context(
    authenticated_user: dict,  # was: prefs: List[Dict]
    favorites: List[Dict],
    dislikes: List[Dict]
) -> str:
```

The "USER PROFILE" section is built from `authenticated_user` fields that already exist in the `users` table:

```python
# USER PROFILE section - built from authenticated_user (users table)
bmr = authenticated_user.get("bmr")
dietary_preference = authenticated_user.get("dietary_preference")  # e.g. "vegan"
dietary_restrictions = authenticated_user.get("dietary_restrictions")  # list, e.g. ["gluten-free"]
goal = authenticated_user.get("goal")  # e.g. "cut", "maintain", "bulk"

if any([bmr, dietary_preference, dietary_restrictions, goal]):
    context_parts.append("USER PROFILE:")
    if bmr:
        context_parts.append(f"• My BMR is {bmr} calories per day.")
    if dietary_preference:
        context_parts.append(f"• My dietary preference is: {dietary_preference}")
    if dietary_restrictions:
        restrictions_text = ", ".join(dietary_restrictions)
        context_parts.append(f"• My dietary restrictions are: {restrictions_text}")
    if goal:
        context_parts.append(f"• My fitness goal is: {goal}")
    context_parts.append("")
```

The favorites and dislikes sections remain unchanged — they work off the `memory` and `metadata` dict keys from `_row_to_memory_format()`.

At the 4 call sites in `app.py`, the call changes from:

```python
prefs = [profile] if profile else []
user_preferences_context = build_preference_context(prefs, favorites, dislikes)
```

to:

```python
user_preferences_context = build_preference_context(authenticated_user, favorites, dislikes)
```

## API endpoint changes

### Kept (same routes, same request/response format)
- `POST /preferences/favorite` — add favorite
- `POST /preferences/dislike` — add dislike
- `GET /preferences/favorites` — list favorites
- `GET /preferences/debug` — debug all raw data
- `DELETE /preferences/favorite/{item_name}` — remove favorite
- `DELETE /preferences/dislike/{item_name}` — remove dislike

### Changed response format
- `GET /preferences/all` — the `profile` field will be populated from `authenticated_user` instead of mem0. The response shape stays the same: `{favorites, dislikes, profile, counts}`.

### Added
- `GET /preferences/dislikes` — list dislikes (with optional `meal_type` query param). Currently only available via `/preferences/all`; adding a dedicated endpoint for parity with `/preferences/favorites`.

### Removed
- `POST /preferences/bmr` — redundant with onboarding
- `PUT /preferences/bmr` — redundant with onboarding

### Internal changes (meal plan generation)
The 4 blocks in `app.py` that call `PreferenceManager.get_user_profile()` will pass `authenticated_user` to `build_preference_context()` instead (already in scope at all call sites).

### Error handling
The existing try/except pattern around preference retrieval is preserved. If Supabase preference queries fail, meal plan generation still proceeds without personalization (graceful degradation).

## Files changed

| File | Action |
|---|---|
| `memory/preference_manager.py` | Rewrite: Supabase queries instead of mem0 |
| `memory/mem0_client.py` | Delete |
| `app.py` | Update: pass `supabase` to PreferenceManager, update `build_preference_context` calls, remove BMR endpoints, add `GET /preferences/dislikes`, update meal plan generation profile retrieval |
| `.env` | Remove `MEM0_API_KEY`, `MEM0_ORG_ID`, `MEM0_PROJECT_ID` |

Note: `httpx` stays in `requirements.txt` — it is used by `app.py`, `subscription.py`, and `refresh_menus.py`.

## Data migration

No migration needed. This is a fresh start — no real user preference data exists in mem0 that needs to be preserved.

## What's NOT changing

- API request/response shapes for kept endpoints (favorites list, add/remove)
- Frontend code
- `build_preference_context()` output format (same string structure for Gemini)
- `_build_preferences_snapshot()` logic (reads `metadata.item_name`, `metadata.preference_type`, `metadata.item_type` — all present in new format)
- Meal plan generation flow (still builds preference context string for Gemini)
- Premium gating on preference endpoints

## Testing

- Verify each CRUD endpoint works against Supabase
- Verify meal plan generation still receives preference context
- Verify duplicate favorites/dislikes are handled (idempotent inserts)
- Verify case-insensitive deletion works
- Verify cascade delete works when user is removed
- Verify `GET /preferences/all` still returns `profile` field from user data
