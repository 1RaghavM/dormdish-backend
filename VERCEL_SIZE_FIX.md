# Vercel 250MB Limit Fix

## The Problem

Vercel calculates deployment size based on:
1. **Source files** from your repository (excluding `.vercelignore`)
2. **Installed Python packages** from `requirements.txt`

Even though your source files are only **0.32 MB**, the installed dependencies can easily exceed 250MB.

## What We've Done

### 1. Enhanced `.vercelignore`
- Added comprehensive patterns to exclude:
  - `.git` directory (5.1MB)
  - `.venv` directory (339MB)
  - All `__pycache__` directories
  - Test files, docs, cache files

### 2. Improved Optimization Script
- Made `optimize_dependencies.py` more aggressive:
  - Removes ALL test directories from ALL packages
  - Removes ALL documentation directories
  - Removes ALL example directories
  - Removes README and LICENSE files
  - Removes `__pycache__` directories
  - Removes unnecessary Google API discovery cache files

### 3. Enhanced Build Process
- Updated `vercel-build` to:
  - Remove `.git` directory if it exists
  - Remove `.venv` directory if it exists
  - Remove all `__pycache__` directories
  - Run optimization script

## How to Verify

1. **Check what's being included:**
   ```bash
   python check_deployment_size.py
   ```

2. **Build locally with Vercel CLI:**
   ```bash
   vercel build
   ```
   Then inspect `.vercel/output` to see what's actually being included.

3. **Check Vercel build logs:**
   - Look for the optimization script output
   - Check if it's finding and cleaning site-packages

## If Still Over 250MB

The issue is likely that **installed dependencies are too large** even after optimization. Consider:

1. **Remove unnecessary dependencies** from `requirements.txt`
2. **Use lighter alternatives** for large packages
3. **Split functionality** into separate serverless functions
4. **Use Vercel's Edge Functions** for lightweight operations
5. **Move large data** to external storage (Supabase, S3, etc.)

## Key Packages That Might Be Large

- `google-api-python-client` - Can be 50-100MB
- `mem0ai` - Can be large
- `supabase` - Can be large
- `fastapi` + dependencies - Can be 20-30MB
- `google-genai` - Can be large

Consider if all of these are necessary, or if you can use lighter alternatives.
