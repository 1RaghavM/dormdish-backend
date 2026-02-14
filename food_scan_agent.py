"""
Gemini 2.5 Flash integration for analyzing food photos against dining hall menus.

Identifies menu items in a photo, estimates portion sizes, and returns
structured nutrition data with server-side totals (never trusting LLM math).
"""

import asyncio
import json
import logging
import os
import random
import uuid
import warnings
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError, PydanticDeprecatedSince20
from dotenv import load_dotenv

from food_scan_prompts import build_system_prompt, build_user_prompt
from food_scan_models import FoodScanGeminiSchema
from food_scan_validation import build_scan_response

# Suppress Pydantic V2 deprecation warnings from Google GenAI library
warnings.filterwarnings("ignore", category=PydanticDeprecatedSince20)

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton Gemini client
# ---------------------------------------------------------------------------
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    """Return a lazily-initialized Gemini client (singleton)."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set")
        _client = genai.Client(api_key=api_key)
        logger.info("Gemini client initialized for food scanning")
    return _client


# ---------------------------------------------------------------------------
# Retry / back-off helpers (mirrors meal_plan_agent.py)
# ---------------------------------------------------------------------------

def _is_overloaded_error(exc: Exception) -> bool:
    """Check whether the exception indicates a transient overload / 503."""
    msg = str(exc).lower()
    return (
        ("503" in msg and "unavailable" in msg)
        or "model is overloaded" in msg
        or "resource exhausted" in msg
        or "temporarily unavailable" in msg
        or "unavailable" in msg
    )


async def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter, capped."""
    base = float(os.getenv("FOODSCAN_BACKOFF_BASE_S", "1.0"))
    cap = float(os.getenv("FOODSCAN_BACKOFF_MAX_S", "30"))
    exp = min(cap, base * (2 ** max(0, attempt - 1)))
    await asyncio.sleep(random.uniform(0.0, exp))


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    raw = (text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_first_json_object(text: str) -> str:
    """Extract the first {...} JSON object from a model response."""
    raw = _strip_code_fences(text)
    first = raw.find("{")
    last = raw.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ValueError("No JSON object detected in model response")
    return raw[first : last + 1]


# ---------------------------------------------------------------------------
# Core public function
# ---------------------------------------------------------------------------

async def analyze_food_photo(
    image_data: bytes,
    menu_json: str,
    dining_hall: str,
    date: str,
    description: str | None = None,
) -> dict:
    """
    Send a food photo + dining hall menu to Gemini 2.5 Flash for analysis.

    Args:
        image_data: JPEG image bytes
        menu_json: JSON string of menu items (from prepare_menu_for_food_scan)
        dining_hall: Name of the dining hall
        date: Date string (YYYY-MM-DD)
        description: Optional user description of the food

    Returns:
        dict with scan_id, items, totals, dining_hall, date
    """
    scan_id = str(uuid.uuid4())
    logger.info(
        "Food scan %s: dining_hall=%s date=%s description=%s image_bytes=%d",
        scan_id,
        dining_hall,
        date,
        description or "(none)",
        len(image_data),
    )

    client = _get_client()

    # ---- Build prompts ----
    system_prompt = build_system_prompt(
        dining_hall=dining_hall,
        date=date,
        menu_json=menu_json,
    )
    user_prompt = build_user_prompt(description=description)

    # ---- Model configuration ----
    model_name = os.getenv("FOODSCAN_MODEL", "gemini-2.5-flash")
    temperature = float(os.getenv("FOODSCAN_TEMPERATURE", "0.2"))
    max_tokens = int(os.getenv("FOODSCAN_MAX_TOKENS", "8000"))
    max_retries = int(os.getenv("FOODSCAN_RETRIES", "3"))

    # ---- Call Gemini with retry logic ----
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        # On retries, lower temperature and add repair hint
        attempt_temperature = temperature if attempt == 1 else 0.1
        attempt_system = system_prompt
        if attempt > 1:
            attempt_system += (
                "\n\nYour previous response could not be parsed. "
                "Return ONLY a single valid JSON object matching the schema. "
                "No markdown, no comments, no extra text."
            )

        def _call_gemini():
            return client.models.generate_content(
                model=model_name,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(
                                data=image_data, mime_type="image/jpeg"
                            ),
                            types.Part.from_text(text=user_prompt),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(
                    system_instruction=attempt_system,
                    response_mime_type="application/json",
                    response_schema=FoodScanGeminiSchema,
                    temperature=attempt_temperature,
                    max_output_tokens=max_tokens,
                ),
            )

        # Overload retry loop (inner)
        overload_retries = int(os.getenv("FOODSCAN_OVERLOAD_RETRIES", "4"))
        response = None
        overloaded_error: Optional[Exception] = None

        for overload_attempt in range(1, overload_retries + 1):
            try:
                logger.info(
                    "Food scan %s: sending request to %s (attempt=%d, overload_try=%d)",
                    scan_id,
                    model_name,
                    attempt,
                    overload_attempt,
                )
                response = await asyncio.to_thread(_call_gemini)
                overloaded_error = None
                break
            except Exception as exc:
                if _is_overloaded_error(exc):
                    overloaded_error = exc
                    if overload_attempt < overload_retries:
                        logger.warning(
                            "Food scan %s: model overloaded (attempt=%d, overload_try=%d)",
                            scan_id,
                            attempt,
                            overload_attempt,
                        )
                        await _sleep_backoff(overload_attempt)
                        continue
                    break
                # Non-transient error -- propagate immediately
                logger.error(
                    "Food scan %s: Gemini API error (attempt=%d): %s",
                    scan_id,
                    attempt,
                    exc,
                )
                raise

        if response is None:
            last_error = overloaded_error or ValueError("Model did not return a response")
            logger.warning("Food scan %s: no response on attempt %d", scan_id, attempt)
            continue

        raw_text = response.text or ""
        if not raw_text.strip():
            last_error = ValueError("Model returned empty response")
            logger.warning(
                "Food scan %s: empty response (attempt=%d)", scan_id, attempt
            )
            continue

        logger.info(
            "Food scan %s: received response (%d chars, attempt=%d)",
            scan_id,
            len(raw_text),
            attempt,
        )

        # ---- Parse JSON ----
        try:
            raw_json = _extract_first_json_object(raw_text)
            parsed = json.loads(raw_json)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Food scan %s: JSON parsing failed (attempt=%d): %s  raw_preview=%s",
                scan_id,
                attempt,
                exc,
                raw_text[:500],
            )
            continue

        # ---- Validate with Pydantic ----
        try:
            result = FoodScanGeminiSchema.model_validate(parsed)
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "Food scan %s: Pydantic validation failed (attempt=%d): %s",
                scan_id,
                attempt,
                exc,
            )
            continue

        # ---- Build response with server-side totals via validation module ----
        items_dicts = [item.model_dump() for item in result.items]
        scan_response = build_scan_response(
            items=items_dicts,
            dining_hall=dining_hall,
            date=date,
        )

        logger.info(
            "Food scan %s: success -- %d items, %.0f cal, %.1fg protein",
            scan_id,
            len(scan_response["items"]),
            scan_response["totals"]["calories"],
            scan_response["totals"]["protein_g"],
        )

        return scan_response

    # All attempts exhausted
    error_msg = f"Food scan failed after {max_retries} attempts: {last_error}"
    logger.error("Food scan %s: %s", scan_id, error_msg)
    raise ValueError(error_msg)
