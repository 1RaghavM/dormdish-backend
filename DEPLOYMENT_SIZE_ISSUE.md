# Deployment Size Issue - 250MB Limit

## Problem
Vercel serverless functions have a 250MB unzipped size limit. Our deployment exceeds this due to large dependencies.

## Current Status
- ✅ Excluded `.venv/` (416MB) via `.vercelignore`
- ✅ Excluded `.git/`, test files, CSV file
- ✅ Removed unused `google-auth-httplib2` dependency
- ❌ Still over 250MB due to installed dependencies during build

## Main Culprits

### 1. `google-api-python-client` (~78MB)
- Includes discovery cache files for ALL Google APIs
- We only need Calendar API
- **Solution**: Refactor to use direct REST API calls (see below)

### 2. Other Dependencies
- `google-genai`, `mem0ai`, `supabase`, etc. add up to ~150-200MB

## Solutions

### Option 1: Refactor to Direct REST API (Recommended)
Replace `google-api-python-client` with direct HTTP calls to Google Calendar API.

**Pros:**
- Saves ~78MB
- More control over API calls
- Faster (no discovery cache overhead)

**Cons:**
- Requires refactoring `google_calendar_service.py`
- Need to manually implement API endpoints

### Option 2: Split Functions
Break the application into smaller functions:
- `/api/auth/*` - Authentication endpoints
- `/api/meal-plan/*` - Meal planning endpoints  
- `/api/calendar/*` - Calendar integration endpoints

**Pros:**
- Each function stays under 250MB
- Better separation of concerns

**Cons:**
- More complex deployment
- Potential code duplication

### Option 3: Alternative Deployment
Use a platform without size limits:
- AWS Lambda (512MB limit)
- Google Cloud Functions (10GB limit)
- Railway, Render, Fly.io

## Immediate Next Steps

1. **Test current deployment** - Verify `.vercelignore` is working
2. **If still over limit**, implement Option 1 (REST API refactor)
3. **Monitor dependency sizes** - Use `pip show <package>` to check sizes

## Files Modified
- `.vercelignore` - Excludes unnecessary files
- `vercel.json` - Deployment configuration
- `requirements.txt` - Removed `google-auth-httplib2`
- `upload_universities.py` - Moved CSV to Supabase
- `app.py` - Updated to fetch universities from Supabase
