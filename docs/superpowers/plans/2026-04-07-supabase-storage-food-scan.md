# Supabase Storage Food Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace direct image upload in `/food-scan/analyze` with a presigned Supabase Storage URL flow to bypass Vercel's 4.5MB body limit, while keeping the legacy multipart path working.

**Architecture:** New `POST /food-scan/upload-url` endpoint generates signed upload URLs. Modified `POST /food-scan/analyze` accepts either an `image_path` (JSON body pointing to a storage object) or the existing multipart file upload. The client uploads images directly to Supabase Storage, bypassing Vercel entirely.

**Tech Stack:** FastAPI, Supabase Python SDK (storage), Pydantic, pytest (new)

**Spec:** `docs/superpowers/specs/2026-04-07-supabase-storage-food-scan-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `food_scan_models.py` | Modify | Add `FoodScanUploadUrlRequest`, `FoodScanUploadUrlResponse`, `FoodScanAnalyzeRequest` Pydantic models |
| `food_scan_endpoints.py` | Modify | Add `handle_food_scan_upload_url` handler; add `handle_food_scan_analyze_json` handler for Mode 1; refactor shared analysis logic into `_run_analysis` |
| `app.py:6650-6675` | Modify | Register new `/food-scan/upload-url` endpoint and new JSON-mode `/food-scan/analyze` route |
| `tests/test_food_scan_storage.py` | Create | Unit tests for upload URL generation, image path validation, and dual-mode analyze |

---

### Task 1: Add Pydantic Models for Upload URL and JSON Analyze

**Files:**
- Modify: `food_scan_models.py`

- [ ] **Step 1: Add the three new models to `food_scan_models.py`**

Add these models after the existing `FoodScanResponse` class (after line 65):

```python
# ── Upload URL endpoint ──────────────────────────────────────────────────────


ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "heic", "heif", "webp"}


class FoodScanUploadUrlRequest(BaseModel):
    """Request body for ``POST /food-scan/upload-url``."""

    file_extension: str = "jpg"


class FoodScanUploadUrlResponse(BaseModel):
    """Response returned by ``POST /food-scan/upload-url``."""

    upload_url: str
    image_path: str


# ── Analyze endpoint (JSON mode — storage path) ─────────────────────────────


class FoodScanAnalyzeRequest(BaseModel):
    """Request body for ``POST /food-scan/analyze`` when using storage path mode.

    Used when the client uploads the image to Supabase Storage first,
    then passes the storage path instead of the image bytes.
    """

    image_path: str
    dining_hall: str
    date: str
    description: Optional[str] = None
```

Also add this import at the top of the file (line 10, alongside the existing `Optional` import — it's already there):

No new imports needed — `Optional` and `List` are already imported.

- [ ] **Step 2: Verify the file parses correctly**

Run: `python -c "from food_scan_models import FoodScanUploadUrlRequest, FoodScanUploadUrlResponse, FoodScanAnalyzeRequest, ALLOWED_IMAGE_EXTENSIONS; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add food_scan_models.py
git commit -m "feat: add Pydantic models for upload-url and JSON analyze endpoints"
```

---

### Task 2: Add Upload URL Handler

**Files:**
- Modify: `food_scan_endpoints.py`

- [ ] **Step 1: Add the `handle_food_scan_upload_url` handler**

Add these imports at the top of `food_scan_endpoints.py` (after the existing imports on line 41):

```python
import uuid as uuid_mod
from food_scan_models import ALLOWED_IMAGE_EXTENSIONS
```

Then add this handler after the existing `handle_food_scan_analyze` function (before the `# ==================== FEEDBACK HANDLER ====================` comment at line 145):

```python
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

    Args:
        file_extension: File extension (e.g. "jpg", "png"). Defaults to "jpg".
        current_user: Authenticated user dict from Depends(get_current_user)
        supabase_client: Supabase client instance

    Returns:
        dict with upload_url and image_path
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in session")

    # Validate extension
    ext = file_extension.lower().strip().lstrip(".")
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}",
        )

    # Generate storage path: {user_id}/{uuid}.{ext}
    image_path = f"{user_id}/{uuid_mod.uuid4()}.{ext}"

    try:
        response = supabase_client.storage.from_(FOOD_SCAN_BUCKET).create_signed_upload_url(image_path)
    except Exception as e:
        logger.error(f"Failed to generate upload URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")

    # Supabase returns a dict with 'signed_url' (or 'signedURL') key
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
```

