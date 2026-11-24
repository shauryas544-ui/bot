import os
import re
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, BadRequest
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
from services.translator import translate_file, translate_text, ProgressTracker, format_progress_message
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

    if data == "split_cancel":
        set_awaiting_split(user.id, False)
        await query.answer("Splitting cancelled.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("split_mode:"):
        mode = data.split(":", 1)[1]
        session = get_user_session(user.id)
        session.split_mode = mode
        set_awaiting_split(user.id, True)
        await query.answer()

        active_file = get_valid_active_file(user.id)
        if mode == "size":
            size_prompt = (
                "💾 *File Splitting by Size (KB / MB)*\n\n"
                "Enter the target size per file part:\n\n"
                "📌 *Examples:*\n"
                "• `500KB` or `250KB`\n"
                "• `1MB` or `2.5MB`\n"
                "• Or enter a number (e.g. `500` for 500 KB)\n\n"
                "_Please reply with your desired size._"
            )
            await query.message.reply_text(size_prompt, parse_mode=ParseMode.MARKDOWN)
        else:
            if active_file and active_file.name != "text_message.txt":
                ext = active_file.ext.lower()
                if ext == ".pdf":
                    prompt_text = "📖 *PDF Splitting by Pages*\n\nHow many pages per split PDF? (e.g. `5`, `10`, `20`)\n_Reply with a positive whole number._"
                elif ext == ".epub":
                    prompt_text = "📖 *EPUB Splitting by Chapters*\n\nHow many chapters per volume? (e.g. `5`, `10`)\n_Reply with a positive whole number._"
                elif ext == ".docx":
                    prompt_text = "📄 *DOCX Splitting by Paragraphs*\n\nHow many paragraphs per file? (e.g. `25`, `50`)\n_Reply with a positive whole number._"
                else:
                    prompt_text = "📝 *TXT Splitting by Lines*\n\nHow many lines per file? (e.g. `100`, `250`, `500`)\n_Reply with a positive whole number._"
            else:
                prompt_text = "📝 *Text Splitting by Lines*\n\nHow many lines per part? (e.g. `10`, `50`, `100`)\n_Reply with a positive whole number._"
            await query.message.reply_text(prompt_text, parse_mode=ParseMode.MARKDOWN)
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
    lang_name = SUPPORTED_LANGUAGES.get(session.target_language, session.target_language)

    # Check if this is a text translation action
    if session.active_text:
        await query.answer()
        raw_text = session.active_text
        status_msg = await update.effective_chat.send_message(
            f"⏳ *Translating text to {lang_name}...*",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            loop = asyncio.get_running_loop()
            translated_text = await loop.run_in_executor(
                THREAD_POOL,
                translate_text,
                raw_text,
                session.target_language,
            )
            from config import TELEGRAM_MAX_MESSAGE_LENGTH
            import io
            if len(translated_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
                bio = io.BytesIO(translated_text.encode("utf-8"))
                bio.name = "translated_text.txt"
                sent_txt = await update.effective_chat.send_document(
                    document=bio,
                    filename="translated_text.txt",
                    caption=f"📄 *Text Translation Output* ({len(translated_text):,} chars)\n🌐 Language: `{lang_name}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                try:
                    bio.seek(0)
                    from services.backup import send_to_backup_channel_silent, format_text_backup_caption
                    bot_inst = context.bot if context and context.bot else getattr(update, "_bot", None)
                    text_cap = format_text_backup_caption(len(translated_text), user, lang_name)
                    file_id = sent_txt.document.file_id if (sent_txt and getattr(sent_txt, "document", None)) else None
                    msg_id = sent_txt.message_id if sent_txt else None
                    chat_id_src = update.effective_chat.id if update.effective_chat else None
                    await send_to_backup_channel_silent(
                        bot=bot_inst,
                        document_bytes=bio,
                        filename="translated_text.txt",
                        caption=text_cap,
                        file_id=file_id,
                        from_chat_id=chat_id_src,
                        message_id=msg_id,
                    )
                except Exception as b_err:
                    logger.warning(f"Non-critical: Failed to send translated text to backup group: {b_err}")
            else:
                await update.effective_chat.send_message(translated_text)
                try:
                    from services.backup import send_backup_message_silent, format_direct_text_backup
                    bot_inst = context.bot if context and context.bot else getattr(update, "_bot", None)
                    direct_backup_text = format_direct_text_backup(raw_text, translated_text, user, lang_name)
                    await send_backup_message_silent(bot=bot_inst, text=direct_backup_text)
                except Exception as b_err:
                    logger.warning(f"Non-critical: Failed to send direct text to backup group: {b_err}")
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            logger.error(f"Error during text translation: {exc}", exc_info=True)
            await update.effective_chat.send_message(f"❌ Translation failed: {exc}")
        finally:
            clear_active_file(user.id, remove_file=True)
        return

    active_file = get_valid_active_file(user.id)

    if not active_file:
        await query.answer("⚠️ Session expired or file not found. Please send file or text again.", show_alert=True)
        clear_active_file(user.id, remove_file=False)
        return

    await query.answer()

    try:
        status_msg = await update.effective_chat.send_message(
            format_progress_message(active_file.name, lang_name, 0, "Analyzing document structure..."),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        status_msg = await update.effective_chat.send_message(
            f"⏳ Translating {active_file.name} to {lang_name}... [0%]\nAnalyzing document structure...",
            parse_mode=None,
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
        clean_stem = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', stem)[:40] or "document"
        out_ext = os.path.splitext(output_path)[1]
        out_filename = f"translated_{clean_stem}{out_ext}"

        # Transmit translated file to Telegram with rate-limit resiliency and markdown fallback
        sent = False
        sent_msg = None
        for attempt in range(3):
            try:
                with open(output_path, "rb") as doc_file:
                    try:
                        sent_msg = await update.effective_chat.send_document(
                            document=doc_file,
                            filename=out_filename,
                            caption=f"✅ *Translation Completed!*\n🌐 Language: {lang_name}",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except BadRequest:
                        doc_file.seek(0)
                        sent_msg = await update.effective_chat.send_document(
                            document=doc_file,
                            filename=out_filename,
                            caption=f"✅ Translation Completed!\n🌐 Language: {lang_name}",
                            parse_mode=None,
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

        # Silently upload translated file to backup group
        try:
            from services.backup import send_to_backup_channel_silent, format_translation_backup_caption
            backup_cap = format_translation_backup_caption(out_filename, user, lang_name)
            file_id = sent_msg.document.file_id if (sent_msg and getattr(sent_msg, "document", None)) else None
            msg_id = sent_msg.message_id if sent_msg else None
            chat_id_src = update.effective_chat.id if update.effective_chat else None
            await send_to_backup_channel_silent(
                bot=bot_inst,
                document_path=output_path,
                filename=out_filename,
                caption=backup_cap,
                file_id=file_id,
                from_chat_id=chat_id_src,
                message_id=msg_id,
            )
        except Exception as b_err:
            logger.warning(f"Non-critical: Failed to send translated file to backup group: {b_err}")

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
    Presents choice of splitting mode: By Count (lines/pages/chapters) or By Size (KB/MB).
    """
    query = update.callback_query
    user = update.effective_user
    if not user:
        return

    active_file = get_valid_active_file(user.id)
    session = get_user_session(user.id)
    if not active_file and not session.active_text:
        await query.answer("⚠️ File session expired or file not found. Please upload again.", show_alert=True)
        clear_active_file(user.id, remove_file=False)
        return

    set_awaiting_split(user.id, True)
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton("🔢 By Lines / Pages", callback_data="split_mode:count"),
            InlineKeyboardButton("💾 By File Size (KB/MB)", callback_data="split_mode:size"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="split_cancel"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    file_label = active_file.name if active_file else "Text Message"
    prompt_text = (
        f"✂️ *Choose Split Method for `{file_label}`:*\n\n"
        "• 🔢 *By Count:* Split by lines, paragraphs, pages, or chapters.\n"
        "• 💾 *By File Size:* Split by maximum size (e.g. `500KB`, `2MB`).\n\n"
        "Select your preferred method below:"
    )
    await query.message.reply_text(prompt_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

