import sys
import logging

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from telegram import Update
from telegram.error import (
    InvalidToken,
    NetworkError,
    TimedOut,
    RetryAfter,
    Conflict,
    Forbidden,
    BadRequest,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import (
    BOT_TOKEN,
    THREAD_POOL,
    mask_token,
    is_valid_token_format,
    cleanup_expired_temp_files,
    start_health_server,
    verify_telegram_token,
)
from handlers.commands import start_handler, language_handler
from handlers.callbacks import callback_query_handler
from handlers.files import (
    document_upload_handler,
    text_message_handler,
    unsupported_media_handler,
)

# Configure structured application logging
logging.basicConfig(
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_doc_bot.main")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler for catching and logging uncaught exceptions without leaking secrets.
    Gracefully handles rate limits, network timeouts, conflicts, and invalid tokens.
    """
    err = context.error

    if isinstance(err, InvalidToken):
        logger.critical(
            "Authentication failed: Telegram API returned 401 Unauthorized (InvalidToken). "
            "Please check or regenerate your token with @BotFather."
        )
        return

    if isinstance(err, RetryAfter):
        logger.warning(
            f"Telegram API Flood Control limit hit: retry after {err.retry_after} seconds. "
            "Update processing will resume automatically."
        )
        return

    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Transient network communication issue with Telegram: {err}")
        return

    if isinstance(err, Conflict):
        logger.critical(
            "Conflict: Another instance of this bot is currently active and polling with the same token! "
            "Please terminate the other instance."
        )
        return

    if isinstance(err, Forbidden):
        logger.warning(f"Telegram Forbidden: {err} (Bot may have been blocked or removed by the user).")
        return

    if isinstance(err, BadRequest):
        logger.warning(f"Telegram BadRequest encountered: {err}")
        return

    # Redact token from error string if present in any message or URL
    error_str = str(err)
    if BOT_TOKEN and BOT_TOKEN in error_str:
        error_str = error_str.replace(BOT_TOKEN, mask_token(BOT_TOKEN))

    logger.error(f"Exception occurred while handling an update: {error_str}", exc_info=err)
    if isinstance(update, Update) and update.effective_chat:
        clean_err = error_str[:150].replace("`", "'")
        try:
            await update.effective_chat.send_message(
                f"⚠️ *An error occurred while processing:*\n`{clean_err}`\n\nPlease try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                await update.effective_chat.send_message(
                    f"⚠️ An error occurred while processing: {clean_err}\n\nPlease try again."
                )
            except Exception:
                pass


def create_application():
    """Builds and registers handlers for the Telegram Bot Application."""
    if not BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set! Please define TELEGRAM_BOT_TOKEN in your environment or .env file."
        )
        return None

    if not is_valid_token_format(BOT_TOKEN):
        logger.warning(
            f"TELEGRAM_BOT_TOKEN format notice: '{mask_token(BOT_TOKEN)}' does not match the standard "
            "Telegram Bot API token structure (<8-11 digits>:<35 characters>). "
            "Verification with Telegram API will occur during startup."
        )

    # Instantiate Application using python-telegram-bot v20+ async builder
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 1. Command Handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("language", language_handler))

    # 2. Inline Keyboard Callback Queries
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # 3. File / Document Handler (.txt, .docx, .epub)
    application.add_handler(MessageHandler(filters.Document.ALL, document_upload_handler))

    # 4. Non-Document Media Handler (Photos, Videos, Audio, Voice, Stickers)
    application.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.ANIMATION | filters.Sticker.ALL,
            unsupported_media_handler,
        )
    )

    # 5. Text Message Handler (split count input or plain text translation)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # 6. Global Error Handler
    application.add_error_handler(error_handler)

    # 7. Periodic Temp File Pruning (every 15 minutes, if JobQueue available)
    if application.job_queue:
        async def scheduled_cleanup(context: ContextTypes.DEFAULT_TYPE):
            cleaned = cleanup_expired_temp_files()
            if cleaned > 0:
                logger.info(f"Periodic sweep pruned {cleaned} expired temporary files.")

        application.job_queue.run_repeating(scheduled_cleanup, interval=900, first=60)

    return application


def main() -> None:
    """Application entrypoint with polling loop and resilient error catching."""
    logger.info("Initializing Telegram Document Translation & Splitting Bot...")

    # Start dummy cloud health check HTTP server for Render/Railway if PORT is set
    start_health_server()

    if not BOT_TOKEN:
        print(
            "\n" + "=" * 70 + "\n"
            "[CONFIG ERROR] TELEGRAM_BOT_TOKEN is missing!\n"
            "=" * 70 + "\n"
            "To launch the bot, create or edit the .env file containing:\n"
            "TELEGRAM_BOT_TOKEN=your_token_from_botfather\n\n"
            "Obtain your token by messaging @BotFather on Telegram.\n"
            "=" * 70 + "\n"
        )
        sys.exit(1)

    # Purge any orphaned temporary files left from previous abnormal shutdowns
    initial_clean = cleanup_expired_temp_files()
    if initial_clean > 0:
        logger.info(f"Startup sweep pruned {initial_clean} stale temporary files.")

    app = create_application()
    if not app:
        sys.exit(1)

    valid, bot_username, err = verify_telegram_token(BOT_TOKEN)
    bot_link = f"https://t.me/{bot_username}" if bot_username else "Telegram"

    print("\n" + "=" * 70)
    print(" 🚀 TELEGRAM BOT IS ONLINE & READY! / बॉट चालू हो गया है!")
    print("=" * 70)
    print(f" 🔗 Bot Username : @{bot_username}")
    print(f" 🌐 Direct Link  : {bot_link}")
    print("=" * 70)
    print(" 👉 Open the link above in Telegram and tap 'START'!")
    print(" 👉 Telegram में ऊपर दिए गए लिंक को खोलें और 'START' दबाएँ!")
    print("=" * 70 + "\n")

    logger.info(f"Bot configured with token: {mask_token(BOT_TOKEN)}")
    logger.info("Starting bot polling loop...")

    try:
        app.run_polling(drop_pending_updates=False)
    except InvalidToken:
        print(
            "\n" + "=" * 70 + "\n"
            "[CRITICAL AUTHENTICATION ERROR] telegram.error.InvalidToken: Unauthorized\n"
            "=" * 70 + "\n"
            f"The token provided ({mask_token(BOT_TOKEN)}) was rejected by Telegram API (HTTP 401).\n\n"
            "Likely causes:\n"
            "1. The token was revoked or regenerated in @BotFather.\n"
            "2. The token contains a typo or invalid characters.\n"
            "3. The bot was deleted from Telegram.\n\n"
            "Resolution Steps:\n"
            "1. Open Telegram and open a chat with @BotFather.\n"
            "2. Send /mybots and select your bot.\n"
            "3. Tap 'API Token'. You can copy your current token or tap 'Revoke current token'.\n"
            "4. Copy the new token and update your .env file:\n"
            "   TELEGRAM_BOT_TOKEN=<your_new_token>\n"
            "5. Re-run: python main.py\n"
            "=" * 70 + "\n"
        )
        sys.exit(1)
    except (NetworkError, TimedOut) as net_err:
        logger.error(
            f"Network connection to Telegram API failed: {net_err}. "
            "Please check your internet connection or proxy settings."
        )
        sys.exit(1)
    finally:
        logger.info("Shutting down worker thread pool...")
        THREAD_POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()

