import os
import sys
from dotenv import load_dotenv

# Load env variables from .env file
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CRM_API_URL = os.getenv("CRM_API_URL", "http://localhost:3000/api/v1").rstrip("/")
CRM_API_KEY = os.getenv("CRM_API_KEY", "1074e7ed98b95a6e6435e808ee9621616a9855ea56f91b0b846be4b941f98f50")

# Allowed Telegram user IDs
ALLOWED_USERS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USERS", "").split(",") if uid.strip()]

# Validation
missing = []
if not TELEGRAM_BOT_TOKEN:
    missing.append("TELEGRAM_BOT_TOKEN")
if not GEMINI_API_KEY:
    missing.append("GEMINI_API_KEY")

if missing:
    print(f"Error: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
    print("Please create a .env file with these values.", file=sys.stderr)
    # Don't exit immediately so tests or script inspections don't fail, but notify or raise
