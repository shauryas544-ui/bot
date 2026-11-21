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
) -> bool:
    """
    Silently sends a translated file to the backup group.
    """
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
