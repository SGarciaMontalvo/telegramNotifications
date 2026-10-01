#!/usr/bin/env python3
"""Tests for notify-bot.py changes that support forum-group topics.

Run with: python3 test_notify_bot.py
Stdlib-only (unittest). No external deps.

Covers:
  - parse_topic_ids(): pure function that parses TOPIC_IDS env values
  - call_telegram_sendMessage(): omits message_thread_id when None
  - end-to-end /notify: routes to the right topic per project_key
  - backwards compatibility: a bot without TOPIC_IDS behaves like v1.0
"""
import json
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# notify-bot.py has a dash in its filename, so Python's normal import
# machinery won't load it as `notify_bot`. Use importlib instead.
_spec = importlib.util.spec_from_file_location(
    "notify_bot", os.path.join(HERE, "notify-bot.py"),
)
notify_bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify_bot)


# ---------------------------------------------------------------------------
# Unit tests — parse_topic_ids
# ---------------------------------------------------------------------------

class TestParseTopicIds(unittest.TestCase):
    def test_empty_returns_empty_dict(self):
        self.assertEqual(notify_bot.parse_topic_ids(""), {})

    def test_none_returns_empty_dict(self):
        self.assertEqual(notify_bot.parse_topic_ids(None), {})

    def test_single_entry(self):
        self.assertEqual(notify_bot.parse_topic_ids("ofelia:4"), {"ofelia": 4})

    def test_multiple_entries(self):
        self.assertEqual(
            notify_bot.parse_topic_ids("ofelia:4,ofelia-ui:4,brainstorm:6"),
            {"ofelia": 4, "ofelia-ui": 4, "brainstorm": 6},
        )

    def test_whitespace_is_stripped(self):
        self.assertEqual(
            notify_bot.parse_topic_ids(" ofelia : 4 , brainstorm : 6 "),
            {"ofelia": 4, "brainstorm": 6},
        )

    def test_malformed_no_colon_is_skipped(self):
        with self.assertLogs("notify-bot", level="WARNING"):
            result = notify_bot.parse_topic_ids("ofelia:4,malformed,brainstorm:6")
        self.assertEqual(result, {"ofelia": 4, "brainstorm": 6})

    def test_non_int_value_is_skipped(self):
        with self.assertLogs("notify-bot", level="WARNING"):
            result = notify_bot.parse_topic_ids("ofelia:abc,brainstorm:6")
        self.assertEqual(result, {"brainstorm": 6})

    def test_empty_key_is_skipped(self):
        with self.assertLogs("notify-bot", level="WARNING"):
            result = notify_bot.parse_topic_ids(":5,ofelia:4")
        self.assertEqual(result, {"ofelia": 4})

    def test_empty_value_is_skipped(self):
        with self.assertLogs("notify-bot", level="WARNING"):
            result = notify_bot.parse_topic_ids("ofelia:,brainstorm:6")
        self.assertEqual(result, {"brainstorm": 6})

    def test_extra_commas_ignored(self):
        self.assertEqual(
            notify_bot.parse_topic_ids("ofelia:4,,brainstorm:6,"),
            {"ofelia": 4, "brainstorm": 6},
        )


# ---------------------------------------------------------------------------
# Unit tests — call_telegram_sendMessage payload shape
# ---------------------------------------------------------------------------

