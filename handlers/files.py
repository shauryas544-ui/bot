import io
import os
import time
import asyncio
import logging
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.error import RetryAfter, TimedOut, BadRequest
from telegram.ext import ContextTypes

from config import (
    SUPPORTED_EXTENSIONS,
    SUPPORTED_LANGUAGES,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TEMP_DIR,
    THREAD_POOL,
    get_user_session,
    get_valid_active_file,
    set_active_file,
    clear_active_file,
    set_awaiting_split,
    sanitize_filename,
    cleanup_expired_temp_files,
)
from services.translator import translate_text
from services.splitter import split_file

logger = logging.getLogger("telegram_doc_bot.files")


def format_file_size(size_bytes: int) -> str:
    """Format bytes to human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


async def safe_send_document_with_retry(
    chat,
    document_path: str,
    filename: str,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
    max_retries: int = 3,
) -> None:
    """
    Safely transmits a document to Telegram, catching rate limits (RetryAfter)
    and timeouts with exponential backoff.
    """
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
            logger.warning(
                f"Telegram rate limit hit during upload. Sleeping for {wait_time}s (attempt {attempt + 1}/{max_retries})..."
            )
            await asyncio.sleep(wait_time)
        except TimedOut:
            if attempt == max_retries - 1:
                raise
            logger.warning(f"Telegram upload timed out. Retrying in 1s (attempt {attempt + 1}/{max_retries})...")
            await asyncio.sleep(1.0)


async def document_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles incoming documents (.txt, .docx, .epub).
    Downloads asynchronously to local temporary storage and presents
    action keyboard: Translate File or Split File.
    """
    message = update.message
    if not message or not message.document:
        return

    user = update.effective_user
    if not user:
        return

    doc = message.document
    original_filename = doc.file_name or "document"
    ext = os.path.splitext(original_filename)[1].lower()

    # Validate file format
    if ext not in SUPPORTED_EXTENSIONS:
        supported_list = ", ".join(f"`{e}`" for e in sorted(SUPPORTED_EXTENSIONS))
        await message.reply_text(
            f"⚠️ *Unsupported File Format*\n\n"
            f"Received: `{original_filename}`\n"
            f"Supported document formats are: {supported_list}\n\n"
            "Please send a valid `.txt`, `.docx`, `.epub`, or `.pdf` file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Telegram bot download size limit (20 MB for standard bot tokens)
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.reply_text(
            "⚠️ *File Too Large*\n\n"
            f"The file `{original_filename}` ({format_file_size(doc.file_size)}) "
            "exceeds Telegram's 20 MB bot download limit.\n"
            "Please upload a smaller file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Ephemeral download setup
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
        # Clean partial download if it exists
        if os.path.exists(local_temp_path):
            try:
                os.remove(local_temp_path)
            except OSError:
                pass
        await status_msg.edit_text("❌ *Failed to download file from Telegram.* Please try again.")
        return

    # Save to user session buffer
    set_active_file(user.id, path=local_temp_path, name=original_filename, ext=ext)

    # Proactively clean up any stale files from abandoned sessions
    try:
        cleanup_expired_temp_files()
    except Exception:
        pass

    session = get_user_session(user.id)
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    # Present action keyboard
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

    await status_msg.edit_text(caption_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def unsupported_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Informs users sending photos, videos, voice notes, stickers, or audio
    that only text and documents (.txt, .docx, .epub, .pdf) are supported.
    """
    if update.message:
        await update.message.reply_text(
            "⚠️ *Unsupported Media Type*\n\n"
            "I can only process documents in `.txt`, `.docx`, `.epub`, or `.pdf` format, "
            "or plain text messages for direct translation.\n\n"
            "Please send a document file (.txt, .docx, .epub, .pdf) as an uncompressed attachment.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles plain text messages:
    - If user is awaiting split count: validates input, executes splitting, transmits parts.
    - If user is not awaiting split count: translates text directly. If output > 4000 characters,
      returns as an in-memory .txt file using io.BytesIO.
    """
    message = update.message
    if not message or not message.text:
        return

    user = update.effective_user
    if not user:
        return

    session = get_user_session(user.id)
    raw_text = message.text.strip()

    # -----------------------------------------------------------------------
    # Case 1: User is responding to "Split File" prompt with chunk count
    # -----------------------------------------------------------------------
    if session.awaiting_split_count:
        try:
            chunk_count = int(raw_text)
            if chunk_count <= 0:
                raise ValueError("Must be positive")
        except ValueError:
            await message.reply_text(
                "⚠️ *Invalid input!*\n"
                "Please enter a valid positive whole number (e.g., `5`, `25`, `100`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Valid count received: Reset awaiting flag
        set_awaiting_split(user.id, False)

        active_file = get_valid_active_file(user.id)
        if not active_file:
            await message.reply_text(
                "⚠️ *Session Expired*\n"
                "Your uploaded document session has expired (files are kept for 30 minutes). "
                "Please upload your document again to proceed.",
                parse_mode=ParseMode.MARKDOWN,
            )
            clear_active_file(user.id, remove_file=False)
            return

        status_msg = await message.reply_text(
            f"✂️ *Splitting `{active_file.name}` into chunks of {chunk_count}...*\n"
            "Please wait...",
            parse_mode=ParseMode.MARKDOWN,
        )

        generated_parts = []

        try:
            loop = asyncio.get_running_loop()
            # Offload heavy splitting to dedicated ThreadPoolExecutor
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

            # Send all resulting parts sequentially with rate-limit protection
            for idx, part_path in enumerate(generated_parts, start=1):
                part_ext = os.path.splitext(part_path)[1]
                part_display_name = f"part_{idx}{part_ext}"
                await safe_send_document_with_retry(
                    chat=update.effective_chat,
                    document_path=part_path,
                    filename=part_display_name,
                    caption=f"📦 Part {idx} of {total_parts}",
                )
                # Politeness delay to avoid Telegram flood limits on rapid multi-file uploads
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
            # Ephemeral File Lifecycle: Delete all generated parts immediately after transmission
            for p in generated_parts:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError as e:
                        logger.warning(f"Failed to remove split part {p}: {e}")

            # Delete original temporary file and clear active session
            clear_active_file(user.id, remove_file=True)

            try:
                await status_msg.delete()
            except Exception:
                pass

        return

    # -----------------------------------------------------------------------
    # Case 2: Plain Text Translation
    # -----------------------------------------------------------------------
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        loop = asyncio.get_running_loop()
        # Offload chunked text translation to dedicated ThreadPoolExecutor
        translated_text = await loop.run_in_executor(
            THREAD_POOL,
            translate_text,
            raw_text,
            session.target_language,
        )

        # If output length > 4000 characters: return in-memory .txt file using io.BytesIO
        if len(translated_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            bio = io.BytesIO(translated_text.encode("utf-8"))
            bio.name = "translated_text.txt"
            await message.reply_document(
                document=bio,
                filename="translated_text.txt",
                caption=(
                    f"📄 *Text Translation Output*\n"
                    f"Content exceeded 4,000 characters ({len(translated_text):,} chars).\n"
                    f"🌐 Translated to: `{lang_name}`"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(translated_text)

    except Exception as exc:
        logger.error(f"Error during plain text translation: {exc}", exc_info=True)
        safe_err = str(exc).replace("`", "'")
        try:
            await message.reply_text(
                f"❌ *Translation Failed:*\n`{safe_err}`\n\nPlease try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await message.reply_text(
                f"❌ Translation Failed: {safe_err}\n\nPlease try again later."
            )

