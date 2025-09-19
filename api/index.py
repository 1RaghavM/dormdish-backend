import sys
import os

# Add the parent directory to the Python path so we can import from app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

# This is the main entry point for Vercel
def handler(request):
    return app(request.environ, request.start_response)

# For Vercel, we need to export the app
application = app

if __name__ == "__main__":
    app.run()