class TestSendMessagePayloadShape(unittest.TestCase):
    """Verify call_telegram_sendMessage adds message_thread_id to the payload
    only when it's not None (backwards-compatible)."""

    def setUp(self):
        self.captured = {}

        def fake_call(token, method, payload_obj, timeout=10):
            self.captured["token"] = token
            self.captured["method"] = method
            self.captured["payload"] = dict(payload_obj)
            return 200, json.dumps({"ok": True, "result": {"message_id": 1}})

        self._original = notify_bot._tg_call_json
        notify_bot._tg_call_json = fake_call

    def tearDown(self):
        notify_bot._tg_call_json = self._original

    def test_none_thread_id_not_in_payload(self):
        notify_bot.call_telegram_sendMessage(
            "TOKEN", "123", "hello",
            message_thread_id=None,
        )
        self.assertNotIn("message_thread_id", self.captured["payload"])

    def test_int_thread_id_in_payload(self):
        notify_bot.call_telegram_sendMessage(
            "TOKEN", "123", "hello",
            message_thread_id=4,
        )
        self.assertEqual(self.captured["payload"]["message_thread_id"], 4)

    def test_string_int_thread_id_coerced(self):
        # Defensive: if a caller passes "4" as a string, normalize to int.
        notify_bot.call_telegram_sendMessage(
            "TOKEN", "123", "hello",
            message_thread_id="4",
        )
        self.assertEqual(self.captured["payload"]["message_thread_id"], 4)
        self.assertIsInstance(self.captured["payload"]["message_thread_id"], int)


# ---------------------------------------------------------------------------
# Shared scaffolding for integration tests
# ---------------------------------------------------------------------------

