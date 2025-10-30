"""
Preference Manager for DormDish - handles all user preference storage using mem0.

This module provides a comprehensive interface for storing and retrieving:
- Favorites (meal plans and food items)
- Likes and dislikes
- BMR and dietary preferences
- User goals and restrictions
"""

from typing import Dict, List, Optional
from memory.mem0_client import get_memory_client


class PreferenceManager:
    """Manages user preferences, favorites, and dietary information using mem0."""
    
    def __init__(self):
        """Initialize the preference manager with mem0 client."""
        self.memory = get_memory_client()
    
    # ========== FAVORITES ==========
    
    async def add_favorite_meal_plan(
        self,
        user_id: str,
        meal_plan_id: str,
        meal_plan_name: str,
        meal_type: str
    ) -> Dict:
        """
        Store a favorited meal plan.
        
        Args:
            user_id: Unique user identifier
            meal_plan_id: ID of the meal plan
            meal_plan_name: Name/description of the meal plan
            meal_type: Type of meal (breakfast, lunch, dinner)
        
        Returns:
            Dict with memory ID and status
        """
        messages = [
            {
                "role": "user",
                "content": f"I want to save '{meal_plan_name}' as a favorite {meal_type} meal plan"
            }
        ]
        
        result = self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata={
                "meal_type": meal_type,
                "meal_plan_id": meal_plan_id,
                "preference_type": "meal_plan",
                "item_name": meal_plan_name
            },
            categories=["favorites"]  # Platform uses categories parameter
        )
        
        return result
    
    async def add_favorite_food_item(
        self,
        user_id: str,
        food_item_name: str,
        meal_type: str
    ) -> Dict:
        """
        Store a favorited individual food item.
        
        Args:
            user_id: Unique user identifier
            food_item_name: Name of the food item
            meal_type: Type of meal (breakfast, lunch, dinner)
        
        Returns:
            Dict with memory ID and status
        """
        messages = [
            {
                "role": "user",
                "content": f"I love {food_item_name} and want to have it more often"
            }
        ]
        
        result = self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata={
                "food_item": food_item_name,
                "meal_type": meal_type,
                "preference_type": "food_item",
                "item_name": food_item_name
            },
            categories=["favorites"]  # Platform uses categories parameter
        )
        
        return result
    
    # ========== LIKES ==========
    
    async def add_like(
        self,
        user_id: str,
        item_name: str,
        item_type: str,
        meal_type: str,
        context: Optional[str] = None
    ) -> Dict:
        """
        Store a like for a meal plan or food item.
        
        Args:
            user_id: Unique user identifier
            item_name: Name of the item liked
            item_type: Type of item ("meal_plan" or "food_item")
            meal_type: Type of meal (breakfast, lunch, dinner)
            context: Optional additional context about why they liked it
        
        Returns:
            Dict with memory ID and status
        """
        content = f"I liked the {item_type.replace('_', ' ')} '{item_name}'"
        if context:
            content += f". {context}"
        
        messages = [{"role": "user", "content": content}]
        
        result = self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata={
                "item_name": item_name,
                "item_type": item_type,
                "meal_type": meal_type,
                "sentiment": "positive"
            },
            categories=["likes"]  # Platform uses categories parameter
        )
        
        return result
    
    # ========== DISLIKES ==========
    
    async def add_dislike(
        self,
        user_id: str,
        item_name: str,
        item_type: str,
        meal_type: str,
        reason: Optional[str] = None
    ) -> Dict:
        """
        Store a dislike for a meal plan or food item.
        
        Args:
            user_id: Unique user identifier
            item_name: Name of the item disliked
            item_type: Type of item ("meal_plan" or "food_item")
            meal_type: Type of meal (breakfast, lunch, dinner)
            reason: Optional reason for the dislike
        
        Returns:
            Dict with memory ID and status
        """
        content = f"I did not like the {item_type.replace('_', ' ')} '{item_name}'"
        if reason:
            content += f" because {reason}"
        
        messages = [{"role": "user", "content": content}]
        
        result = self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata={
                "item_name": item_name,
                "item_type": item_type,
                "meal_type": meal_type,
                "sentiment": "negative"
            },
            categories=["dislikes"]  # Platform uses categories parameter
        )
        
        return result
    
    # ========== BMR & DIETARY PREFERENCES ==========
    
    async def store_user_bmr(
        self,
        user_id: str,
        bmr: float,
        dietary_restrictions: List[str],
        goals: str
    ) -> Dict:
        """
        Store user's BMR and dietary information.
        
        Args:
            user_id: Unique user identifier
            bmr: Basal Metabolic Rate in calories
            dietary_restrictions: List of dietary restrictions (e.g., ["vegan", "gluten-free"])
            goals: User's fitness/health goals
        
        Returns:
            Dict with memory ID and status
        """
        restrictions_text = ", ".join(dietary_restrictions) if dietary_restrictions else "none"
        
        messages = [
            {
                "role": "user",
                "content": f"My BMR is {bmr} calories. My dietary restrictions are: {restrictions_text}. My goal is: {goals}"
            }
        ]
        
        result = self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata={
                "bmr": bmr,
                "dietary_restrictions": dietary_restrictions,
                "goals": goals
            },
            categories=["user_profile"]  # Platform uses categories parameter
        )
        
        return result
    
    async def update_user_bmr(
        self,
        user_id: str,
        bmr: Optional[float] = None,
        dietary_restrictions: Optional[List[str]] = None,
        goals: Optional[str] = None
    ) -> Dict:
        """
        Update user's BMR and dietary information.
        
        Args:
            user_id: Unique user identifier
            bmr: New BMR value (optional)
            dietary_restrictions: New dietary restrictions (optional)
            goals: New goals (optional)
        
        Returns:
            Dict with update status
        """
        # First, search for existing user profile
        existing = await self.get_user_profile(user_id)
        
        if not existing:
            # If no profile exists, create one
            return await self.store_user_bmr(
                user_id,
                bmr or 2000.0,  # Default BMR
                dietary_restrictions or [],
                goals or "Maintain health"
            )
        
        # Build update message
        updates = []
        metadata = {}  # Don't use metadata.category - use categories parameter instead
        
        if bmr is not None:
            updates.append(f"My new BMR is {bmr} calories")
            metadata["bmr"] = bmr
        
        if dietary_restrictions is not None:
            restrictions_text = ", ".join(dietary_restrictions) if dietary_restrictions else "none"
            updates.append(f"My dietary restrictions are now: {restrictions_text}")
            metadata["dietary_restrictions"] = dietary_restrictions
        
        if goals is not None:
            updates.append(f"My new goal is: {goals}")
            metadata["goals"] = goals
        
        if not updates:
            return {"status": "no_changes"}
        
        messages = [{"role": "user", "content": ". ".join(updates)}]
        
        result = self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata=metadata,
            categories=["user_profile"]  # Platform uses categories parameter
        )
        
        return result
    
    # ========== RETRIEVAL ==========
    
    async def get_user_preferences(
        self,
        user_id: str,
        categories: Optional[List[str]] = None
    ) -> List[Dict]:
        """
        Retrieve all preferences for personalization.
        
        Args:
            user_id: Unique user identifier
            categories: Optional list of categories to filter by
        
        Returns:
            List of memory dictionaries
        """
        query = "What are this user's food preferences, favorites, likes, and dislikes?"
        
        try:
            # Use Platform syntax - categories as dictionary
            filters = {"user_id": user_id}
            
            if categories:
                filters["categories"] = {"in": categories}  # Platform expects {"in": [...]}
            
            results = self.memory.search(
                query=query,
                user_id=user_id,
                filters=filters
            )
            
            # Validate results type
            if not isinstance(results, list):
                print(f"[mem0] Warning: search returned non-list type: {type(results)}")
                return []
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] Error retrieving user preferences: {e}")
            return []
    
    async def get_favorites(
        self,
        user_id: str,
        meal_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Get user's favorited items.
        
        Args:
            user_id: Unique user identifier
            meal_type: Optional meal type filter (breakfast, lunch, dinner)
        
        Returns:
            List of favorite items
        """
        meal_text = f"{meal_type} " if meal_type else ""
        query = f"What are the user's favorite {meal_text}meal plans and food items?"
        
        # Build filters - use 'categories' for Platform, 'metadata.category' for Open Source
        # Mem0 Platform uses 'categories' as a top-level field
        try:
            # Platform syntax: categories must be a dictionary with an array value
            filters = {
                "user_id": user_id,
                "categories": {"in": ["favorites"]}  # Platform expects {"in": [...]}
            }
            
            # Note: Platform doesn't support nested AND with categories yet
            # So we filter by meal_type in code instead
            results = self.memory.search(query=query, user_id=user_id, filters=filters)
            
            # Validate results type
            if not isinstance(results, list):
                print(f"[mem0] Warning: search returned non-list type: {type(results)}")
                return []
            
            # Filter by meal_type if specified
            if meal_type and results:
                results = [r for r in results if isinstance(r, dict) and r.get("metadata", {}).get("meal_type") == meal_type]
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] Error retrieving favorites: {e}")
            return []
    
    async def get_dislikes(
        self,
        user_id: str,
        meal_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Get items user dislikes - critical for filtering meal recommendations.
        
        Args:
            user_id: Unique user identifier
            meal_type: Optional meal type filter
        
        Returns:
            List of disliked items
        """
        query = "What foods or meal plans does this user dislike or want to avoid?"
        
        try:
            # Use Platform syntax: categories must be a dictionary
            filters = {
                "user_id": user_id,
                "categories": {"in": ["dislikes"]}  # Platform expects {"in": [...]}
            }
            
            results = self.memory.search(query=query, user_id=user_id, filters=filters)
            
            # Validate results type
            if not isinstance(results, list):
                print(f"[mem0] Warning: search returned non-list type: {type(results)}")
                return []
            
            # Filter by meal_type if specified
            if meal_type and results:
                results = [r for r in results if isinstance(r, dict) and r.get("metadata", {}).get("meal_type") == meal_type]
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] Error retrieving dislikes: {e}")
            return []
    
    async def get_likes(
        self,
        user_id: str,
        meal_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Get items user likes.
        
        Args:
            user_id: Unique user identifier
            meal_type: Optional meal type filter
        
        Returns:
            List of liked items
        """
        query = "What foods or meal plans does this user like?"
        
        try:
            # Use Platform syntax: categories must be a dictionary
            filters = {
                "user_id": user_id,
                "categories": {"in": ["likes"]}  # Platform expects {"in": [...]}
            }
            
            results = self.memory.search(query=query, user_id=user_id, filters=filters)
            
            # Validate results type
            if not isinstance(results, list):
                print(f"[mem0] Warning: search returned non-list type: {type(results)}")
                return []
            
            # Filter by meal_type if specified
            if meal_type and results:
                results = [r for r in results if isinstance(r, dict) and r.get("metadata", {}).get("meal_type") == meal_type]
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] Error retrieving likes: {e}")
            return []
    
    async def get_user_profile(
        self,
        user_id: str
    ) -> Optional[Dict]:
        """
        Get user's profile information (BMR, restrictions, goals).
        
        Args:
            user_id: Unique user identifier
        
        Returns:
            User profile dictionary or None
        """
        query = "What is the user's BMR, dietary restrictions, and goals?"
        
        try:
            # Use Platform syntax: categories must be a dictionary
            filters = {
                "user_id": user_id,
                "categories": {"in": ["user_profile"]}  # Platform expects {"in": [...]}
            }
            
            results = self.memory.search(query=query, user_id=user_id, filters=filters)
            
            # Validate results type
            if not isinstance(results, list):
                print(f"[mem0] Warning: search returned non-list type: {type(results)}")
                return None
            
            # Return first result if it's a dict, otherwise None
            if results and isinstance(results[0], dict):
                return results[0]
            return None
        except Exception as e:
            print(f"[mem0] Error retrieving user profile: {e}")
            return None
    
    # ========== DELETION ==========
    
    async def remove_favorite(
        self,
        user_id: str,
        item_name: str
    ) -> Dict:
        """
        Remove a favorite item.
        
        Args:
            user_id: Unique user identifier
            item_name: Name of the item to unfavorite
        
        Returns:
            Dict with deletion status
        """
        # Search for the favorite
        favorites = await self.get_favorites(user_id)
        
        for fav in favorites:
            metadata = fav.get("metadata", {})
            if metadata.get("item_name") == item_name:
                memory_id = fav.get("id")
                if memory_id:
                    self.memory.delete(memory_id)
                    return {"status": "deleted", "memory_id": memory_id}
        
        return {"status": "not_found"}
    
    async def get_all_memories(
        self,
        user_id: str
    ) -> List[Dict]:
        """
        Get all memories for a user (useful for debugging).
        
        Args:
            user_id: Unique user identifier
        
        Returns:
            List of all memories
        """
        try:
            results = self.memory.get_all(user_id=user_id)
            return results if results else []
        except Exception as e:
            print(f"[mem0] Error retrieving all memories: {e}")
            return []


def build_preference_context(
    prefs: List[Dict],
    favorites: List[Dict],
    dislikes: List[Dict],
    likes: List[Dict]
) -> str:
    """
    Format mem0 memories into LLM context string.
    
    Args:
        prefs: General user preferences
        favorites: Favorite items
        dislikes: Disliked items (critical for filtering)
        likes: Liked items
    
    Returns:
        Formatted context string for LLM
    """
    context_parts = []
    
    # User Profile
    if prefs:
        context_parts.append("=== USER PROFILE ===")
        for pref in prefs:
            memory_text = pref.get("memory", "")
            if memory_text:
                context_parts.append(f"• {memory_text}")
        context_parts.append("")
    
    # Favorites (high priority)
    if favorites:
        context_parts.append("=== FAVORITES (Prioritize These) ===")
        for fav in favorites:
            memory_text = fav.get("memory", "")
            metadata = fav.get("metadata", {})
            meal_type = metadata.get("meal_type", "")
            if memory_text:
                context_parts.append(f"• [{meal_type.upper()}] {memory_text}")
        context_parts.append("")
    
    # Dislikes (CRITICAL - must avoid)
    if dislikes:
        context_parts.append("=== DISLIKES (MUST AVOID THESE) ===")
        for dislike in dislikes:
            memory_text = dislike.get("memory", "")
            if memory_text:
                context_parts.append(f"• ❌ {memory_text}")
        context_parts.append("")
    
    # Likes
    if likes:
        context_parts.append("=== LIKES (User Enjoys These) ===")
        for like in likes:
            memory_text = like.get("memory", "")
            if memory_text:
                context_parts.append(f"• ✓ {memory_text}")
        context_parts.append("")
    
    if not context_parts:
        return ""
    
    return "\n".join(context_parts)

