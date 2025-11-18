
import os
from dotenv import load_dotenv

load_dotenv()

# --- Bot Credentials ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing required environment variable: TELEGRAM_BOT_TOKEN")

# --- Target Channel ---
try:
    TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))
except (ValueError, TypeError):
    raise ValueError("TARGET_CHAT_ID must be a valid integer in your .env file")

# --- Telethon Credentials ---
try:
    TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
except (ValueError, TypeError):
    raise ValueError("TELEGRAM_API_ID must be a valid integer in your .env file")

TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
if not TELEGRAM_API_HASH:
    raise ValueError("Missing required environment variable: TELEGRAM_API_HASH")

# --- Application Constants ---
FILES_PER_PAGE = 10
STORAGE_BUFFER_GB = 2.0
VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.webm')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')
AUDIO_EXTENSIONS = ('.mp3', '.flac', '.wav', '.ogg', '.m4a')
# --- NEW: Define archive extensions ---
ARCHIVE_EXTENSIONS = ('.zip', '.rar', '.7z')
# ------------------------------------
UPLOAD_WORKERS = 10
TRACKER_URLS = [
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all_udp.txt"
]
PUBLIC_TRACKERS = []
NUM_UPLOAD_WORKERS = 7