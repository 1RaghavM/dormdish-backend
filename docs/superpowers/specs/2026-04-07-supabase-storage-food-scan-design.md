# Supabase Storage for Food Scan Image Uploads

**Date:** 2026-04-07
**Status:** Approved
**Problem:** `POST /food-scan/analyze` accepts images via multipart form upload, which hits Vercel's 4.5MB serverless function body limit (`FUNCTION_PAYLOAD_TOO_LARGE` / 413). Phone photos frequently exceed this after multipart encoding overhead.

## Solution

Replace direct image upload with a presigned URL flow through Supabase Storage. The client uploads the image directly to Supabase (bypassing Vercel), then passes the storage path to the analyze endpoint.

Backward compatibility is required: the existing multipart upload path must continue working for older app versions. The backend ships first; the iOS app update follows later.

## Architecture

### New Endpoint: `POST /food-scan/upload-url`

Generates a signed upload URL so the client can upload directly to Supabase Storage.

**Request:**
```
POST /food-scan/upload-url
Authorization: Bearer <session_token>
Content-Type: application/json

{
  "file_extension": "jpg"   // optional, defaults to "jpg"
}
```

**Server logic:**
1. Authenticate user via `get_current_user`
2. Validate `file_extension` is one of: `jpg`, `jpeg`, `png`, `heic`, `heif`, `webp` (default: `jpg`). Return 400 if unsupported.
3. Generate storage path: `{user_id}/{uuid}.{ext}`
4. Call `supabase.storage.from_("food-scan-images").create_signed_upload_url(path)`
5. Return the signed URL and path

**Response:**
```json
{
  "upload_url": "https://xyz.supabase.co/storage/v1/upload/sign/food-scan-images/...",
  "image_path": "abc123/550e8400-e29b.jpg"
}
```

The signed URL is valid for 2 hours (Supabase default). The client PUTs raw image bytes to this URL with no auth header.

### New Endpoint: `POST /food-scan/analyze-json`

Accepts the new storage-path-based JSON mode as a separate route.

**Mode 1 — Storage path (new):**
```
POST /food-scan/analyze-json
Authorization: Bearer <session_token>
Content-Type: application/json

{
  "image_path": "abc123/550e8400-e29b.jpg",
  "dining_hall": "Busch Dining Hall",
  "date": "2026-04-07",
  "description": "chicken and rice"
}
```

### Existing Endpoint: `POST /food-scan/analyze`

Continues to handle the legacy multipart upload path, unchanged.

**Mode 2 — Direct upload (legacy, unchanged):**
```
POST /food-scan/analyze
Authorization: Bearer <session_token>
Content-Type: multipart/form-data

image: <file bytes>
dining_hall: Busch Dining Hall
date: 2026-04-07
description: chicken and rice
```

**Route separation:** These are two separate routes. `/food-scan/analyze` handles the legacy multipart upload exclusively. `/food-scan/analyze-json` handles the new storage path JSON mode exclusively. This avoids content-type sniffing and keeps each route's contract clear.

**Mode 1 logic:**
1. Validate `image_path` matches pattern `{user_id}/{uuid}.{ext}`
2. Verify the `user_id` segment matches the authenticated user (prevents cross-user access)
3. Download image bytes: `supabase.storage.from_("food-scan-images").download(image_path)`
4. Proceed with existing Gemini analysis pipeline

**Mode 2 logic:** Unchanged from current implementation.

## Supabase Storage Bucket: `food-scan-images`

| Setting | Value |
|---|---|
| Name | `food-scan-images` |
| Public | No (private) |
| Allowed MIME types | `image/jpeg`, `image/png`, `image/heic`, `image/heif`, `image/webp` |
| File size limit | 10MB |
| Retention | Indefinite (kept for history, re-analysis, model training) |

**Path structure:** `{user_id}/{uuid}.{ext}` — organized by user, UUID prevents collisions.

**Access control:** No RLS policies needed. All access goes through the server using the service key. The client only interacts with storage via time-limited signed upload URLs.

The bucket must be created manually in the Supabase dashboard before deployment.

## Client-Server Sequence (New Path)

```
Client                          Backend                         Supabase Storage
  |                                |                                |
  |-- POST /food-scan/upload-url ->|                                |
  |   (Bearer token)              |-- create_signed_upload_url() -->|
  |                                |<-- signed URL + path ----------|
  |<-- { upload_url, image_path } -|                                |
  |                                |                                |
  |-- PUT upload_url ------------->|                                |
  |   (raw image bytes)           |                   (direct) --> |
  |<-- 200 OK --------------------|                                |
  |                                |                                |
  |-- POST /food-scan/analyze-json>|                                |
  |   { image_path, dining_hall,  |-- download(image_path) ------->|
  |     date, description }       |<-- image bytes ----------------|
  |                                |                                |
  |                                |-- analyze_food_photo(bytes) -->| Gemini
  |                                |<-- structured result ----------|
  |<-- { scan_id, items, totals } -|                                |
```

The PUT to the signed URL goes directly from client to Supabase Storage, bypassing Vercel entirely. The backend downloads the image (~100-200ms) only when analyzing, which is negligible next to Gemini's ~2-5s processing time.

## Error Handling

### `POST /food-scan/upload-url`

| Scenario | Status | Detail |
|---|---|---|
| Not authenticated | 401 | `"Unauthorized"` |
| Unsupported file extension | 400 | `"Unsupported file extension"` |
| Supabase Storage unreachable | 500 | `"Failed to generate upload URL"` |

### `POST /food-scan/analyze-json` (Mode 1 — storage path)

| Scenario | Status | Detail |
|---|---|---|
| `image_path` missing or malformed | 400 | `"Invalid image_path format"` |
| `image_path` user ID doesn't match authenticated user | 403 | `"Access denied"` |
| Image not found in storage | 404 | `"Image not found in storage"` |
| Image download from storage fails | 500 | `"Failed to retrieve image from storage"` |
| Gemini analysis fails | 500 | `"Food scan analysis failed. Please try again."` |

### `POST /food-scan/analyze` (Mode 2 — legacy multipart)

Unchanged from current implementation.

## Rollout Plan

1. **Backend first:** Deploy new `upload-url` endpoint and new `analyze-json` endpoint. Create `food-scan-images` bucket in Supabase dashboard.
2. **iOS app later:** Update `FoodScanService.swift` to use three-step flow (get URL → upload to storage → analyze-json with path). Changes isolated to `FoodScanService.swift`; `FoodAnalysisManager.swift` requires no structural changes.
3. Old app versions continue working via the legacy multipart path indefinitely.

## Files Affected (Backend)

- `app.py` — Register new `/food-scan/upload-url` endpoint and new `/food-scan/analyze-json` route
- `food_scan_endpoints.py` — Add `handle_food_scan_upload_url` handler; add new `handle_food_scan_analyze_json` handler for the `/food-scan/analyze-json` route (existing `handle_food_scan_analyze` remains unchanged)
- No new dependencies required (Supabase SDK already installed)