- [ ] **Step 2: Verify the file parses correctly**

Run: `python -c "from food_scan_endpoints import handle_food_scan_upload_url; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add food_scan_endpoints.py
git commit -m "feat: add handle_food_scan_upload_url handler"
```

---

### Task 3: Add JSON-Mode Analyze Handler (Mode 1)

**Files:**
- Modify: `food_scan_endpoints.py`

- [ ] **Step 1: Refactor shared analysis logic into `_run_analysis`**

Extract the common logic (steps 2-5 from the existing `handle_food_scan_analyze`) into a private helper. Replace lines 82-142 of `food_scan_endpoints.py` with:

First, add this private helper *before* the existing `handle_food_scan_analyze` function (after `logger = logging.getLogger(...)` on line 43):

```python
async def _run_analysis(
    image_data: bytes,
    dining_hall: str,
    date: str,
    description: Optional[str] = None,
) -> dict:
    """
    Shared analysis logic used by both multipart and JSON-mode handlers.

    Validates dining_hall/date, fetches the menu, calls Gemini, returns results.
    """
    # ── Validate dining_hall ──
    if not dining_hall or not dining_hall.strip():
        raise HTTPException(status_code=400, detail="dining_hall is required")
    dining_hall = dining_hall.strip()

    # ── Validate date format (YYYY-MM-DD) ──
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

    # ── Fetch menu for the dining hall ──
    from food_scan_menu import prepare_menu_for_food_scan
    try:
        menu_info = await prepare_menu_for_food_scan(dining_hall, date)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to fetch menu for food scan: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dining hall menu")

    # ── Look up Gemini cache handle (optional optimization) ──
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

    # ── Analyze with Gemini ──
    from food_scan_agent import analyze_food_photo
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

    return result
```

- [ ] **Step 2: Simplify the existing `handle_food_scan_analyze` to use `_run_analysis`**

Replace the body of `handle_food_scan_analyze` (everything inside the function after the docstring) with:

```python
    # ── 1. Validate image ──
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_data = await image.read()

    # Limit image size (4MB – Vercel free plan has a 4.5MB body limit)
    if len(image_data) > 4 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 4MB)")

    return await _run_analysis(image_data, dining_hall, date, description)
```

- [ ] **Step 3: Add the JSON-mode handler `handle_food_scan_analyze_json`**

Add this after the `handle_food_scan_upload_url` function (and before the feedback handler):

```python
# ==================== ANALYZE (JSON MODE) HANDLER ====================

IMAGE_PATH_PATTERN = re.compile(
    r"^[a-f0-9\-]+/[a-f0-9\-]+\.(jpg|jpeg|png|heic|heif|webp)$"
)


async def handle_food_scan_analyze_json(
    data,  # FoodScanAnalyzeRequest pydantic model
    current_user: dict,
    supabase_client,
) -> dict:
    """
    Handler for POST /food-scan/analyze (JSON mode — storage path).

    Downloads the image from Supabase Storage using the provided image_path,
    then runs the same Gemini analysis pipeline as the multipart handler.

    Args:
        data: FoodScanAnalyzeRequest with image_path, dining_hall, date, description
        current_user: Authenticated user dict from Depends(get_current_user)
        supabase_client: Supabase client instance

    Returns:
        dict with scan_id, identified items, and nutrition totals
    """
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in session")

    # ── 1. Validate image_path format ──
    if not IMAGE_PATH_PATTERN.match(data.image_path):
        raise HTTPException(status_code=400, detail="Invalid image_path format")

    # ── 2. Verify ownership — path must start with the authenticated user's ID ──
    path_user_id = data.image_path.split("/")[0]
    if path_user_id != str(user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    # ── 3. Download image from Supabase Storage ──
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

    # ── 4. Run shared analysis pipeline ──
    return await _run_analysis(image_data, data.dining_hall, data.date, data.description)
```

- [ ] **Step 4: Verify the file parses correctly**

Run: `python -c "from food_scan_endpoints import handle_food_scan_analyze, handle_food_scan_upload_url, handle_food_scan_analyze_json, _run_analysis; print('OK')"`

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add food_scan_endpoints.py
git commit -m "feat: add JSON-mode analyze handler and refactor shared analysis logic"
```

---

### Task 4: Wire New Endpoints in app.py

**Files:**
- Modify: `app.py:6650-6675`

- [ ] **Step 1: Update the food scan endpoints section in `app.py`**

Find the existing food scan endpoints block (around line 6650-6675) and replace it with:

```python
# ==================== FOOD SCAN ENDPOINTS ====================

