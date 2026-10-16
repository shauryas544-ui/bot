import os
import time
import unittest
from unittest.mock import patch
import docx
import ebooklib
from ebooklib import epub

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import (
    TEMP_DIR,
    get_user_session,
    set_user_language,
    set_active_file,
    clear_active_file,
    get_valid_active_file,
    set_awaiting_split,
    sanitize_filename,
    cleanup_expired_temp_files,
)
from services.translator import (
    split_text_into_chunks,
    translate_text,
    translate_txt,
    translate_docx,
    translate_epub,
    translate_pdf,
    translate_file,
)
from services.splitter import (
    split_txt,
    split_docx,
    split_epub,
    split_pdf,
    split_file,
)
from handlers.commands import build_language_keyboard


class TestConfigAndSession(unittest.TestCase):
    def test_session_management(self):
        user_id = 99999
        session = get_user_session(user_id)
        self.assertEqual(session.target_language, "hi")
        self.assertFalse(session.awaiting_split_count)
        self.assertIsNone(session.active_file)

        # Update language
        set_user_language(user_id, "fr")
        self.assertEqual(session.target_language, "fr")

        # Set active file
        dummy_file = os.path.join(TEMP_DIR, "dummy_test.txt")
        with open(dummy_file, "w") as f:
            f.write("hello")
        set_active_file(user_id, dummy_file, "dummy_test.txt", ".txt")
        self.assertIsNotNone(session.active_file)
        self.assertEqual(session.active_file.name, "dummy_test.txt")

        # Awaiting split
        set_awaiting_split(user_id, True)
        self.assertTrue(session.awaiting_split_count)

        # Clear active file and verify file deletion
        clear_active_file(user_id, remove_file=True)
        self.assertIsNone(session.active_file)
        self.assertFalse(session.awaiting_split_count)
        self.assertFalse(os.path.exists(dummy_file))

    def test_session_ttl_expiration(self):
        user_id = 88888
        dummy_file = os.path.join(TEMP_DIR, "dummy_expire.txt")
        with open(dummy_file, "w") as f:
            f.write("content")
        set_active_file(user_id, dummy_file, "dummy_expire.txt", ".txt")

        session = get_user_session(user_id)
        # Simulate elapsed time past TTL (e.g. 2 hours ago)
        session.active_file.created_at = time.time() - 7200
        self.assertTrue(session.active_file.is_expired(ttl_seconds=1800))

        # get_valid_active_file should detect expiration and prune
        valid_file = get_valid_active_file(user_id, ttl_seconds=1800)
        self.assertIsNone(valid_file)
        self.assertFalse(os.path.exists(dummy_file))

    def test_cleanup_expired_temp_files(self):
        # Create an old temp file
        old_file = os.path.join(TEMP_DIR, "temp_old_test.txt")
        with open(old_file, "w") as f:
            f.write("old data")
        # Backdate mtime by 2 hours
        two_hours_ago = time.time() - 7200
        os.utime(old_file, (two_hours_ago, two_hours_ago))

        # Create a fresh temp file
        fresh_file = os.path.join(TEMP_DIR, "temp_fresh_test.txt")
        with open(fresh_file, "w") as f:
            f.write("fresh data")

        try:
            cleaned = cleanup_expired_temp_files(max_age_seconds=1800)
            self.assertGreaterEqual(cleaned, 1)
            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(os.path.exists(fresh_file))
        finally:
            if os.path.exists(fresh_file):
                os.remove(fresh_file)
            if os.path.exists(old_file):
                os.remove(old_file)

    def test_sanitize_filename(self):
        dirty = "../../etc/passwd:sample*file?.docx"
        clean = sanitize_filename(dirty)
        self.assertNotIn("/", clean)
        self.assertNotIn("\\", clean)
        self.assertNotIn(":", clean)
        self.assertTrue(clean.endswith(".docx"))

    def test_language_keyboard(self):
        markup = build_language_keyboard("hi")
        self.assertIsNotNone(markup.inline_keyboard)
        found_checked = False
        for row in markup.inline_keyboard:
            for btn in row:
                if "✅" in btn.text and btn.callback_data == "set_lang:hi":
                    found_checked = True
        self.assertTrue(found_checked)