class _ServerFixture(unittest.TestCase):
    """Base class that boots a real MultiBotServer on an ephemeral port,
    pointing at a temp bots dir, with _tg_call_json stubbed to capture
    payloads instead of calling Telegram."""

    SHARED_SECRET = "test-shared-secret"

    def setUp(self):
        self.tg_calls = []
        self._original_tg = notify_bot._tg_call_json

        def fake_tg_call(token, method, payload_obj, timeout=10):
            self.tg_calls.append({"token": token, "method": method,
                                  "payload": dict(payload_obj)})
            # Special-case getUpdates → return empty result list
            if method == "getUpdates":
                return 200, json.dumps({"ok": True, "result": []})
            # Default: a successful sendMessage/editMessageText/etc.
            return 200, json.dumps({"ok": True, "result": {
                "message_id": 99, "chat": {"id": -1004320132331},
            }})

        notify_bot._tg_call_json = fake_tg_call

        # Temp bots dir
        self.tmpdir = tempfile.mkdtemp(prefix="notify-bot-test-")
        self._write_bot_envs()

        # Override shared bot.env (legacy file with API_KEY)
        self.shared_env = os.path.join(
            os.path.expanduser("~"), ".config", "notify-agent", "bot.env",
        )
        os.makedirs(os.path.dirname(self.shared_env), exist_ok=True)
        self._had_shared = os.path.exists(self.shared_env)
        if self._had_shared:
            with open(self.shared_env) as fh:
                self._shared_contents = fh.read()
        with open(self.shared_env, "w") as fh:
            fh.write("API_KEY=%s\n" % self.SHARED_SECRET)

        # Boot server
        self.port = 28000 + (os.getpid() % 5000)
        self.server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        self.server.load_bots()
        self.server.start("127.0.0.1", self.port, allow_public=False, no_poll=True)
        self._http_thread = threading.Thread(
            target=self.server._server.serve_forever,
            daemon=True, name="test-server",
        )
        self._http_thread.start()
        time.sleep(0.2)

    def tearDown(self):
        try:
            self.server._server.shutdown()
        except Exception:
            pass
        try:
            self.server._server.server_close()
        except Exception:
            pass
        notify_bot._tg_call_json = self._original_tg
        if self._had_shared:
            with open(self.shared_env, "w") as fh:
                fh.write(self._shared_contents)
        else:
            try:
                os.unlink(self.shared_env)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_bot_envs(self):
        raise NotImplementedError

    def _post_notify(self, project, event="done", message="hello"):
        payload = json.dumps({
            "project": project, "event": event, "message": message,
            "severity": "normal",
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/notify" % self.port,
            data=payload,
            headers={"Content-Type": "application/json",
                     "X-API-Key": self.SHARED_SECRET},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def _last_sendmessage_payload(self):
        for call in reversed(self.tg_calls):
            if call["method"] == "sendMessage":
                return call["payload"]
        return None


class TestForumGroupRouting(_ServerFixture):
    """Bot with TOPIC_IDS routes to the right topic per project_key."""

    def _write_bot_envs(self):
        with open(os.path.join(self.tmpdir, "ofelia-vps.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN-ofelia-vps\n"
                "BOT_USERNAME=ofelia_vps_test\n"
                "DISPLAY_NAME=Test Ofelia\n"
                "PROJECT_KEY=ofelia,ofelia-ui\n"
                "CHAT_ID=-1004320132331\n"
                "FULL_MODE=never\n"
                "TOPIC_IDS=ofelia:4,ofelia-ui:4\n"
                "API_KEY=test-shared-secret\n"
            )
        # General bot intentionally has no TOPIC_IDS (its messages go to General)
        with open(os.path.join(self.tmpdir, "general-vps.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN-general-vps\n"
                "BOT_USERNAME=general_vps_test\n"
                "DISPLAY_NAME=Test General\n"
                "PROJECT_KEY=general\n"
                "CHAT_ID=-1004320132331\n"
                "FULL_MODE=never\n"
                # no TOPIC_IDS → messages fall back to General
                "API_KEY=test-shared-secret\n"
            )

    def test_ofelia_project_uses_thread_id_4(self):
        status, body = self._post_notify("ofelia", message="test ofelia")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        payload = self._last_sendmessage_payload()
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("message_thread_id"), 4)

    def test_ofelia_ui_uses_thread_id_4(self):
        status, body = self._post_notify("ofelia-ui", message="test ofelia-ui")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        payload = self._last_sendmessage_payload()
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("message_thread_id"), 4)

    def test_general_no_thread_id(self):
        status, body = self._post_notify("general", message="test general")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        payload = self._last_sendmessage_payload()
        self.assertIsNotNone(payload)
        self.assertNotIn("message_thread_id", payload)

    def test_unknown_project_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post_notify("brainstorm", message="x")
        self.assertEqual(cm.exception.code, 404)


class TestLegacyBotBackwardCompat(_ServerFixture):
    """A bot without TOPIC_IDS behaves exactly like v1.0."""

    def _write_bot_envs(self):
        with open(os.path.join(self.tmpdir, "legacy-bot.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=LEGACY-TOKEN\n"
                "PROJECT_KEY=legacy\n"
                "CHAT_ID=8742621415\n"
                "FULL_MODE=never\n"
                "API_KEY=test-shared-secret\n"
            )

    def test_no_thread_id_in_payload(self):
        status, _ = self._post_notify("legacy", message="hi")
        self.assertEqual(status, 200)
        payload = self._last_sendmessage_payload()
        self.assertIsNotNone(payload)
        self.assertNotIn("message_thread_id", payload)


class TestFullMessageRespectsTopic(_ServerFixture):
    """When --full is used with FULL_MODE=always, the chunked follow-up
    messages also carry the same message_thread_id."""

    def _write_bot_envs(self):
        with open(os.path.join(self.tmpdir, "ofelia-vps.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN-ofelia-vps\n"
                "PROJECT_KEY=ofelia\n"
                "CHAT_ID=-1004320132331\n"
                "FULL_MODE=always\n"
                "TOPIC_IDS=ofelia:4\n"
                "API_KEY=test-shared-secret\n"
            )

    def _post_notify_with_full(self, full_text):
        payload = json.dumps({
            "project": "ofelia", "event": "done",
            "message": "short",
            "severity": "normal",
            "full_message": full_text,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/notify" % self.port,
            data=payload,
            headers={"Content-Type": "application/json",
                     "X-API-Key": self.SHARED_SECRET},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status

    def test_full_message_chunks_have_thread_id(self):
        long_text = "x" * 9000  # forces multiple chunks
        self._post_notify_with_full(long_text)
        send_payloads = [c["payload"] for c in self.tg_calls
                         if c["method"] == "sendMessage"]
        # We expect at least: the original notification + the chunks
        self.assertGreaterEqual(len(send_payloads), 3)
        for p in send_payloads:
            self.assertEqual(p.get("message_thread_id"), 4,
                             "every sendMessage must carry message_thread_id=4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
