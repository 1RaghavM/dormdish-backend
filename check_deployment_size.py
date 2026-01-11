#!/usr/bin/env python3
"""
Script to check what files would be included in Vercel deployment.
This helps diagnose the 250MB limit issue.
"""

import os
from pathlib import Path
from collections import defaultdict

# Patterns from .vercelignore that should be excluded
EXCLUDE_PATTERNS = [
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".cache",
    "cache",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.log",
    "*.md",
    "tests",
    ".vscode",
    ".idea",
    ".env",
    "build",
    "dist",
    ".github",
]

def should_exclude(path: Path) -> bool:
    """Check if a path should be excluded based on .vercelignore patterns."""
    path_str = str(path)
    
    # Check if any part of the path matches exclude patterns
    for pattern in EXCLUDE_PATTERNS:
        if pattern in path_str:
            # Special handling for patterns with wildcards
            if pattern.startswith("*"):
                if path.name.endswith(pattern[1:]):
                    return True
            elif path.name == pattern or pattern in path.parts:
                return True
    
    return False

def get_file_size(path: Path) -> int:
    """Get file size in bytes."""
    try:
        return path.stat().st_size
    except (OSError, FileNotFoundError):
        return 0

def main():
    """Main function to analyze deployment size."""
    root = Path(".")
    
    total_size = 0
    included_files = []
    excluded_files = []
    size_by_type = defaultdict(int)
    size_by_directory = defaultdict(int)
    
    print("🔍 Analyzing files for Vercel deployment...")
    print("=" * 60)
    
    for root_path, dirs, files in os.walk(root):
        # Skip excluded directories early
        root_path_obj = Path(root_path)
        if should_exclude(root_path_obj):
            dirs[:] = []  # Don't traverse into excluded directories
            continue
        
        for file in files:
            file_path = root_path_obj / file
            
            if should_exclude(file_path):
                excluded_files.append(file_path)
                continue
            
            size = get_file_size(file_path)
            total_size += size
            
            included_files.append((file_path, size))
            
            # Track by extension
            ext = file_path.suffix or "no extension"
            size_by_type[ext] += size
            
            # Track by directory
            rel_dir = str(file_path.parent.relative_to(root))
            size_by_directory[rel_dir] += size
    
    # Sort by size
    included_files.sort(key=lambda x: x[1], reverse=True)
    
    print(f"\n📊 Total size of included files: {total_size / (1024*1024):.2f} MB")
    print(f"📁 Total files included: {len(included_files)}")
    print(f"🚫 Total files excluded: {len(excluded_files)}")
    
    print("\n📈 Top 20 largest files:")
    print("-" * 60)
    for file_path, size in included_files[:20]:
        print(f"  {size / 1024:8.2f} KB  {file_path}")
    
    print("\n📦 Size by file type:")
    print("-" * 60)
    for ext, size in sorted(size_by_type.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {ext:20} {size / (1024*1024):8.2f} MB")
    
    print("\n📁 Size by directory:")
    print("-" * 60)
    for dir_path, size in sorted(size_by_directory.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {dir_path:40} {size / (1024*1024):8.2f} MB")
    
    if total_size > 250 * 1024 * 1024:
        print(f"\n⚠️  WARNING: Total size ({total_size / (1024*1024):.2f} MB) exceeds 250MB limit!")
        print("   Check .vercelignore to ensure large files are excluded.")
    else:
        print(f"\n✅ Total size ({total_size / (1024*1024):.2f} MB) is under 250MB limit.")

if __name__ == "__main__":
    main()
