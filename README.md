# Telegram Document Translation & File Splitting Bot

A production-ready, asynchronous Telegram Bot engineered for document translation and file splitting supporting `.txt`, `.docx`, and `.epub` formats.

---

## 🏗️ Architecture Overview

The codebase is organized following clean architectural principles and strict separation of concerns:

```text
telegram_doc_translator_bot/
├── config.py                 # Bot tokens, dedicated ThreadPoolExecutor, session manager with TTL
├── requirements.txt          # Python dependency specifications
├── .env.example              # Environment variables template
├── services/
│   ├── __init__.py
│   ├── translator.py         # Chunked translation engine with GoogleTranslator, rate limiter & session pooling
│   └── splitter.py           # Text, Word (.docx), and E-Book (.epub) splitting engines with file rollback
├── handlers/
│   ├── __init__.py
│   ├── commands.py           # /start, /language commands & inline keyboard matrix
│   ├── files.py              # Document upload, split transmission, plain text translation & media filter
│   └── callbacks.py          # Inline button actions (Translate, Split, Set Language)
├── tests/
│   ├── __init__.py
│   ├── test_services.py      # Unit tests for translation, splitting & session logic
│   └── test_bot_flows.py     # Isolated async integration tests for bot handlers
└── main.py                   # Telegram ApplicationBuilder polling entrypoint & global error handler
```

---

## 🚀 Key Features & Specifications

### 1. Asynchronous Concurrency
- Uses `python-telegram-bot` (v20+ async engine).
- **Non-blocking concurrency:** All CPU-heavy tasks (HTML DOM parsing with BeautifulSoup, EPUB repackaging, DOCX XML manipulation) and network translation requests are offloaded to a dedicated `ThreadPoolExecutor` via `await loop.run_in_executor(THREAD_POOL, func, *args)` to ensure polling never freezes.

### 2. Ephemeral File Lifecycle & Storage Safety
- Uploaded files are downloaded to temporary storage (`temp_storage/temp_{user_id}_{timestamp}_{filename}`).
- Output files use unique collision-free session IDs (`translated_{session_id}_{filename}`).
- Translation output files and split partitions are wrapped in strict `try...finally` blocks.
- If splitting or translation fails midway, all partial output chunks are immediately rolled back and purged from disk.
- Session TTL expiry (default 30 mins) prunes abandoned files automatically on startup and during periodic sweeps.

### 3. Translation Subsystem
- **Chunked Text Processor:** Slices text into blocks up to 3,500 characters preserving paragraph, sentence, and word boundaries.
- **Fault-Tolerant Engine:** Uses `deep-translator` (`GoogleTranslator`) with thread-safe rate throttling, persistent `requests.Session` connection pooling, and jittered exponential backoff.
- **TXT Translation:** Decodes UTF-8 (with fallback to Latin-1), translates iteratively, outputs `translated_<filename>.txt`.
- **DOCX Translation:** Iterates through paragraphs and table cells, translating text while preserving paragraph and run styling.
- **EPUB Translation:** Iterates over `ITEM_DOCUMENT` nodes, parses DOM via `BeautifulSoup`, targets text leaf nodes (`<p>`, `<h1>`-`<h5>`, `<span>`), translates content while preserving HTML hierarchy, CSS styling, and repacks into a valid `.epub`.
- **Large Plain Text Messages:** If a user sends a raw message and the translated output exceeds 4,000 characters, it is returned as an in-memory `.txt` file using `io.BytesIO`.

### 4. Splitting Subsystem
- Prompts for dynamic user chunk size:
  - `.epub`: Chapters per volume (e.g. 5, 10). Reconstructs spine, TOC, NCX, metadata, and styles.
  - `.docx`: Paragraphs per file (e.g. 25, 50). Preserves run formatting and styles.
  - `.txt`: Lines per file (e.g. 200, 500).
- Delivers all resulting parts (`part_1.ext`, `part_2.ext`...) sequentially with polite spacing delays and automatic Telegram `RetryAfter` flood control retries.

---

## 🛠️ Installation & Setup

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```powershell
cp .env.example .env
```
Edit `.env` and configure your settings:
```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
DEFAULT_LANGUAGE=hi
TEMP_STORAGE_DIR=./temp_storage
SESSION_TTL_SECONDS=1800
```

---

## 🧪 Running Tests

Execute the automated test suite:
```powershell
python -m unittest discover tests
```

All 23 unit and integration tests run deterministically without external rate limits, verifying session management, file operations, translation, splitting, and async Telegram handlers.

---

## 🤖 Running the Bot

Start the bot with:
```powershell
python main.py
```

