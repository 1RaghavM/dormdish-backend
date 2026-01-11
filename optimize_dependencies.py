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
        "documentation",
        ".pytest_cache",
        ".tox",
        "*.egg-info",
        "examples",
        "example",
        "samples",
        "sample",
        "benchmarks",
        "benchmark",
    ]
    
    # Files to remove
    files_to_remove = [
        "*.pyc",
        "*.pyo",
        "*.pyd",
        "*.so.dbg",
        "*.dist-info",
        "*.md",
        "*.txt",
        "*.rst",
        "*.LICENSE",
        "*.license",
    ]
    
    # Clean ALL packages, not just specific ones
    print("🧹 Cleaning all packages in site-packages...")
    
    for item in site_packages.iterdir():
        if not item.is_dir():
            continue
        
        # Skip if it's a dist-info or egg-info directory
        if item.name.endswith(('.dist-info', '.egg-info')):
            try:
                size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                shutil.rmtree(item)
                total_saved += size
                removed_dirs += 1
            except Exception as e:
                print(f"⚠️  Could not remove {item.name}: {e}")
            continue
        
        package_path = item
        
        # Remove test directories
        for test_dir in ["tests", "test", "__tests__", "testing"]:
            test_path = package_path / test_dir
            if test_path.exists() and test_path.is_dir():
                try:
                    size = sum(f.stat().st_size for f in test_path.rglob('*') if f.is_file())
                    shutil.rmtree(test_path)
                    total_saved += size
                    removed_dirs += 1
                except Exception as e:
                    print(f"⚠️  Could not remove {test_path}: {e}")
        
        # Remove docs directories
        for doc_dir in ["docs", "doc", "documentation", "Documentation"]:
            doc_path = package_path / doc_dir
            if doc_path.exists() and doc_path.is_dir():
                try:
                    size = sum(f.stat().st_size for f in doc_path.rglob('*') if f.is_file())
                    shutil.rmtree(doc_path)
                    total_saved += size
                    removed_dirs += 1
                except Exception as e:
                    print(f"⚠️  Could not remove {doc_path}: {e}")
        
        # Remove example directories
        for example_dir in ["examples", "example", "samples", "sample"]:
            example_path = package_path / example_dir
            if example_path.exists() and example_path.is_dir():
                try:
                    size = sum(f.stat().st_size for f in example_path.rglob('*') if f.is_file())
                    shutil.rmtree(example_path)
                    total_saved += size
                    removed_dirs += 1
                except Exception as e:
                    print(f"⚠️  Could not remove {example_path}: {e}")
        
        # Remove README and license files
        for readme_file in package_path.glob("README*"):
            try:
                size = readme_file.stat().st_size
                readme_file.unlink()
                total_saved += size
            except Exception:
                pass
        
        for license_file in package_path.glob("*LICENSE*"):
            try:
                size = license_file.stat().st_size
                license_file.unlink()
                total_saved += size
            except Exception:
                pass
    
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


def remove_large_unused_packages(site_packages: Path):
    """Remove large packages that might not be needed."""
    total_saved = 0
    
    # Packages that are often large but might have unused parts
    # Be careful - only remove if we're sure they're not needed
    packages_to_check = [
        "googleapiclient/discovery_cache/documents",
    ]
    
    for pkg_path_str in packages_to_check:
        pkg_path = site_packages / pkg_path_str
        if pkg_path.exists():
            # Already handled by remove_unnecessary_discovery_cache
            continue
    
    return total_saved


def analyze_package_sizes(site_packages: Path):
    """Analyze which packages are taking up the most space."""
    package_sizes = []
    
    if not site_packages.exists():
        return
    
    for item in site_packages.iterdir():
        if item.is_dir() and not item.name.endswith(('.dist-info', '.egg-info')):
            try:
                size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                if size > 1024 * 1024:  # Only show packages > 1MB
                    package_sizes.append((item.name, size))
            except Exception:
                pass
    
    if package_sizes:
        package_sizes.sort(key=lambda x: x[1], reverse=True)
        print("\n📊 Largest packages (after optimization):")
        print("-" * 60)
        for name, size in package_sizes[:10]:
            print(f"  {name:40} {size / (1024*1024):8.2f} MB")


def main():
    """Main optimization function."""
    print("🔧 Optimizing dependencies for Vercel deployment...")
    print("=" * 60)
    
    # Try multiple locations
    site_packages = find_site_packages()
    if not site_packages:
        print("⚠️  Could not find site-packages directory")
        print("   Trying to optimize in current directory...")
        site_packages = Path.cwd()
    
    print(f"📦 Found site-packages: {site_packages}")
    
    # Calculate initial size
    if site_packages.exists():
        initial_size = sum(f.stat().st_size for f in site_packages.rglob('*') if f.is_file())
        print(f"📊 Initial site-packages size: {initial_size / (1024*1024):.2f} MB")
    
    # Also optimize current directory
    current_dir = Path.cwd()
    print(f"📁 Also optimizing current directory: {current_dir}")
    
    total_saved = 0
    
    # Remove Google API discovery cache (except Calendar)
    print("\n1️⃣ Removing unnecessary Google API discovery cache...")
    saved = remove_unnecessary_discovery_cache(site_packages)
    total_saved += saved
    
    # Remove __pycache__ directories
    print("\n2️⃣ Removing __pycache__ directories...")
    saved = remove_pycache(site_packages)
    total_saved += saved
    
    # Also remove from current directory
    if current_dir != site_packages:
        for pycache_dir in current_dir.rglob("__pycache__"):
            if pycache_dir.is_dir():
                try:
                    size = sum(f.stat().st_size for f in pycache_dir.rglob('*') if f.is_file())
                    shutil.rmtree(pycache_dir)
                    total_saved += size
                except Exception:
                    pass
    
    # Remove docs and tests
    print("\n3️⃣ Removing documentation, tests, and examples...")
    saved = remove_package_docs_and_tests(site_packages)
    total_saved += saved
    
    print("\n" + "=" * 60)
    print(f"✅ Total space saved: {total_saved / (1024*1024):.2f} MB")
    
    # Calculate final size
    if site_packages.exists():
        final_size = sum(f.stat().st_size for f in site_packages.rglob('*') if f.is_file())
        print(f"📊 Final site-packages size: {final_size / (1024*1024):.2f} MB")
        
        if final_size > 250 * 1024 * 1024:
            print(f"\n⚠️  WARNING: Still over 250MB limit!")
            print("   Consider removing unnecessary dependencies or using lighter alternatives.")
            analyze_package_sizes(site_packages)
        else:
            print(f"\n✅ Under 250MB limit!")
    
    print("✅ Optimization complete!")


if __name__ == "__main__":
    main()
