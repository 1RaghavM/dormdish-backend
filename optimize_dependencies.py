"""
Post-install script to optimize dependencies for Vercel deployment.
This script removes unnecessary Google API discovery cache files to reduce size.

Run this after pip install in Vercel build process.
"""

import os
import shutil
from pathlib import Path


def remove_unnecessary_discovery_cache():
    """Remove discovery cache files for APIs we don't use."""
    # We only need Calendar API, so we can remove cache for other APIs
    discovery_cache_path = Path("googleapiclient/discovery_cache/documents")
    
    if not discovery_cache_path.exists():
        # Try to find it in site-packages
        import site
        for site_packages in site.getsitepackages():
            cache_path = Path(site_packages) / "googleapiclient/discovery_cache/documents"
            if cache_path.exists():
                discovery_cache_path = cache_path
                break
        else:
            print("⚠️  Could not find googleapiclient discovery cache directory")
            return
    
    # Keep only calendar.v3.json
    files_to_keep = {"calendar.v3.json"}
    
    removed_count = 0
    total_size = 0
    
    for cache_file in discovery_cache_path.glob("*.json"):
        if cache_file.name not in files_to_keep:
            size = cache_file.stat().st_size
            total_size += size
            cache_file.unlink()
            removed_count += 1
    
    print(f"✅ Removed {removed_count} discovery cache files ({total_size / (1024*1024):.2f} MB)")
    print(f"✅ Kept calendar.v3.json for Google Calendar API")


if __name__ == "__main__":
    remove_unnecessary_discovery_cache()
