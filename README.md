# Telegram Document Translation & File Splitting Bot

A production-grade, asynchronous Telegram Bot engineered for document translation and dynamic file splitting supporting `.txt`, `.docx`, `.epub`, and `.pdf` formats.

Designed to work out-of-the-box on **Localhost (Windows/Mac/Linux)** and **Cloud Platforms (Railway, Render Free Web Service, VPS)** with zero configuration crashes.

---

## 🏗️ Architecture Overview

The project provides two seamless execution modes:

1. **`bot.py`**: A fully functional, self-contained single-file script containing the entire bot logic, handlers, cloud health-check dummy server, and processing engines.
2. **Modular Architecture** (`main.py`, `config.py`, `services/`, `handlers/`): Production-grade separation of concerns with full unit and integration test coverage.

```text
telegram_doc_translator_bot/
├── bot.py                    # Standalone, self-contained single-file production bot
├── main.py                   # Modular application entrypoint
├── config.py                 # Configuration, ThreadPoolExecutor, session management & TTL
├── requirements.txt          # Dependency specifications (including pypdf)
├── Procfile                  # Cloud process declaration (worker: python bot.py)
├── .env.example              # Environment variables template
├── services/
│   ├── translator.py         # Multi-format translation engine (.txt, .docx, .epub, .pdf)
│   └── splitter.py           # Multi-format splitting engine (.txt, .docx, .epub, .pdf)
├── handlers/
│   ├── commands.py           # /start, /language commands & inline matrix
│   ├── files.py              # File downloads, uploads, and media filters
│   └── callbacks.py          # Inline button callback dispatcher
└── tests/                    # 25 automated unit & async integration tests
```

---

## 🚀 Key Features

### 1. Bulletproof Token Loading & Localhost Zero-Crash Fallback
- Resolves `.env` using absolute path: `load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))`.
- Checks both `TELEGRAM_BOT_TOKEN` and `BOT_TOKEN`.
- **Localhost Fallback:** If the token is not found in `.env` or environment variables, prompts the user interactively in the terminal via `input(...)` and saves it, preventing infinite crash loops.

### 2. Render & Railway Cloud Web Service Compatibility
- Includes a lightweight background dummy HTTP health server (`threading` + `http.server`) bound to `os.getenv("PORT", 8080)`.
- Responds with `200 OK` on `/` and `/healthz` so Render's Free Web Service passes initial deployment health checks while Telegram `run_polling()` runs concurrently.

### 3. Supported Formats & Interaction
* **Direct Text Messages:**
  - Translates text directly to the user's active language.
  - Automatically streams responses exceeding 4,000 characters as in-memory `.txt` files using `io.BytesIO`.
* **Document Ingestion (`.txt`, `.docx`, `.epub`, `.pdf`):**
  - Downloads files asynchronously to isolated staging paths (`temp_{user_id}_{timestamp}_{filename}`).
  - Displays an interactive inline keyboard:
    - `🌐 Translate File`
    - `✂️ Split File`
* **Dynamic Splitting Subsystem:**
  - **PDF Splitting:** Uses `pypdf` (`PdfReader` and `PdfWriter`) to slice every N pages into separate PDF files (`part_1.pdf`, `part_2.pdf`).
  - **EPUB Splitting:** Slices `ITEM_DOCUMENT` items into separate valid EPUB containers with navigation, spine, CSS styles, and metadata rebuilt.
  - **DOCX Splitting:** Groups non-empty paragraphs into new `.docx` documents preserving run-level styles and font attributes.
  - **TXT Splitting:** Slices lines into clean `.txt` documents.
* **Translation Subsystem:**
  - Slices text into safe blocks (<= 3,500 chars) using `deep-translator` (`GoogleTranslator`) with thread-safe rate throttling and exponential backoff.
  - **PDF Translation:** Extracts text page-by-page using `pypdf` and compiles translated content into a clean, formatted Word (`.docx`) document.
  - **EPUB Translation:** Parses XHTML chapters using `BeautifulSoup`, replaces text in leaf tags (`<p>`, `<h1>`-`<h5>`, `<span>`) while keeping styles and DOM intact.
  - **DOCX Translation:** Iterates through paragraphs and tables, translating while preserving formatting.
  - **TXT Translation:** Translates iteratively and outputs `translated_<filename>.txt`.

### 4. Concurrency & Ephemeral Lifecycle
- Offloads all CPU-heavy work (HTML DOM parsing, PDF manipulation, DOCX XML formatting) and network translation requests to worker threads via `await loop.run_in_executor(THREAD_POOL, ...)`.
- Temporary files use unique session UUIDs to eliminate race conditions between concurrent users.
- Wrapped in strict `try...finally` blocks with automatic rollback if any error occurs midway.
- Automatic session TTL pruning (default 30 mins) deletes abandoned files on startup and periodically.

---

## 🛠️ Localhost Setup (PowerShell / Windows / Mac)

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```powershell
cp .env.example .env
```
Edit `.env` and set your token:
```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
DEFAULT_LANGUAGE=hi
TEMP_STORAGE_DIR=./temp_storage
SESSION_TTL_SECONDS=1800
```
*(If you do not configure `.env`, `bot.py` will prompt you for your token in the terminal on first run).*

### 3. Run the Bot
You can run the self-contained script:
```powershell
python bot.py
```
Or run the modular architecture:
```powershell
python main.py
```

---

## ☁️ Cloud Deployment

### Deploying to Railway
1. Push this repository to GitHub or deploy via Railway CLI.
2. In Railway, click **New Project** -> **Deploy from GitHub repo**.
3. Under **Variables**, add:
   - `TELEGRAM_BOT_TOKEN` = `your_token_from_botfather`
   - `DEFAULT_LANGUAGE` = `hi`
4. Railway automatically detects the `Procfile` (`worker: python bot.py`).

### Deploying to Render (Free Web Service)
1. In Render, select **New Web Service** and connect your repository.
2. Configure settings:
   - **Environment:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
3. Under **Environment Variables**, add:
   - `TELEGRAM_BOT_TOKEN` = `your_token_from_botfather`
   - `PORT` = `8080`
4. The built-in dummy HTTP server automatically responds `200 OK` on `$PORT`, satisfying Render's health checks while the bot polls Telegram.

---

## 🧪 Running Automated Tests

Run the full test suite (26 unit and async integration tests):
```powershell
python -m unittest discover tests
```
All tests run deterministically in ~2 seconds without external network dependencies.