@patch("services.translator._translate_single_chunk", side_effect=lambda text, target_lang, **kw: f"[{target_lang}] {text}")
class TestTranslatorService(unittest.TestCase):
    def test_chunking_small_and_large(self, mock_translate):
        small = "Hello world! This is a simple sentence."
        chunks = split_text_into_chunks(small, max_chars=100)
        self.assertEqual(len(chunks), 1)

        # Large text with paragraphs
        paragraphs = ["Paragraph " + str(i) + " with some repeated text to fill buffer. " * 15 for i in range(20)]
        large = "\n\n".join(paragraphs)
        chunks = split_text_into_chunks(large, max_chars=500)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 500)

    def test_translate_text(self, mock_translate):
        text = "Good morning, friend."
        res = translate_text(text, target_lang="hi")
        self.assertIn("[hi]", res)
        self.assertIn("Good morning", res)

    def test_translate_txt(self, mock_translate):
        input_file = os.path.join(TEMP_DIR, "sample_input.txt")
        output_file = os.path.join(TEMP_DIR, "sample_output.txt")
        with open(input_file, "w", encoding="utf-8") as f:
            f.write("Welcome to the automated translation system.\nEnjoy reading books.")

        try:
            res_path = translate_txt(input_file, output_file, target_lang="es")
            self.assertTrue(os.path.exists(res_path))
            with open(res_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("[es]", content)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            if os.path.exists(output_file):
                os.remove(output_file)

    def test_translate_docx(self, mock_translate):
        input_file = os.path.join(TEMP_DIR, "sample_input.docx")
        output_file = os.path.join(TEMP_DIR, "sample_output.docx")

        doc = docx.Document()
        doc.add_heading("Header One", 0)
        doc.add_paragraph("This is the first paragraph.")
        doc.save(input_file)

        try:
            res_path = translate_docx(input_file, output_file, target_lang="fr")
            self.assertTrue(os.path.exists(res_path))
            read_doc = docx.Document(res_path)
            self.assertTrue(len(read_doc.paragraphs) > 0)
            self.assertIn("[fr]", read_doc.paragraphs[1].text)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            if os.path.exists(output_file):
                os.remove(output_file)

    def test_translate_epub(self, mock_translate):
        input_file = os.path.join(TEMP_DIR, "sample_input.epub")
        output_file = os.path.join(TEMP_DIR, "sample_output.epub")

        book = epub.EpubBook()
        book.set_identifier("test-translate-epub")
        book.set_title("Translation Test")
        book.set_language("en")

        c1 = epub.EpubHtml(title="Chapter 1", file_name="c1.xhtml", lang="en")
        c1.content = "<html><body><h1>Title</h1><p>Hello world.</p></body></html>"
        book.add_item(c1)
        book.spine = ["nav", c1]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(input_file, book)

        try:
            res_path = translate_epub(input_file, output_file, target_lang="de")
            self.assertTrue(os.path.exists(res_path))
            read_b = epub.read_epub(res_path)
            self.assertEqual(read_b.get_metadata("DC", "language")[0][0], "de")
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            if os.path.exists(output_file):
                os.remove(output_file)


    def test_translate_pdf(self, mock_translate):
        import pypdf
        input_file = os.path.join(TEMP_DIR, "sample_input.pdf")
        output_file = os.path.join(TEMP_DIR, "sample_output.docx")

        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(input_file, "wb") as f:
            writer.write(f)

        try:
            res_path = translate_pdf(input_file, output_file, target_lang="es")
            self.assertTrue(os.path.exists(res_path))
            read_doc = docx.Document(res_path)
            self.assertTrue(len(read_doc.paragraphs) > 0)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            if os.path.exists(output_file):
                os.remove(output_file)


class TestSplitterService(unittest.TestCase):
    def test_split_txt(self):
        input_file = os.path.join(TEMP_DIR, "test_split.txt")
        with open(input_file, "w", encoding="utf-8") as f:
            for i in range(1, 11):
                f.write(f"Line {i}\n")

        parts = []
        try:
            parts = split_txt(input_file, lines_per_file=3, original_filename="test_split.txt")
            self.assertEqual(len(parts), 4)
            for p in parts:
                self.assertTrue(os.path.exists(p))
            with open(parts[0], "r", encoding="utf-8") as f:
                self.assertEqual(len(f.readlines()), 3)
            with open(parts[-1], "r", encoding="utf-8") as f:
                self.assertEqual(len(f.readlines()), 1)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            for p in parts:
                if os.path.exists(p):
                    os.remove(p)

    def test_split_docx(self):
        input_file = os.path.join(TEMP_DIR, "test_split.docx")
        doc = docx.Document()
        for i in range(1, 13):
            doc.add_paragraph(f"Paragraph {i}")
        doc.save(input_file)

        parts = []
        try:
            parts = split_docx(input_file, paragraphs_per_file=5, original_filename="test_split.docx")
            self.assertEqual(len(parts), 3)
            for p in parts:
                self.assertTrue(os.path.exists(p))
            d1 = docx.Document(parts[0])
            self.assertEqual(len(d1.paragraphs), 5)
            d3 = docx.Document(parts[2])
            self.assertEqual(len(d3.paragraphs), 2)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            for p in parts:
                if os.path.exists(p):
                    os.remove(p)

    def test_split_epub(self):
        input_file = os.path.join(TEMP_DIR, "test_split.epub")
        book = epub.EpubBook()
        book.set_identifier("test-split-epub")
        book.set_title("Splitting Test Book")
        book.set_language("en")

        chaps = []
        for i in range(1, 7):
            c = epub.EpubHtml(title=f"Chap {i}", file_name=f"ch_{i}.xhtml", lang="en")
            c.content = f"<html><body><p>Chap {i} body</p></body></html>"
            book.add_item(c)
            chaps.append(c)

        book.spine = ["nav"] + chaps
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(input_file, book)

        parts = []
        try:
            parts = split_epub(input_file, chapters_per_volume=2, original_filename="test_split.epub")
            self.assertEqual(len(parts), 3)
            for p in parts:
                self.assertTrue(os.path.exists(p))
                part_book = epub.read_epub(p)
                doc_items = [
                    item for item in part_book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
                    if not item.file_name.endswith("nav.xhtml")
                ]
                self.assertEqual(len(doc_items), 2)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            for p in parts:
                if os.path.exists(p):
                    os.remove(p)

    def test_split_pdf(self):
        import pypdf
        input_file = os.path.join(TEMP_DIR, "test_split.pdf")
        writer = pypdf.PdfWriter()
        for _ in range(5):
            writer.add_blank_page(width=100, height=100)
        with open(input_file, "wb") as f:
            writer.write(f)

        parts = []
        try:
            parts = split_pdf(input_file, pages_per_file=2, original_filename="test_split.pdf")
            self.assertEqual(len(parts), 3)
            for p in parts:
                self.assertTrue(os.path.exists(p))
            r1 = pypdf.PdfReader(parts[0])
            self.assertEqual(len(r1.pages), 2)
            r3 = pypdf.PdfReader(parts[2])
            self.assertEqual(len(r3.pages), 1)
        finally:
            if os.path.exists(input_file):
                os.remove(input_file)
            for p in parts:
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()


