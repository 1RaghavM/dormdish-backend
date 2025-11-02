"""
Google Calendar Integration Service for DormDish
Handles OAuth, event reading, time slot detection, and meal plan exports
"""

import datetime
import os
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Google Calendar API scopes
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Default meal time windows (can be customized per user)
DEFAULT_MEAL_WINDOWS = {
    'breakfast': {'start_hour': 7, 'end_hour': 10, 'duration_minutes': 30},
    'lunch': {'start_hour': 11, 'end_hour': 14, 'duration_minutes': 45},
    'dinner': {'start_hour': 17, 'end_hour': 20, 'duration_minutes': 60}
}


class GoogleCalendarService:
    """Service for interacting with Google Calendar API"""
    
    def __init__(self):
        # Get redirect URI from env, default to localhost for development
        # For production, set GOOGLE_REDIRECT_URI to your deployed backend URL
        redirect_uri = os.getenv(
            "GOOGLE_REDIRECT_URI", 
            "http://localhost:8000/calendar/auth/callback"
        )
        
        self.client_config = {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "redirect_uris": [redirect_uri],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
    
    def create_authorization_url(self, state: str) -> str:
        """
        Create an authorization URL for OAuth flow
        
        Args:
            state: A random state string for CSRF protection
            
        Returns:
            Authorization URL for user to visit
        """
        flow = Flow.from_client_config(
            self.client_config,
            scopes=SCOPES,
            redirect_uri=self.client_config["web"]["redirect_uris"][0]
        )
        flow.state = state
        authorization_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        return authorization_url
    
    def exchange_code_for_credentials(self, code: str, state: str) -> Dict:
        """
        Exchange authorization code for credentials
        
        Args:
            code: Authorization code from OAuth callback
            state: State string for verification
            
        Returns:
            Dictionary with credential data
        """
        flow = Flow.from_client_config(
            self.client_config,
            scopes=SCOPES,
            redirect_uri=self.client_config["web"]["redirect_uris"][0],
            state=state
        )
        flow.fetch_token(code=code)
        
        credentials = flow.credentials
        return {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes,
            'expiry': credentials.expiry.isoformat() if credentials.expiry else None
        }
    
    def credentials_from_dict(self, creds_dict: Dict) -> Credentials:
        """Create Credentials object from dictionary"""
        expiry = None
        if creds_dict.get('expiry'):
            try:
                expiry = datetime.datetime.fromisoformat(creds_dict['expiry'])
            except Exception:
                pass
        
        return Credentials(
            token=creds_dict.get('token'),
            refresh_token=creds_dict.get('refresh_token'),
            token_uri=creds_dict.get('token_uri'),
            client_id=creds_dict.get('client_id'),
            client_secret=creds_dict.get('client_secret'),
            scopes=creds_dict.get('scopes'),
            expiry=expiry
        )
    
    def refresh_credentials_if_needed(self, creds_dict: Dict) -> Optional[Dict]:
        """
        Refresh credentials if expired
        
        Returns:
            Updated credentials dict if refreshed, None otherwise
        """
        creds = self.credentials_from_dict(creds_dict)
        
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes,
                'expiry': creds.expiry.isoformat() if creds.expiry else None
            }
        return None
    
    def get_events_for_day(
        self, 
        creds_dict: Dict, 
        date: datetime.date,
        timezone: str = "America/New_York"
    ) -> List[Dict]:
        """
        Get all events for a specific day
        
        Args:
            creds_dict: User's Google Calendar credentials
            date: Date to fetch events for
            timezone: Timezone string (default: America/New_York)
            
        Returns:
            List of event dictionaries with start/end times
        """
        creds = self.credentials_from_dict(creds_dict)
        
        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        
        try:
            service = build('calendar', 'v3', credentials=creds)
            
            # Set time range for the day
            tz = ZoneInfo(timezone)
            start_of_day = datetime.datetime.combine(date, datetime.time.min).replace(tzinfo=tz)
            end_of_day = datetime.datetime.combine(date, datetime.time.max).replace(tzinfo=tz)
            
            # Fetch events
            events_result = service.events().list(
                calendarId='primary',
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            
            # Parse events
            parsed_events = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                
                # Skip all-day events
                if 'T' not in start:
                    continue
                
                parsed_events.append({
                    'summary': event.get('summary', 'Busy'),
                    'start': start,
                    'end': end,
                    'id': event['id']
                })
            
            return parsed_events
        
        except HttpError as error:
            print(f'Google Calendar API error: {error}')
            raise
    
    def find_meal_time_slots(
        self,
        events: List[Dict],
        date: datetime.date,
        timezone: str = "America/New_York",
        meal_windows: Optional[Dict] = None
    ) -> Dict[str, Dict]:
        """
        Find optimal time slots for meals based on user's schedule
        
        Args:
            events: List of calendar events for the day
            date: Date to find meal slots for
            timezone: Timezone string
            meal_windows: Custom meal time windows (optional)
            
        Returns:
            Dictionary with meal types as keys and time slot info as values
            Format: {
                'breakfast': {'start': datetime, 'end': datetime, 'available': bool},
                'lunch': {...},
                'dinner': {...}
            }
        """
        tz = ZoneInfo(timezone)
        meal_windows = meal_windows or DEFAULT_MEAL_WINDOWS
        
        # Convert events to datetime objects
        busy_periods = []
        for event in events:
            try:
                start = datetime.datetime.fromisoformat(event['start'].replace('Z', '+00:00'))
                end = datetime.datetime.fromisoformat(event['end'].replace('Z', '+00:00'))
                busy_periods.append((start, end))
            except Exception as e:
                print(f"Error parsing event time: {e}")
                continue
        
        # Find available slots for each meal
        meal_slots = {}
        
        for meal_type, window in meal_windows.items():
            window_start = datetime.datetime.combine(
                date, 
                datetime.time(window['start_hour'], 0)
            ).replace(tzinfo=tz)
            
            window_end = datetime.datetime.combine(
                date,
                datetime.time(window['end_hour'], 0)
            ).replace(tzinfo=tz)
            
            duration = datetime.timedelta(minutes=window['duration_minutes'])
            
            # Find first available slot in the window
            slot_found = False
            current_time = window_start
            
            while current_time + duration <= window_end:
                slot_end = current_time + duration
                
                # Check if this slot conflicts with any busy period
                is_free = True
                for busy_start, busy_end in busy_periods:
                    # Check for overlap
                    if not (slot_end <= busy_start or current_time >= busy_end):
                        is_free = False
                        # Jump to the end of this busy period
                        current_time = busy_end
                        break
                
                if is_free:
                    meal_slots[meal_type] = {
                        'start': current_time.isoformat(),
                        'end': slot_end.isoformat(),
                        'available': True,
                        'duration_minutes': window['duration_minutes']
                    }
                    slot_found = True
                    break
                else:
                    # Move forward by 15 minutes if not at end of busy period
                    if current_time == window_start or current_time < busy_end:
                        current_time = max(current_time + datetime.timedelta(minutes=15), busy_end)
            
            # If no slot found, use default time at start of window
            if not slot_found:
                meal_slots[meal_type] = {
                    'start': window_start.isoformat(),
                    'end': (window_start + duration).isoformat(),
                    'available': False,  # Conflicts with schedule
                    'duration_minutes': window['duration_minutes']
                }
        
        return meal_slots
    
    def export_meal_plan_to_calendar(
        self,
        creds_dict: Dict,
        meal_plan: Dict,
        meal_slots: Dict[str, Dict],
        date: datetime.date,
        dining_hall: str
    ) -> List[Dict]:
        """
        Export meal plan to Google Calendar
        
        Args:
            creds_dict: User's Google Calendar credentials
            meal_plan: Meal plan data with meals
            meal_slots: Time slots for each meal
            date: Date of the meal plan
            dining_hall: Name of dining hall
            
        Returns:
            List of created event IDs
        """
        creds = self.credentials_from_dict(creds_dict)
        
        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        
        try:
            service = build('calendar', 'v3', credentials=creds)
            created_events = []
            
            meals = meal_plan.get('meals', {})
            
            for meal_type, slot in meal_slots.items():
                meal_data = meals.get(meal_type)
                if not meal_data:
                    continue
                
                # Build event description
                items = meal_data.get('items', [])
                description_lines = [
                    f"🍽️ {dining_hall}",
                    "",
                    "Meal Items:",
                ]
                
                total_calories = 0
                total_protein = 0
                
                for item in items:
                    item_name = item.get('name', 'Unknown')
                    calories = item.get('calories', 0)
                    protein = item.get('protein_g', 0)
                    total_calories += calories
                    total_protein += protein
                    description_lines.append(f"• {item_name} ({int(calories)} cal, {int(protein)}g protein)")
                
                description_lines.extend([
                    "",
                    f"Total: {int(total_calories)} calories, {int(total_protein)}g protein",
                    "",
                    "Generated by DormDish AI 🤖"
                ])
                
                # Create event
                event = {
                    'summary': f"🍴 {meal_type.capitalize()} at {dining_hall}",
                    'description': '\n'.join(description_lines),
                    'start': {
                        'dateTime': slot['start'],
                        'timeZone': 'America/New_York',
                    },
                    'end': {
                        'dateTime': slot['end'],
                        'timeZone': 'America/New_York',
                    },
                    'reminders': {
                        'useDefault': False,
                        'overrides': [
                            {'method': 'popup', 'minutes': 15},
                        ],
                    },
                    'colorId': '9',  # Blue color for meal events
                }
                
                result = service.events().insert(calendarId='primary', body=event).execute()
                created_events.append({
                    'id': result.get('id'),
                    'meal_type': meal_type,
                    'link': result.get('htmlLink')
                })
            
            return created_events
        
        except HttpError as error:
            print(f'Google Calendar API error: {error}')
            raise


# TODO: MEM0 INTEGRATION NEEDED
# ================================
# The following function needs to be implemented by the user to save
# the detected meal time slots to mem0 for future reference.
#
# This will allow the AI model to:
# 1. Remember the user's typical class schedule
# 2. Suggest meal times that work with their schedule
# 3. Learn patterns over time (e.g., "User always has class 11-12 on MWF")
#
# Implementation steps:
# 1. Import the mem0 client (from memory.mem0_client import get_mem0_client)
# 2. Create a memory for each detected time slot
# 3. Include context like: day of week, meal type, time slot, conflicts
# 4. Tag memories with user_id and date for easy retrieval
#
# Example implementation:
# def save_meal_slots_to_mem0(user_id: str, date: datetime.date, meal_slots: Dict, events: List[Dict]):
#     """Save meal time slots and schedule info to mem0"""
#     mem0_client = get_mem0_client()
#     
#     # Create memory for today's schedule
#     day_of_week = date.strftime("%A")
#     event_summaries = [e['summary'] for e in events]
#     
#     memory_text = f"On {day_of_week}, user has classes/events: {', '.join(event_summaries)}. "
#     memory_text += f"Optimal meal times: "
#     memory_text += f"Breakfast {meal_slots['breakfast']['start']}, "
#     memory_text += f"Lunch {meal_slots['lunch']['start']}, "
#     memory_text += f"Dinner {meal_slots['dinner']['start']}."
#     
#     mem0_client.add(
#         messages=[{"role": "user", "content": memory_text}],
#         user_id=user_id,
#         metadata={"type": "meal_schedule", "date": str(date), "day": day_of_week}
#     )
# ================================


calendar_service = GoogleCalendarService()

