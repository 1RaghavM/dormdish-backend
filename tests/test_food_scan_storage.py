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
            "ABC123/file.jpg",
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
            image_path="def456/550e8400-e29b.jpg",
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
        fake_image = b"\xff\xd8\xff\xe0" + b"\x00" * 100
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
