# DormDish Backend

A Flask-based meal planning backend that generates personalized nutrition plans for university students using real dining hall menu data.

## Features

- 🍽️ **Real Menu Data**: Fetches actual dining hall menus from Rutgers and Johns Hopkins via Nutrislice API
- 🎯 **Personalized Nutrition**: Calculates calorie and macro needs based on user profile (age, height, weight, activity level, goals)
- 🤖 **AI Meal Planning**: Uses OpenAI-compatible models to generate optimized meal plans
- 💾 **Smart Caching**: Caches menu data in Supabase for faster subsequent requests
- 🏫 **Multi-University**: Supports Rutgers University and Johns Hopkins University dining halls

## Supported Dining Halls

### Rutgers University
- Busch Dining Hall
- Livingston Dining Commons
- Brower Commons
- Neilson Dining Hall

### Johns Hopkins University
- Hopkins Cafe
- Nolan's on 33rd
- Peabody Institute

## API Endpoints

### Health Check
```
GET /health
```

### Generate Meal Plan
```
POST /generate-meal-plan
```

**Authenticated Request (minimal):**
```json
{
  "date": "2025-09-19"
}
```

**Authenticated Request (with goal override):**
```json
{
  "goal": "cut",
  "date": "2025-09-19"
}
```

**Guest Request (full profile required):**
```json
{
  "age": 18,
  "height_feet": 5,
  "height_inches": 10,
  "weight_lbs": 175,
  "activity_level": "very_active",
  "goal": "bulk",
  "preferred_dining_hall": "busch",
  "date": "2025-09-19"
}
```

**Response:**
```json
{
  "success": true,
  "user_profile": {...},
  "nutrition_targets": {
    "calories": 3000,
    "protein_grams": 175,
    "carb_grams": 375,
    "fat_grams": 90
  },
  "bmr_tdee": {
    "bmr": 1800,
    "tdee": 2600
  },
  "meal_plan": {...},
  "meta": {
    "authenticated": true,
    "dining_hall": "busch"
  }
}
```

## Authentication

### Register User
```
POST /auth/register
```

**Request Body:**
```json
{
  "username": "johndoe",
  "email": "john@example.com",
  "password": "securepassword",
  "age": 20,
  "height_feet": 5,
  "height_inches": 10,
  "weight_lbs": 175,
  "gender": "male",
  "activity_level": "moderately_active",
  "goal": "bulk",
  "university": "rutgers",
  "preferred_dining_hall": "busch"
}
```

**Fitness Goals:**
- `cut` - Weight loss with calorie deficit
- `maintain` - Maintain current weight
- `bulk` - Muscle gain with calorie surplus

**Activity Levels:**
- `sedentary` - Little/no exercise
- `lightly_active` - Exercise 1-3 times/week
- `moderately_active` - Exercise 4-5/week
- `very_active` - Daily or intense 3-4
- `extremely_active` - Intense 6-7
- `super_active` - Very intense

### Login
```
POST /auth/login
```

**Request Body:**
```json
{
  "email": "john@example.com",
  "password": "securepassword"
}
```

### Get Profile
```
GET /auth/profile
Authorization: Bearer <session_token>
```

### Update Profile
```
PUT /auth/profile
Authorization: Bearer <session_token>
```

### Logout
```
POST /auth/logout
Authorization: Bearer <session_token>
```

### Get Menu
```
GET /menu/<dining_hall>/<date>
```

### Get Supported Dining Halls
```
GET /dining-halls
```

### Cache Management
```
DELETE /cache/clear/<dining_hall>/<date>
GET /cache/status
```

## Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd dormdish-backend
   ```

2. **Create a conda environment**
   ```bash
   conda create -n dormdish python=3.9
   conda activate dormdish
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   
   Create a `.env` file in the root directory:
   ```env
   SUPABASE_URL=your_supabase_project_url
   SUPABASE_SERVICE_KEY=your_supabase_service_key
   ```

5. **Set up Supabase database**
   
   Follow the instructions in `supabase_setup.md` to create the required database schema.

6. **Configure AI Model**
   
   The app uses OpenAI-compatible APIs. By default, it's configured for LM Studio running locally:
   - Install and run LM Studio
   - Load a compatible model
   - The app will connect to `http://127.0.0.1:1234/v1`

## Usage

1. **Start the server**
   ```bash
   conda activate dormdish
   python app.py
   ```

2. **Test the API**
   ```bash
   curl http://localhost:5000/health
   ```

