import os
import re
import time
import uuid
import random
import logging
import threading
from typing import List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, NavigableString, Comment
import docx
import ebooklib
from ebooklib import epub
from deep_translator import GoogleTranslator, MyMemoryTranslator
from deep_translator.exceptions import TooManyRequests, RequestError

from config import MAX_CHUNK_CHARS, TEMP_DIR

logger = logging.getLogger("telegram_doc_bot.translator")

# ---------------------------------------------------------------------------
# Rate Limiting & HTTP Connection Pooling
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 0.25  # Respect Google's ~4-5 req/sec burst limit

# Persistent HTTP Session with connection pooling and retry adapter
_http_session = requests.Session()
_retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=_retries)
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)


def _throttle_request() -> None:
    """Enforce thread-safe rate limiting between outbound translation API calls."""
    global _last_request_time
    with _session_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.time()


# ---------------------------------------------------------------------------
# Core Translation Engine with Fault Tolerance
# ---------------------------------------------------------------------------

def _translate_single_chunk(text: str, target_lang: str, source_lang: str = "auto", retries: int = 3) -> str:
    """
    Translates a single block of text (<= 3500 characters).
    Uses deep-translator's GoogleTranslator as primary engine, with
    graceful fallback to Google GTX endpoint and MyMemory if rate limits occur.
    """
    if not text or not text.strip():
        return text

    clean_text = text.strip()
    last_exception = None

    for attempt in range(retries):
        _throttle_request()

        # 1. Primary engine: deep-translator GoogleTranslator
        try:
            translator = GoogleTranslator(source=source_lang, target=target_lang)
            translated = translator.translate(clean_text)
            if translated:
                return translated
        except (TooManyRequests, RequestError, Exception) as exc:
            last_exception = exc
            logger.warning(
                f"GoogleTranslator attempt {attempt + 1}/{retries} encountered {type(exc).__name__}: {exc}. "
                "Falling back to Google GTX endpoint..."
            )

        # 2. Resilient fallback endpoint: Google GTX client via pooled session
        try:
            _throttle_request()
            response = _http_session.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": source_lang,
                    "tl": target_lang,
                    "dt": "t",
                    "q": clean_text,
                },
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    )
                },
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                if data and isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                    translated_parts = [part[0] for part in data[0] if part and len(part) > 0 and part[0]]
                    if translated_parts:
                        return "".join(translated_parts)
            elif response.status_code == 429:
                logger.warning("GTX endpoint returned 429 Too Many Requests, backing off with jitter...")
        except Exception as fallback_exc:
            last_exception = fallback_exc
            logger.debug(f"Fallback GTX translation failed on attempt {attempt + 1}: {fallback_exc}")

        # 3. Tertiary fallback for small segments: MyMemoryTranslator
        if len(clean_text) <= 500:
            try:
                _throttle_request()
                mm_source = "auto" if source_lang == "auto" else source_lang
                mm = MyMemoryTranslator(source=mm_source, target=target_lang)
                mm_result = mm.translate(clean_text)
                if mm_result:
                    return mm_result
            except Exception as mm_exc:
                logger.debug(f"MyMemory fallback failed on attempt {attempt + 1}: {mm_exc}")

        # Exponential backoff with random jitter before next retry
        backoff_sleep = min(1.0 * (2 ** attempt) + random.uniform(0.1, 0.4), 6.0)
        time.sleep(backoff_sleep)

    logger.error(f"Failed to translate chunk after {retries} attempts: {last_exception}")
    # Return original text rather than dropping user message content
    return text


def split_text_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """
    Slices long strings into blocks (max max_chars) without breaking words or sentences.
    First splits on paragraph breaks, then newlines, then sentences, then words.
    """
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
    """
    Chunked Text Processor:
    Slices long strings into blocks (max 3,500 chars), translates iteratively, and joins results.
    """
    if not text or not text.strip():
        return text

    chunks = split_text_into_chunks(text, max_chars=MAX_CHUNK_CHARS)
    translated_parts: List[str] = []

    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            translated_parts.append(chunk)
            continue
        translated = _translate_single_chunk(chunk, target_lang=target_lang, source_lang=source_lang)
        translated_parts.append(translated)
        if i < len(chunks) - 1:
            time.sleep(0.1)

    return "\n\n".join(translated_parts) if len(chunks) > 1 and "\n\n" in text else "".join(translated_parts)


# ---------------------------------------------------------------------------
# Format-Specific Translation Processors
# ---------------------------------------------------------------------------

def translate_txt(input_path: str, output_path: str, target_lang: str) -> str:
    """
    TXT Translation: Reads text, translates in chunks, outputs translated file.
    """
    content = ""
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(input_path, "r", encoding="latin-1") as f:
            content = f.read()

    translated_content = translate_text(content, target_lang=target_lang)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(translated_content)

    return output_path


