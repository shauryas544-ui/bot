import re
import asyncio
import logging
from typing import Optional, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    SUPPORTED_LANGUAGES,
    BACKUP_INVITE_LINK,
    get_user_session,
    get_backup_chat_id,
    save_backup_chat_id_to_env,
)

logger = logging.getLogger("telegram_doc_bot.commands")


def build_language_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    """
    Builds a 3-column inline keyboard matrix for language selection,
    marking the currently active language with a checkmark.
    """
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
    """
    /start: Initializes user session and displays welcome overview.
    """
    user = update.effective_user
    if not user:
        return

    session = get_user_session(user.id)
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    welcome_text = (
        f"👋 *Welcome, {user.first_name}!*\n\n"
        "I am your production-grade *Document Translation & File Splitting Bot*.\n\n"
        "✨ *Core Capabilities:*\n"
        "• 📝 *Plain Text Translation:* Send any text to translate it directly. "
        "If output exceeds 4,000 characters, you'll receive a ready-to-read `.txt` file.\n"
        "• 📁 *Document Translation:* Upload `.txt`, `.docx`, or `.epub` files. "
        "I will translate them while strictly preserving structure and styling.\n"
        "• ✂️ *Dynamic File Splitting:* Split documents into manageable chunks:\n"
        "   — `.epub`: Split by chapters per volume\n"
        "   — `.docx`: Split by paragraphs per file\n"
        "   — `.txt`: Split by lines per file\n\n"
        f"🌐 *Current Target Language:* `{lang_name}`\n"
        "Use /language anytime to change your target language."
    )

    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)


async def language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /language: Presents an inline keyboard matrix to select target translation language.
    """
    user = update.effective_user
    if not user:
        return

    session = get_user_session(user.id)
    reply_markup = build_language_keyboard(session.target_language)
    lang_label = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    prompt_text = (
        f"🌐 *Select Target Translation Language*\n\n"
        f"Currently selected: *{lang_label}*\n"
        "Choose a language from the options below:"
    )

    await update.message.reply_text(
        prompt_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
    )


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


async def set_backup_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /set_backup_group: Admin command to register current group as backup group.
    """
    chat = update.effective_chat
    if not chat or chat.type not in ["group", "supergroup"]:
        if update.message:
            await update.message.reply_text("This command must be run inside the target backup group.")
        return
    save_backup_chat_id_to_env(str(chat.id))
    try:
        await update.message.delete()
    except Exception:
        pass


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Auto-detects when bot is added to or made admin of a group.
    """
    chat = update.effective_chat
    if not chat or chat.type not in ["group", "supergroup"]:
        return
    if update.my_chat_member and update.my_chat_member.new_chat_member:
        new_status = update.my_chat_member.new_chat_member.status
        if new_status in ["administrator", "member"]:
            save_backup_chat_id_to_env(str(chat.id))

