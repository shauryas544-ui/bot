import io
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram.error import RetryAfter
from config import (
    TEMP_DIR,
    get_user_session,
    set_user_language,
    set_active_file,
    clear_active_file,
    set_awaiting_split,
)
from handlers.commands import start_handler, language_handler
from handlers.callbacks import callback_query_handler
from handlers.files import (
    document_upload_handler,
    text_message_handler,
    unsupported_media_handler,
    safe_send_document_with_retry,
)


class TestBotFlows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = 12345
        clear_active_file(self.user_id, remove_file=True)
        set_user_language(self.user_id, "hi")

    def tearDown(self):
        clear_active_file(self.user_id, remove_file=True)

    async def test_start_command(self):
        update = MagicMock()
        update.effective_user.id = self.user_id
        update.effective_user.first_name = "Alice"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await start_handler(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("Welcome, Alice", args[0])
        self.assertIn("Hindi", args[0])

    async def test_language_command(self):
        update = MagicMock()
        update.effective_user.id = self.user_id
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await language_handler(update, context)

        update.message.reply_text.assert_awaited_once()
        _, kwargs = update.message.reply_text.call_args
        self.assertIn("reply_markup", kwargs)

    async def test_callback_set_language(self):
        update = MagicMock()
        update.effective_user.id = self.user_id
        update.callback_query.data = "set_lang:fr"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        context = MagicMock()

        await callback_query_handler(update, context)

        update.callback_query.answer.assert_awaited_once()
        session = get_user_session(self.user_id)
        self.assertEqual(session.target_language, "fr")

    @patch("handlers.files.translate_text", return_value="नमस्ते दुनिया")
    async def test_plain_text_short_translation(self, mock_translate):
        update = MagicMock()
        update.effective_user.id = self.user_id
        update.effective_chat.id = 12345
        update.message.text = "Hello world"
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        session = get_user_session(self.user_id)
        session.awaiting_split_count = False

        await text_message_handler(update, context)

        update.message.reply_text.assert_awaited_once_with("नमस्ते दुनिया")

    @patch("handlers.files.translate_text", return_value="A" * 4500)
    async def test_plain_text_over_4000_chars(self, mock_translate):
        update = MagicMock()
        update.effective_user.id = self.user_id
        update.effective_chat.id = 12345
        update.message.text = "Hello world. " * 350
        update.message.reply_document = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        session = get_user_session(self.user_id)
        session.awaiting_split_count = False

        await text_message_handler(update, context)

        update.message.reply_document.assert_awaited_once()
        _, kwargs = update.message.reply_document.call_args
        self.assertEqual(kwargs["filename"], "translated_text.txt")
        self.assertIsInstance(kwargs["document"], io.BytesIO)

    async def test_split_flow_txt(self):
        test_txt = os.path.join(TEMP_DIR, "bot_flow_test.txt")
        with open(test_txt, "w", encoding="utf-8") as f:
            for i in range(1, 21):
                f.write(f"Line {i}\n")

        set_active_file(self.user_id, test_txt, "bot_flow_test.txt", ".txt")

        # Trigger action_split callback
        cb_update = MagicMock()
        cb_update.effective_user.id = self.user_id
        cb_update.callback_query.data = "action_split"
        cb_update.callback_query.answer = AsyncMock()
        cb_update.callback_query.message.reply_text = AsyncMock()
        context = MagicMock()

        await callback_query_handler(cb_update, context)

        session = get_user_session(self.user_id)
        self.assertTrue(session.awaiting_split_count)
        cb_update.callback_query.message.reply_text.assert_awaited_once()

        # User responds with numeric chunk size "5"
        txt_update = MagicMock()
        txt_update.effective_user.id = self.user_id
        txt_update.effective_chat.id = self.user_id
        txt_update.message.text = "5"
        status_msg_mock = MagicMock()
        status_msg_mock.edit_text = AsyncMock()
        status_msg_mock.delete = AsyncMock()
        txt_update.message.reply_text = AsyncMock(return_value=status_msg_mock)
        txt_update.effective_chat.send_document = AsyncMock()
        txt_update.effective_chat.send_message = AsyncMock()

        await text_message_handler(txt_update, context)

        # Should send 4 parts (20 lines / 5 = 4 parts)
        self.assertEqual(txt_update.effective_chat.send_document.await_count, 4)
        self.assertFalse(session.awaiting_split_count)
        self.assertIsNone(session.active_file)
        self.assertFalse(os.path.exists(test_txt))

    async def test_document_unsupported_format(self):
        update = MagicMock()
        update.effective_user.id = self.user_id
        update.message.document.file_name = "malware.exe"
        update.message.document.file_size = 1024
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await document_upload_handler(update, context)

        update.message.reply_text.assert_awaited_once()
        args, _ = update.message.reply_text.call_args
        self.assertIn("Unsupported File Format", args[0])

    async def test_unsupported_media_handler(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await unsupported_media_handler(update, context)

        update.message.reply_text.assert_awaited_once()
        args, _ = update.message.reply_text.call_args
        self.assertIn("Unsupported Media Type", args[0])

    async def test_expired_session_in_callback(self):
        dummy = os.path.join(TEMP_DIR, "expired_test.txt")
        with open(dummy, "w") as f:
            f.write("content")
        set_active_file(self.user_id, dummy, "expired_test.txt", ".txt")

        session = get_user_session(self.user_id)
        # Force expiration
        session.active_file.created_at = time.time() - 7200

        update = MagicMock()
        update.effective_user.id = self.user_id
        update.effective_chat.id = self.user_id
        update.callback_query.data = "action_translate"
        update.callback_query.answer = AsyncMock()
        context = MagicMock()

        await callback_query_handler(update, context)

        update.callback_query.answer.assert_awaited_once()
        self.assertIn("expired", update.callback_query.answer.call_args[0][0].lower())
        self.assertFalse(os.path.exists(dummy))

    async def test_safe_send_document_rate_limit_retry(self):
        chat = MagicMock()
        # First attempt raises RetryAfter(1), second attempt succeeds
        chat.send_document = AsyncMock(side_effect=[RetryAfter(1), True])

        test_file = os.path.join(TEMP_DIR, "test_retry_file.txt")
        with open(test_file, "w") as f:
            f.write("test")

        try:
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await safe_send_document_with_retry(
                    chat=chat,
                    document_path=test_file,
                    filename="test_retry_file.txt",
                )
                self.assertEqual(chat.send_document.await_count, 2)
                mock_sleep.assert_awaited_once_with(2)  # 1s + 1s buffer
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)


if __name__ == "__main__":
    unittest.main()