def translate_docx(input_path: str, output_path: str, target_lang: str) -> str:
    """
    DOCX Translation:
    Iterates through doc.paragraphs and table cells, translates chunked content,
    reconstructs formatted document, outputs translated docx file.
    """
    try:
        doc = docx.Document(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid DOCX document: {exc}")

    # 1. Translate document paragraphs
    for p in doc.paragraphs:
        if p.text and p.text.strip():
            translated = translate_text(p.text, target_lang=target_lang)
            if len(p.runs) == 1:
                p.runs[0].text = translated
            elif len(p.runs) > 1:
                # Update text while preserving primary run formatting
                p.runs[0].text = translated
                for extra_run in p.runs[1:]:
                    extra_run.text = ""
            else:
                p.text = translated

    # 2. Translate table cells
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if p.text and p.text.strip():
                        translated = translate_text(p.text, target_lang=target_lang)
                        if len(p.runs) == 1:
                            p.runs[0].text = translated
                        elif len(p.runs) > 1:
                            p.runs[0].text = translated
                            for extra_run in p.runs[1:]:
                                extra_run.text = ""
                        else:
                            p.text = translated

    doc.save(output_path)
    return output_path


def translate_epub(input_path: str, output_path: str, target_lang: str) -> str:
    """
    EPUB Translation:
    - Iterates over ITEM_DOCUMENT items.
    - Uses BeautifulSoup to parse HTML DOM.
    - Targets text leaf elements (<p>, <h1>-<h5>, <span>) and translates inner text
      while strictly preserving HTML tags, hierarchy, and styling.
    - Repacks and outputs a valid translated .epub.
    """
    try:
        book = epub.read_epub(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid EPUB document: {exc}")

    doc_items = [item for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]
    target_tag_names = {"p", "h1", "h2", "h3", "h4", "h5", "span", "li", "blockquote", "td", "th"}

    for item in doc_items:
        if item.file_name.endswith("nav.xhtml") and not item.is_chapter():
            continue

        raw_html = item.get_content().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(raw_html, "html.parser")
        candidate_tags = soup.find_all(lambda tag: tag.name in target_tag_names)

        for tag in candidate_tags:
            child_tags = [c for c in tag.children if not isinstance(c, NavigableString) and not isinstance(c, Comment)]
            if not child_tags:
                text_content = tag.get_text()
                if text_content and text_content.strip():
                    translated = translate_text(text_content, target_lang=target_lang)
                    tag.string = translated
            else:
                for child in list(tag.children):
                    if isinstance(child, NavigableString) and not isinstance(child, Comment):
                        s = str(child).strip()
                        if s:
                            translated = translate_text(s, target_lang=target_lang)
                            child.replace_with(NavigableString(f" {translated} "))

        item.set_content(str(soup).encode("utf-8"))

    # Update metadata language to target
    dc_ns = "http://purl.org/dc/elements/1.1/"
    if dc_ns in book.metadata and "language" in book.metadata[dc_ns]:
        book.metadata[dc_ns]["language"] = []
    book.set_language(target_lang)
    epub.write_epub(output_path, book)
    return output_path


def translate_pdf(input_path: str, output_path: str, target_lang: str) -> str:
    """
    Extracts text page-by-page from PDF using pypdf, translates content,
    and formats into a clean Word (.docx) document.
    """
    import pypdf
    try:
        reader = pypdf.PdfReader(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid PDF document: {exc}")

    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError("The provided PDF has no pages.")

    doc = docx.Document()
    doc.add_heading("Translated Document", level=0)

    found_text = False
    for i, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        page_text = page_text.strip()
        if page_text:
            found_text = True
            doc.add_heading(f"Page {i}", level=2)
            translated = translate_text(page_text, target_lang=target_lang)
            for para in translated.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())

    if not found_text:
        doc.add_paragraph(
            "[Notice: No extractable text found in this PDF. It may contain scanned images or rasterized pages.]"
        )

    doc.save(output_path)
    return output_path


def translate_file(input_path: str, ext: str, target_lang: str, original_filename: str) -> str:
    """
    Router for file translation based on extension.
    Outputs path to translated file in TEMP_DIR with a unique session ID
    to prevent file collision among concurrent users.
    PDF files are translated into formatted .docx for clean readability.
    """
    stem, _ = os.path.splitext(original_filename)
    session_id = uuid.uuid4().hex[:8]
    ext = ext.lower()
    out_ext = ".docx" if ext == ".pdf" else ext
    output_filename = f"translated_{session_id}_{stem}{out_ext}"
    output_path = os.path.join(TEMP_DIR, output_filename)

    try:
        if ext == ".txt":
            return translate_txt(input_path, output_path, target_lang)
        elif ext == ".docx":
            return translate_docx(input_path, output_path, target_lang)
        elif ext == ".epub":
            return translate_epub(input_path, output_path, target_lang)
        elif ext == ".pdf":
            return translate_pdf(input_path, output_path, target_lang)
        else:
            raise ValueError(f"Unsupported file format: {ext}")
    except Exception:
        # File Lifecycle: Clean up any partial output file if translation failed
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise


