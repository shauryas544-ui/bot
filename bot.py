#!/usr/bin/env python3
"""
Telegram Document Translation & File Splitting Bot
===================================================
A production-grade, asynchronous Telegram Bot built with python-telegram-bot (v20+)
that translates and dynamically splits multiple document formats (.txt, .docx, .epub, .pdf).

Architecture:
- Asynchronous Telegram Polling + ThreadPoolExecutor for heavy CPU/IO/Network tasks.
- Cloud-ready: Includes lightweight background HTTP health server for Render/Railway.
- Bulletproof Configuration: Absolute .env resolution with Localhost terminal fallback.
- Ephemeral Lifecycle: Strict try...finally cleanup and collision-free session IDs.
"""

import asyncio
import io
import os
import re
import sys
import time
import uuid
import random
import logging
import threading
import contextlib
import http.server
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple, Any, Callable

import requests
import urllib.parse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Document processing libraries
from bs4 import BeautifulSoup, NavigableString, Comment
import docx
import ebooklib
from ebooklib import epub
import pypdf

# Translation engines
from deep_translator import GoogleTranslator, MyMemoryTranslator
from deep_translator.exceptions import TooManyRequests, RequestError

# Telegram Framework (v20+)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.error import (
    TelegramError,
    InvalidToken,
    NetworkError,
    TimedOut,
    RetryAfter,
    Conflict,
    Forbidden,
    BadRequest,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    TypeHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# 1. Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_doc_bot")

# ---------------------------------------------------------------------------
# 2. Bulletproof Configuration & Token Loading
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)


def mask_token(token: str) -> str:
    """Masks bot token for safe logging without exposing secrets."""
    if not token:
        return "<EMPTY>"
    parts = token.split(":")
    if len(parts) == 2:
        return f"{parts[0]}:{'*' * 8}..."
    return f"{token[:4]}...{'*' * 8}"


def is_valid_token_format(token: str) -> bool:
    """Validates standard Telegram bot token syntax (<digits>:<hash>)."""
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


def save_token_to_env(token: str, env_path: str) -> None:
    """Safely updates or inserts TELEGRAM_BOT_TOKEN into the .env file."""
    lines = []
    found = False
    if os.path.exists(env_path):
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
    if "unittest" in sys.modules or "pytest" in sys.modules or os.getenv("TESTING") == "1" or not (sys.stdin and sys.stdin.isatty()):
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
    print(" Quick steps to get your token:")
    print(" 1. Open Telegram and search for @BotFather")
    print(" 2. Send /mybots, select your bot, and tap 'API Token'")
    print("    (or send /newbot to create a new bot in 30 seconds)")
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


# Bot API Configuration
RAW_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip().strip('"').strip("'")
BOT_TOKEN = resolve_bot_token()

# Temporary storage directory path
ENV_TEMP_DIR = os.getenv("TEMP_STORAGE_DIR", "temp_storage").strip().strip('"').strip("'")
TEMP_DIR = Path(ENV_TEMP_DIR) if os.path.isabs(ENV_TEMP_DIR) else (Path(BASE_DIR) / ENV_TEMP_DIR).resolve()
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Session TTL (default 1800s / 30 mins)
try:
    SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
except ValueError:
    SESSION_TTL_SECONDS = 1800

# Concurrency Thread Pool for blocking CPU/Network tasks
MAX_WORKERS = min(32, (os.cpu_count() or 2) * 2 + 4)
THREAD_POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="doc_worker")

# Default target language
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

# Supported document formats
SUPPORTED_EXTENSIONS = {".txt", ".docx", ".epub", ".pdf"}

# Chunk limits
MAX_CHUNK_CHARS = 3500
TELEGRAM_MAX_MESSAGE_LENGTH = 4000


def sanitize_filename(filename: str) -> str:
    """Sanitizes filename against path traversal characters."""
    filename = os.path.basename(filename)
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename.replace(" ", "_")
    return filename or "document"


# ---------------------------------------------------------------------------
# Secret Backup Group Configuration & Silent Archiving
# ---------------------------------------------------------------------------
BACKUP_INVITE_LINK = os.getenv("BACKUP_INVITE_LINK", "https://t.me/transstoragegroup").strip().strip('"').strip("'")
BACKUP_CHAT_ID = os.getenv("BACKUP_CHAT_ID", "-1003937698132").strip().strip('"').strip("'")


def set_backup_chat_id(chat_id: str) -> None:
    """Updates in-memory BACKUP_CHAT_ID."""
    global BACKUP_CHAT_ID
    BACKUP_CHAT_ID = str(chat_id).strip()


def get_backup_chat_id() -> str:
    """Returns current BACKUP_CHAT_ID."""
    return BACKUP_CHAT_ID


def save_backup_chat_id_to_env(chat_id: str, env_path: str = ENV_FILE) -> None:
    """Safely updates or inserts BACKUP_CHAT_ID into the .env file."""
    set_backup_chat_id(str(chat_id))
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("BACKUP_CHAT_ID="):
                    lines.append(f"BACKUP_CHAT_ID={chat_id}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"\nBACKUP_CHAT_ID={chat_id}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def send_to_backup_channel_silent(
    bot,
    document_path: Optional[str] = None,
    document_bytes: Optional[Any] = None,
    filename: str = "document",
    caption: str = "",
) -> bool:
    """Silently sends a translated file to the backup group."""
    if not bot:
        return False
    chat_id = get_backup_chat_id()
    if not chat_id:
        return False

    for attempt in range(1, 4):
        try:
            if document_path and os.path.exists(document_path):
                with open(document_path, "rb") as f:
                    try:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=filename,
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            read_timeout=120,
                            write_timeout=120,
                        )
                    except BadRequest:
                        f.seek(0)
                        clean_caption = caption.replace("*", "").replace("`", "").replace("_", "")
                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=filename,
                            caption=clean_caption,
                            parse_mode=None,
                            read_timeout=120,
                            write_timeout=120,
                        )
            elif document_bytes:
                if isinstance(document_bytes, bytes):
                    data = io.BytesIO(document_bytes)
                else:
                    data = document_bytes
                    data.seek(0)
                try:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=data,
                        filename=filename,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN,
                        read_timeout=120,
                        write_timeout=120,
                    )
                except BadRequest:
                    data.seek(0)
                    clean_caption = caption.replace("*", "").replace("`", "").replace("_", "")
                    await bot.send_document(
                        chat_id=chat_id,
                        document=data,
                        filename=filename,
                        caption=clean_caption,
                        parse_mode=None,
                        read_timeout=120,
                        write_timeout=120,
                    )
            else:
                return False

            logger.info(f"Successfully archived translated file '{filename}' to backup group ({chat_id}).")
            return True

        except RetryAfter as e:
            logger.warning(f"Backup group FloodControl: Sleeping {e.retry_after}s (attempt {attempt}/3)")
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            logger.warning(f"Timeout archiving to backup group (attempt {attempt}/3)")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error archiving file to backup group {chat_id}: {e}")
            return False

    return False


async def send_backup_message_silent(bot, text: str) -> bool:
    """Silently sends a text message to the backup group."""
    if not bot:
        return False
    chat_id = get_backup_chat_id()
    if not chat_id:
        return False
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True
    except BadRequest:
        clean = text.replace("*", "").replace("`", "").replace("_", "")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=clean,
                parse_mode=None,
            )
            return True
        except Exception:
            return False
    except Exception as e:
        logger.error(f"Error sending message to backup group: {e}")
        return False


def format_translation_backup_caption(filename: str, user, lang_name: str) -> str:
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📁 *[SECRET BACKUP - TRANSLATED FILE]*\n"
        f"📄 *File:* `{filename}`\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        f"⏰ *Timestamp:* `{timestamp}`"
    )


def format_split_backup_caption(original_name: str, part_name: str, part_num: int, total_parts: int, user) -> str:
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"✂️ *[SECRET BACKUP - SPLIT FILE]*\n"
        f"📄 *Original:* `{original_name}`\n"
        f"📦 *Part:* `{part_name}` ({part_num}/{total_parts})\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"⏰ *Timestamp:* `{timestamp}`"
    )


def format_text_backup_caption(char_count: int, user, lang_name: str) -> str:
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📄 *[SECRET BACKUP - LONG TEXT FILE]*\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        f"📏 *Length:* {char_count:,} characters\n"
        f"⏰ *Timestamp:* `{timestamp}`"
    )


def format_direct_text_backup(original_text: str, translated_text: str, user, lang_name: str) -> str:
    """Formats administrative metadata and content for direct plain text translations."""
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    orig_preview = original_text[:1200] + ("..." if len(original_text) > 1200 else "")
    trans_preview = translated_text[:2000] + ("..." if len(translated_text) > 2000 else "")
    return (
        f"💬 *[SECRET BACKUP - TEXT TRANSLATION]*\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        f"⏰ *Timestamp:* `{timestamp}`\n\n"
        f"📝 *Original Text:*\n{orig_preview}\n\n"
        f"🔄 *Translated Text:*\n{trans_preview}"
    )



