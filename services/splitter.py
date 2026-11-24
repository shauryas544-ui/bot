import os
import io
import re
import uuid
import logging
from typing import List, Tuple, Any, Optional
import docx
import ebooklib
from ebooklib import epub

from config import TEMP_DIR
from services.translator import _sanitize_epub_book, safe_lxml_scope

logger = logging.getLogger("telegram_doc_bot.splitter")


def _rollback_files(files: List[str]) -> None:
    """Helper to remove any partially created files on failure."""
    for f in files:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError as e:
                logger.warning(f"Failed to rollback partial split file {f}: {e}")


def split_txt(input_path: str, lines_per_file: int, original_filename: str = "") -> List[str]:
    """
    TXT Splitting: Slices lines array by N lines -> writes part_1.txt, part_2.txt...
    Guarantees strict file cleanup on failure.
    """
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
    """
    DOCX Splitting: Groups paragraphs by N -> writes new .docx documents (part_1.docx...).
    Preserves text runs, styles, and character formatting.
    Guarantees strict file cleanup on failure.
    """
    if paragraphs_per_file <= 0:
        raise ValueError("Paragraph count must be greater than zero.")

    try:
        source_doc = docx.Document(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid DOCX document: {exc}")

    paragraphs = source_doc.paragraphs
    if not paragraphs:
        chunks = [[]]
    else:
        chunks = [paragraphs[i:i + paragraphs_per_file] for i in range(0, len(paragraphs), paragraphs_per_file)]

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
    """
    EPUB Splitting:
    - Extracts ITEM_DOCUMENT nodes.
    - Slices by N chapters.
    - Assembles into valid new epub.EpubBook objects with reconstructed spine,
      toc, navigation, and metadata -> writes part_1.epub, part_2.epub...
    Guarantees strict file cleanup on failure.
    """
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
    except Exception as e:
        logger.warning(f"Could not read full metadata from EPUB: {e}")

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
            _sanitize_epub_book(new_book, metadata_lang)
            with safe_lxml_scope():
                epub.write_epub(out_path, new_book)
            output_files.append(out_path)

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_pdf(input_path: str, pages_per_file: int, original_filename: str = "") -> List[str]:
    """
    PDF Splitting: Slices every N pages into separate PDF files.
    Guarantees strict file cleanup on failure.
    """
    import pypdf
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


def parse_split_input(user_input: str, default_mode: str = "count") -> Tuple[str, int]:
    """
    Parses user input for splitting.
    Returns: (mode, value) where mode is "count" or "size", and value is integer.
    For "size", value is in bytes (minimum 512 bytes).
    For "count", value is unit count (minimum 1).
    """
    cleaned = user_input.strip().lower()
    match = re.match(r"^([\d\.]+)\s*([a-zA-Z]*)$", cleaned)
    if not match:
        raise ValueError("Invalid format. Enter a number (e.g. 50) or size (e.g. 500KB, 2MB).")

    num_str, unit = match.groups()
    try:
        num = float(num_str)
    except ValueError:
        raise ValueError("Invalid numeric value.")

    if num <= 0:
        raise ValueError("Value must be greater than zero.")

    unit = unit.strip().lower()
    if unit in ("mb", "m", "mib"):
        return "size", max(int(num * 1024 * 1024), 1024)
    elif unit in ("kb", "k", "kib"):
        return "size", max(int(num * 1024), 512)
    elif unit in ("gb", "g", "gib"):
        return "size", max(int(num * 1024 * 1024 * 1024), 1024)
    elif unit in ("b", "byte", "bytes"):
        return "size", max(int(num), 512)
    elif not unit:
        if default_mode == "size":
            return "size", max(int(num * 1024), 512)
        else:
            return "count", max(int(num), 1)
    else:
        raise ValueError(f"Unknown unit '{unit}'. Use KB or MB (e.g. 500KB, 2MB).")


def split_txt_by_size(input_path: str, max_bytes: int, original_filename: str = "") -> List[str]:
    """
    Splits TXT document into parts where each part is approximately <= max_bytes.
    Preserves whole lines and clean UTF-8 encoding.
    """
    if max_bytes < 512:
        max_bytes = 512

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        with open(input_path, "r", encoding="latin-1") as f:
            lines = f.readlines()

    if not lines:
        lines = [""]

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]
    part_idx = 1
    current_chunk: List[str] = []
    current_size = 0

    try:
        for line in lines:
            line_bytes = len(line.encode("utf-8", errors="replace"))
            if current_chunk and (current_size + line_bytes > max_bytes):
                filename = f"split_{session_id}_part_{part_idx}.txt"
                out_path = os.path.join(TEMP_DIR, filename)
                with open(out_path, "w", encoding="utf-8") as f_out:
                    f_out.writelines(current_chunk)
                output_files.append(out_path)
                part_idx += 1
                current_chunk = []
                current_size = 0

            current_chunk.append(line)
            current_size += line_bytes

        if current_chunk:
            filename = f"split_{session_id}_part_{part_idx}.txt"
            out_path = os.path.join(TEMP_DIR, filename)
            with open(out_path, "w", encoding="utf-8") as f_out:
                f_out.writelines(current_chunk)
            output_files.append(out_path)

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_docx_by_size(input_path: str, max_bytes: int, original_filename: str = "") -> List[str]:
    """
    Splits DOCX document paragraph-by-paragraph such that each output .docx is approximately <= max_bytes.
    Preserves styles and formatting.
    """
    if max_bytes < 5120:
        max_bytes = 5120

    try:
        source_doc = docx.Document(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid DOCX document: {exc}")

    paragraphs = source_doc.paragraphs
    if not paragraphs:
        paragraphs = []

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]
    part_idx = 1

    try:
        new_doc = docx.Document()
        paras_in_doc = 0

        for p in paragraphs:
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
            paras_in_doc += 1

            if paras_in_doc % 15 == 0:
                bio = io.BytesIO()
                new_doc.save(bio)
                if bio.tell() >= max_bytes and paras_in_doc > 1:
                    filename = f"split_{session_id}_part_{part_idx}.docx"
                    out_path = os.path.join(TEMP_DIR, filename)
                    with open(out_path, "wb") as f_out:
                        f_out.write(bio.getvalue())
                    output_files.append(out_path)
                    part_idx += 1
                    new_doc = docx.Document()
                    paras_in_doc = 0

        if paras_in_doc > 0:
            filename = f"split_{session_id}_part_{part_idx}.docx"
            out_path = os.path.join(TEMP_DIR, filename)
            new_doc.save(out_path)
            output_files.append(out_path)

        if not output_files:
            filename = f"split_{session_id}_part_1.docx"
            out_path = os.path.join(TEMP_DIR, filename)
            new_doc.save(out_path)
            output_files.append(out_path)

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_epub_by_size(input_path: str, max_bytes: int, original_filename: str = "") -> List[str]:
    """
    Splits EPUB document chapter-by-chapter into valid EPUB volumes targeting <= max_bytes.
    """
    if max_bytes < 10240:
        max_bytes = 10240

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

    metadata_title = "Book"
    metadata_lang = "en"
    try:
        titles = source_book.get_metadata("DC", "title")
        if titles and len(titles) > 0:
            metadata_title = titles[0][0]
        langs = source_book.get_metadata("DC", "language")
        if langs and len(langs) > 0:
            metadata_lang = langs[0][0]
    except Exception as e:
        logger.warning(f"Could not read full metadata from EPUB: {e}")

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]
    chunks: List[List[Any]] = []
    current_chunk = []
    current_bytes = 0

    for doc_item in all_docs:
        c_bytes = len(doc_item.get_content() or b"")
        if current_chunk and (current_bytes + c_bytes > max_bytes):
            chunks.append(current_chunk)
            current_chunk = []
            current_bytes = 0
        current_chunk.append(doc_item)
        current_bytes += c_bytes

    if current_chunk:
        chunks.append(current_chunk)

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
            _sanitize_epub_book(new_book, metadata_lang)
            with safe_lxml_scope():
                epub.write_epub(out_path, new_book)
            output_files.append(out_path)

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_pdf_by_size(input_path: str, max_bytes: int, original_filename: str = "") -> List[str]:
    """
    Splits PDF document page-by-page such that each generated PDF part is approximately <= max_bytes.
    Guarantees valid, uncorrupted PDF documents.
    """
    import pypdf
    if max_bytes < 5120:
        max_bytes = 5120

    try:
        reader = pypdf.PdfReader(input_path)
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid PDF document: {exc}")

    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError("The provided PDF contains no pages.")

    output_files: List[str] = []
    session_id = uuid.uuid4().hex[:8]
    part_idx = 1

    try:
        writer = pypdf.PdfWriter()
        pages_in_writer = 0

        for page_num in range(total_pages):
            page = reader.pages[page_num]
            test_bio = io.BytesIO()
            writer.add_page(page)
            pages_in_writer += 1
            writer.write(test_bio)
            current_size = test_bio.tell()

            if current_size > max_bytes and pages_in_writer > 1:
                prev_writer = pypdf.PdfWriter()
                for p_idx in range(page_num - (pages_in_writer - 1), page_num):
                    prev_writer.add_page(reader.pages[p_idx])

                filename = f"split_{session_id}_part_{part_idx}.pdf"
                out_path = os.path.join(TEMP_DIR, filename)
                with open(out_path, "wb") as f_out:
                    prev_writer.write(f_out)
                output_files.append(out_path)
                part_idx += 1

                writer = pypdf.PdfWriter()
                writer.add_page(page)
                pages_in_writer = 1

        if pages_in_writer > 0:
            filename = f"split_{session_id}_part_{part_idx}.pdf"
            out_path = os.path.join(TEMP_DIR, filename)
            with open(out_path, "wb") as f_out:
                writer.write(f_out)
            output_files.append(out_path)

        return output_files
    except Exception:
        _rollback_files(output_files)
        raise


def split_file(
    input_path: str,
    ext: str,
    chunk_size: int,
    original_filename: str,
    split_mode: str = "count",
) -> List[str]:
    """
    Router for file splitting based on extension and mode ('count' vs 'size').
    Returns list of generated file paths in TEMP_DIR.
    """
    ext = ext.lower()
    if split_mode == "size":
        if ext == ".txt":
            return split_txt_by_size(input_path, max_bytes=chunk_size, original_filename=original_filename)
        elif ext == ".docx":
            return split_docx_by_size(input_path, max_bytes=chunk_size, original_filename=original_filename)
        elif ext == ".epub":
            return split_epub_by_size(input_path, max_bytes=chunk_size, original_filename=original_filename)
        elif ext == ".pdf":
            return split_pdf_by_size(input_path, max_bytes=chunk_size, original_filename=original_filename)
        else:
            raise ValueError(f"Unsupported file format: {ext}")
    else:
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


