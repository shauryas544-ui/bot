import os
import re
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

import sys
import threading
import http.server
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Base directory determination
BASE_DIR = Path(__file__).resolve().parent

# Explicitly load .env from the project base directory
ENV_FILE = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE)

# Logger setup
logger = logging.getLogger("telegram_doc_bot")


def mask_token(token: str) -> str:
    """Safely mask token for logs or user display without leaking secrets."""
    if not token:
        return "<EMPTY>"
    parts = token.split(":")
    if len(parts) == 2:
        return f"{parts[0]}:{'*' * 8}..."
    return f"{token[:4]}...{'*' * 8}"


def is_valid_token_format(token: str) -> bool:
    """Validate standard Telegram Bot Token syntax (<id>:<hash>)."""
    if not token:
        return False
    return bool(re.match(r"^\d{8,11}:[A-Za-z0-9_-]{35}$", token))


def verify_telegram_token(token: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Directly checks token validity against Telegram getMe API.
    Returns: (is_valid, bot_username, error_description)
    """
    if not token or not is_valid_token_format(token):
        return False, None, "Invalid token format (<digits>:<35 chars>)"
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("ok"):
                username = payload.get("result", {}).get("username", "TelegramBot")
                return True, username, None
        return False, None, f"HTTP {resp.status_code} Unauthorized"
    except Exception as exc:
        return False, None, f"Network check failed: {exc}"


def save_token_to_env(token: str, env_path: Path) -> None:
    """Safely updates or inserts TELEGRAM_BOT_TOKEN into the .env file."""
    lines = []
    found = False
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("TELEGRAM_BOT_TOKEN=") or stripped.startswith("BOT_TOKEN="):
                    lines.append(f"TELEGRAM_BOT_TOKEN={token}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"\nTELEGRAM_BOT_TOKEN={token}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def resolve_bot_token() -> str:
    """
    Resolves, tests, and interactively prompts for a working Telegram Bot Token.
    Eliminates crashes due to empty or 401 Unauthorized tokens.
    """
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("BOT_TOKEN")
        or ""
    ).strip().strip('"').strip("'")

    # In non-interactive environments (unit tests or headless), return whatever is set
    if "unittest" in sys.modules or not (sys.stdin and sys.stdin.isatty()):
        return token

    if token:
        valid, username, err = verify_telegram_token(token)
        if valid:
            logger.info(f"Telegram Bot Token verified! Connected as @{username}")
            return token
        else:
            logger.warning(f"Configured token '{mask_token(token)}' failed validation: {err}")

    # Interactive prompt for user in terminal
    print("\n" + "=" * 72)
    print(" [!] TELEGRAM BOT TOKEN REQUIRED / टेलीग्राम बॉट टोकन आवश्यक है")
    print("=" * 72)
    print(" .env file mein diya gaya token invalid ya unauthorized hai (HTTP 401).")
    print(" The bot cannot start without a valid token from @BotFather.")
    print("")
    print(" Steps to get your token:")
    print(" 1. Open Telegram and search for @BotFather")
    print(" 2. Send /mybots, select your bot, and tap 'API Token'")
    print("    (or send /newbot to create a new bot)")
    print(" 3. Copy the token and paste it below.")
    print("=" * 72)

    while True:
        try:
            entered = input("\nEnter Telegram Bot Token: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print("\nOperation cancelled by user.")
            sys.exit(0)

        if not entered:
            print("Token cannot be empty. Please enter a valid token.")
            continue

        valid, username, err = verify_telegram_token(entered)
        if valid:
            print(f"[*] Token verified successfully! Connected as @{username}")
            try:
                save_token_to_env(entered, ENV_FILE)
                print(f"[*] Token saved to {ENV_FILE}")
            except Exception as save_err:
                logger.warning(f"Could not persist token to .env: {save_err}")
            return entered
        else:
            print(f"[-] Token rejected by Telegram ({err}). Please re-copy from @BotFather and try again.")


# Bot API Configuration (stripped of trailing spaces, carriage returns, or quotes)
RAW_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip().strip('"').strip("'")
BOT_TOKEN = resolve_bot_token()

# Temporary storage directory path (configurable via .env, defaults to temp_storage)
env_temp_dir = os.getenv("TEMP_STORAGE_DIR", "temp_storage").strip().strip('"').strip("'")
TEMP_DIR = Path(env_temp_dir) if os.path.isabs(env_temp_dir) else (BASE_DIR / env_temp_dir).resolve()
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Session TTL (default 1800 seconds / 30 minutes)
try:
    SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
except ValueError:
    SESSION_TTL_SECONDS = 1800

# Dedicated ThreadPoolExecutor for offloading CPU-heavy and network-blocking work
MAX_WORKERS = min(32, (os.cpu_count() or 2) * 2 + 4)
THREAD_POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="doc_worker")

# Default translation target language
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "hi").strip().lower()

# Supported languages mapping (Code -> Label + Flag)
SUPPORTED_LANGUAGES: Dict[str, str] = {
    "hi": "Hindi 🇮🇳",
    "en": "English 🇬🇧",
    "es": "Spanish 🇪🇸",
    "fr": "French 🇫🇷",
    "de": "German 🇩🇪",
    "ru": "Russian 🇷🇺",
    "ar": "Arabic 🇸🇦",
    "bn": "Bengali 🇧🇩",
    "mr": "Marathi 🇮🇳",
}

# Supported document extensions (including PDF)
SUPPORTED_EXTENSIONS = {".txt", ".docx", ".epub", ".pdf"}

# Translation engine character block limits (Specification: max 3500 chars)
MAX_CHUNK_CHARS = 3500

# Telegram message length limit
TELEGRAM_MAX_MESSAGE_LENGTH = 4000


# ---------------------------------------------------------------------------
# Cloud Deployment (Render/Railway) Dummy Health Check Server
# ---------------------------------------------------------------------------
class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    """Responds with 200 OK to satisfy Render/Railway Web Service health checks."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Telegram Bot is healthy and polling.\n")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # Silence access logs to keep terminal and bot logs clean
        pass


