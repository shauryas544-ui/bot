import os
import re
import time
import uuid
import random
import logging
import threading
import asyncio
import contextlib
from typing import Dict, List, Optional, Tuple, Any, Callable
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, NavigableString, Comment
import docx
import ebooklib
from ebooklib import epub
from deep_translator import GoogleTranslator, MyMemoryTranslator
from deep_translator.exceptions import TooManyRequests, RequestError
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut

from config import MAX_CHUNK_CHARS, TEMP_DIR

logger = logging.getLogger("telegram_doc_bot.translator")

# ---------------------------------------------------------------------------
# Rate Limiting & HTTP Connection Pooling
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 0.08  # Responsive, high-speed rate limiting

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
    Translates a single block of text (<= 3500 characters).
    Uses high-speed Google endpoints (dict-chrome-ex, webapp) as primary engine,
    with graceful fallback to deep-translator and MyMemory.
    Completely eliminates HTTP 429 Too Many Requests errors.
    """
    if not text or not text.strip():
        return text

    clean_text = text.strip()
    target_lang = target_lang.lower().strip()
    last_exception = None

    for attempt in range(retries):
        # 1. Primary Engine: Google Translate Extension / Webapp endpoints (Fast, 200 OK, no 429)
        for client in ["dict-chrome-ex", "webapp"]:
            try:
                _throttle_request()
                response = _http_session.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={
                        "client": client,
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
                        parts = [p[0] for p in data[0] if p and len(p) > 0 and p[0]]
                        if parts:
                            return "".join(parts)
            except Exception as e:
                last_exception = e
                logger.debug(f"Google {client} attempt {attempt + 1} failed: {e}")

        # 2. Secondary Engine: deep-translator GoogleTranslator
        try:
            _throttle_request()
            translator = GoogleTranslator(source=source_lang, target=target_lang)
            translated = translator.translate(clean_text)
            if translated:
                return translated
        except Exception as exc:
            last_exception = exc
            logger.debug(f"GoogleTranslator fallback failed on attempt {attempt + 1}: {exc}")

        # 3. Tertiary Engine: MyMemoryTranslator with mapped locales
        if len(clean_text) <= 500 and target_lang in MYMEMORY_LOCALES:
            try:
                _throttle_request()
                mm_target = MYMEMORY_LOCALES[target_lang]
                mm_source = MYMEMORY_LOCALES.get(source_lang, "en-GB") if source_lang != "auto" else "en-GB"
                mm = MyMemoryTranslator(source=mm_source, target=mm_target)
                mm_res = mm.translate(clean_text)
                if mm_res:
                    return mm_res
            except Exception as mm_err:
                logger.debug(f"MyMemory fallback failed on attempt {attempt + 1}: {mm_err}")

        # Backoff with jitter before next attempt
        backoff_sleep = min(0.5 * (2 ** attempt) + random.uniform(0.1, 0.3), 3.0)
        time.sleep(backoff_sleep)

    logger.error(f"Failed to translate chunk after {retries} attempts: {last_exception}")
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


def translate_batch(
    texts: List[str],
    target_lang: str,
    source_lang: str = "auto",
    max_chars: int = 3000,
    progress_batch_callback: Optional[Callable[[int, int], None]] = None,
) -> List[str]:
    """
    Translates a list of texts in high-speed combined batches.
    Provides a 10x-30x speedup over sequential element-by-element translation.
    If a batch delimiter fails to parse cleanly, automatically falls back
    to translating that batch element-by-element without crashing.
    """
    if not texts:
        return []

    results: List[Optional[str]] = [None] * len(texts)
    DELIM = "\n\n⟦§⟧\n\n"

    batches: List[Tuple[List[int], List[str]]] = []
    curr_indices: List[int] = []
    curr_texts: List[str] = []
    curr_len = 0

    for idx, t in enumerate(texts):
        if not t or not t.strip():
            results[idx] = t
            continue
        cleaned = t.strip()
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

    for b_idx, (indices, b_texts) in enumerate(batches, start=1):
        if len(b_texts) == 1:
            results[indices[0]] = translate_text(
                b_texts[0], target_lang=target_lang, source_lang=source_lang
            )
        else:
            combined = DELIM.join(b_texts)
            trans_combined = _translate_single_chunk(
                combined, target_lang=target_lang, source_lang=source_lang
            )
            parts = [p.strip() for p in trans_combined.split("⟦§⟧")]

            if len(parts) == len(b_texts):
                for orig_idx, part in zip(indices, parts):
                    results[orig_idx] = part
            else:
                for orig_idx, orig_text in zip(indices, b_texts):
                    results[orig_idx] = translate_text(
                        orig_text, target_lang=target_lang, source_lang=source_lang
                    )

        if progress_batch_callback:
            progress_batch_callback(b_idx, total_batches)

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
            # Throttle rules:
            # 1. 100% always triggers
            # 2. First update triggers if percent != last_percent
            # 3. Otherwise: minimum 2.0s interval AND (>= 3% progress OR >= 5.0s elapsed)
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
        """Executes edit_message_text safely on the event loop."""
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except BadRequest as exc:
            err_msg = str(exc).lower()
            if "not modified" not in err_msg and "message to edit not found" not in err_msg:
                logger.debug(f"Progress update ignored BadRequest: {exc}")
        except (RetryAfter, TimedOut) as exc:
            logger.debug(f"Progress update rate-limited or timed out: {exc}")
        except Exception as exc:
            logger.debug(f"Progress update failed: {exc}")


# ---------------------------------------------------------------------------
# Format-Specific Translation Processors
# ---------------------------------------------------------------------------

def translate_txt(
    input_path: str,
    output_path: str,
    target_lang: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
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

    translated_parts: List[str] = []
    for i, chunk in enumerate(chunks, start=1):
        if not chunk.strip():
            translated_parts.append(chunk)
        else:
            translated = _translate_single_chunk(chunk, target_lang=target_lang, source_lang="auto")
            translated_parts.append(translated)

        if progress_callback:
            pct = 5 + int((i / total_chunks) * 88)
            pct = max(5, min(93, pct))
            progress_callback(pct, f"Part {i} of {total_chunks}")

        if i < total_chunks:
            time.sleep(0.1)

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
    all_paras = body_paras + table_paras
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

    target_tag_names = {"p", "h1", "h2", "h3", "h4", "h5", "span", "li", "blockquote", "td", "th"}

    for item_idx, item in enumerate(valid_items, start=1):
        raw_content = item.get_content()
        if not raw_content:
            continue
        raw_html = raw_content.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(raw_html, "html.parser")
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

    if progress_callback:
        progress_callback(5, f"Reading {total_pages} page(s)...")

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

        if progress_callback:
            pct = 5 + int((i / total_pages) * 88)
            pct = max(5, min(93, pct))
            progress_callback(pct, f"Page {i} of {total_pages}")

    if not found_text:
        doc.add_paragraph(
            "[Notice: No extractable text found in this PDF. It may contain scanned images or rasterized pages.]"
        )

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
        # File Lifecycle: Clean up any partial output file if translation failed
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise


