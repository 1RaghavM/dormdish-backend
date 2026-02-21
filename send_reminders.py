"""
Daily Meal Plan Reminder Script

Sends push notifications to users who haven't created their meal plan for today.
Designed to run via GitHub Actions cron but can be run manually.

Usage:
    python send_reminders.py [--dry-run]

Options:
    --dry-run: Show what would be sent without actually sending notifications
"""
import os
import sys
import argparse
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from supabase import create_client


def send_push_notification(expo_push_token: str, title: str, body: str, data: dict = None, priority: str = "high") -> dict:
    """Send a push notification via Expo's push notification service."""
    if not expo_push_token or not expo_push_token.startswith('ExponentPushToken['):
        print(f"  Invalid token format: {expo_push_token[:30] if expo_push_token else 'None'}...")
        return {"success": False, "error": "Invalid token format"}

    message = {
        'to': expo_push_token,
        'sound': 'default',
        'title': title,
        'body': body,
        'data': data or {},
        'priority': priority,
        'channelId': 'default',
    }

    try:
        response = httpx.post(
            'https://exp.host/--/api/v2/push/send',
            json=message,
            headers={
                'Accept': 'application/json',
                'Accept-encoding': 'gzip, deflate',
                'Content-Type': 'application/json',
            },
            timeout=15.0,
        )
        result = response.json()
        return {"success": True, "result": result}
    except Exception as e:
        print(f"  Failed to send push notification: {e}")
        return {"success": False, "error": str(e)}


def get_user_push_token(supabase_client, user_id: str) -> str | None:
    """Get the most recent active push token for a user."""
    try:
        response = supabase_client.table('push_tokens')\
            .select('token')\
            .eq('user_id', user_id)\
            .eq('active', True)\
            .order('updated_at', desc=True)\
            .limit(1)\
            .execute()

        if response.data and len(response.data) > 0:
            return response.data[0]['token']
        return None
    except Exception as e:
        print(f"  Error fetching push token for user {user_id}: {e}")
        return None


def check_user_has_meal_plan_today(supabase_client, user_id: str) -> bool:
    """Check if user has created a meal plan for today (Eastern Time)."""
    try:
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        response = supabase_client.table('saved_meal_plans')\
            .select('id')\
            .eq('user_id', user_id)\
            .eq('date', today)\
            .limit(1)\
            .execute()
        return len(response.data) > 0
    except Exception as e:
        print(f"  Error checking meal plan for user {user_id}: {e}")
        return False


def send_meal_plan_reminder(supabase_client, user_id: str, dry_run: bool = False) -> str:
    """Send reminder to a single user. Returns 'sent', 'skipped', or 'failed'."""
    has_plan = check_user_has_meal_plan_today(supabase_client, user_id)
    if has_plan:
        return "skipped"  # Already has plan

    push_token = get_user_push_token(supabase_client, user_id)
    if not push_token:
        return "skipped"  # No token

    if dry_run:
        print(f"  [DRY RUN] Would send reminder to user {user_id[:8]}...")
        return "sent"

    result = send_push_notification(
        expo_push_token=push_token,
        title="\U0001f37d\ufe0f Time to Plan Your Meals!",
        body="Haven't created your meal plan for today yet. Tap here to get started!",
        data={"screen": "home", "action": "create_meal_plan"}
    )
    return "sent" if result.get('success') else "failed"


def main():
    parser = argparse.ArgumentParser(description="Send daily meal plan reminder notifications")
    parser.add_argument('--dry-run', action='store_true', help='Show what would be sent without actually sending')
    args = parser.parse_args()

    load_dotenv()

    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_KEY') or os.getenv('SUPABASE_SERVICE_KEY')
    if not supabase_url or not supabase_key:
        print('Error: SUPABASE_URL and SUPABASE_KEY/SUPABASE_SERVICE_KEY must be set')
        sys.exit(1)

    supabase_client = create_client(supabase_url, supabase_key)
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    print("Daily Reminder Script")
    print(f"Date: {today}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("-" * 50)

    # Get all users with active push tokens
    try:
        response = supabase_client.table('push_tokens')\
            .select('user_id')\
            .eq('active', True)\
            .execute()
    except Exception as e:
        print(f"Error querying push tokens: {e}")
        sys.exit(1)

    if not response.data:
        print("No active push tokens found")
        return

    user_ids = list(set(token['user_id'] for token in response.data))
    print(f"Found {len(user_ids)} users with active tokens")
    print()

    sent = 0
    skipped = 0
    failed = 0

    for user_id in user_ids:
        result = send_meal_plan_reminder(supabase_client, user_id, args.dry_run)
        if result == "sent":
            sent += 1
        elif result == "skipped":
            skipped += 1
        else:
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Done: {sent} sent, {skipped} skipped, {failed} failed (of {len(user_ids)} users)")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
