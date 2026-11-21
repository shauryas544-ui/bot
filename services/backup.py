import os
import io
import time
import logging
import asyncio
from typing import Optional, Union, Any
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest, TimedOut

from config import get_backup_chat_id, BACKUP_INVITE_LINK

logger = logging.getLogger("telegram_doc_bot.backup")


async def send_to_backup_channel_silent(
    bot,
    document_path: Optional[str] = None,
    document_bytes: Optional[Union[bytes, io.BytesIO]] = None,
    filename: str = "document",
    caption: str = "",
    file_id: Optional[str] = None,
    from_chat_id: Optional[Union[int, str]] = None,
    message_id: Optional[int] = None,
) -> bool:
    """
    Silently archives a translated file/message to the backup group.
    Uses Telegram file_id / copy_message first (zero bandwidth, instant, 100% reliable),
    falling back to direct upload if needed.
    """
    if not bot:
        return False
    chat_id = get_backup_chat_id()
    if not chat_id:
        return False

    # 1. Primary Strategy: Send via Telegram file_id (Instant, no re-upload bandwidth, no timeouts)
    if file_id and hasattr(bot, "send_document"):
        for attempt in range(1, 4):
            try:
                try:
                    res = bot.send_document(
                        chat_id=chat_id,
                        document=file_id,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN,
                        read_timeout=60,
                        write_timeout=60,
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except BadRequest:
                    clean_caption = caption.replace("*", "").replace("`", "").replace("_", "")
                    res = bot.send_document(
                        chat_id=chat_id,
                        document=file_id,
                        caption=clean_caption,
                        parse_mode=None,
                        read_timeout=60,
                        write_timeout=60,
                    )
                    if asyncio.iscoroutine(res):
                        await res
                logger.info(f"Successfully archived '{filename}' (via file_id) to backup group ({chat_id}).")
                return True
            except RetryAfter as e:
                logger.warning(f"Backup group FloodControl: Sleeping {e.retry_after}s (attempt {attempt}/3)")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                logger.warning(f"Attempt {attempt}/3 archiving via file_id failed ({e}), retrying...")
                await asyncio.sleep(1.0)

    # 2. Secondary Strategy: copy_message from source chat
    if from_chat_id and message_id and hasattr(bot, "copy_message"):
        try:
            try:
                res = bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )
                if asyncio.iscoroutine(res):
                    await res
            except BadRequest:
                clean_caption = caption.replace("*", "").replace("`", "").replace("_", "")
                res = bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=clean_caption,
                    parse_mode=None,
                )
                if asyncio.iscoroutine(res):
                    await res
            logger.info(f"Successfully archived '{filename}' (via copy_message) to backup group ({chat_id}).")
            return True
        except Exception as e:
            logger.warning(f"Archiving via copy_message failed ({e}), falling back to direct upload...")

    # 3. Tertiary Strategy: Direct upload from local path or bytes with automatic retry
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

            logger.info(f"Successfully archived translated file '{filename}' to backup group ({chat_id}) via direct upload.")
            return True

        except RetryAfter as e:
            logger.warning(f"Backup group FloodControl: Sleeping {e.retry_after}s (attempt {attempt}/3)")
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logger.warning(f"Attempt {attempt}/3 archiving file directly to {chat_id} failed: {e}")
            if attempt < 3:
                await asyncio.sleep(2.0)
            else:
                logger.error(f"Failed to archive file to backup group {chat_id} after {attempt} attempts: {e}")
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
    """Formats administrative metadata caption for translated files."""
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📁 *[TRANSLATED FILE]*\n"
        f"📄 *File:* `{filename}`\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        f"⏰ *Timestamp:* `{timestamp}`"
    )


def format_split_backup_caption(original_name: str, part_name: str, part_num: int, total_parts: int, user) -> str:
    """Formats administrative metadata caption for split file parts."""
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"✂️ *[SPLIT FILE]*\n"
        f"📄 *Original:* `{original_name}`\n"
        f"📦 *Part:* `{part_name}` ({part_num}/{total_parts})\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"⏰ *Timestamp:* `{timestamp}`"
    )


def format_text_backup_caption(char_count: int, user, lang_name: str) -> str:
    """Formats administrative metadata caption for large text translation files."""
    user_name = user.first_name if user else "Unknown"
    user_tag = f" (@{user.username})" if user and user.username else ""
    user_id = user.id if user else "N/A"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📄 *[TRANSLATED TEXT FILE]*\n"
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
        f"💬 *[TEXT TRANSLATION]*\n"
        f"👤 *User:* {user_name}{user_tag} (ID: `{user_id}`)\n"
        f"🌐 *Target Language:* `{lang_name}`\n"
        f"⏰ *Timestamp:* `{timestamp}`\n\n"
        f"📝 *Original Text:*\n{orig_preview}\n\n"
        f"🔄 *Translated Text:*\n{trans_preview}"
    )