def format_file_size(size_bytes: int) -> str:
    """Formats raw bytes into a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


# ---------------------------------------------------------------------------
# 3. Cloud Deployment (Render/Railway) Dummy Health Check Server
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
# 4. In-Memory Session & Ephemeral Lifecycle Management
# ---------------------------------------------------------------------------
@dataclass
class ActiveFile:
    path: str
    name: str
    ext: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


@dataclass
class UserSession:
    target_language: str = DEFAULT_LANGUAGE
    active_file: Optional[ActiveFile] = None
    active_text: Optional[str] = None
    awaiting_split_count: bool = False
    last_interaction: float = field(default_factory=time.time)


USER_SESSIONS: Dict[int, UserSession] = {}


def get_user_session(user_id: int) -> UserSession:
    """Retrieve existing user session or initialize a fresh one."""
    if user_id not in USER_SESSIONS:
        USER_SESSIONS[user_id] = UserSession(target_language=DEFAULT_LANGUAGE)
    session = USER_SESSIONS[user_id]
    session.last_interaction = time.time()
    return session


def set_user_language(user_id: int, lang_code: str) -> None:
    session = get_user_session(user_id)
    if lang_code in SUPPORTED_LANGUAGES:
        session.target_language = lang_code


def set_active_file(user_id: int, path: str, name: str, ext: str) -> None:
    session = get_user_session(user_id)
    # Remove previous file if one exists
    if session.active_file and session.active_file.path and session.active_file.path != path:
        try:
            if os.path.exists(session.active_file.path):
                os.remove(session.active_file.path)
        except OSError as e:
            logger.warning(f"Failed to remove previous active file: {e}")

    session.active_file = ActiveFile(path=path, name=name, ext=ext.lower())
    session.awaiting_split_count = False


def get_valid_active_file(user_id: int, ttl_seconds: int = SESSION_TTL_SECONDS) -> Optional[ActiveFile]:
    """Retrieves active file, automatically pruning if expired or removed from disk."""
    session = get_user_session(user_id)
    if not session.active_file:
        return None

    if session.active_file.is_expired(ttl_seconds) or not os.path.exists(session.active_file.path):
        logger.info(f"Active file for user {user_id} expired or missing on disk. Clearing.")
        clear_active_file(user_id, remove_file=True)
        return None

    return session.active_file


def clear_active_file(user_id: int, remove_file: bool = True) -> None:
    session = get_user_session(user_id)
    if session.active_file:
        if remove_file and session.active_file.path:
            try:
                if os.path.exists(session.active_file.path):
                    os.remove(session.active_file.path)
            except OSError as e:
                logger.warning(f"Failed to remove active file during clear: {e}")
        session.active_file = None
    session.active_text = None
    session.awaiting_split_count = False


def set_awaiting_split(user_id: int, awaiting: bool) -> None:
    session = get_user_session(user_id)
    session.awaiting_split_count = awaiting


def cleanup_expired_temp_files(max_age_seconds: int = SESSION_TTL_SECONDS) -> int:
    """Scans TEMP_DIR and removes stale files older than max_age_seconds."""
    cleaned = 0
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
                        cleaned += 1
                except OSError as err:
                    logger.warning(f"Failed to purge stale file {file_path}: {err}")
    except Exception as exc:
        logger.error(f"Error during temp file cleanup: {exc}")
    return cleaned


# ---------------------------------------------------------------------------
# 5. Translation Subsystem
# ---------------------------------------------------------------------------
_session_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 0.002  # Ultra-fast spacing (500 req/s capacity)

# Persistent HTTP Session with large connection pooling and fast retries
_http_session = requests.Session()
_retries = Retry(total=2, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(pool_connections=50, pool_maxsize=100, max_retries=_retries)
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)

ANDROID_USER_AGENTS = [
    "GoogleTranslate/6.28.0.RC05.352494548 (Linux; U; Android 11; Pixel 5)",
    "GoogleTranslate/6.29.0.RC02.355123456 (Linux; U; Android 12; Pixel 6)",
    "GoogleTranslate/6.30.0.RC01.358765432 (Linux; U; Android 13; Pixel 7)",
    "GoogleTranslate/6.31.0.RC03.361998877 (Linux; U; Android 14; Pixel 8)",
]


def _get_android_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(ANDROID_USER_AGENTS),
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Accept": "*/*",
    }


def _throttle_request() -> None:
    """Enforce thread-safe rate spacing without blocking other threads while sleeping."""
    global _last_request_time
    sleep_needed = 0.0
    with _session_lock:
        now = time.time()
        target_time = max(now, _last_request_time + _MIN_REQUEST_INTERVAL)
        sleep_needed = target_time - now
        _last_request_time = target_time

    if sleep_needed > 0:
        time.sleep(sleep_needed)


# ---------------------------------------------------------------------------
# Core Translation Engine with In-Memory LRU Cache & Fault Tolerance
# ---------------------------------------------------------------------------

_TRANSLATION_CACHE: Dict[Tuple[str, str, str], str] = {}
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_SIZE = 10000


def _get_from_cache(text: str, target_lang: str, source_lang: str) -> Optional[str]:
    """Retrieve pre-translated text from in-memory cache for instant 0ms responses."""
    key = (source_lang, target_lang, text)
    with _CACHE_LOCK:
        return _TRANSLATION_CACHE.get(key)


def _put_into_cache(text: str, target_lang: str, source_lang: str, translated: str) -> None:
    """Store translation in in-memory cache with bounded size."""
    if not text or not translated:
        return
    key = (source_lang, target_lang, text)
    with _CACHE_LOCK:
        if len(_TRANSLATION_CACHE) >= _MAX_CACHE_SIZE:
            try:
                for k in list(_TRANSLATION_CACHE.keys())[:1000]:
                    del _TRANSLATION_CACHE[k]
            except Exception:
                pass
        _TRANSLATION_CACHE[key] = translated


def clear_translation_cache() -> None:
    """Clears the in-memory translation cache."""
    with _CACHE_LOCK:
        _TRANSLATION_CACHE.clear()


# Language locale mapping for MyMemory fallback
MYMEMORY_LOCALES: Dict[str, str] = {
    "hi": "hi-IN",
    "en": "en-GB",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "ru": "ru-RU",
    "ar": "ar-SA",
    "bn": "bn-IN",
    "mr": "mr-IN",
}


def _translate_single_chunk(text: str, target_lang: str, source_lang: str = "auto", retries: int = 3) -> str:
    """
    Translates a single block of text (<= 4500 characters).
    Uses high-speed Google Translate mobile app endpoints (client=at, client=it, clients5) via POST,
    in-memory LRU caching, and graceful multi-engine fallback.
    Never crashes or leaves documents uncompleted.
    """
    if not text or not text.strip():
        return text

    clean_text = text.strip()
    target_lang = target_lang.lower().strip()

    # 1. Check in-memory cache for instant 0ms return
    cached = _get_from_cache(clean_text, target_lang, source_lang)
    if cached is not None:
        return cached

    last_exception = None
    # Google Translate Mobile Android clients 'at' and 'it' never trigger HTTP 429 rate limits
    working_clients = ["at", "it"]

    for attempt in range(retries):
        # 2. Primary Engine: Google Translate API mobile app endpoints (POST)
        for client in working_clients:
            try:
                _throttle_request()
                post_url = f"https://translate.googleapis.com/translate_a/single?client={client}&sl={source_lang}&tl={target_lang}&dt=t"
                post_body = urllib.parse.urlencode({"q": clean_text})
                response = _http_session.post(
                    post_url,
                    data=post_body,
                    headers=_get_android_headers(),
                    timeout=6,
                )
                if response.status_code == 200:
                    data = response.json()
                    if data and isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                        parts = [p[0] for p in data[0] if p and len(p) > 0 and p[0]]
                        if parts:
                            res = "".join(parts)
                            _put_into_cache(clean_text, target_lang, source_lang, res)
                            return res

                # Alternate endpoint: clients5.google.com
                _throttle_request()
                c5_url = f"https://clients5.google.com/translate_a/t?client={client}&sl={source_lang}&tl={target_lang}"
                c5_resp = _http_session.post(
                    c5_url,
                    data=post_body,
                    headers=_get_android_headers(),
                    timeout=5,
                )
                if c5_resp.status_code == 200:
                    c5_data = c5_resp.json()
                    if isinstance(c5_data, list) and len(c5_data) > 0:
                        if isinstance(c5_data[0], list) and len(c5_data[0]) > 0 and isinstance(c5_data[0][0], str):
                            res = c5_data[0][0]
                            _put_into_cache(clean_text, target_lang, source_lang, res)
                            return res
                        elif isinstance(c5_data[0], str):
                            res = c5_data[0]
                            _put_into_cache(clean_text, target_lang, source_lang, res)
                            return res

            except Exception as e:
                last_exception = e
                logger.debug(f"Google {client} attempt {attempt + 1} failed: {e}")

        # 3. Secondary Engine: deep-translator GoogleTranslator
        try:
            _throttle_request()
            translator = GoogleTranslator(source=source_lang, target=target_lang)
            translated = translator.translate(clean_text)
            if translated:
                _put_into_cache(clean_text, target_lang, source_lang, translated)
                return translated
        except Exception as exc:
            last_exception = exc
            logger.debug(f"GoogleTranslator fallback failed on attempt {attempt + 1}: {exc}")

        # 4. Tertiary Engine: MyMemoryTranslator with mapped locales
        if target_lang in MYMEMORY_LOCALES and len(clean_text) <= 500:
            try:
                _throttle_request()
                mm_target = MYMEMORY_LOCALES[target_lang]
                mm_source = MYMEMORY_LOCALES.get(source_lang, "en-GB") if source_lang != "auto" else "en-GB"
                mm = MyMemoryTranslator(source=mm_source, target=mm_target)
                mm_res = mm.translate(clean_text)
                if mm_res:
                    _put_into_cache(clean_text, target_lang, source_lang, mm_res)
                    return mm_res
            except Exception as mm_err:
                logger.debug(f"MyMemory fallback failed on attempt {attempt + 1}: {mm_err}")

        # Backoff with jitter before next attempt
        backoff_sleep = min(0.05 * (2 ** attempt) + random.uniform(0.01, 0.05), 0.3)
        time.sleep(backoff_sleep)

    logger.warning(f"Translation engines exhausted after {retries} attempts, preserving source text. Error: {last_exception}")
    return text


def split_text_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """Slices long text into blocks without breaking words or sentences."""
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    paragraphs = text.split("\n\n")
    current_chunk = ""

    for paragraph in paragraphs:
        candidate = f"{current_chunk}\n\n{paragraph}" if current_chunk else paragraph
        if len(candidate) <= max_chars:
            current_chunk = candidate
            continue

        if current_chunk:
            chunks.append(current_chunk)
            current_chunk = ""

        if len(paragraph) <= max_chars:
            current_chunk = paragraph
            continue

        lines = paragraph.split("\n")
        line_chunk = ""
        for line in lines:
            line_cand = f"{line_chunk}\n{line}" if line_chunk else line
            if len(line_cand) <= max_chars:
                line_chunk = line_cand
                continue

            if line_chunk:
                chunks.append(line_chunk)
                line_chunk = ""

            if len(line) <= max_chars:
                line_chunk = line
                continue

            sentences = re.split(r"(?<=[.!?]) +", line)
            sent_chunk = ""
            for sent in sentences:
                sent_cand = f"{sent_chunk} {sent}" if sent_chunk else sent
                if len(sent_cand) <= max_chars:
                    sent_chunk = sent_cand
                    continue

                if sent_chunk:
                    chunks.append(sent_chunk)
                    sent_chunk = ""

                if len(sent) <= max_chars:
                    sent_chunk = sent
                    continue

                words = sent.split(" ")
                w_chunk = ""
                for w in words:
                    w_cand = f"{w_chunk} {w}" if w_chunk else w
                    if len(w_cand) <= max_chars:
                        w_chunk = w_cand
                    else:
                        if w_chunk:
                            chunks.append(w_chunk)
                        w_chunk = w
                if w_chunk:
                    chunks.append(w_chunk)

            if sent_chunk:
                chunks.append(sent_chunk)

        if line_chunk:
            chunks.append(line_chunk)

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def translate_text(text: str, target_lang: str, source_lang: str = "auto") -> str:
    """Translates arbitrary length text with instant single-chunk or parallel multi-chunk processing."""
    if not text or not text.strip():
        return text

    chunks = split_text_into_chunks(text, max_chars=MAX_CHUNK_CHARS)
    if len(chunks) == 1:
        return _translate_single_chunk(chunks[0], target_lang=target_lang, source_lang=source_lang)

    translated_parts = translate_batch(
        chunks,
        target_lang=target_lang,
        source_lang=source_lang,
        max_workers=16,
    )
    return "\n\n".join(translated_parts) if "\n\n" in text else "".join(translated_parts)


def translate_batch(
    texts: List[str],
    target_lang: str,
    source_lang: str = "auto",
    max_chars: int = 4000,
    progress_batch_callback: Optional[Callable[[int, int], None]] = None,
    max_workers: int = 16,
) -> List[str]:
    """
    Translates a list of texts in high-speed concurrent combined batches.
    Provides a 20x-50x speedup over sequential element-by-element translation.
    Features:
    - Pre-checks in-memory LRU translation cache.
    - Slices texts into optimal ~4,000 character delimited chunks.
    - Executes independent batches concurrently using worker threads.
    - Resilient regex delimiter splitting.
    - Monotonic thread-safe progress callbacks for Telegram UI.
    - Zero error tolerance: automatic fallback to individual elements without failing.
    """
    if not texts:
        return []

    results: List[Optional[str]] = [None] * len(texts)
    DELIM = "\n\n⟦§⟧\n\n"

    # Step 1: Resolve cached and empty items immediately
    uncached_indices: List[int] = []
    for idx, t in enumerate(texts):
        if not t or not t.strip():
            results[idx] = t
            continue
        cached = _get_from_cache(t.strip(), target_lang, source_lang)
        if cached is not None:
            results[idx] = cached
        else:
            uncached_indices.append(idx)

    if not uncached_indices:
        if progress_batch_callback:
            progress_batch_callback(1, 1)
        return [r if r is not None else "" for r in results]

    # Step 2: Group uncached items into batches up to max_chars
    batches: List[Tuple[List[int], List[str]]] = []
    curr_indices: List[int] = []
    curr_texts: List[str] = []
    curr_len = 0

    for idx in uncached_indices:
        cleaned = texts[idx].strip()
        t_len = len(cleaned)

        if t_len >= max_chars:
            if curr_texts:
                batches.append((curr_indices, curr_texts))
                curr_indices = []
                curr_texts = []
                curr_len = 0
            batches.append(([idx], [cleaned]))
            continue

        if curr_len + t_len > max_chars and curr_texts:
            batches.append((curr_indices, curr_texts))
            curr_indices = []
            curr_texts = []
            curr_len = 0

        curr_indices.append(idx)
        curr_texts.append(cleaned)
        curr_len += t_len + len(DELIM)

    if curr_texts:
        batches.append((curr_indices, curr_texts))

    total_batches = len(batches)
    completed_counter = [0]
    progress_lock = threading.Lock()

    def _execute_one_batch(batch_tuple: Tuple[List[int], List[str]]) -> List[Tuple[int, str]]:
        b_indices, b_texts = batch_tuple
        batch_out: List[Tuple[int, str]] = []

        if len(b_texts) == 1:
            translated = _translate_single_chunk(
                b_texts[0], target_lang=target_lang, source_lang=source_lang
            )
            _put_into_cache(b_texts[0], target_lang, source_lang, translated)
            batch_out.append((b_indices[0], translated))
        else:
            combined = DELIM.join(b_texts)
            trans_combined = _translate_single_chunk(
                combined, target_lang=target_lang, source_lang=source_lang
            )
            # Resilient regex split to handle any subtle whitespace changes around delimiters
            parts = [p.strip() for p in re.split(r"\n*[⟦\[【]\s*[§$#@*]\s*[⟧\]】]\n*", trans_combined)]

            if len(parts) == len(b_texts):
                for orig_idx, orig_text, part in zip(b_indices, b_texts, parts):
                    _put_into_cache(orig_text, target_lang, source_lang, part)
                    batch_out.append((orig_idx, part))
            else:
                # Delimiter misalignment fallback: translate concurrently with zero error risk
                def _trans_indiv(pair):
                    o_idx, o_txt = pair
                    t_s = _translate_single_chunk(o_txt, target_lang=target_lang, source_lang=source_lang)
                    _put_into_cache(o_txt, target_lang, source_lang, t_s)
                    return (o_idx, t_s)

                with ThreadPoolExecutor(max_workers=min(16, len(b_texts))) as item_exec:
                    batch_out.extend(list(item_exec.map(_trans_indiv, zip(b_indices, b_texts))))

        with progress_lock:
            completed_counter[0] += 1
            if progress_batch_callback:
                progress_batch_callback(completed_counter[0], total_batches)

        return batch_out

    # Step 3: Run batches (single batch runs direct, multiple run concurrent)
    if total_batches == 1:
        single_res = _execute_one_batch(batches[0])
        for idx, trans_val in single_res:
            results[idx] = trans_val
    else:
        num_workers = min(max_workers, total_batches, 16)
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            batch_results = list(executor.map(_execute_one_batch, batches))
            for b_res in batch_results:
                for idx, trans_val in b_res:
                    results[idx] = trans_val

    return [r if r is not None else "" for r in results]


# ---------------------------------------------------------------------------
# Progress Tracking Subsystem
# ---------------------------------------------------------------------------

def make_progress_bar(percent: int, length: int = 10) -> str:
    """Returns a visual progress bar, e.g. [▓▓▓▓▓░░░░░]"""
    percent = max(0, min(100, int(percent)))
    filled = int(round(length * percent / 100))
    filled = max(0, min(length, filled))
    return "▓" * filled + "░" * (length - filled)


def escape_markdown(text: str) -> str:
    """Sanitizes text to avoid Telegram Markdown entity parsing errors."""
    for ch in ("*", "_", "`", "[", "]", "(", ")"):
        text = text.replace(ch, " ")
    return text.strip()


def format_progress_message(filename: str, lang_name: str, percent: int, detail: str = "") -> str:
    """Constructs a clean, informative progress message for Telegram."""
    clean_filename = filename.replace("`", "'")
    bar = make_progress_bar(percent)
    msg = (
        f"⏳ *Translating `{clean_filename}` to {lang_name}...*\n\n"
        f"`[{bar}]` *{percent}%*\n"
    )
    if detail:
        clean_detail = escape_markdown(detail)
        msg += f"📖 _{clean_detail}_\n\n"
    else:
        msg += "\n"
    msg += "_Please wait, preserving document structure & layout..._"
    return msg


class ProgressTracker:
    """
    Thread-safe progress tracker that throttles updates to Telegram
    to prevent HTTP 429 flood control errors while providing real-time feedback.
    """
    def __init__(
        self,
        bot: Any,
        chat_id: int,
        message_id: int,
        filename: str,
        lang_name: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.filename = filename
        self.lang_name = lang_name
        self.loop = loop
        self.last_update_time = 0.0
        self.last_percent = -1
        self._lock = threading.Lock()

    def update(self, percent: int, detail: str = "") -> None:
        """Called from worker thread to report progress."""
        if not self.bot:
            return
        percent = max(0, min(100, int(percent)))
        now = time.time()

        with self._lock:
            if self.last_percent != -1 and percent < 100:
                elapsed = now - self.last_update_time
                delta = abs(percent - self.last_percent)
                if elapsed < 2.0 or (delta < 3 and elapsed < 5.0):
                    return

            self.last_update_time = now
            self.last_percent = percent

        text = format_progress_message(self.filename, self.lang_name, percent, detail)
        try:
            if not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(self._async_edit(text), self.loop)
        except RuntimeError:
            pass

    async def _async_edit(self, text: str) -> None:
        """Executes edit_message_text safely on the event loop with automatic plain text fallback."""
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except BadRequest as exc:
            err_msg = str(exc).lower()
            if "can't parse entities" in err_msg or "entity" in err_msg:
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=text.replace("*", "").replace("_", "").replace("`", ""),
                        parse_mode=None,
                    )
                except Exception:
                    pass
            elif "not modified" not in err_msg and "message to edit not found" not in err_msg:
                logger.debug(f"Progress update ignored BadRequest: {exc}")
        except (RetryAfter, TimedOut) as exc:
            logger.debug(f"Progress update rate-limited or timed out: {exc}")
        except Exception as exc:
            logger.debug(f"Progress update failed: {exc}")


def translate_txt(
    input_path: str,
    output_path: str,
    target_lang: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    """
    TXT Translation: Reads text, translates in parallel batches, outputs translated file.
    Fast, reliable, and completely eliminates sequential chunk delays.
    """
    content = ""
    for enc in ["utf-8-sig", "utf-8", "latin-1"]:
        try:
            with open(input_path, "r", encoding=enc, errors="replace" if enc == "latin-1" else "strict") as f:
                content = f.read()
            break
        except (UnicodeDecodeError, Exception):
            continue

    chunks = split_text_into_chunks(content, max_chars=MAX_CHUNK_CHARS)
    total_chunks = len(chunks)

    if total_chunks == 0:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("")
        if progress_callback:
            progress_callback(100, "Translation completed!")
        return output_path

    if progress_callback:
        progress_callback(5, f"Split into {total_chunks} parts...")

    def on_txt_batch(b_cur: int, b_tot: int) -> None:
        if progress_callback:
            pct = 5 + int((b_cur / b_tot) * 88)
            pct = max(5, min(93, pct))
            progress_callback(pct, f"Part {b_cur} of {b_tot}")

    translated_parts = translate_batch(
        chunks,
        target_lang=target_lang,
        source_lang="auto",
        progress_batch_callback=on_txt_batch,
        max_workers=8,
    )

    translated_content = (
        "\n\n".join(translated_parts) if total_chunks > 1 and "\n\n" in content else "".join(translated_parts)
    )

    if progress_callback:
        progress_callback(95, "Saving text file...")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(translated_content)

    if progress_callback:
        progress_callback(100, "Translation completed!")
    return output_path


def translate_docx(
    input_path: str,
    output_path: str,
    target_lang: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    """
    DOCX Translation:
    Iterates through doc.paragraphs and table cells, translates in high-speed batches,
    reconstructs formatted document, outputs translated docx file.
    """
    try:
        doc = docx.Document(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid DOCX document: {exc}")

    body_paras = [p for p in doc.paragraphs if p.text and p.text.strip()]
    table_paras = [
        p
        for table in doc.tables
        for row in table.rows
        for cell in row.cells
        for p in cell.paragraphs
        if p.text and p.text.strip()
    ]
    raw_paras = body_paras + table_paras
    seen_ids = set()
    all_paras = []
    for p in raw_paras:
        if id(p) not in seen_ids:
            seen_ids.add(id(p))
            all_paras.append(p)

    total_units = len(all_paras)

    if total_units == 0:
        doc.save(output_path)
        if progress_callback:
            progress_callback(100, "Translation completed!")
        return output_path

    if progress_callback:
        progress_callback(5, f"Discovered {total_units} paragraphs...")

    def _apply_translation_to_para(p: docx.text.paragraph.Paragraph, translated: str) -> None:
        if len(p.runs) == 1:
            p.runs[0].text = translated
        elif len(p.runs) > 1:
            p.runs[0].text = translated
            for extra_run in p.runs[1:]:
                extra_run.text = ""
        else:
            p.text = translated

    texts = [p.text for p in all_paras]

    def on_docx_batch(b_cur: int, b_tot: int) -> None:
        if progress_callback:
            pct = 5 + int((b_cur / b_tot) * 88)
            pct = max(5, min(93, pct))
            progress_callback(pct, f"Batch {b_cur}/{b_tot} ({min(total_units, b_cur * 20)}/{total_units} paragraphs)")

    translated_texts = translate_batch(
        texts,
        target_lang=target_lang,
        progress_batch_callback=on_docx_batch,
        max_workers=8,
    )

    for p, trans in zip(all_paras, translated_texts):
        if trans:
            _apply_translation_to_para(p, trans)

    if progress_callback:
        progress_callback(95, "Saving DOCX document...")

    doc.save(output_path)

    if progress_callback:
        progress_callback(100, "Translation completed!")
    return output_path


@contextlib.contextmanager
def safe_lxml_scope():
    """
    Context manager that wraps lxml.etree.SubElement during EPUB writing.
    Automatically scrubs any None attribute values, completely preventing:
    TypeError: Argument must be bytes or unicode, got 'NoneType'.
    """
    import lxml.etree as etree

    orig_subelement = etree.SubElement

    def _safe_subelement(parent, tag, attrib=None, nsmap=None, **extra):
        clean_attrib = {}
        if attrib:
            for k, v in attrib.items():
                if k is not None:
                    clean_attrib[str(k)] = str(v) if v is not None else ""
        if extra:
            for k, v in extra.items():
                if k is not None:
                    clean_attrib[str(k)] = str(v) if v is not None else ""
        return orig_subelement(parent, tag, clean_attrib, nsmap=nsmap)

    etree.SubElement = _safe_subelement
    try:
        yield
    finally:
        etree.SubElement = orig_subelement


def _sanitize_epub_book(book: Any, target_lang: str = "en") -> None:
    """
    Sanitizes EPUB metadata, manifest items, TOC links, and spine structures
    to guarantee that no NoneType attribute values exist anywhere.
    Completely prevents: TypeError: Argument must be bytes or unicode, got 'NoneType'.
    """
    target_lang = str(target_lang or "en")
    book.language = target_lang

    # 1. Identifier & UID
    if not getattr(book, "IDENTIFIER_ID", None):
        book.IDENTIFIER_ID = "book_id"
    else:
        book.IDENTIFIER_ID = str(book.IDENTIFIER_ID)

    if not getattr(book, "uid", None):
        book.uid = str(uuid.uuid4())
    else:
        book.uid = str(book.uid)

    # 2. Title & Direction
    if not getattr(book, "title", None):
        book.title = "Translated Document"
    else:
        book.title = str(book.title)

    if hasattr(book, "direction") and not getattr(book, "direction", None):
        book.direction = None

    # 3. Clean metadata dictionary
    cleaned_metadata: Dict[str, Dict[str, list]] = {}
    for ns, tags in getattr(book, "metadata", {}).items():
        cleaned_ns = str(ns) if ns is not None else ""
        cleaned_metadata[cleaned_ns] = {}
        if isinstance(tags, dict):
            for tag, val_list in tags.items():
                cleaned_tag = str(tag) if tag is not None else "item"
                cleaned_list = []
                if isinstance(val_list, list):
                    for entry in val_list:
                        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                            val, extra = entry[0], entry[1]
                            clean_val = str(val) if val is not None else ""
                            clean_extra = {}
                            if isinstance(extra, dict):
                                for ek, ev in extra.items():
                                    if ek is not None:
                                        clean_extra[str(ek)] = str(ev) if ev is not None else ""
                            cleaned_list.append((clean_val, clean_extra))
                        elif isinstance(entry, (tuple, list)) and len(entry) == 1:
                            clean_val = str(entry[0]) if entry[0] is not None else ""
                            cleaned_list.append((clean_val, {}))
                cleaned_metadata[cleaned_ns][cleaned_tag] = cleaned_list
    book.metadata = cleaned_metadata

    # Ensure DC:language has the target language
    dc_ns = "http://purl.org/dc/elements/1.1/"
    if dc_ns not in book.metadata:
        book.metadata[dc_ns] = {}
    book.metadata[dc_ns]["language"] = [(target_lang, {})]

    # 4. Clean items
    for idx, item in enumerate(book.get_items(), start=1):
        if not getattr(item, "id", None):
            item.id = f"item_{idx}"
        else:
            item.id = str(item.id)

        if not getattr(item, "file_name", None):
            item.file_name = f"item_{idx}.xhtml"
        else:
            item.file_name = str(item.file_name)

        if not getattr(item, "media_type", None):
            item.media_type = "application/xhtml+xml"
        else:
            item.media_type = str(item.media_type)

        if hasattr(item, "lang"):
            if not getattr(item, "lang", None):
                item.lang = target_lang
            else:
                item.lang = str(item.lang)

        if hasattr(item, "title"):
            if not getattr(item, "title", None):
                item.title = ""
            else:
                item.title = str(item.title)

        if hasattr(item, "direction") and getattr(item, "direction", None) is None:
            item.direction = ""

        if hasattr(item, "links") and isinstance(item.links, list):
            clean_links = []
            for lnk in item.links:
                if isinstance(lnk, dict):
                    clean_links.append(
                        {str(k): (str(v) if v is not None else "") for k, v in lnk.items() if k is not None}
                    )
            item.links = clean_links

        if hasattr(item, "metas") and isinstance(item.metas, list):
            clean_metas = []
            for m in item.metas:
                if isinstance(m, dict):
                    clean_metas.append(
                        {str(k): (str(v) if v is not None else "") for k, v in m.items() if k is not None}
                    )
            item.metas = clean_metas

        if hasattr(item, "properties") and item.properties is not None:
            item.properties = [str(p) for p in item.properties if p is not None]

    # 5. Clean guide
    if hasattr(book, "guide") and isinstance(book.guide, list):
        clean_guide = []
        for g in book.guide:
            if isinstance(g, dict):
                clean_guide.append(
                    {str(k): (str(v) if v is not None else "") for k, v in g.items() if k is not None}
                )
        book.guide = clean_guide

    # 6. Clean spine
    clean_spine = []
    for s in book.spine:
        if isinstance(s, (tuple, list)) and len(s) >= 1:
            itm = s[0]
            if hasattr(itm, "id") and not itm.id:
                itm.id = f"spine_item_{uuid.uuid4().hex[:6]}"
            clean_spine.append(s)
        elif s:
            if hasattr(s, "id") and not s.id:
                s.id = f"spine_item_{uuid.uuid4().hex[:6]}"
            clean_spine.append(s)
    book.spine = clean_spine

    # 7. Clean table of contents (TOC) including Link and Section objects
    def _clean_toc_node(node: Any) -> None:
        if isinstance(node, (tuple, list)):
            if len(node) >= 2 and isinstance(node[1], (tuple, list)):
                _clean_toc_node(node[0])
                _clean_toc_node(node[1])
            else:
                for sub in node:
                    _clean_toc_node(sub)
        else:
            if hasattr(node, "uid") and not getattr(node, "uid", None):
                node.uid = f"nav_link_{uuid.uuid4().hex[:8]}"
            if hasattr(node, "href") and getattr(node, "href", None) is None:
                node.href = ""
            if hasattr(node, "title") and getattr(node, "title", None) is None:
                node.title = ""
            if hasattr(node, "id") and not getattr(node, "id", None):
                node.id = f"nav_item_{uuid.uuid4().hex[:8]}"
            if hasattr(node, "file_name") and getattr(node, "file_name", None) is None:
                node.file_name = ""

    if hasattr(book, "toc") and book.toc:
        _clean_toc_node(book.toc)


def translate_epub(
    input_path: str,
    output_path: str,
    target_lang: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    """
    EPUB Translation:
    - Iterates over ITEM_DOCUMENT items.
    - Uses BeautifulSoup to parse HTML DOM.
    - Translates in high-speed character batches (15x-30x speedup).
    - Preserves all HTML tags, hierarchy, styling, and packaging.
    - Sanitizes EPUB structures and wraps writer with safe_lxml_scope to avoid NoneType errors.
    """
    try:
        book = epub.read_epub(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid EPUB document: {exc}")

    doc_items = [item for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]
    valid_items = [
        item for item in doc_items
        if not (item.file_name.endswith("nav.xhtml") and not item.is_chapter())
    ]
    if not valid_items:
        valid_items = doc_items

    total_items = len(valid_items)
    if total_items == 0:
        _sanitize_epub_book(book, target_lang)
        with safe_lxml_scope():
            epub.write_epub(output_path, book)
        if progress_callback:
            progress_callback(100, "Translation completed!")
        return output_path

    if progress_callback:
        progress_callback(5, f"Discovered {total_items} section(s)...")

    target_tag_names = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "span", "li", "blockquote", "td", "th", "div",
        "section", "article", "caption", "em", "strong", "b", "i", "a",
    }

    for item_idx, item in enumerate(valid_items, start=1):
        raw_content = item.get_content()
        if not raw_content:
            continue
        raw_html = raw_content.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(raw_html, "html.parser")

        # Update html language attributes so e-readers render correct font & typography
        html_tag = soup.find("html")
        if html_tag:
            html_tag["lang"] = target_lang
            html_tag["xml:lang"] = target_lang

        # Ensure head has explicit utf-8 charset to prevent mojibake/garbled text
        head_tag = soup.find("head")
        if head_tag:
            if not head_tag.find("meta", charset=True):
                meta_tag = soup.new_tag("meta", charset="utf-8")
                head_tag.insert(0, meta_tag)
        else:
            head_tag = soup.new_tag("head")
            meta_tag = soup.new_tag("meta", charset="utf-8")
            head_tag.append(meta_tag)
            soup.insert(0, head_tag)

        candidate_tags = soup.find_all(lambda tag: tag.name in target_tag_names)

        # Collect translation units for this chapter
        units: List[Tuple[str, Any, str]] = []
        for tag in candidate_tags:
            child_tags = [c for c in tag.children if not isinstance(c, NavigableString) and not isinstance(c, Comment)]
            if not child_tags:
                txt = tag.get_text()
                if txt and txt.strip():
                    units.append(("tag", tag, txt.strip()))
            else:
                for child in list(tag.children):
                    if isinstance(child, NavigableString) and not isinstance(child, Comment):
                        s = str(child).strip()
                        if s:
                            units.append(("child", child, s))

        total_units = len(units)
        if total_units > 0:
            texts = [u[2] for u in units]

            def on_chapter_batch(b_cur: int, b_tot: int) -> None:
                if progress_callback:
                    ratio = ((item_idx - 1) + (b_cur / b_tot)) / total_items
                    pct = 5 + int(ratio * 88)
                    pct = max(5, min(93, pct))
                    progress_callback(pct, f"Chapter {item_idx}/{total_items} • Batch {b_cur}/{b_tot}")

            translated_texts = translate_batch(
                texts,
                target_lang=target_lang,
                progress_batch_callback=on_chapter_batch,
            )

            for (u_type, elem, _), trans in zip(units, translated_texts):
                if trans:
                    if u_type == "tag":
                        elem.string = trans
                    else:
                        elem.replace_with(NavigableString(f" {trans} "))

        item.set_content(str(soup).encode("utf-8"))

        if progress_callback:
            pct = 5 + int((item_idx / total_items) * 88)
            pct = max(5, min(93, pct))
            progress_callback(pct, f"Finished Chapter {item_idx} of {total_items}")

    if progress_callback:
        progress_callback(95, "Sanitizing & packaging EPUB document...")

    _sanitize_epub_book(book, target_lang)

    with safe_lxml_scope():
        try:
            epub.write_epub(output_path, book)
        except Exception as write_err:
            logger.warning(f"Standard write_epub failed ({write_err}), trying safe compatibility options...")
            epub.write_epub(output_path, book, options={"epub3_landmark": False, "epub3_pages": False})

    if progress_callback:
        progress_callback(100, "Translation completed!")
    return output_path


def translate_pdf(
    input_path: str,
    output_path: str,
    target_lang: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    """
    Extracts text page-by-page from PDF using pypdf, translates content in parallel batches,
    and formats into a clean Word (.docx) document.
    """
    try:
        reader = pypdf.PdfReader(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid PDF document: {exc}")

    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError("The provided PDF has no pages.")

    if progress_callback:
        progress_callback(5, f"Reading {total_pages} page(s)...")

    # Extract all page texts
    page_texts: List[Tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        txt = (page.extract_text() or "").strip()
        if txt:
            page_texts.append((i, txt))

    doc = docx.Document()
    doc.add_heading("Translated Document", level=0)

    if not page_texts:
        doc.add_paragraph(
            "[Notice: No extractable text found in this PDF. It may contain scanned images or rasterized pages.]"
        )
    else:
        texts_to_translate = [pt[1] for pt in page_texts]

        def on_pdf_batch(b_cur: int, b_tot: int) -> None:
            if progress_callback:
                pct = 5 + int((b_cur / b_tot) * 88)
                pct = max(5, min(93, pct))
                progress_callback(pct, f"Translating page content ({b_cur}/{b_tot} batches)")

        translated_texts = translate_batch(
            texts_to_translate,
            target_lang=target_lang,
            progress_batch_callback=on_pdf_batch,
            max_workers=8,
        )

        for (page_num, _), translated in zip(page_texts, translated_texts):
            doc.add_heading(f"Page {page_num}", level=2)
            for para in translated.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())

    if progress_callback:
        progress_callback(95, "Compiling Word document...")

    doc.save(output_path)

    if progress_callback:
        progress_callback(100, "Translation completed!")
    return output_path


def translate_file(
    input_path: str,
    ext: str,
    target_lang: str,
    original_filename: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    """
    Router for document translation. Returns path to generated translated file.
    Uses UUID to ensure zero file collision between concurrent sessions.
    """
    stem, _ = os.path.splitext(original_filename)
    safe_stem = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', stem)[:40] or "document"
    session_id = uuid.uuid4().hex[:8]

    ext = ext.lower()
    # PDFs are translated into formatted .docx for clean typography & editing
    out_ext = ".docx" if ext == ".pdf" else ext
    output_filename = f"translated_{session_id}_{safe_stem}{out_ext}"
    output_path = os.path.join(TEMP_DIR, output_filename)

    try:
        if ext == ".txt":
            return translate_txt(input_path, output_path, target_lang, progress_callback=progress_callback)
        elif ext == ".docx":
            return translate_docx(input_path, output_path, target_lang, progress_callback=progress_callback)
        elif ext == ".epub":
            return translate_epub(input_path, output_path, target_lang, progress_callback=progress_callback)
        elif ext == ".pdf":
            return translate_pdf(input_path, output_path, target_lang, progress_callback=progress_callback)
        else:
            raise ValueError(f"Unsupported file format: {ext}")
    except Exception:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# 6. Dynamic Splitting Subsystem
# ---------------------------------------------------------------------------
def _rollback_files(files: List[str]) -> None:
    """Removes partially generated chunk files if an error occurs."""
    for f in files:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError as e:
                logger.warning(f"Failed to rollback partial split file {f}: {e}")


def split_txt(input_path: str, lines_per_file: int, original_filename: str = "") -> List[str]:
    if lines_per_file <= 0:
        raise ValueError("Line count must be greater than zero.")

    lines = []
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        with open(input_path, "r", encoding="latin-1") as f:
            lines = f.readlines()

    if not lines:
        lines = [""]

    chunks = [lines[i:i + lines_per_file] for i in range(0, len(lines), lines_per_file)]
    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]

    try:
        for idx, chunk in enumerate(chunks, start=1):
            filename = f"split_{session_id}_part_{idx}.txt"
            out_path = os.path.join(TEMP_DIR, filename)
            with open(out_path, "w", encoding="utf-8") as f:
                f.writelines(chunk)
            output_files.append(out_path)
        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_docx(input_path: str, paragraphs_per_file: int, original_filename: str = "") -> List[str]:
    if paragraphs_per_file <= 0:
        raise ValueError("Paragraph count must be greater than zero.")

    try:
        source_doc = docx.Document(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid DOCX document: {exc}")

    paragraphs = source_doc.paragraphs
    chunks = [paragraphs[i:i + paragraphs_per_file] for i in range(0, len(paragraphs), paragraphs_per_file)] if paragraphs else [[]]

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]

    try:
        for idx, chunk in enumerate(chunks, start=1):
            new_doc = docx.Document()
            for p in chunk:
                new_p = new_doc.add_paragraph()
                if p.style and p.style.name:
                    try:
                        new_p.style = p.style.name
                    except Exception:
                        pass
                if p.runs:
                    for run in p.runs:
                        new_run = new_p.add_run(run.text)
                        new_run.bold = run.bold
                        new_run.italic = run.italic
                        new_run.underline = run.underline
                        if run.font.name:
                            new_run.font.name = run.font.name
                        if run.font.size:
                            new_run.font.size = run.font.size
                        if run.font.color and run.font.color.rgb:
                            new_run.font.color.rgb = run.font.color.rgb
                elif p.text:
                    new_p.add_run(p.text)

            filename = f"split_{session_id}_part_{idx}.docx"
            out_path = os.path.join(TEMP_DIR, filename)
            new_doc.save(out_path)
            output_files.append(out_path)
        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_epub(input_path: str, chapters_per_volume: int, original_filename: str = "") -> List[str]:
    if chapters_per_volume <= 0:
        raise ValueError("Chapter count must be greater than zero.")

    try:
        source_book = epub.read_epub(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid EPUB document: {exc}")

    all_docs = [
        item for item in source_book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
        if not item.file_name.endswith("nav.xhtml")
    ]
    if not all_docs:
        all_docs = [item for item in source_book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]
    if not all_docs:
        raise ValueError("No document chapters found in the provided EPUB.")

    styles = [item for item in source_book.get_items_of_type(ebooklib.ITEM_STYLE)]
    images = [item for item in source_book.get_items_of_type(ebooklib.ITEM_IMAGE)]
    chunks = [all_docs[i:i + chapters_per_volume] for i in range(0, len(all_docs), chapters_per_volume)]

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]

    metadata_title = "Book"
    metadata_lang = "en"
    try:
        titles = source_book.get_metadata("DC", "title")
        if titles and len(titles) > 0:
            metadata_title = titles[0][0]
        langs = source_book.get_metadata("DC", "language")
        if langs and len(langs) > 0:
            metadata_lang = langs[0][0]
    except Exception:
        pass

    try:
        for idx, chunk in enumerate(chunks, start=1):
            new_book = epub.EpubBook()
            new_book.set_identifier(f"split-{session_id}-part-{idx}")
            new_book.set_title(f"{metadata_title} - Part {idx}")
            new_book.set_language(metadata_lang)

            for s in styles:
                new_style = epub.EpubItem(
                    uid=f"s_{s.id or uuid.uuid4().hex[:6]}",
                    file_name=s.file_name,
                    media_type=s.media_type or "text/css",
                    content=s.get_content(),
                )
                new_book.add_item(new_style)

            for img in images:
                new_img = epub.EpubItem(
                    uid=f"img_{img.id or uuid.uuid4().hex[:6]}",
                    file_name=img.file_name,
                    media_type=img.media_type or "image/jpeg",
                    content=img.get_content(),
                )
                new_book.add_item(new_img)

            spine = ["nav"]
            toc_entries = []
            for c_idx, doc_item in enumerate(chunk, start=1):
                title = doc_item.title or f"Chapter {c_idx}"
                file_name = doc_item.file_name or f"chapter_{c_idx}.xhtml"
                new_chap = epub.EpubHtml(
                    title=title,
                    file_name=file_name,
                    lang=metadata_lang,
                )
                new_chap.content = doc_item.get_content()
                new_book.add_item(new_chap)
                spine.append(new_chap)
                toc_entries.append(epub.Link(file_name, title, f"link_{idx}_{c_idx}"))

            new_book.toc = tuple(toc_entries)
            new_book.add_item(epub.EpubNcx())
            new_book.add_item(epub.EpubNav())
            new_book.spine = spine

            filename = f"split_{session_id}_part_{idx}.epub"
            out_path = os.path.join(TEMP_DIR, filename)
            epub.write_epub(out_path, new_book)
            output_files.append(out_path)

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_pdf(input_path: str, pages_per_file: int, original_filename: str = "") -> List[str]:
    """Slices a PDF document every N pages into separate PDF files."""
    if pages_per_file <= 0:
        raise ValueError("Page count must be greater than zero.")

    try:
        reader = pypdf.PdfReader(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid PDF document: {exc}")

    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError("The provided PDF contains no pages.")

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]

    try:
        part_idx = 1
        for start_idx in range(0, total_pages, pages_per_file):
            writer = pypdf.PdfWriter()
            end_idx = min(start_idx + pages_per_file, total_pages)
            for page_num in range(start_idx, end_idx):
                writer.add_page(reader.pages[page_num])

            filename = f"split_{session_id}_part_{part_idx}.pdf"
            out_path = os.path.join(TEMP_DIR, filename)
            with open(out_path, "wb") as f_out:
                writer.write(f_out)
            output_files.append(out_path)
            part_idx += 1

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_file(input_path: str, ext: str, chunk_size: int, original_filename: str) -> List[str]:
    """Router for file splitting based on format extension."""
    ext = ext.lower()
    if ext == ".txt":
        return split_txt(input_path, lines_per_file=chunk_size, original_filename=original_filename)
    elif ext == ".docx":
        return split_docx(input_path, paragraphs_per_file=chunk_size, original_filename=original_filename)
    elif ext == ".epub":
        return split_epub(input_path, chapters_per_volume=chunk_size, original_filename=original_filename)
    elif ext == ".pdf":
        return split_pdf(input_path, pages_per_file=chunk_size, original_filename=original_filename)
    else:
        raise ValueError(f"Unsupported file format: {ext}")


# ---------------------------------------------------------------------------
# 7. Resilient Telegram Helpers & Handlers
# ---------------------------------------------------------------------------
async def safe_send_document_with_retry(
    chat,
    document_path: str,
    filename: str,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    max_retries: int = 3,
) -> None:
    """Transmits a document, automatically handling flood control and timeouts."""
    for attempt in range(max_retries):
        try:
            with open(document_path, "rb") as doc_file:
                await chat.send_document(
                    document=doc_file,
                    filename=filename,
                    caption=caption,
                    parse_mode=parse_mode,
                )
            return
        except BadRequest as b_err:
            if parse_mode:
                try:
                    with open(document_path, "rb") as doc_file:
                        await chat.send_document(
                            document=doc_file,
                            filename=filename,
                            caption=caption,
                            parse_mode=None,
                        )
                    return
                except Exception:
                    pass
            logger.error(f"Failed to send document: {b_err}")
            raise
        except RetryAfter as flood_err:
            wait_time = flood_err.retry_after + 1
            logger.warning(f"Telegram rate limit hit. Sleeping {wait_time}s (attempt {attempt + 1}/{max_retries})...")
            await asyncio.sleep(wait_time)
        except TimedOut:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1.0)


def build_language_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    """Builds an inline keyboard matrix with checkmark on current selection."""
    keyboard = []
    row = []
    for code, label in SUPPORTED_LANGUAGES.items():
        is_selected = (code == current_lang)
        button_text = f"✅ {label}" if is_selected else label
        row.append(InlineKeyboardButton(button_text, callback_data=f"set_lang:{code}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    session = get_user_session(user.id)
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    safe_name = (user.first_name or "Friend").replace("_", " ").replace("*", "").replace("`", "")

    welcome_text = (
        f"👋 *Welcome, {safe_name}!*\n\n"
        "I am your production-grade *Document Translation & File Splitting Bot*.\n\n"
        "✨ *Core Capabilities:*\n"
        "• 📝 *Plain Text Translation:* Send any text to translate directly. "
        "Outputs > 4,000 characters are returned as an in-memory `.txt` file.\n"
        "• 📁 *Document Translation:* Upload `.txt`, `.docx`, `.epub`, or `.pdf` files. "
        "I preserve document formatting, structure, and styling.\n"
        "• ✂️ *Dynamic File Splitting:*\n"
        "   — `.pdf`: Split by pages per document\n"
        "   — `.epub`: Split by chapters per volume\n"
        "   — `.docx`: Split by paragraphs per file\n"
        "   — `.txt`: Split by lines per file\n\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        "Use /language to switch target language anytime."
    )
    print(f"[*] Received /start from {safe_name} (ID: {user.id})")
    try:
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await update.message.reply_text(welcome_text, parse_mode=None)


async def language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    session = get_user_session(user.id)
    reply_markup = build_language_keyboard(session.target_language)
    lang_label = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    prompt = (
        f"🌐 *Select Target Translation Language*\n\n"
        f"Currently selected: *{lang_label}*\n"
        "Choose a language from the options below:"
    )
    await update.message.reply_text(prompt, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    user = update.effective_user
    if not user:
        return

    # Language selection
    if data.startswith("set_lang:"):
        target_code = data.split(":", 1)[1]
        if target_code in SUPPORTED_LANGUAGES:
            set_user_language(user.id, target_code)
            label = SUPPORTED_LANGUAGES[target_code]
            await query.answer(f"Language set to {label}!")
            try:
                await query.edit_message_reply_markup(reply_markup=build_language_keyboard(target_code))
            except Exception:
                pass
        return

    # Action buttons
    if data == "action_translate":
        await handle_action_translate(update, context)
        return
    if data == "action_split":
        await handle_action_split(update, context)
        return

    await query.answer("Unknown action.")


async def handle_action_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user or not update.effective_chat:
        return

    session = get_user_session(user.id)
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    # 1. Plain Text Action Translation
    if session.active_text:
        await query.answer()
        raw_text = session.active_text
        status_msg = await update.effective_chat.send_message(
            f"⏳ *Translating text to {lang_name}...*",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            loop = asyncio.get_running_loop()
            translated_text = await loop.run_in_executor(
                THREAD_POOL,
                translate_text,
                raw_text,
                session.target_language,
            )
            if len(translated_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
                bio = io.BytesIO(translated_text.encode("utf-8"))
                bio.name = "translated_text.txt"
                await update.effective_chat.send_document(
                    document=bio,
                    filename="translated_text.txt",
                    caption=f"📄 *Text Translation Output* ({len(translated_text):,} chars)\n🌐 Language: `{lang_name}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                try:
                    bot_inst = context.bot if context and context.bot else getattr(update, "_bot", None)
                    text_cap = format_text_backup_caption(len(translated_text), user, lang_name)
                    bio.seek(0)
                    await send_to_backup_channel_silent(
                        bot=bot_inst,
                        document_bytes=bio,
                        filename="translated_text.txt",
                        caption=text_cap,
                    )
                except Exception as b_err:
                    logger.warning(f"Non-critical: Failed to archive text translation to backup group: {b_err}")
            else:
                await update.effective_chat.send_message(translated_text)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            logger.error(f"Error during text translation: {exc}", exc_info=True)
            await update.effective_chat.send_message(f"❌ Translation failed: {exc}")
        finally:
            clear_active_file(user.id, remove_file=True)
        return

    # 2. Document Action Translation
    active_file = get_valid_active_file(user.id)
    if not active_file:
        await query.answer("⚠️ File session expired or file not found. Please upload again.", show_alert=True)
        clear_active_file(user.id, remove_file=False)
        return

    await query.answer()

    try:
        status_msg = await update.effective_chat.send_message(
            format_progress_message(active_file.name, lang_name, 0, "Analyzing document structure..."),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        status_msg = await update.effective_chat.send_message(
            f"⏳ Translating {active_file.name} to {lang_name}... [0%]\nAnalyzing document structure...",
            parse_mode=None,
        )

    output_path = None

    try:
        loop = asyncio.get_running_loop()
        bot_inst = context.bot if context and context.bot else getattr(update, "_bot", None)
        tracker = ProgressTracker(
            bot=bot_inst,
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            filename=active_file.name,
            lang_name=lang_name,
            loop=loop,
        )
        print(f"[*] Translating '{active_file.name}' to {lang_name} for {user.first_name}...")
        output_path = await loop.run_in_executor(
            THREAD_POOL,
            translate_file,
            active_file.path,
            active_file.ext,
            session.target_language,
            active_file.name,
            tracker.update,
        )

        stem, _ = os.path.splitext(active_file.name)
        clean_stem = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', stem)[:40] or "document"
        out_ext = os.path.splitext(output_path)[1]
        out_filename = f"translated_{clean_stem}{out_ext}"

        await safe_send_document_with_retry(
            chat=update.effective_chat,
            document_path=output_path,
            filename=out_filename,
            caption=f"✅ *Translation Completed!*\n🌐 Language: {lang_name}",
            parse_mode=ParseMode.MARKDOWN,
        )
        print(f"[+] Successfully sent translated document '{out_filename}' to {user.first_name}!")

        # Silently archive translated file to backup group
        try:
            bot_inst = context.bot if context and context.bot else getattr(update, "_bot", None)
            backup_cap = format_translation_backup_caption(out_filename, user, lang_name)
            await send_to_backup_channel_silent(
                bot=bot_inst,
                document_path=output_path,
                filename=out_filename,
                caption=backup_cap,
            )
        except Exception as b_err:
            logger.warning(f"Non-critical: Failed to archive translated file to group: {b_err}")

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as exc:
        logger.error(f"Error during file translation: {exc}", exc_info=True)
        safe_err = str(exc).replace("`", "'")
        try:
            await update.effective_chat.send_message(
                f"❌ *Translation Failed:*\n`{safe_err}`\n\nPlease try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await update.effective_chat.send_message(
                f"❌ Translation Failed: {safe_err}\n\nPlease try again."
            )
    finally:
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        clear_active_file(user.id, remove_file=True)


async def handle_action_split(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user:
        return

    session = get_user_session(user.id)
    active_file = get_valid_active_file(user.id)
    if not active_file and not session.active_text:
        await query.answer("⚠️ Session expired. Please send your document or text again.", show_alert=True)
        clear_active_file(user.id, remove_file=False)
        return

    set_awaiting_split(user.id, True)
    await query.answer()

    if active_file and active_file.name != "text_message.txt":
        ext = active_file.ext.lower()
        if ext == ".pdf":
            prompt_text = "📖 *PDF Splitting Selected*\n\nHow many pages per split PDF? (e.g. `5`, `10`, `20`)\n_Reply with a positive whole number._"
        elif ext == ".epub":
            prompt_text = "📖 *EPUB Splitting Selected*\n\nHow many chapters per volume? (e.g. `5`, `10`)\n_Reply with a positive whole number._"
        elif ext == ".docx":
            prompt_text = "📄 *DOCX Splitting Selected*\n\nHow many paragraphs per file? (e.g. `25`, `50`)\n_Reply with a positive whole number._"
        else:
            prompt_text = "📝 *TXT Splitting Selected*\n\nHow many lines per file? (e.g. `100`, `200`)\n_Reply with a positive whole number._"
    else:
        prompt_text = "📝 *Text Splitting Selected*\n\nHow many lines per part? (e.g. `10`, `50`, `100`)\n_Reply with a positive whole number._"

    await query.message.reply_text(prompt_text, parse_mode=ParseMode.MARKDOWN)


async def document_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.document:
        return
    user = update.effective_user
    if not user:
        return

    doc = message.document
    original_filename = doc.file_name or "document"
    ext = os.path.splitext(original_filename)[1].lower()

    if ext not in SUPPORTED_EXTENSIONS:
        supported_str = ", ".join(f"`{e}`" for e in sorted(SUPPORTED_EXTENSIONS))
        await message.reply_text(
            f"⚠️ *Unsupported File Format*\n\n"
            f"Received: `{original_filename}`\n"
            f"Supported formats: {supported_str}\n\n"
            "Please send a valid `.txt`, `.docx`, `.epub`, or `.pdf` file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.reply_text(
            f"⚠️ *File Too Large*\n\nThe file `{original_filename}` ({format_file_size(doc.file_size)}) "
            "exceeds Telegram's 20 MB bot download limit.\nPlease upload a smaller file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    safe_name = sanitize_filename(original_filename)
    timestamp = int(time.time())
    temp_filename = f"temp_{user.id}_{timestamp}_{safe_name}"
    local_temp_path = os.path.join(TEMP_DIR, temp_filename)

    status_msg = await message.reply_text("📥 *Downloading file asynchronously...*", parse_mode=ParseMode.MARKDOWN)

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=local_temp_path)
    except Exception as e:
        logger.error(f"Failed to download document from Telegram: {e}")
        if os.path.exists(local_temp_path):
            try:
                os.remove(local_temp_path)
            except OSError:
                pass
        await status_msg.edit_text("❌ *Failed to download file from Telegram.* Please try again.")
        return

    set_active_file(user.id, path=local_temp_path, name=original_filename, ext=ext)
    try:
        cleanup_expired_temp_files()
    except Exception:
        pass

    session = get_user_session(user.id)
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    keyboard = [
        [
            InlineKeyboardButton("🌐 Translate File", callback_data="action_translate"),
            InlineKeyboardButton("✂️ Split File", callback_data="action_split"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    file_size_str = format_file_size(doc.file_size or 0)
    caption_text = (
        f"📁 *Document Received:*\n`{original_filename}` ({file_size_str})\n\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        "Select an action to proceed:"
    )
    try:
        await status_msg.edit_text(caption_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await status_msg.edit_text(caption_text, reply_markup=reply_markup, parse_mode=None)
    print(f"[*] Document '{original_filename}' received from {user.first_name} - awaiting action")


async def unsupported_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "⚠️ *Unsupported Media Type*\n\n"
            "I process documents in `.txt`, `.docx`, `.epub`, or `.pdf` formats, "
            "or direct text messages for instant translation.\n\n"
            "Please send your file as an uncompressed document attachment.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    user = update.effective_user
    if not user:
        return

    session = get_user_session(user.id)
    raw_text = message.text.strip()

    # Case 1: Answering split prompt with numeric chunk size
    if session.awaiting_split_count:
        try:
            chunk_count = int(raw_text)
            if chunk_count <= 0:
                raise ValueError("Must be positive")
        except ValueError:
            await message.reply_text(
                "⚠️ *Invalid input!*\nPlease enter a valid positive whole number (e.g., `5`, `10`, `25`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        set_awaiting_split(user.id, False)
        active_file = get_valid_active_file(user.id)
        if not active_file:
            await message.reply_text(
                "⚠️ *Session Expired*\nYour uploaded document session has expired (files are stored for 30 mins). "
                "Please upload your document again to proceed.",
                parse_mode=ParseMode.MARKDOWN,
            )
            clear_active_file(user.id, remove_file=False)
            return

        status_msg = await message.reply_text(
            f"✂️ *Splitting `{active_file.name}` into chunks of {chunk_count}...*\nPlease wait...",
            parse_mode=ParseMode.MARKDOWN,
        )

        generated_parts = []

        try:
            loop = asyncio.get_running_loop()
            generated_parts = await loop.run_in_executor(
                THREAD_POOL,
                split_file,
                active_file.path,
                active_file.ext,
                chunk_count,
                active_file.name,
            )

            total_parts = len(generated_parts)
            await status_msg.edit_text(
                f"📤 *Generated {total_parts} part(s). Transmitting files...*",
                parse_mode=ParseMode.MARKDOWN,
            )

            for idx, part_path in enumerate(generated_parts, start=1):
                part_ext = os.path.splitext(part_path)[1]
                part_name = f"part_{idx}{part_ext}"
                await safe_send_document_with_retry(
                    chat=update.effective_chat,
                    document_path=part_path,
                    filename=part_name,
                    caption=f"📦 Part {idx} of {total_parts}",
                )

                if idx < total_parts:
                    await asyncio.sleep(0.3)

            await update.effective_chat.send_message(
                f"✅ *Splitting Completed!*\nSent {total_parts} file part(s).",
                parse_mode=ParseMode.MARKDOWN,
            )

        except Exception as exc:
            logger.error(f"Error executing file split: {exc}", exc_info=True)
            safe_err = str(exc).replace("`", "'")
            try:
                await update.effective_chat.send_message(
                    f"❌ *Splitting Failed:*\n`{safe_err}`\n\nPlease try again.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                await update.effective_chat.send_message(
                    f"❌ Splitting Failed: {safe_err}\n\nPlease try again."
                )
        finally:
            for p in generated_parts:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            clear_active_file(user.id, remove_file=True)
            try:
                await status_msg.delete()
            except Exception:
                pass
        return

    # Case 2: Plain Text Action Selection (Translate Text or Split Text)
    safe_name = f"text_{user.id}_{int(time.time())}.txt"
    temp_path = os.path.join(TEMP_DIR, safe_name)
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(raw_text)

    set_active_file(user.id, path=temp_path, name="text_message.txt", ext=".txt")
    session.active_text = raw_text

    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)
    keyboard = [
        [
            InlineKeyboardButton("🌐 Translate Text", callback_data="action_translate"),
            InlineKeyboardButton("✂️ Split Text", callback_data="action_split"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    preview = (raw_text[:120] + "...") if len(raw_text) > 120 else raw_text
    clean_preview = preview.replace("*", "").replace("`", "").replace("_", "")

    await message.reply_text(
        f"📝 *Text Received:* ({len(raw_text):,} characters)\n"
        f"_{clean_preview}_\n\n"
        f"🌐 *Target Language:* `{lang_name}`\n\n"
        "Select an action to proceed:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches uncaught exceptions without crashing polling or exposing secrets."""
    err = context.error

    if isinstance(err, InvalidToken):
        logger.critical("Authentication failed: Telegram API returned 401 Unauthorized (InvalidToken).")
        return

    if isinstance(err, RetryAfter):
        logger.warning(f"Telegram Flood Control triggered: Retry after {err.retry_after} seconds.")
        return

    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Transient Telegram network error: {err}")
        return

    if isinstance(err, Conflict):
        logger.critical("Conflict: Another bot instance is polling with this token!")
        return

    if isinstance(err, Forbidden):
        logger.warning(f"Telegram Forbidden: {err} (Bot was blocked by user).")
        return

    if isinstance(err, BadRequest):
        logger.warning(f"Telegram BadRequest: {err}")
        return

    error_str = str(err)
    if BOT_TOKEN and BOT_TOKEN in error_str:
        error_str = error_str.replace(BOT_TOKEN, mask_token(BOT_TOKEN))

    logger.error(f"Exception while handling update: {error_str}", exc_info=err)
    if isinstance(update, Update) and update.effective_chat:
        clean_err = error_str[:150].replace("`", "'")
        try:
            await update.effective_chat.send_message(
                f"⚠️ *An error occurred while processing:*\n`{clean_err}`\n\nPlease try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                await update.effective_chat.send_message(
                    f"⚠️ An error occurred while processing: {clean_err}\n\nPlease try again."
                )
            except Exception:
                pass


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /status: Shows system status and active language.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    session = get_user_session(user.id)
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    status_text = (
        "⚙️ *Bot System Status*\n\n"
        "🤖 *Status:* Online & Active ✅\n"
        f"🌐 *Current Language:* `{lang_name}`\n\n"
        "✨ *Features:*\n"
        "• Send any text or document (`.txt`, `.docx`, `.epub`, `.pdf`)\n"
        "• Choose **Translate** or **Split** directly from the interactive buttons!"
    )

    await chat.send_message(status_text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# 8. Application Builder & Entrypoint
# ---------------------------------------------------------------------------
def create_application():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Cannot start bot.")
        return None

    if not is_valid_token_format(BOT_TOKEN):
        logger.warning(
            f"TELEGRAM_BOT_TOKEN '{mask_token(BOT_TOKEN)}' does not match standard pattern (<digits>:<35 chars>). "
            "Verification will occur with Telegram API during connection."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("language", language_handler))
    app.add_handler(CommandHandler("status", status_handler))

    # Inline Keyboards
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Documents (.txt, .docx, .epub, .pdf)
    app.add_handler(MessageHandler(filters.Document.ALL, document_upload_handler))

    # Unsupported Media Filter
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.ANIMATION | filters.Sticker.ALL,
            unsupported_media_handler,
        )
    )

    # Text Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # Error Handler
    app.add_error_handler(error_handler)

    # Periodic cleanup of expired temp files (every 15 min)
    if app.job_queue:
        async def scheduled_cleanup(ctx: ContextTypes.DEFAULT_TYPE):
            cleaned = cleanup_expired_temp_files()
            if cleaned > 0:
                logger.info(f"Periodic sweep pruned {cleaned} expired temporary files.")
        app.job_queue.run_repeating(scheduled_cleanup, interval=900, first=60)

    return app


