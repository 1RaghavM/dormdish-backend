
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
    """Delete menu records for the specified date and associated user meal plans."""

    
    load_dotenv()

    
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_KEY') or os.getenv('SUPABASE_SERVICE_KEY')

    if not supabase_url or not supabase_key:
        print('❌ Supabase credentials not found in environment variables')
        print('   Make sure SUPABASE_URL and SUPABASE_KEY or SUPABASE_SERVICE_KEY are set in your .env file / GitHub Secrets')
        exit(1)

    try:
        
        supabase = create_client(supabase_url, supabase_key)

        if dry_run:
            print(f"🔍 DRY RUN: Checking what would be deleted for date {target_date}")

            
            menus = supabase.table('menus').select('id, university, dining_hall, date').eq('date', target_date).execute()
            menu_count = len(menus.data or [])
            print(f"📋 Menus that would be deleted: {menu_count}")
            for record in menus.data or []:
                print(f"   - {record['university']}/{record['dining_hall']}")

            
            plans = supabase.table('user_meal_plans').select('user_id, date, calories, protein_g').eq('date', target_date).execute()
            plans_count = len(plans.data or [])
            print(f"📋 User meal plans that would be deleted: {plans_count}")
            for record in plans.data or []:
                print(f"   - user_id={record['user_id']} calories={record.get('calories')} protein_g={record.get('protein_g')}")
        else:
            print(f"🗑️  Deleting menu records for date {target_date}")

            
            deleted_menus = supabase.table('menus').delete().eq('date', target_date).execute()
            deleted_menu_count = len(deleted_menus.data or [])
            print(f"✅ Deleted {deleted_menu_count} menu records from {target_date}")

            
            print(f"🗑️  Deleting user meal plans for date {target_date}")
            deleted_plans = supabase.table('user_meal_plans').delete().eq('date', target_date).execute()
            deleted_plan_count = len(deleted_plans.data or [])
            print(f"✅ Deleted {deleted_plan_count} user meal plans from {target_date}")

    except Exception as e:
        print(f"❌ Error during cleanup: {e}")
        exit(1)

def main():
    parser = argparse.ArgumentParser(description="Clean up old menu records and user meal plans from Supabase")
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
    
    
    if args.date:
        target_date = args.date
        
        try:
            datetime.strptime(target_date, '%Y-%m-%d')
        except ValueError:
            print(f"❌ Invalid date format: {target_date}. Use YYYY-MM-DD format.")
            exit(1)
    else:
        
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    
    print(f"🧹 Cleanup Script")
    print(f"📅 Target date: {target_date}")
    print(f"🔄 Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("-" * 50)
    
    cleanup_menus(target_date, args.dry_run)

if __name__ == "__main__":
    main()