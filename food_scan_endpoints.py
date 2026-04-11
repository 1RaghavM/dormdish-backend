"""
Food scan endpoint handlers.

These functions are imported by app.py and registered as FastAPI endpoints.
They follow the same patterns as the barcode/lookup and food-log endpoints.

=== WIRING INSTRUCTIONS FOR app.py ===

1. Add these imports at the top of app.py:

   from fastapi import UploadFile, File, Form  # add to existing fastapi import line
   from food_scan_endpoints import handle_food_scan_analyze, handle_food_scan_feedback
   from food_scan_models import FoodScanFeedbackRequest

2. Add these endpoint registrations (e.g. after the food-log endpoints):

   # ==================== FOOD SCAN ENDPOINTS ====================

   @app.post('/food-scan/analyze', summary='Analyze a food photo using AI')
   async def food_scan_analyze(
       image: UploadFile = File(...),
       dining_hall: str = Form(...),
       date: str = Form(...),
       description: Optional[str] = Form(None),
       current_user=Depends(get_current_user),
   ):
       return await handle_food_scan_analyze(image, dining_hall, date, description, current_user)

   @app.post('/food-scan/feedback', summary='Submit feedback on food scan accuracy')
   async def food_scan_feedback(
       data: FoodScanFeedbackRequest,
       current_user=Depends(get_current_user),
   ):
       return await handle_food_scan_feedback(data, current_user, supabase)
"""

import logging
import re
from datetime import datetime
from fastapi import UploadFile, File, Form, HTTPException, Depends
from typing import Optional
import uuid as uuid_mod
from food_scan_models import ALLOWED_IMAGE_EXTENSIONS

logger = logging.getLogger("dormdish.api")


async def _run_analysis(
    image_data: bytes,
    dining_hall: str | None,
    date: str,
    description: Optional[str] = None,
) -> dict:
    """Shared analysis logic used by both multipart and JSON-mode handlers."""
    if dining_hall is not None:
        dining_hall = dining_hall.strip() or None

    if not date or not date.strip():
        raise HTTPException(status_code=400, detail="date is required")
    date = date.strip()

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if not date_pattern.match(date):
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format")

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date is not a valid calendar date")

    from food_scan_agent import analyze_food_photo

    if dining_hall:
        # --- DINING HALL MODE (existing flow) ---
        from food_scan_menu import prepare_menu_for_food_scan
        try:
            menu_info = await prepare_menu_for_food_scan(dining_hall, date)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error(f"Failed to fetch menu for food scan: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch dining hall menu")

        cache_name = None
        try:
            from menu_cache import get_cache_name
            from food_scan_menu import resolve_dining_hall
            university, hall_slug = resolve_dining_hall(dining_hall)
            cache_name = await get_cache_name(
                dining_hall=hall_slug,
                university=university,
                date=date,
                use_case="food_scan",
            )
        except Exception as e:
            logger.debug(f"Cache lookup skipped: {e}")

        try:
            result = await analyze_food_photo(
                image_data=image_data,
                menu_json=menu_info["menu_json"],
                dining_hall=dining_hall,
                date=date,
                description=description,
                cached_content=cache_name,
            )
        except Exception as e:
            logger.error(f"Food scan analysis failed: {e}")
            raise HTTPException(status_code=500, detail="Food scan analysis failed. Please try again.")

    else:
        # --- OWN FOOD MODE (no menu context) ---
        try:
            result = await analyze_food_photo(
                image_data=image_data,
                menu_json=None,
                dining_hall=None,
                date=date,
                description=description,
                cached_content=None,
            )
        except Exception as e:
            logger.error(f"Own-food scan analysis failed: {e}")
            raise HTTPException(status_code=500, detail="Food scan analysis failed. Please try again.")

        # Enrich with USDA nutrition data
        from usda_service import enrich_items_with_usda
        from food_scan_validation import calculate_totals
        items = result.get("items", [])
        result["items"] = await enrich_items_with_usda(items)
        # Recalculate totals since USDA may have replaced nutrition values
        result["totals"] = calculate_totals(result["items"])

    return result


# ==================== ANALYZE HANDLER ====================

