import os
os.environ["TESTING"] = "1"
import io
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut

import config
from services.backup import (
    send_to_backup_channel_silent,
    format_translation_backup_caption,
    format_split_backup_caption,
    format_text_backup_caption,
)
from handlers.commands import set_backup_group_handler, my_chat_member_handler, status_handler
from handlers.files import document_upload_handler, text_message_handler


class TestBackupService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_chat_id = config.get_backup_chat_id()
        config.set_backup_chat_id("")

    def tearDown(self):
        config.set_backup_chat_id(self.original_chat_id)

    async def test_send_backup_without_chat_id(self):
        config.set_backup_chat_id("")
        bot = MagicMock()
        bot.send_document = AsyncMock()

        res = await send_to_backup_channel_silent(
            bot=bot,
            document_path="nonexistent.txt",
            filename="test.txt",
            caption="test caption",
        )
        self.assertFalse(res)
        bot.send_document.assert_not_called()

    async def test_send_backup_with_document_path(self):
        config.set_backup_chat_id("-100987654321")
        test_file = os.path.join(config.TEMP_DIR, "test_backup_doc.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("Secret content to backup")

        try:
            bot = MagicMock()
            bot.send_document = AsyncMock()

            res = await send_to_backup_channel_silent(
                bot=bot,
                document_path=test_file,
                filename="test_backup_doc.txt",
                caption="📁 *Backup test*",
            )
            self.assertTrue(res)
            bot.send_document.assert_awaited_once()
            _, kwargs = bot.send_document.call_args
            self.assertEqual(kwargs["chat_id"], "-100987654321")
            self.assertEqual(kwargs["filename"], "test_backup_doc.txt")
            self.assertEqual(kwargs["caption"], "📁 *Backup test*")
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    async def test_send_backup_with_document_bytes(self):
        config.set_backup_chat_id("-100987654321")
        bio = io.BytesIO(b"In-memory document data")
        bio.name = "memory.txt"

        bot = MagicMock()
        bot.send_document = AsyncMock()

        res = await send_to_backup_channel_silent(
            bot=bot,
            document_bytes=bio,
            filename="memory.txt",
            caption="📄 *Memory doc*",
        )
        self.assertTrue(res)
        bot.send_document.assert_awaited_once()
        _, kwargs = bot.send_document.call_args
        self.assertEqual(kwargs["chat_id"], "-100987654321")
        self.assertEqual(kwargs["filename"], "memory.txt")

    async def test_send_backup_resilience_on_exception(self):
        config.set_backup_chat_id("-100987654321")
        test_file = os.path.join(config.TEMP_DIR, "test_resilient.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("Test error resilience")

        try:
            bot = MagicMock()
            # Bot raises BadRequest (e.g. Chat not found or permissions denied)
            bot.send_document = AsyncMock(side_effect=BadRequest("Chat not found"))

            # Must NOT raise exception; must return False cleanly
            res = await send_to_backup_channel_silent(
                bot=bot,
                document_path=test_file,
                filename="test_resilient.txt",
                caption="Test",
            )
            self.assertFalse(res)
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_caption_formatters(self):
        user = MagicMock()
        user.first_name = "Rahul"
        user.username = "rahul_dev"
        user.id = 123456

        # 1. Translation caption
        t_cap = format_translation_backup_caption("translated_doc.pdf", user, "Hindi")
        self.assertIn("translated_doc.pdf", t_cap)
        self.assertIn("Rahul", t_cap)
        self.assertIn("@rahul_dev", t_cap)
        self.assertIn("123456", t_cap)
        self.assertIn("Hindi", t_cap)

        # 2. Split caption
        s_cap = format_split_backup_caption("book.epub", "part_1.epub", 1, 5, user)
        self.assertIn("book.epub", s_cap)
        self.assertIn("part_1.epub", s_cap)
        self.assertIn("1/5", s_cap)
        self.assertIn("Rahul", s_cap)

        # 3. Text caption
        txt_cap = format_text_backup_caption(5200, user, "Spanish")
        self.assertIn("5,200", txt_cap)
        self.assertIn("Spanish", txt_cap)
        self.assertIn("Rahul", txt_cap)

    def test_save_backup_chat_id_to_env(self):
        env_test_path = config.TEMP_DIR / "test.env"
        with open(env_test_path, "w", encoding="utf-8") as f:
            f.write("TELEGRAM_BOT_TOKEN=dummy\nBACKUP_CHAT_ID=\n")

        try:
            config.save_backup_chat_id_to_env("-10011223344", env_path=env_test_path)
            self.assertEqual(config.get_backup_chat_id(), "-10011223344")

            with open(env_test_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("BACKUP_CHAT_ID=-10011223344", content)
        finally:
            if env_test_path.exists():
                env_test_path.unlink()


class TestBackupHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_set_backup_group_in_supergroup(self):
        update = MagicMock()
        update.effective_chat.id = -10055667788
        update.effective_chat.title = "Secret Archive Group"
        update.effective_chat.type = "supergroup"
        update.message.delete = AsyncMock()
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()

        with patch("handlers.commands.save_backup_chat_id_to_env") as mock_save:
            with patch("handlers.commands.asyncio.sleep", new_callable=AsyncMock):
                await set_backup_group_handler(update, context)

            mock_save.assert_called_once_with("-10055667788")
            update.message.delete.assert_awaited_once()

    async def test_my_chat_member_handler_auto_registers(self):
        update = MagicMock()
        update.effective_chat.id = -10099887766
        update.effective_chat.title = "Target Group"
        update.effective_chat.type = "supergroup"
        update.my_chat_member.new_chat_member.status = "administrator"
        context = MagicMock()

        with patch("handlers.commands.save_backup_chat_id_to_env") as mock_save:
            await my_chat_member_handler(update, context)
            mock_save.assert_called_once_with("-10099887766")

    async def test_group_messages_ignored_for_user_privacy(self):
        # 1. Document upload in group should be ignored
        update = MagicMock()
        update.effective_chat.type = "supergroup"
        update.message.document = MagicMock()
        context = MagicMock()

        await document_upload_handler(update, context)
        # Should return early without trying to download
        update.message.reply_text.assert_not_called()

        # 2. Text message in group should be ignored
        update_text = MagicMock()
        update_text.effective_chat.type = "supergroup"
        update_text.message.text = "Hello in secret group"
        context_text = MagicMock()

        await text_message_handler(update_text, context_text)
        # Should return early without replying
        update_text.message.reply_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
