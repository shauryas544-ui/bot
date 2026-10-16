import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    SUPPORTED_LANGUAGES,
    get_user_session,
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
