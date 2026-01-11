# Root Cause Analysis: Vercel 250MB Deployment Size Error

## The Problem

Vercel serverless functions have a **250MB unzipped size limit**. Your deployment exceeds this limit.

## Root Cause

**`.vercelignore` only excludes source files from your repository** - it does NOT exclude packages that Vercel installs during the build process.

### How Vercel Builds Python Projects:

1. Vercel automatically detects Python projects
2. It installs ALL dependencies from `requirements.txt` into site-packages
3. **ALL installed packages count towards the 250MB limit**
4. `.vercelignore` cannot exclude these installed packages

### Why Your Deployment is Over 250MB:

The installed dependencies themselves are too large:

1. **`google-api-python-client` (~78MB)**
   - Includes discovery cache files for ALL Google APIs
   - You only need Calendar API
   - Contains ~200+ JSON files for APIs you don't use

2. **Other large dependencies:**
   - `google-genai` (~50-70MB)
   - `mem0ai` (~30-50MB)
   - `supabase` (~20-30MB)
   - `fastapi` + `uvicorn` + dependencies (~30-40MB)
   - **Total: ~200-250MB+**

3. **Additional bloat:**
   - `__pycache__` directories
   - Test files in packages
   - Documentation files
   - Unused discovery cache files

## The Solution

### 1. Enhanced Optimization Script

I've updated `optimize_dependencies.py` to:
- Remove Google API discovery cache files (except Calendar API) - saves ~70MB
- Remove `__pycache__` directories - saves ~10-20MB
- Remove test and documentation files from packages - saves ~20-30MB
- **Total potential savings: ~100-120MB**

### 2. Updated Vercel Configuration

Updated `vercel.json` to use `buildCommand` that:
- Runs the optimization script AFTER Vercel installs dependencies
- Vercel automatically installs from `requirements.txt`, then runs `buildCommand`

### 3. Additional Recommendations

If still over limit after optimization:

#### Option A: Replace `google-api-python-client` with Direct REST API
- Replace `google-api-python-client` (~78MB) with direct HTTP calls
- Use `httpx` or `requests` (already in dependencies)
- Saves ~78MB
- Requires refactoring `google_calendar_service.py`

#### Option B: Split into Multiple Functions
- Break into smaller functions:
  - `/api/auth/*` - Authentication
  - `/api/meal-plan/*` - Meal planning
  - `/api/calendar/*` - Calendar integration
- Each function can have different dependencies

#### Option C: Use Alternative Platform
- AWS Lambda (512MB limit)
- Google Cloud Functions (10GB limit)
- Railway, Render, Fly.io (no strict limits)

## Testing the Fix

1. Deploy to Vercel
2. Check build logs for optimization script output
3. Verify deployment size is under 250MB
4. Test all endpoints to ensure nothing broke

## Alternative: If `installCommand` Doesn't Work

Vercel might not support `installCommand` for Python projects. If the optimization script doesn't run automatically, you have two options:

### Option 1: Create a `vercel-build` Script

Create a file named `vercel-build` (no extension) in the root directory:

```bash
#!/bin/bash
pip install -r requirements.txt
python optimize_dependencies.py
```

Make it executable:
```bash
chmod +x vercel-build
```

### Option 2: Use Build Command in Project Settings

In Vercel dashboard:
1. Go to Project Settings → Build & Development Settings
2. Set "Build Command" to: `pip install -r requirements.txt && python optimize_dependencies.py`
3. Set "Install Command" to: (leave empty or remove)

### Option 3: Post-Install Hook (if using pip)

If Vercel uses pip directly, you might need to create a `setup.py` or use pip's post-install hooks, but this is more complex.

## Files Modified

- ✅ `vercel.json` - Added `buildCommand` to run optimization after dependency installation
- ✅ `optimize_dependencies.py` - Enhanced to remove more unnecessary files (saves ~100-120MB)
- ✅ `vercel-build` - Backup build script (executable)
- ✅ `.vercelignore` - Already properly configured (but only helps with source files)

## Next Steps

1. **Deploy and test** - See if optimization brings size under 250MB
2. **If still over limit** - Implement Option A (replace google-api-python-client)
3. **Monitor** - Check Vercel build logs to see actual savings
