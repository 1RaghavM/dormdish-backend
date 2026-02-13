import sys
import os

# Add the parent directory to the Python path so we can import from app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

# For Vercel with FastAPI, we export the FastAPI app directly
# Vercel expects the ASGI app to be available as a variable
app = app

# Also export as 'application' for compatibility
application = app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