from food_scan_models import FoodScanFeedbackRequest, FoodScanUploadUrlRequest, FoodScanAnalyzeRequest


@app.post('/food-scan/upload-url', summary='Get a signed URL to upload a food photo')
async def food_scan_upload_url(
    data: FoodScanUploadUrlRequest = Body(default=FoodScanUploadUrlRequest()),
    current_user=Depends(get_current_user),
):
    from food_scan_endpoints import handle_food_scan_upload_url
    return await handle_food_scan_upload_url(data.file_extension, current_user, supabase)


@app.post('/food-scan/analyze', summary='Analyze a food photo using AI')
async def food_scan_analyze(
    image: UploadFile = File(...),
    dining_hall: str = Form(...),
    date: str = Form(...),
    description: Optional[str] = Form(None),
    current_user=Depends(get_current_user),
):
    from food_scan_endpoints import handle_food_scan_analyze
    return await handle_food_scan_analyze(image, dining_hall, date, description, current_user)


@app.post('/food-scan/analyze-json', summary='Analyze a food photo from storage path')
async def food_scan_analyze_json(
    data: FoodScanAnalyzeRequest,
    current_user=Depends(get_current_user),
):
    from food_scan_endpoints import handle_food_scan_analyze_json
    return await handle_food_scan_analyze_json(data, current_user, supabase)


@app.post('/food-scan/feedback', summary='Submit feedback on food scan accuracy')
async def food_scan_feedback(
    data: FoodScanFeedbackRequest,
    current_user=Depends(get_current_user),
):
    from food_scan_endpoints import handle_food_scan_feedback
    return await handle_food_scan_feedback(data, current_user, supabase)
```

**Note:** We use a separate route `/food-scan/analyze-json` instead of overloading `/food-scan/analyze` because FastAPI cannot have two route handlers with different body types (multipart vs JSON) on the same path. The iOS app update will call `/food-scan/analyze-json` for the new flow and `/food-scan/analyze` remains unchanged for legacy clients.

- [ ] **Step 2: Verify the app imports and parses correctly**

Run: `python -c "from app import app; print('routes:', [r.path for r in app.routes if hasattr(r, 'path') and 'food-scan' in r.path])"`

Expected output should include: `/food-scan/upload-url`, `/food-scan/analyze`, `/food-scan/analyze-json`, `/food-scan/feedback`

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat: wire upload-url and analyze-json endpoints in app.py"
```

---

### Task 5: Add Tests

**Files:**
- Create: `tests/test_food_scan_storage.py`

- [ ] **Step 1: Install pytest as a dev dependency**

Run: `pip install pytest pytest-asyncio`

Note: Do NOT add pytest to `requirements.txt` — it's a dev-only dependency.

- [ ] **Step 2: Create `tests/__init__.py`**

Create an empty `tests/__init__.py`:

```python
```

- [ ] **Step 3: Create `tests/test_food_scan_storage.py`**

