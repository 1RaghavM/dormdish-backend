"""
Mem0 client initialization for DormDish backend.

This module provides a singleton memory client for storing and retrieving
user preferences, favorites, likes/dislikes, and dietary information.

Properly configured for entity-based memory with user_id.
"""

import os
import asyncio
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize mem0 client as None (lazy initialization)
_memory_client: Optional[object] = None


def get_memory_client():
    """
    Get or create the mem0 client instance (singleton pattern).
    
    This function implements lazy initialization for the Mem0 Platform API.
    Uses entity-based memory with user_id for proper memory retrieval.
    
    Returns:
        Memory client instance (MemoryClient)
    
    Environment Variables:
        MEM0_API_KEY: API key for Mem0 Platform (required)
        MEM0_ORG_ID: Organization ID (optional, for team usage)
        MEM0_PROJECT_ID: Project ID (optional, for multi-project usage)
    """
    global _memory_client
    
    if _memory_client is not None:
        return _memory_client
    
    try:
        from mem0 import MemoryClient
        
        api_key = os.getenv("MEM0_API_KEY")
        if not api_key:
            raise ValueError(
                "MEM0_API_KEY environment variable is required. "
                "Get your API key from https://app.mem0.ai"
            )
        
        # Initialize Mem0 Platform client
        # org_id and project_id are optional
        client_kwargs = {"api_key": api_key}
        
        org_id = os.getenv("MEM0_ORG_ID")
        if org_id:
            client_kwargs["org_id"] = org_id
        
        project_id = os.getenv("MEM0_PROJECT_ID")
        if project_id:
            client_kwargs["project_id"] = project_id
        
        _memory_client = MemoryClient(**client_kwargs)
        print(f"[mem0] ✓ Initialized Mem0 Platform client (org: {org_id or 'default'}, project: {project_id or 'default'})")
        
        return _memory_client
    
    except ImportError as e:
        raise ImportError(
            "mem0ai package not found. Install it with: pip install mem0ai"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Failed to initialize mem0 client: {e}") from e


def reset_memory_client():
    """
    Reset the memory client instance (useful for testing).
    """
    global _memory_client
    _memory_client = None

