"""
Mem0 client initialization for DormDish backend.

This module provides a singleton memory client for storing and retrieving
user preferences, favorites, likes/dislikes, and dietary information.

Uses the Mem0 Platform hosted REST API directly (no SDK required).
"""

import os
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv
import httpx

# Load environment variables
load_dotenv()

# Mem0 Platform API base URL
MEM0_API_BASE = "https://api.mem0.ai/v1"


class Mem0Client:
    """
    Lightweight client for Mem0 Platform REST API.
    
    This replaces the mem0ai SDK with direct HTTP calls to reduce
    dependencies and deployment size.
    """
    
    def __init__(self, api_key: str, org_id: Optional[str] = None, project_id: Optional[str] = None):
        """
        Initialize the Mem0 client.
        
        Args:
            api_key: Mem0 Platform API key
            org_id: Optional organization ID
            project_id: Optional project ID
        """
        self.api_key = api_key
        self.org_id = org_id
        self.project_id = project_id
        self.headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json"
        }
        # Use a persistent client for connection pooling
        self._client = httpx.Client(timeout=30.0)
    
    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict:
        """Make an HTTP request to the Mem0 API."""
        url = f"{MEM0_API_BASE}{endpoint}"
        
        try:
            if method == "GET":
                response = self._client.get(url, headers=self.headers, params=data)
            elif method == "POST":
                response = self._client.post(url, headers=self.headers, json=data)
            elif method == "DELETE":
                response = self._client.delete(url, headers=self.headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            response.raise_for_status()
            return response.json() if response.content else {}
        except httpx.HTTPStatusError as e:
            print(f"[mem0] HTTP error: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            print(f"[mem0] Request error: {e}")
            raise
    
    def add(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict:
        """
        Add a memory to Mem0.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            user_id: User identifier for the memory
            metadata: Optional metadata to attach to the memory
        
        Returns:
            Response from Mem0 API
        """
        payload = {
            "messages": messages,
            "user_id": user_id
        }
        
        if metadata:
            payload["metadata"] = metadata
        
        if self.org_id:
            payload["org_id"] = self.org_id
        
        if self.project_id:
            payload["project_id"] = self.project_id
        
        return self._make_request("POST", "/memories/", payload)
    
    def search(
        self,
        query: str,
        user_id: str,
        filters: Optional[Dict] = None,
        limit: int = 100
    ) -> Dict:
        """
        Search memories in Mem0.
        
        Args:
            query: Search query string
            user_id: User identifier to search within
            filters: Optional metadata filters
            limit: Maximum number of results
        
        Returns:
            Search results from Mem0 API
        """
        payload = {
            "query": query,
            "user_id": user_id,
            "limit": limit
        }
        
        if filters:
            payload["filters"] = filters
        
        if self.org_id:
            payload["org_id"] = self.org_id
        
        if self.project_id:
            payload["project_id"] = self.project_id
        
        return self._make_request("POST", "/memories/search/", payload)
    
    def delete(self, memory_id: str) -> Dict:
        """
        Delete a specific memory by ID.
        
        Args:
            memory_id: ID of the memory to delete
        
        Returns:
            Response from Mem0 API
        """
        return self._make_request("DELETE", f"/memories/{memory_id}/")
    
    def get_all(self, user_id: str) -> Dict:
        """
        Get all memories for a user.
        
        Args:
            user_id: User identifier
        
        Returns:
            All memories for the user
        """
        params = {"user_id": user_id}
        
        if self.org_id:
            params["org_id"] = self.org_id
        
        if self.project_id:
            params["project_id"] = self.project_id
        
        return self._make_request("GET", "/memories/", params)
    
    def __del__(self):
        """Clean up the HTTP client."""
        if hasattr(self, '_client'):
            self._client.close()


# Initialize mem0 client as None (lazy initialization)
_memory_client: Optional[Mem0Client] = None


def get_memory_client() -> Mem0Client:
    """
    Get or create the mem0 client instance (singleton pattern).
    
    This function implements lazy initialization for the Mem0 Platform API.
    Uses entity-based memory with user_id for proper memory retrieval.
    
    Returns:
        Mem0Client instance
    
    Environment Variables:
        MEM0_API_KEY: API key for Mem0 Platform (required)
        MEM0_ORG_ID: Organization ID (optional, for team usage)
        MEM0_PROJECT_ID: Project ID (optional, for multi-project usage)
    """
    global _memory_client
    
    if _memory_client is not None:
        return _memory_client
    
    api_key = os.getenv("MEM0_API_KEY")
    if not api_key:
        raise ValueError(
            "MEM0_API_KEY environment variable is required. "
            "Get your API key from https://app.mem0.ai"
        )
    
    org_id = os.getenv("MEM0_ORG_ID")
    project_id = os.getenv("MEM0_PROJECT_ID")
    
    _memory_client = Mem0Client(
        api_key=api_key,
        org_id=org_id,
        project_id=project_id
    )
    
    print(f"[mem0] ✓ Initialized Mem0 Platform client (org: {org_id or 'default'}, project: {project_id or 'default'})")
    
    return _memory_client


def reset_memory_client():
    """
    Reset the memory client instance (useful for testing).
    """
    global _memory_client
    _memory_client = None

