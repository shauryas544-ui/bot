import os
import asyncio
import logging
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ContextTypes

from config import (
    SUPPORTED_LANGUAGES,
    THREAD_POOL,
    get_user_session,
    get_valid_active_file,
    set_user_language,
    clear_active_file,
    set_awaiting_split,
)
from services.translator import translate_file, ProgressTracker, format_progress_message
from handlers.commands import build_language_keyboard

logger = logging.getLogger("telegram_doc_bot.callbacks")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main callback dispatcher for inline keyboard buttons.
    """
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user = update.effective_user
    if not user:
        return

    session = get_user_session(user.id)

    # 1. Handle Language Selection
    if data.startswith("set_lang:"):
        target_code = data.split(":", 1)[1]
        if target_code in SUPPORTED_LANGUAGES:
            set_user_language(user.id, target_code)
            lang_label = SUPPORTED_LANGUAGES[target_code]
            await query.answer(f"Language set to {lang_label}!")

            # Update keyboard matrix with new checkmark
            updated_keyboard = build_language_keyboard(target_code)
            try:
                await query.edit_message_reply_markup(reply_markup=updated_keyboard)
            except Exception as e:
                logger.debug(f"Keyboard already reflects selection: {e}")
        else:
            await query.answer("Unknown language option.")
        return

    # 2. Handle File Actions
    if data == "action_translate":
        await handle_action_translate(update, context)
        return

    if data == "action_split":
        await handle_action_split(update, context)
        return

    await query.answer("Unknown action.")


async def handle_action_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Executes file translation asynchronously offloaded to dedicated thread executor,
    maintains strict ephemeral file lifecycle, and sends translated document back
    with automatic rate-limit retry handling.
    """
    query = update.callback_query
    user = update.effective_user
    if not user or not update.effective_chat:
        return

    session = get_user_session(user.id)
    active_file = get_valid_active_file(user.id)

    if not active_file:
        await query.answer("⚠️ File session expired or file not found. Please upload again.", show_alert=True)
        clear_active_file(user.id, remove_file=False)
        return

    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)
    await query.answer()

    status_msg = await update.effective_chat.send_message(
        format_progress_message(active_file.name, lang_name, 0, "Analyzing document structure..."),
        parse_mode=ParseMode.MARKDOWN,
    )

    output_path = None

    try:
        loop = asyncio.get_running_loop()
        bot_inst = context.bot if context and context.bot else getattr(update, "_bot", None)
        tracker = ProgressTracker(
            bot=bot_inst,
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            filename=active_file.name,
            lang_name=lang_name,
            loop=loop,
        )
        # Offload CPU/IO intensive translation to dedicated ThreadPoolExecutor
        output_path = await loop.run_in_executor(
            THREAD_POOL,
            translate_file,
            active_file.path,
            active_file.ext,
            session.target_language,
            active_file.name,
            tracker.update,
        )

        stem, _ = os.path.splitext(active_file.name)
        out_ext = os.path.splitext(output_path)[1]
        out_filename = f"translated_{stem}{out_ext}"

        # Transmit translated file to Telegram with rate-limit resiliency
        sent = False
        for attempt in range(3):
            try:
                with open(output_path, "rb") as doc_file:
                    await update.effective_chat.send_document(
                        document=doc_file,
                        filename=out_filename,
                        caption=f"✅ *Translation Completed!*\n🌐 Language: {lang_name}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                sent = True
                break
            except RetryAfter as flood_err:
                logger.warning(f"Telegram flood control: waiting {flood_err.retry_after}s before retrying send...")
                await asyncio.sleep(flood_err.retry_after + 1)
            except TimedOut:
                if attempt == 2:
                    raise
                logger.warning("Telegram send_document timed out, retrying in 1s...")
                await asyncio.sleep(1.0)

        if not sent:
            raise RuntimeError("Failed to transmit translated document after retries.")

        # Delete status message
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as exc:
        logger.error(f"Error during file translation: {exc}", exc_info=True)
        safe_err = str(exc).replace("`", "'")
        try:
            await update.effective_chat.send_message(
                f"❌ *Translation Failed:*\n`{safe_err}`\n\nPlease try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await update.effective_chat.send_message(
                f"❌ Translation Failed: {safe_err}\n\nPlease try again."
            )
    finally:
        # Ephemeral File Lifecycle: Ensure input & output temporary files are strictly deleted
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError as err:
                logger.warning(f"Failed to remove translated temp file {output_path}: {err}")

        # Delete active input temp file & clear session buffer
        clear_active_file(user.id, remove_file=True)


async def handle_action_split(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Shifts user session into awaiting_split_count = True and prompts
    for chunk count based on document format.
    """
    query = update.callback_query
    user = update.effective_user
    if not user:
        return

    active_file = get_valid_active_file(user.id)
    if not active_file:
        await query.answer("⚠️ File session expired or file not found. Please upload again.", show_alert=True)
        clear_active_file(user.id, remove_file=False)
        return

    set_awaiting_split(user.id, True)
    await query.answer()

    ext = active_file.ext.lower()
    if ext == ".pdf":
        prompt_text = (
            "📖 *PDF Splitting Selected*\n\n"
            "How many pages per split PDF? (e.g., `5`, `10`, `20`)\n"
            "_Please reply with a positive whole number._"
        )
    elif ext == ".epub":
        prompt_text = (
            "📖 *EPUB Splitting Selected*\n\n"
            "How many chapters per volume? (e.g., `5`, `10`)\n"
            "_Please reply with a positive whole number._"
        )
    elif ext == ".docx":
        prompt_text = (
            "📄 *DOCX Splitting Selected*\n\n"
            "How many paragraphs per file? (e.g., `25`, `50`)\n"
            "_Please reply with a positive whole number._"
        )
    elif ext == ".txt":
        prompt_text = (
            "📝 *TXT Splitting Selected*\n\n"
            "How many lines per file? (e.g., `200`, `500`)\n"
            "_Please reply with a positive whole number._"
        )
    else:
        prompt_text = (
            "✂️ *File Splitting Selected*\n\n"
            "How many units per file chunk?\n"
            "_Please reply with a positive whole number._"
        )

    await query.message.reply_text(prompt_text, parse_mode=ParseMode.MARKDOWN)