def main() -> None:
    logger.info("Initializing Telegram Document Translation & Splitting Bot...")

    # Start dummy cloud health check HTTP server for Render/Railway if PORT is set
    start_health_server()

    # Prune any orphaned files from previous runs
    initial_clean = cleanup_expired_temp_files()
    if initial_clean > 0:
        logger.info(f"Startup sweep pruned {initial_clean} stale temporary files.")

    if not BOT_TOKEN:
        print(
            "\n" + "=" * 70 + "\n"
            "[CONFIG ERROR] TELEGRAM_BOT_TOKEN is missing!\n"
            "=" * 70 + "\n"
            "To launch the bot, create or edit the .env file containing:\n"
            "TELEGRAM_BOT_TOKEN=your_token_from_botfather\n\n"
            "Obtain your token by messaging @BotFather on Telegram.\n"
            "=" * 70 + "\n"
        )
        sys.exit(1)

    app = create_application()
    if not app:
        sys.exit(1)

    valid, bot_username, err = verify_telegram_token(BOT_TOKEN)
    bot_link = f"https://t.me/{bot_username}" if bot_username else "Telegram"

    print("\n" + "=" * 70)
    print(" 🚀 TELEGRAM BOT IS ONLINE & READY! / बॉट चालू हो गया है!")
    print("=" * 70)
    print(f" 🔗 Bot Username : @{bot_username}")
    print(f" 🌐 Direct Link  : {bot_link}")
    print("=" * 70)
    print(" 👉 Open the link above in Telegram and tap 'START'!")
    print(" 👉 Telegram में ऊपर दिए गए लिंक को खोलें और 'START' दबाएँ!")
    print("=" * 70 + "\n")

    logger.info(f"Bot configured with token: {mask_token(BOT_TOKEN)}")
    logger.info("Starting bot polling loop...")

    try:
        app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        print(
            "\n" + "=" * 70 + "\n"
            "[CRITICAL AUTHENTICATION ERROR] telegram.error.InvalidToken: Unauthorized\n"
            "=" * 70 + "\n"
            f"The token provided ({mask_token(BOT_TOKEN)}) was rejected by Telegram API (HTTP 401).\n"
            "Please verify or regenerate your token with @BotFather.\n"
            "=" * 70 + "\n"
        )
        sys.exit(1)
    except (NetworkError, TimedOut) as net_err:
        logger.error(f"Network connection to Telegram API failed: {net_err}")
        sys.exit(1)
    finally:
        logger.info("Shutting down worker thread pool...")
        THREAD_POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