```python
"""Tests for Supabase Storage food scan upload URL and JSON-mode analyze."""

import re
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException

from food_scan_models import (
    ALLOWED_IMAGE_EXTENSIONS,
    FoodScanUploadUrlRequest,
    FoodScanUploadUrlResponse,
    FoodScanAnalyzeRequest,
)
from food_scan_endpoints import (
    handle_food_scan_upload_url,
    handle_food_scan_analyze_json,
    IMAGE_PATH_PATTERN,
    FOOD_SCAN_BUCKET,
)


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


class TestFoodScanUploadUrlRequest:
    def test_default_extension(self):
        req = FoodScanUploadUrlRequest()
        assert req.file_extension == "jpg"

    def test_custom_extension(self):
        req = FoodScanUploadUrlRequest(file_extension="png")
        assert req.file_extension == "png"


class TestFoodScanAnalyzeRequest:
    def test_valid_request(self):
        req = FoodScanAnalyzeRequest(
            image_path="abc123/550e8400-e29b.jpg",
            dining_hall="Busch Dining Hall",
            date="2026-04-07",
        )
        assert req.image_path == "abc123/550e8400-e29b.jpg"
        assert req.description is None

    def test_with_description(self):
        req = FoodScanAnalyzeRequest(
            image_path="abc123/550e8400-e29b.jpg",
            dining_hall="Busch Dining Hall",
            date="2026-04-07",
            description="chicken and rice",
        )
        assert req.description == "chicken and rice"


# ---------------------------------------------------------------------------
# IMAGE_PATH_PATTERN regex tests
# ---------------------------------------------------------------------------


class TestImagePathPattern:
    @pytest.mark.parametrize(
        "path",
        [
            "abc-123/550e8400-e29b-41d4-a716-446655440000.jpg",
            "abcdef/12345678-1234-1234-1234-123456789abc.png",
            "a1b2c3/deadbeef-cafe-babe-face-123456789abc.heic",
            "abc123/550e8400-e29b.webp",
        ],
    )
    def test_valid_paths(self, path):
        assert IMAGE_PATH_PATTERN.match(path), f"Should match: {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "no-slash.jpg",
            "../etc/passwd",
            "user/../other/file.jpg",
            "abc123/file.exe",
            "abc123/file.pdf",
            "ABC123/file.jpg",  # uppercase not allowed (user IDs are lowercase hex/uuid)
        ],
    )
    def test_invalid_paths(self, path):
        assert not IMAGE_PATH_PATTERN.match(path), f"Should NOT match: {path}"


# ---------------------------------------------------------------------------
# handle_food_scan_upload_url tests
# ---------------------------------------------------------------------------


class TestHandleFoodScanUploadUrl:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_supabase = MagicMock()
        mock_supabase.storage.from_.return_value.create_signed_upload_url.return_value = {
            "signed_url": "https://xyz.supabase.co/storage/v1/upload/sign/food-scan-images/test-path"
        }
        current_user = {"id": "abc123"}

        result = await handle_food_scan_upload_url("jpg", current_user, mock_supabase)

        assert "upload_url" in result
        assert "image_path" in result
        assert result["image_path"].startswith("abc123/")
        assert result["image_path"].endswith(".jpg")
        mock_supabase.storage.from_.assert_called_with(FOOD_SCAN_BUCKET)

    @pytest.mark.asyncio
    async def test_unsupported_extension(self):
        mock_supabase = MagicMock()
        current_user = {"id": "abc123"}

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_upload_url("exe", current_user, mock_supabase)
        assert exc_info.value.status_code == 400
        assert "Unsupported file extension" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_user_id(self):
        mock_supabase = MagicMock()
        current_user = {}

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_upload_url("jpg", current_user, mock_supabase)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_supabase_error(self):
        mock_supabase = MagicMock()
        mock_supabase.storage.from_.return_value.create_signed_upload_url.side_effect = Exception(
            "Connection refused"
        )
        current_user = {"id": "abc123"}

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_upload_url("jpg", current_user, mock_supabase)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_strips_dot_from_extension(self):
        mock_supabase = MagicMock()
        mock_supabase.storage.from_.return_value.create_signed_upload_url.return_value = {
            "signed_url": "https://example.com/signed"
        }
        current_user = {"id": "abc123"}

        result = await handle_food_scan_upload_url(".PNG", current_user, mock_supabase)
        assert result["image_path"].endswith(".png")


# ---------------------------------------------------------------------------
# handle_food_scan_analyze_json tests
# ---------------------------------------------------------------------------


class TestHandleFoodScanAnalyzeJson:
    @pytest.mark.asyncio
    async def test_invalid_image_path_format(self):
        data = FoodScanAnalyzeRequest(
            image_path="../etc/passwd",
            dining_hall="Busch",
            date="2026-04-07",
        )
        current_user = {"id": "abc123"}
        mock_supabase = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_analyze_json(data, current_user, mock_supabase)
        assert exc_info.value.status_code == 400
        assert "Invalid image_path format" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_access_denied_wrong_user(self):
        data = FoodScanAnalyzeRequest(
            image_path="other-user/550e8400-e29b.jpg",
            dining_hall="Busch",
            date="2026-04-07",
        )
        current_user = {"id": "abc123"}
        mock_supabase = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_analyze_json(data, current_user, mock_supabase)
        assert exc_info.value.status_code == 403
        assert "Access denied" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_image_not_found(self):
        data = FoodScanAnalyzeRequest(
            image_path="abc123/550e8400-e29b.jpg",
            dining_hall="Busch",
            date="2026-04-07",
        )
        current_user = {"id": "abc123"}
        mock_supabase = MagicMock()
        mock_supabase.storage.from_.return_value.download.side_effect = Exception(
            "Object not found (404)"
        )

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_analyze_json(data, current_user, mock_supabase)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_storage_download_error(self):
        data = FoodScanAnalyzeRequest(
            image_path="abc123/550e8400-e29b.jpg",
            dining_hall="Busch",
            date="2026-04-07",
        )
        current_user = {"id": "abc123"}
        mock_supabase = MagicMock()
        mock_supabase.storage.from_.return_value.download.side_effect = Exception(
            "Connection timeout"
        )

        with pytest.raises(HTTPException) as exc_info:
            await handle_food_scan_analyze_json(data, current_user, mock_supabase)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_success_calls_run_analysis(self):
        fake_image = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # fake JPEG bytes
        data = FoodScanAnalyzeRequest(
            image_path="abc123/550e8400-e29b.jpg",
            dining_hall="Busch Dining Hall",
            date="2026-04-07",
            description="chicken",
        )
        current_user = {"id": "abc123"}
        mock_supabase = MagicMock()
        mock_supabase.storage.from_.return_value.download.return_value = fake_image

        expected_result = {
            "success": True,
            "scan_id": "test-id",
            "items": [],
            "totals": {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
            "dining_hall": "Busch Dining Hall",
            "date": "2026-04-07",
        }

        with patch(
            "food_scan_endpoints._run_analysis",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_analysis:
            result = await handle_food_scan_analyze_json(data, current_user, mock_supabase)

        assert result == expected_result
        mock_analysis.assert_called_once_with(
            fake_image, "Busch Dining Hall", "2026-04-07", "chicken"
        )
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_food_scan_storage.py -v`

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: add unit tests for food scan storage upload and JSON analyze"
```

---

### Task 6: Update Design Spec Endpoint Path

**Files:**
- Modify: `docs/superpowers/specs/2026-04-07-supabase-storage-food-scan-design.md`

- [ ] **Step 1: Update spec to reflect `/food-scan/analyze-json` route**

During implementation in Task 4, we discovered that FastAPI cannot serve two handlers with different body types on the same route. The JSON-mode handler is registered at `/food-scan/analyze-json` instead of overloading `/food-scan/analyze`. Update the spec to reflect this:

In the spec, find the "Mode 1 — Storage path (new)" section and update the endpoint path from `POST /food-scan/analyze` to `POST /food-scan/analyze-json`. Update the sequence diagram client line from `POST /food-scan/analyze` to `POST /food-scan/analyze-json`.

Also update the "Files Affected" section to note the separate route.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-07-supabase-storage-food-scan-design.md
git commit -m "docs: update spec to reflect /food-scan/analyze-json route"
```

