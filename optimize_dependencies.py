"""
Post-install script to optimize dependencies for Vercel deployment.
This script removes unnecessary files from installed packages to reduce deployment size.

Run this after pip install in Vercel build process.
"""

import os
import shutil
from pathlib import Path
import site


def find_site_packages():
    """Find site-packages directory where dependencies are installed."""
    # In Vercel, dependencies are installed in a specific location
    # Try common locations
    possible_paths = [
        Path("/var/task"),  # Vercel's function directory
        Path.cwd() / ".venv" / "lib" / f"python{site.getsitepackages()[0].split('/')[-2] if site.getsitepackages() else '3.11'}" / "site-packages",
    ]
    
    # Add actual site-packages
    for sp in site.getsitepackages():
        possible_paths.append(Path(sp))
    
    # Check if we're in Vercel build environment
    vercel_env = os.getenv("VERCEL")
    if vercel_env:
        # In Vercel, packages might be in /var/task or current directory
        possible_paths.insert(0, Path("/var/task"))
        possible_paths.insert(1, Path.cwd())
    
    for sp_path in possible_paths:
        if sp_path.exists() and sp_path.is_dir():
            # Check if it looks like site-packages (has common packages)
            if any((sp_path / pkg).exists() for pkg in ["fastapi", "googleapiclient", "supabase"]):
                return sp_path
    
    # Fallback: use first site-packages
    if site.getsitepackages():
        return Path(site.getsitepackages()[0])
    
    return None


def remove_unnecessary_discovery_cache(site_packages: Path):
    """Remove discovery cache files for APIs we don't use."""
    discovery_cache_path = site_packages / "googleapiclient" / "discovery_cache" / "documents"
    
    if not discovery_cache_path.exists():
        print("⚠️  Could not find googleapiclient discovery cache directory")
        return 0
    
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
    
    if removed_count > 0:
        print(f"✅ Removed {removed_count} discovery cache files ({total_size / (1024*1024):.2f} MB)")
        print(f"✅ Kept calendar.v3.json for Google Calendar API")
    
    return total_size


def remove_package_docs_and_tests(site_packages: Path):
    """Remove documentation, tests, and other unnecessary files from packages."""
    total_saved = 0
    removed_dirs = 0
    
    # Directories to remove from each package
    dirs_to_remove = [
        "__pycache__",
        "*.pyc",
        "*.pyo",
        "tests",
        "test",
        "tests.py",
        "docs",
        "doc",
        ".pytest_cache",
        ".tox",
        "*.egg-info",
    ]
    
    # Files to remove
    files_to_remove = [
        "*.pyc",
        "*.pyo",
        "*.pyd",
        "*.so.dbg",
        "*.dist-info",
    ]
    
    # Packages to clean (focus on large ones)
    packages_to_clean = [
        "googleapiclient",
        "google",
        "google_auth",
        "google_auth_oauthlib",
        "supabase",
        "mem0ai",
        "google_genai",
        "fastapi",
        "pydantic",
    ]
    
    for package_name in packages_to_clean:
        package_path = site_packages / package_name
        if not package_path.exists():
            continue
        
        # Remove test directories
        for test_dir in ["tests", "test", "__tests__"]:
            test_path = package_path / test_dir
            if test_path.exists() and test_path.is_dir():
                size = sum(f.stat().st_size for f in test_path.rglob('*') if f.is_file())
                shutil.rmtree(test_path)
                total_saved += size
                removed_dirs += 1
        
        # Remove docs directories
        for doc_dir in ["docs", "doc", "documentation"]:
            doc_path = package_path / doc_dir
            if doc_path.exists() and doc_path.is_dir():
                size = sum(f.stat().st_size for f in doc_path.rglob('*') if f.is_file())
                shutil.rmtree(doc_path)
                total_saved += size
                removed_dirs += 1
    
    if removed_dirs > 0:
        print(f"✅ Removed {removed_dirs} unnecessary directories ({total_saved / (1024*1024):.2f} MB)")
    
    return total_saved


def remove_pycache(site_packages: Path):
    """Remove all __pycache__ directories."""
    total_saved = 0
    removed_count = 0
    
    for pycache_dir in site_packages.rglob("__pycache__"):
        if pycache_dir.is_dir():
            size = sum(f.stat().st_size for f in pycache_dir.rglob('*') if f.is_file())
            shutil.rmtree(pycache_dir)
            total_saved += size
            removed_count += 1
    
    if removed_count > 0:
        print(f"✅ Removed {removed_count} __pycache__ directories ({total_saved / (1024*1024):.2f} MB)")
    
    return total_saved


def main():
    """Main optimization function."""
    print("🔧 Optimizing dependencies for Vercel deployment...")
    
    site_packages = find_site_packages()
    if not site_packages:
        print("⚠️  Could not find site-packages directory")
        print("   Trying to optimize in current directory...")
        site_packages = Path.cwd()
    
    print(f"📦 Found site-packages: {site_packages}")
    
    total_saved = 0
    
    # Remove Google API discovery cache (except Calendar)
    saved = remove_unnecessary_discovery_cache(site_packages)
    total_saved += saved
    
    # Remove __pycache__ directories
    saved = remove_pycache(site_packages)
    total_saved += saved
    
    # Remove docs and tests
    saved = remove_package_docs_and_tests(site_packages)
    total_saved += saved
    
    print(f"\n✅ Total space saved: {total_saved / (1024*1024):.2f} MB")
    print("✅ Optimization complete!")


if __name__ == "__main__":
    main()