async def handle_food_scan_analyze(
    image: UploadFile,
    dining_hall: str | None,
    date: str,
    description: Optional[str] = None,
    current_user: dict = None,
) -> dict:
    """
    Handler for POST /food-scan/analyze

    Accepts a food photo, fetches the dining hall menu for the given date,
    sends both to Gemini 2.5 Flash for analysis, and returns structured
    nutrition results matched against the menu.

    Args:
        image: Uploaded food photo (JPEG, PNG, HEIC, etc.)
        dining_hall: Name of the dining hall (e.g. "Busch Dining Hall")
        date: Date in YYYY-MM-DD format
        description: Optional text description of the food for better accuracy
        current_user: Authenticated user dict from Depends(get_current_user)

    Returns:
        dict with scan_id, identified items, and nutrition totals
    """
    # ── 1. Validate image ──
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_data = await image.read()

    # Limit image size (4MB – Vercel free plan has a 4.5MB body limit)
    if len(image_data) > 4 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 4MB)")

    return await _run_analysis(image_data, dining_hall, date, description)


# ==================== UPLOAD URL HANDLER ====================

FOOD_SCAN_BUCKET = "food-scan-images"


async def handle_food_scan_upload_url(
    file_extension: str,
    current_user: dict,
    supabase_client,
) -> dict:
    """
    Handler for POST /food-scan/upload-url

    Generates a signed upload URL so the client can upload an image
    directly to Supabase Storage, bypassing Vercel's body size limit.
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in session")

    ext = file_extension.lower().strip().lstrip(".")
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}",
        )

    image_path = f"{user_id}/{uuid_mod.uuid4()}.{ext}"

    try:
        response = supabase_client.storage.from_(FOOD_SCAN_BUCKET).create_signed_upload_url(image_path)
    except Exception as e:
        logger.error(f"Failed to generate upload URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")

    signed_url = None
    if isinstance(response, dict):
        signed_url = response.get("signed_url") or response.get("signedURL") or response.get("signedUrl")
    elif hasattr(response, "signed_url"):
        signed_url = response.signed_url

    if not signed_url:
        logger.error(f"Unexpected upload URL response format: {response}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")

    logger.info(f"Generated upload URL for user {user_id}: {image_path}")

    return {"upload_url": signed_url, "image_path": image_path}


# ==================== ANALYZE (JSON MODE) HANDLER ====================

IMAGE_PATH_PATTERN = re.compile(
    r"^[a-f0-9\-]+/[a-f0-9\-]+\.(jpg|jpeg|png|heic|heif|webp)$"
)


async def handle_food_scan_analyze_json(
    data,  # FoodScanAnalyzeRequest pydantic model
    current_user: dict,
    supabase_client,
) -> dict:
    """Handler for POST /food-scan/analyze-json (storage path mode)."""
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in session")

    if not IMAGE_PATH_PATTERN.match(data.image_path):
        raise HTTPException(status_code=400, detail="Invalid image_path format")

    path_user_id = data.image_path.split("/")[0]
    if path_user_id != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        image_data = supabase_client.storage.from_(FOOD_SCAN_BUCKET).download(data.image_path)
    except Exception as e:
        error_msg = str(e).lower()
        if "not found" in error_msg or "404" in error_msg:
            raise HTTPException(status_code=404, detail="Image not found in storage")
        logger.error(f"Failed to download image from storage: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve image from storage")

    if not image_data:
        raise HTTPException(status_code=404, detail="Image not found in storage")

    logger.info(
        f"Downloaded image from storage: path={data.image_path} size={len(image_data)} bytes"
    )

    return await _run_analysis(image_data, data.dining_hall, data.date, data.description)


# ==================== FEEDBACK HANDLER ====================

async def handle_food_scan_feedback(
    feedback_data,  # FoodScanFeedbackRequest pydantic model
    current_user: dict,
    supabase_client,  # Supabase client from app.py global
) -> dict:
    """
    Handler for POST /food-scan/feedback

    Stores the user's feedback on scan accuracy so we can track AI performance
    and improve the model over time.

    Args:
        feedback_data: FoodScanFeedbackRequest with scan_id, accurate flag,
                       original_items, edited_items, dining_hall, and date
        current_user: Authenticated user dict from Depends(get_current_user)
        supabase_client: Supabase client instance

    Returns:
        dict with success status
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in session")

    try:
        supabase_client.table("food_scan_feedback").insert({
            "user_id": str(user_id),
            "scan_id": feedback_data.scan_id,
            "accurate": feedback_data.accurate,
            "original_items": [item.model_dump() for item in feedback_data.original_items],
            "edited_items": [item.model_dump() for item in feedback_data.edited_items],
            "dining_hall": feedback_data.dining_hall,
            "date": feedback_data.date,
        }).execute()

        logger.info(
            f"Food scan feedback saved: scan_id={feedback_data.scan_id}, "
            f"accurate={feedback_data.accurate}, user_id={user_id}"
        )
        return {"success": True, "message": "Feedback recorded. Thank you!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save food scan feedback: {e}")
        raise HTTPException(status_code=500, detail="Failed to save feedback")
