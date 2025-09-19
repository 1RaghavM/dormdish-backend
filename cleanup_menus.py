#!/usr/bin/env python3
"""
Daily Menu Cleanup Script

This script deletes menu records from the previous day to keep the database clean.
It's designed to run automatically via GitHub Actions but can also be run manually.

Usage:
    python cleanup_menus.py [--date YYYY-MM-DD] [--dry-run]

Options:
    --date: Specific date to clean up (default: yesterday)
    --dry-run: Show what would be deleted without actually deleting
"""

import os
import argparse
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from supabase import create_client
    from dotenv import load_dotenv
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    print("❌ Required packages not installed. Run: pip install supabase python-dotenv")
    exit(1)

def cleanup_menus(target_date: str, dry_run: bool = False) -> None:
    """Delete menu records for the specified date."""
    
    # Load environment variables
    load_dotenv()
    
    # Get Supabase credentials
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_KEY')
    
    if not supabase_url or not supabase_key:
        print('❌ Supabase credentials not found in environment variables')
        print('   Make sure SUPABASE_URL and SUPABASE_KEY are set in your .env file')
        exit(1)
    
    try:
        # Initialize Supabase client
        supabase = create_client(supabase_url, supabase_key)
        
        if dry_run:
            print(f"🔍 DRY RUN: Checking what would be deleted for date {target_date}")
            
            # Query to see what would be deleted
            result = supabase.table('menus').select('*').eq('date', target_date).execute()
            
            if result.data:
                print(f"📋 Found {len(result.data)} menu records that would be deleted:")
                for record in result.data:
                    print(f"   - {record['university']}/{record['dining_hall']} ({record['item_count']} items)")
            else:
                print(f"ℹ️  No menu records found for {target_date}")
        else:
            print(f"🗑️  Deleting menu records for date {target_date}")
            
            # Delete records for the target date
            result = supabase.table('menus').delete().eq('date', target_date).execute()
            
            if result.data:
                deleted_count = len(result.data)
                total_items = sum(record.get('item_count', 0) for record in result.data)
                print(f"✅ Successfully deleted {deleted_count} menu records ({total_items} total items) from {target_date}")
                
                # Show what was deleted
                for record in result.data:
                    print(f"   - {record['university']}/{record['dining_hall']} ({record['item_count']} items)")
            else:
                print(f"ℹ️  No menu records found for {target_date}")
                
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")
        exit(1)

def main():
    parser = argparse.ArgumentParser(description="Clean up old menu records from Supabase")
    parser.add_argument(
        '--date', 
        type=str, 
        help='Specific date to clean up in YYYY-MM-DD format (default: yesterday)'
    )
    parser.add_argument(
        '--dry-run', 
        action='store_true', 
        help='Show what would be deleted without actually deleting'
    )
    
    args = parser.parse_args()
    
    # Determine target date
    if args.date:
        target_date = args.date
        # Validate date format
        try:
            datetime.strptime(target_date, '%Y-%m-%d')
        except ValueError:
            print(f"❌ Invalid date format: {target_date}. Use YYYY-MM-DD format.")
            exit(1)
    else:
        # Default to yesterday
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    
    print(f"🧹 Menu Cleanup Script")
    print(f"📅 Target date: {target_date}")
    print(f"🔄 Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("-" * 50)
    
    cleanup_menus(target_date, args.dry_run)

if __name__ == "__main__":
    main()