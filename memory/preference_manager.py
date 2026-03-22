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
            .eq("item_name", item_name)
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
            .eq("item_name", item_name)
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

    # Only return empty if we have no user data at all
    has_profile = any([bmr is not None, dietary_preference, dietary_restrictions, goal])
    if not favorites and not dislikes and not has_profile:
        return ""

    return "\n".join(context_parts)