---

### Task 7: Manual Verification — Create Supabase Bucket

This is a manual step performed in the Supabase dashboard before deployment.

- [ ] **Step 1: Create the bucket**

1. Go to Supabase Dashboard → Storage
2. Click "New Bucket"
3. Name: `food-scan-images`
4. Public: **OFF**
5. Allowed MIME types: `image/jpeg`, `image/png`, `image/heic`, `image/heif`, `image/webp`
6. File size limit: `10485760` (10MB in bytes)

- [ ] **Step 2: Verify bucket exists**

In the Supabase SQL editor or via the API, confirm the bucket is listed:

```sql
SELECT * FROM storage.buckets WHERE id = 'food-scan-images';
```

---

### Task 8: End-to-End Smoke Test (Manual)

- [ ] **Step 1: Test upload URL generation**

```bash
curl -X POST https://your-api.vercel.app/food-scan/upload-url \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"file_extension": "jpg"}'
```

Expected: 200 with `{"upload_url": "...", "image_path": "..."}`

- [ ] **Step 2: Test image upload to signed URL**

```bash
curl -X PUT "SIGNED_URL_FROM_STEP_1" \
  -H "Content-Type: image/jpeg" \
  --data-binary @test-photo.jpg
```

Expected: 200

- [ ] **Step 3: Test JSON-mode analyze**

```bash
curl -X POST https://your-api.vercel.app/food-scan/analyze-json \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image_path": "IMAGE_PATH_FROM_STEP_1", "dining_hall": "Busch Dining Hall", "date": "2026-04-07"}'
```

Expected: 200 with scan results

- [ ] **Step 4: Test legacy multipart still works**

```bash
curl -X POST https://your-api.vercel.app/food-scan/analyze \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "image=@small-test-photo.jpg" \
  -F "dining_hall=Busch Dining Hall" \
  -F "date=2026-04-07"
```

Expected: 200 with scan results (same as before — backward compatible)