3. **Generate a meal plan**
   ```bash
   curl -X POST http://localhost:5000/generate-meal-plan \
        -H "Content-Type: application/json" \
        -d '{
          "age": 18,
          "height_feet": 5,
          "height_inches": 10,
          "weight_lbs": 175,
          "activity_level": "very_active",
          "goal": "bulk",
          "preferred_dining_hall": "busch"
        }'
   ```

## Frontend routing and hosting

Path-based SPA routing is enabled in the frontend. For production hosting, add a fallback so deep links like /login or /dashboard resolve to index.html:

- Netlify: a `_redirects` file is included at `DormDish Website Design/public/_redirects` with:
  `/* /index.html 200`
- Vercel (separate frontend project): add a rewrite to serve `/index.html` for all non-asset routes in the frontend project settings or `vercel.json` there. Do not change the root `vercel.json` used by the backend API.
- Nginx: `try_files $uri /index.html;`

`/dashboard` is protected in-app and requires a valid token. `/dashboard-test` is currently unprotected (optional to guard similarly).

## Architecture

### Data Flow
1. **User Request** → Flask API endpoint
2. **Cache Check** → Supabase database lookup
3. **Fresh Data** → Nutrislice API calls (if not cached)
4. **Menu Processing** → Extract nutritional information
5. **AI Generation** → Create personalized meal plan
6. **Cache Storage** → Save to Supabase for future requests

### Key Components

- **`app.py`**: Main Flask application with API endpoints
- **`server.py`**: MCP server implementation (alternative approach)
- **Nutrislice Integration**: Direct API calls to university dining services
- **Supabase Caching**: Database storage for menu data
- **OpenAI Integration**: AI-powered meal plan generation

## Configuration

### Activity Levels
- `sedentary`: Office work, no exercise
- `active`: Light exercise 3-5 days/week
- `very_active`: Heavy exercise 6-7 days/week

### Goals
- `bulk`: Gain weight/muscle (300 calorie surplus)
- `cut`: Lose weight (500 calorie deficit)
- `maintain`: Maintain current weight

### Dining Hall Name Variations
The system accepts multiple name formats:
- `busch`, `busch-dining-hall`, `busch dining hall`
- `hopkins`, `hopkins-cafe`, `hopkins cafe`
- `nolans`, `nolans-on-33rd`, `nolan`

## Database SQL

Run `sql/user_meal_plans.sql` in the Supabase SQL editor to create the `public.user_meal_plans` table with constraints, indexes, and an `updated_at` trigger.

## Development

### Adding New Universities
1. Add API base URL to `api_bases` in `fetch_nutrislice_menu()`
2. Configure meal slugs in `meal_configs`
3. Add dining hall mappings in `dining_hall_mapping`
4. Update university detection logic

### Running Tests
```bash
# Test individual endpoints
curl http://localhost:5000/health
curl http://localhost:5000/menu/busch/2025-09-19

# Test full meal plan generation
curl -X POST http://localhost:5000/generate-meal-plan -H "Content-Type: application/json" -d @test_user.json
```

### Environment Variables
- `SUPABASE_URL`: Your Supabase project URL
- `SUPABASE_SERVICE_KEY`: Service role key for database access

## Automation

### Daily Menu and User Meal Plan Cleanup

The project includes automated cleanup of old data to keep the database optimized:

**GitHub Actions (Automatic)**
- Runs daily at 06:00 UTC (`.github/workflows/daily-cleanup.yml`)
- Deletes previous day's records from both `menus` and `user_meal_plans`
- Requires repository secrets:
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_KEY` (service role key)
- You can also trigger it manually via the Actions tab (workflow_dispatch)

Note: A legacy workflow (`cleanup-menus.yml`) is disabled to prevent duplicate runs.

**Manual Cleanup Script**
```bash
# Dry run
python cleanup_menus.py --dry-run
# Live - defaults to yesterday
python cleanup_menus.py
# Specific date
python cleanup_menus.py --date 2025-09-18
```

## Troubleshooting

### Common Issues

1. **No menu items returned**
   - Check if the date is valid (dining halls may be closed on weekends/holidays)
   - Verify dining hall name spelling
   - Check Nutrislice API availability

2. **Supabase connection errors**
   - Verify environment variables are set correctly
   - Check internet connection
   - Ensure Supabase project is active

3. **AI model errors**
   - Ensure LM Studio is running with a loaded model
   - Check model compatibility with OpenAI API format
   - Verify network connectivity to AI service

### Debug Mode
The Flask app runs in debug mode by default. Check the console output for detailed error information.

## License

[Add your license information here]

## Contributing

[Add contribution guidelines here]