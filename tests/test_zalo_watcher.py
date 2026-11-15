"""Unit tests for watcher/zalo_watcher.py (runs inside caronc/apprise or Python 3)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "watcher"))
import zalo_watcher as zw  # noqa: E402

TOKEN = "123456789:AbC_deF-12345"


class TestHelpers(unittest.TestCase):
    def test_parse_command_dm_and_group_mention(self):
        self.assertEqual(zw.parse_command("/subscribe mypass"), ("subscribe", "mypass"))
        self.assertEqual(zw.parse_command("@Bot Dockhand /subscribe mypass"), ("subscribe", "mypass"))
        self.assertEqual(zw.parse_command("@Bot Dockhand   /unsubscribe"), ("unsubscribe", ""))
        self.assertEqual(zw.parse_command("/STATUS"), ("status", ""))
        self.assertEqual(zw.parse_command("hello bot"), (None, ""))
        self.assertEqual(zw.parse_command(None), (None, ""))

    def test_redact_token(self):
        msg = f"Error calling https://bot-api.zaloplatforms.com/bot{TOKEN}/getUpdates"
        redacted = zw.redact_token(msg, TOKEN)
        self.assertNotIn(TOKEN, redacted)
        self.assertIn("***", redacted)

    def test_validation(self):
        self.assertTrue(zw.is_valid_token(TOKEN))
        self.assertFalse(zw.is_valid_token("<BOT_TOKEN>"))
        self.assertFalse(zw.is_valid_token("not-a-token"))
        self.assertTrue(zw.is_valid_chat_id("2bd5af23b7f0a4a8fde7"))
        self.assertFalse(zw.is_valid_chat_id("<CHAT_ID>"))
        self.assertFalse(zw.is_valid_chat_id("bad/chat"))


class TestConfigUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "dockhand.yml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_new_config_from_scratch(self):
        changed, chats = zw.update_dockhand_config(self.cfg, TOKEN, "chat_1", "add")
        self.assertTrue(changed)
        self.assertEqual(chats, ["chat_1"])
        data = zw.load_config_data(self.cfg)
        self.assertEqual(data["urls"], [f"zalo://{TOKEN}/chat_1"])

    def test_preserves_existing_chats_other_urls_and_tags_and_drops_placeholders(self):
        self.cfg.write_text(
            "urls:\n"
            "  - zalo://<BOT_TOKEN>/<CHAT_ID>\n"
            f"  - zalo://{TOKEN}/chat_1\n"
            f"  - zalo://{TOKEN}/ops_chat:\n"
            "      tag: ops\n"
            "  - discord://111/222\n",
            encoding="utf-8",
        )
        changed, chats = zw.update_dockhand_config(self.cfg, TOKEN, "chat_2", "add")
        self.assertTrue(changed)
        self.assertEqual(chats, ["chat_1", "chat_2"])

        data = zw.load_config_data(self.cfg)
        self.assertEqual(
            data["urls"],
            [
                f"zalo://{TOKEN}/chat_1/chat_2",
                {f"zalo://{TOKEN}/ops_chat": {"tag": "ops"}},
                "discord://111/222",
            ],
        )

    def test_deduplicates_existing_chat(self):
        self.cfg.write_text(f"urls:\n  - zalo://{TOKEN}/chat_1\n", encoding="utf-8")
        changed, chats = zw.update_dockhand_config(self.cfg, TOKEN, "chat_1", "add")
        self.assertFalse(changed)
        self.assertEqual(chats, ["chat_1"])

    def test_unsubscribe_last_chat_leaves_valid_empty_urls(self):
        self.cfg.write_text(f"urls:\n  - zalo://{TOKEN}/chat_1\n", encoding="utf-8")
        changed, chats = zw.update_dockhand_config(self.cfg, TOKEN, "chat_1", "remove")
        self.assertTrue(changed)
        self.assertEqual(chats, [])
        self.assertEqual(zw.load_config_data(self.cfg)["urls"], [])


class TestHandleUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "dockhand.yml"
        self.session = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_update(self, chat_id: str, text: str, chat_type: str = "GROUP", is_bot: bool = False):
        return {
            "event_name": "message.text.received",
            "message": {
                "message_id": "m1",
                "from": {"id": "u1", "display_name": "Alice", "is_bot": is_bot},
                "chat": {"id": chat_id, "chat_type": chat_type},
                "text": text,
            },
        }

    def test_ignores_bot_messages(self):
        res = zw.handle_update(
            self._make_update("c1", "/subscribe secret", is_bot=True),
            config_path=self.cfg,
            token=TOKEN,
            passcode="secret",
            auto_subscribe=True,
            send_welcome=False,
        )
        self.assertEqual(res, "ignored:is_bot")
        self.assertFalse(self.cfg.exists())

    def test_passcode_mode_rejects_bad_passcode_and_accepts_good_passcode(self):
        bad = zw.handle_update(
            self._make_update("g1", "@Bot Dockhand /subscribe wrong"),
            config_path=self.cfg,
            token=TOKEN,
            passcode="secret",
            auto_subscribe=False,
            send_welcome=False,
        )
        self.assertEqual(bad, "rejected:bad_passcode")
        self.assertFalse(self.cfg.exists())

        ok = zw.handle_update(
            self._make_update("g1", "@Bot Dockhand /subscribe secret"),
            config_path=self.cfg,
            token=TOKEN,
            passcode="secret",
            auto_subscribe=False,
            send_welcome=False,
        )
        self.assertEqual(ok, "subscribed")
        self.assertEqual(zw.get_subscribed_chats(self.cfg, TOKEN), ["g1"])

    def test_auto_subscribe_adds_any_chat_on_message_or_mention(self):
        res = zw.handle_update(
            self._make_update("g2", "@Bot Dockhand hi everyone!"),
            config_path=self.cfg,
            token=TOKEN,
            passcode="",
            auto_subscribe=True,
            send_welcome=False,
        )
        self.assertEqual(res, "subscribed")
        self.assertEqual(zw.get_subscribed_chats(self.cfg, TOKEN), ["g2"])

    def test_unconfigured_mode_fails_closed(self):
        res = zw.handle_update(
            self._make_update("c1", "/subscribe anything"),
            config_path=self.cfg,
            token=TOKEN,
            passcode="",
            auto_subscribe=False,
            send_welcome=False,
        )
        self.assertEqual(res, "rejected:unconfigured")
        self.assertFalse(self.cfg.exists())

    def test_call_zalo_api_handles_408_polling_timeout(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {"ok": False, "error_code": 408, "description": "Request Timeout"}
        sess = MagicMock()
        sess.post.return_value = resp
        with self.assertRaises(zw.ZaloPollingTimeout):
            zw.call_zalo_api(sess, TOKEN, "getUpdates", {"timeout": "25"})


if __name__ == "__main__":
    unittest.main()
