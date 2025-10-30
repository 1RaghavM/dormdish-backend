"""
Mem0 client initialization for DormDish backend.

This module provides a singleton memory client for storing and retrieving
user preferences, favorites, likes/dislikes, and dietary information.

Configured to use Google Gemini for memory inference (same model used for meal planning).
"""

import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize mem0 client as None (lazy initialization)
_memory_client: Optional[object] = None


def get_memory_client():
    """
    Get or create the mem0 client instance (singleton pattern).
    
    This function implements lazy initialization to avoid importing mem0
    until it's actually needed. Supports both Mem0 Platform (managed)
    and Open Source (self-hosted) options.
    
    **Uses Google Gemini for memory inference** - the same API you're already using
    for meal plan generation.
    
    Returns:
        Memory client instance (MemoryClient or Memory)
    
    Environment Variables:
        MEM0_API_KEY: API key for Mem0 Platform (if using managed service)
        GEMINI_API_KEY: Google Gemini API key (required for memory inference)
        MEM0_USE_PLATFORM: Set to "true" to use Mem0 Platform, otherwise uses Open Source
    """
    global _memory_client
    
    if _memory_client is not None:
        return _memory_client
    
    try:
        # Determine which mem0 implementation to use
        use_platform = os.getenv("MEM0_USE_PLATFORM", "false").lower() == "true"
        
        # Get Gemini API key
        gemini_key = os.getenv("GEMINI_API_KEY")
        if not gemini_key:
            raise ValueError(
                "GEMINI_API_KEY environment variable is required for memory inference. "
                "This is the same API key you use for meal plan generation."
            )
        
        if use_platform:
            # Option 1: Mem0 Platform (Managed - Recommended for production)
            from mem0 import MemoryClient
            
            api_key = os.getenv("MEM0_API_KEY")
            if not api_key:
                raise ValueError(
                    "MEM0_API_KEY environment variable is required when using Mem0 Platform. "
                    "Get your API key from https://app.mem0.ai"
                )
            
            # Note: Mem0 Platform handles LLM configuration server-side
            # You may need to configure Gemini in the Mem0 dashboard
            _memory_client = MemoryClient(api_key=api_key)
            print("[mem0] Initialized Mem0 Platform client")
        else:
            # Option 2: Mem0 Open Source (Self-hosted with Gemini)
            from mem0 import Memory
            
            # Configure mem0 to use Google Gemini for memory inference
            config = {
                "llm": {
                    "provider": "gemini",
                    "config": {
                        "model": "gemini-1.5-flash",  # Fast and cost-effective for memory operations
                        "temperature": 0.1,  # Low temperature for consistent memory extraction
                        "api_key": gemini_key,
                    }
                },
                # Optional: Configure embeddings (defaults to OpenAI if not specified)
                # "embedder": {
                #     "provider": "gemini",
                #     "config": {
                #         "model": "models/embedding-001",
                #         "api_key": gemini_key,
                #     }
                # }
            }
            
            _memory_client = Memory.from_config(config)
            print("[mem0] Initialized Mem0 Open Source client with Google Gemini")
        
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