def start_health_server():
    """Starts a background HTTP health server if PORT environment variable is present."""
    port_str = os.getenv("PORT")
    if not port_str:
        return

    try:
        port = int(port_str)
    except ValueError:
        port = 8080

    def run_server():
        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
            logger.info(f"Cloud health check HTTP server listening on port {port}")
            server.serve_forever()
        except Exception as e:
            logger.warning(f"Could not start cloud health server on port {port}: {e}")

    server_thread = threading.Thread(target=run_server, daemon=True, name="HealthCheckServer")
    server_thread.start()



# ---------------------------------------------------------------------------
# In-Memory Session & State Management
# ---------------------------------------------------------------------------

@dataclass
class ActiveFile:
    path: str
    name: str
    ext: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> bool:
        """Check if file session has exceeded expiration TTL."""
        return (time.time() - self.created_at) > ttl_seconds


@dataclass
class UserSession:
    target_language: str = DEFAULT_LANGUAGE
    active_file: Optional[ActiveFile] = None
    awaiting_split_count: bool = False
    last_interaction: float = field(default_factory=time.time)


# In-memory dictionary storing session states by Telegram user ID
USER_SESSIONS: Dict[int, UserSession] = {}


def get_user_session(user_id: int) -> UserSession:
    """Retrieve existing user session or initialize a fresh one."""
    if user_id not in USER_SESSIONS:
        USER_SESSIONS[user_id] = UserSession(target_language=DEFAULT_LANGUAGE)
    session = USER_SESSIONS[user_id]
    session.last_interaction = time.time()
    return session


def set_user_language(user_id: int, lang_code: str) -> None:
    """Update target translation language for a user session."""
    session = get_user_session(user_id)
    if lang_code in SUPPORTED_LANGUAGES:
        session.target_language = lang_code


def set_active_file(user_id: int, path: str, name: str, ext: str) -> None:
    """
    Store an active uploaded file buffer.
    Automatically deletes previous file if one was already buffered.
    """
    session = get_user_session(user_id)
    # Ephemeral hygiene: Clean up previous active file if it exists
    if session.active_file and session.active_file.path and session.active_file.path != path:
        try:
            if os.path.exists(session.active_file.path):
                os.remove(session.active_file.path)
        except OSError as e:
            logger.warning(f"Failed to remove previous active file: {e}")

    session.active_file = ActiveFile(path=path, name=name, ext=ext.lower())
    session.awaiting_split_count = False


def get_valid_active_file(user_id: int, ttl_seconds: int = SESSION_TTL_SECONDS) -> Optional[ActiveFile]:
    """
    Retrieve active file for user, verifying that it has not expired and still exists on disk.
    If expired or missing, cleans up resources and returns None.
    """
    session = get_user_session(user_id)
    if not session.active_file:
        return None

    if session.active_file.is_expired(ttl_seconds) or not os.path.exists(session.active_file.path):
        logger.info(f"Session or active file for user {user_id} expired/missing on disk. Pruning.")
        clear_active_file(user_id, remove_file=True)
        return None

    return session.active_file


def clear_active_file(user_id: int, remove_file: bool = True) -> None:
    """Clear active file from user session and optionally delete it from disk."""
    session = get_user_session(user_id)
    if session.active_file:
        if remove_file and session.active_file.path:
            try:
                if os.path.exists(session.active_file.path):
                    os.remove(session.active_file.path)
            except OSError as e:
                logger.warning(f"Failed to remove active file during clear: {e}")
        session.active_file = None
    session.awaiting_split_count = False


def set_awaiting_split(user_id: int, awaiting: bool) -> None:
    """Update awaiting_split_count state flag."""
    session = get_user_session(user_id)
    session.awaiting_split_count = awaiting


def cleanup_expired_temp_files(max_age_seconds: int = SESSION_TTL_SECONDS) -> int:
    """
    Scans TEMP_DIR and removes any temp_*, split_*, translated_* files older than max_age_seconds.
    Prevents storage leaks from abandoned sessions. Returns number of purged files.
    """
    cleaned_count = 0
    now = time.time()
    try:
        for file_path in TEMP_DIR.iterdir():
            if not file_path.is_file():
                continue
            name = file_path.name
            if name.startswith(("temp_", "split_", "translated_")):
                try:
                    mtime = file_path.stat().st_mtime
                    if now - mtime > max_age_seconds:
                        file_path.unlink(missing_ok=True)
                        cleaned_count += 1
                except OSError as err:
                    logger.warning(f"Failed to purge stale file {file_path}: {err}")
    except Exception as exc:
        logger.error(f"Error during temp file cleanup: {exc}")
    return cleaned_count


def sanitize_filename(filename: str) -> str:
    """Clean filename of potentially dangerous path traversal characters."""
    filename = os.path.basename(filename)
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename.replace(" ", "_")
    return filename or "document"

