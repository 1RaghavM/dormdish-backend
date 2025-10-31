"""
Preference Manager for DormDish - handles all user preference storage using mem0.

This module provides a comprehensive interface for storing and retrieving:
- Favorites (meal plans and food items)
- Likes and dislikes
- BMR and dietary preferences
- User goals and restrictions

Uses entity-based memory with user_id for proper retrieval.
"""

import asyncio
from typing import Dict, List, Optional
from memory.mem0_client import get_memory_client


class PreferenceManager:
    """Manages user preferences, favorites, and dietary information using mem0."""
    
    def __init__(self):
        """Initialize the preference manager with mem0 client."""
        self.memory = get_memory_client()
    
    def _run_sync(self, coro):
        """
        Run an async coroutine synchronously.
        Handles event loop detection for compatibility.
        """
        try:
            # Try to get the current event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, create a new task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, coro)
                    return future.result()
            else:
                # No running loop, safe to use run
                return loop.run_until_complete(coro)
        except RuntimeError:
            # No event loop exists, create one
            return asyncio.run(coro)
    
    # ========== FAVORITES ==========
    
    async def add_favorite_meal_plan(
        self,
        user_id: str,
        meal_plan_id: str,
        meal_plan_name: str,
        meal_type: str
    ) -> Dict:
        """
        Store a favorited meal plan using entity-based memory.
        
        Args:
            user_id: Unique user identifier
            meal_plan_id: ID of the meal plan
            meal_plan_name: Name/description of the meal plan
            meal_type: Type of meal (breakfast, lunch, dinner)
        
        Returns:
            Dict with memory ID and status
        """
        # Be VERY explicit that this is a favorite
        messages = [
            {
                "role": "user",
                "content": f"FAVORITE: {meal_plan_name} is my favorite {meal_type} meal plan. This is one of my favorite meal plans."
            }
        ]
        
        print(f"[mem0] Adding favorite meal plan for user_id: {user_id}")
        print(f"[mem0] Item: {meal_plan_name}, Type: {meal_type}")
        
        # Mem0 Platform API: pass messages and user_id directly
        # The client.add() method may be sync, so we call it in a thread
        def _add():
            return self.memory.add(
                messages=messages,
                user_id=user_id,
                metadata={
                    "category": "favorites",  # CRITICAL: Mark as favorite
                    "type": "meal_plan",
                    "meal_type": meal_type,
                    "meal_plan_id": meal_plan_id,
                    "item_name": meal_plan_name
                }
            )
        
        result = await asyncio.to_thread(_add)
        print(f"[mem0] ✓ Added favorite: {result}")
        return result
    
    async def add_favorite_food_item(
        self,
        user_id: str,
        food_item_name: str,
        meal_type: str
    ) -> Dict:
        """
        Store a favorited individual food item using entity-based memory.
        
        Args:
            user_id: Unique user identifier
            food_item_name: Name of the food item
            meal_type: Type of meal (breakfast, lunch, dinner)
        
        Returns:
            Dict with memory ID and status
        """
        # Be VERY explicit that this is a favorite
        messages = [
            {
                "role": "user",
                "content": f"FAVORITE: {food_item_name} is my favorite {meal_type} food item. This is one of my favorite food items."
            }
        ]
        
        print(f"[mem0] Adding favorite food item for user_id: {user_id}")
        print(f"[mem0] Item: {food_item_name}, Type: {meal_type}")
        
        def _add():
            return self.memory.add(
                messages=messages,
                user_id=user_id,
                metadata={
                    "category": "favorites",  # CRITICAL: Mark as favorite
                    "type": "food_item",
                    "meal_type": meal_type,
                    "item_name": food_item_name
                }
            )
        
        result = await asyncio.to_thread(_add)
        print(f"[mem0] ✓ Added favorite: {result}")
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
        Store a dislike for a meal plan or food item using entity-based memory.
        
        Args:
            user_id: Unique user identifier
            item_name: Name of the item disliked
            item_type: Type of item ("meal_plan" or "food_item")
            meal_type: Type of meal (breakfast, lunch, dinner)
            reason: Optional reason for the dislike
        
        Returns:
            Dict with memory ID and status
        """
        # Be VERY explicit that this is a dislike
        content = f"DISLIKE: I do not like {item_name}. This is something I dislike."
        if reason:
            content += f" Reason: {reason}"
        
        messages = [{"role": "user", "content": content}]
        
        print(f"[mem0] Adding dislike for user_id: {user_id}")
        print(f"[mem0] Item: {item_name}, Type: {item_type}, Meal: {meal_type}")
        
        def _add():
            return self.memory.add(
                messages=messages,
                user_id=user_id,
                metadata={
                    "category": "dislikes",  # CRITICAL: Mark as dislike
                    "type": item_type,
                    "meal_type": meal_type,
                    "item_name": item_name,
                    "sentiment": "negative"
                }
            )
        
        result = await asyncio.to_thread(_add)
        print(f"[mem0] ✓ Added dislike: {result}")
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
        Store user's BMR and dietary information using entity-based memory.
        
        Args:
            user_id: Unique user identifier
            bmr: Basal Metabolic Rate in calories
            dietary_restrictions: List of dietary restrictions (e.g., ["vegan", "gluten-free"])
            goals: User's fitness/health goals
        
        Returns:
            Dict with memory ID and status
        """
        restrictions_text = ", ".join(dietary_restrictions) if dietary_restrictions else "none"
        
        # Be explicit about user profile
        messages = [
            {
                "role": "user",
                "content": f"USER PROFILE: My BMR is {bmr} calories per day. My dietary restrictions are: {restrictions_text}. My fitness goal is: {goals}"
            }
        ]
        
        print(f"[mem0] Storing BMR for user_id: {user_id}")
        
        def _add():
            return self.memory.add(
                messages=messages,
                user_id=user_id,
                metadata={
                    "category": "user_profile",  # CRITICAL: Mark as profile
                    "bmr": bmr,
                    "dietary_restrictions": dietary_restrictions,
                    "goals": goals
                }
            )
        
        result = await asyncio.to_thread(_add)
        print(f"[mem0] ✓ Stored user profile: {result}")
        return result
    
    async def update_user_bmr(
        self,
        user_id: str,
        bmr: Optional[float] = None,
        dietary_restrictions: Optional[List[str]] = None,
        goals: Optional[str] = None
    ) -> Dict:
        """
        Update user's BMR and dietary information using entity-based memory.
        
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
        metadata = {"category": "user_profile"}  # For filtering
        
        if bmr is not None:
            updates.append(f"My updated BMR is {bmr} calories")
            metadata["bmr"] = bmr
        
        if dietary_restrictions is not None:
            restrictions_text = ", ".join(dietary_restrictions) if dietary_restrictions else "none"
            updates.append(f"My updated dietary restrictions are: {restrictions_text}")
            metadata["dietary_restrictions"] = dietary_restrictions
        
        if goals is not None:
            updates.append(f"My updated fitness goal is: {goals}")
            metadata["goals"] = goals
        
        if not updates:
            return {"status": "no_changes"}
        
        messages = [{"role": "user", "content": ". ".join(updates)}]
        
        print(f"[mem0] Updating BMR for user_id: {user_id}")
        
        def _add():
            return self.memory.add(
                messages=messages,
                user_id=user_id,
                metadata=metadata
            )
        
        result = await asyncio.to_thread(_add)
        print(f"[mem0] ✓ Updated user profile: {result}")
        return result
    
    # ========== RETRIEVAL ==========
    
    def _extract_results(self, results) -> List[Dict]:
        """
        Extract results from mem0 response (Platform API format).
        
        Args:
            results: Response from mem0 search
        
        Returns:
            List of memory dictionaries
        """
        if isinstance(results, dict):
            # Platform API returns {'results': [...], ...}
            extracted = results.get('results', [])
            print(f"[mem0] ✓ Retrieved {len(extracted)} memories")
            return extracted
        elif isinstance(results, list):
            # Sometimes may return list directly
            print(f"[mem0] ✓ Retrieved {len(results)} memories (list format)")
            return results
        else:
            print(f"[mem0] ⚠️ Unexpected results type: {type(results)}")
            return []
    
    async def get_user_preferences(
        self,
        user_id: str,
        categories: Optional[List[str]] = None
    ) -> List[Dict]:
        """
        Retrieve all preferences for personalization using entity-based memory.
        
        Args:
            user_id: Unique user identifier
            categories: Optional list of categories to filter by
        
        Returns:
            List of memory dictionaries
        """
        query = "preferences favorites dislikes"
        
        print(f"[mem0] Searching preferences for user {user_id}")
        
        try:
            # Mem0 Platform API v2: user_id must be in filters (required)
            filters = {"user_id": user_id}
            
            def _search():
                return self.memory.search(
                    query=query,
                    filters=filters,
                    limit=100  # Get more results for general preferences
                )
            
            results = await asyncio.to_thread(_search)
            print(f"[mem0] Raw preferences results: {type(results)}")
            
            # Extract results from Platform format
            results = self._extract_results(results)
            
            # Filter by categories if specified
            if categories and results:
                results = [
                    r for r in results 
                    if isinstance(r, dict) and r.get("metadata", {}).get("category") in categories
                ]
                print(f"[mem0] ✓ Found {len(results)} preferences in categories {categories}")
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] ⚠️ Error retrieving user preferences: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def get_favorites(
        self,
        user_id: str,
        meal_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Get user's favorited items using entity-based memory retrieval.
        
        Args:
            user_id: Unique user identifier
            meal_type: Optional meal type filter (breakfast, lunch, dinner)
        
        Returns:
            List of favorite items
        """
        query = "favorites"
        if meal_type:
            query = f"{meal_type} favorites"
        
        print(f"[mem0] Searching favorites for user {user_id} with query '{query}'")
        
        try:
            # Mem0 Platform API v2: user_id must be in filters (required)
            filters = {"user_id": user_id}
            
            def _search():
                return self.memory.search(
                    query=query,
                    filters=filters,
                    limit=50  # Get more results for favorites
                )
            
            results = await asyncio.to_thread(_search)
            print(f"[mem0] Raw search results: {type(results)}")
            
            # Extract results from Platform format
            results = self._extract_results(results)
            
            # DEBUG: Print what we actually got
            if results:
                print(f"[mem0] DEBUG: First memory structure: {results[0]}")
                print(f"[mem0] DEBUG: Memory text: {results[0].get('memory', 'N/A')}")
                print(f"[mem0] DEBUG: Metadata: {results[0].get('metadata', {})}")
            
            # Filter by checking if the memory text contains "favorite" or by metadata
            if results:
                # Try multiple filtering strategies
                filtered = []
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    
                    # Strategy 1: Check metadata category
                    if r.get("metadata", {}).get("category") == "favorites":
                        filtered.append(r)
                        continue
                    
                    # Strategy 2: Check memory text for "favorite" keyword
                    memory_text = r.get("memory", "").lower()
                    if "favorite" in memory_text or "favorited" in memory_text:
                        filtered.append(r)
                        continue
                
                results = filtered
                print(f"[mem0] ✓ Found {len(results)} favorites after filtering")
            
            # Further filter by meal_type if specified
            if meal_type and results:
                results = [
                    r for r in results 
                    if r.get("metadata", {}).get("meal_type") == meal_type
                ]
                print(f"[mem0] ✓ Found {len(results)} {meal_type} favorites")
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] ⚠️ Error retrieving favorites: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def get_dislikes(
        self,
        user_id: str,
        meal_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Get items user dislikes using entity-based memory retrieval.
        Critical for filtering meal recommendations.
        
        Args:
            user_id: Unique user identifier
            meal_type: Optional meal type filter
        
        Returns:
            List of disliked items
        """
        query = "dislikes"
        if meal_type:
            query = f"{meal_type} dislikes"
        
        print(f"[mem0] Searching dislikes for user {user_id} with query '{query}'")
        
        try:
            # Mem0 Platform API v2: user_id must be in filters (required)
            filters = {"user_id": user_id}
            
            def _search():
                return self.memory.search(
                    query=query,
                    filters=filters,
                    limit=50  # Get more results for dislikes
                )
            
            results = await asyncio.to_thread(_search)
            print(f"[mem0] Raw search results: {type(results)}")
            
            # Extract results from Platform format
            results = self._extract_results(results)
            
            # Filter by checking if the memory text contains "dislike" or by metadata
            if results:
                # Try multiple filtering strategies
                filtered = []
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    
                    # Strategy 1: Check metadata category
                    if r.get("metadata", {}).get("category") == "dislikes":
                        filtered.append(r)
                        continue
                    
                    # Strategy 2: Check memory text for "dislike" keyword
                    memory_text = r.get("memory", "").lower()
                    if "dislike" in memory_text or "do not like" in memory_text:
                        filtered.append(r)
                        continue
                
                results = filtered
                print(f"[mem0] ✓ Found {len(results)} dislikes after filtering")
            
            # Further filter by meal_type if specified
            if meal_type and results:
                results = [
                    r for r in results 
                    if r.get("metadata", {}).get("meal_type") == meal_type
                ]
                print(f"[mem0] ✓ Found {len(results)} {meal_type} dislikes")
            
            return results if results else []
        except Exception as e:
            print(f"[mem0] ⚠️ Error retrieving dislikes: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def get_user_profile(
        self,
        user_id: str
    ) -> Optional[Dict]:
        """
        Get user's profile information using entity-based memory retrieval.
        Retrieves BMR, dietary restrictions, and goals.
        
        Args:
            user_id: Unique user identifier
        
        Returns:
            User profile dictionary or None
        """
        query = "BMR profile dietary restrictions fitness goals"
        
        print(f"[mem0] Searching user profile for user {user_id}")
        
        try:
            # Mem0 Platform API v2: user_id must be in filters (required)
            filters = {"user_id": user_id}
            
            def _search():
                return self.memory.search(
                    query=query,
                    filters=filters,
                    limit=10  # Get recent profile memories
                )
            
            results = await asyncio.to_thread(_search)
            print(f"[mem0] Raw profile results: {type(results)}")
            
            # Extract results from Platform format
            results = self._extract_results(results)
            
            # Filter by category=user_profile in metadata
            if results:
                results = [
                    r for r in results 
                    if isinstance(r, dict) and r.get("metadata", {}).get("category") == "user_profile"
                ]
                print(f"[mem0] ✓ Found {len(results)} profile memories")
            
            # Return first result if available (most recent)
            if results and len(results) > 0:
                return results[0]
            return None
        except Exception as e:
            print(f"[mem0] ⚠️ Error retrieving user profile: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    # ========== DELETION ==========
    
    async def remove_favorite(
        self,
        user_id: str,
        item_name: str
    ) -> Dict:
        """
        Remove a favorite item using entity-based memory deletion.
        
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
                    def _delete():
                        return self.memory.delete(memory_id)
                    
                    await asyncio.to_thread(_delete)
                    print(f"[mem0] ✓ Deleted favorite: {item_name}")
                    return {"status": "deleted", "memory_id": memory_id}
        
        print(f"[mem0] ⚠️ Favorite not found: {item_name}")
        return {"status": "not_found"}
    
    async def get_all_memories(
        self,
        user_id: str
    ) -> List[Dict]:
        """
        Get all memories for a user using entity-based memory retrieval.
        Useful for debugging.
        
        Args:
            user_id: Unique user identifier
        
        Returns:
            List of all memories
        """
        print(f"[mem0] Getting all memories for user {user_id}")
        
        try:
            # Mem0 Platform API v2: user_id must be in filters (required)
            filters = {"user_id": user_id}
            
            # Broad query to get all memories for this user
            def _search():
                return self.memory.search(
                    query="preferences favorites dislikes profile",  # Broad query
                    filters=filters,
                    limit=100  # Get many results for debugging
                )
            
            results = await asyncio.to_thread(_search)
            print(f"[mem0] All memories raw results: {type(results)}")
            
            extracted = self._extract_results(results)
            print(f"[mem0] ✓ Found {len(extracted)} total memories")
            
            return extracted
        except Exception as e:
            print(f"[mem0] ⚠️ Error retrieving all memories: {e}")
            import traceback
            traceback.print_exc()
            return []


def build_preference_context(
    prefs: List[Dict],
    favorites: List[Dict],
    dislikes: List[Dict]
) -> str:
    """
    Format mem0 memories into LLM context string.
    
    Args:
        prefs: General user preferences
        favorites: Favorite items (from heart button)
        dislikes: Disliked items (from broken heart button - critical for filtering)
    
    Returns:
        Formatted context string for LLM
    """
    context_parts = []
    
    context_parts.append("=== USER PREFERENCES (Learn from Past Choices) ===")
    context_parts.append("IMPORTANT: Use these as inspiration for SIMILAR items available in today's menu.")
    context_parts.append("Do NOT copy these exact items unless they are available in the current menu.")
    context_parts.append("Instead, find similar foods with comparable nutritional profiles and flavors.")
    context_parts.append("")
    
    # User Profile
    if prefs:
        context_parts.append("USER PROFILE:")
        for pref in prefs:
            memory_text = pref.get("memory", "")
            if memory_text:
                context_parts.append(f"• {memory_text}")
        context_parts.append("")
    
    # Favorites - user's positive preferences from heart button
    if favorites:
        context_parts.append("FAVORITES (Find similar items with these characteristics):")
        for item in favorites:
            memory_text = item.get("memory", "")
            metadata = item.get("metadata", {})
            meal_type = metadata.get("meal_type", "")
            item_name = metadata.get("item_name", "")
            if memory_text:
                meal_label = f"[{meal_type.upper()}] " if meal_type else ""
                context_parts.append(f"• {meal_label}{memory_text}")
                if item_name:
                    context_parts.append(f"  → Look for items similar to: {item_name}")
        context_parts.append("")
    
    # Dislikes (CRITICAL - must avoid)
    if dislikes:
        context_parts.append("DISLIKES (NEVER include these or similar items):")
        for dislike in dislikes:
            memory_text = dislike.get("memory", "")
            metadata = dislike.get("metadata", {})
            item_name = metadata.get("item_name", "")
            if memory_text:
                context_parts.append(f"• ❌ {memory_text}")
                if item_name:
                    context_parts.append(f"  → Avoid: {item_name} and similar items")
        context_parts.append("")
    
    if len(context_parts) <= 5:  # Only has header
        return ""
    
    return "\n".join(context_parts)

