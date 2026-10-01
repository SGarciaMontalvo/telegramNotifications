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


class TestDispatchAuth(unittest.TestCase):
    """POST /dispatch requires Bearer auth matching server.dispatch_token.

    The token is loaded from the env var NOTIFY_BOT_DISPATCH_TOKEN or from
    the legacy bot.env (DISPATCH_TOKEN / TELEGRAM_DISPATCH_TOKEN). We use a
    small stub server that doesn't need a token at all so we don't have to
    plumb env vars through the test runner.
    """

    DISPATCH_TOKEN = "test-dispatch-secret"

    def setUp(self):
        # Stub _tg_call_json so the bot can send messages without hitting
        # the Telegram API.
        self.tg_calls = []
        self._original_tg = notify_bot._tg_call_json

        def fake_tg_call(token, method, payload_obj, timeout=10):
            self.tg_calls.append({"token": token, "method": method,
                                  "payload": dict(payload_obj)})
            if method == "getUpdates":
                return 200, json.dumps({"ok": True, "result": []})
            return 200, json.dumps({"ok": True, "result": {
                "message_id": 100, "chat": {"id": -1001234567890},
            }})

        notify_bot._tg_call_json = fake_tg_call

        self.tmpdir = tempfile.mkdtemp(prefix="notify-bot-dispatch-")
        with open(os.path.join(self.tmpdir, "alexandria_ofelia.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN-dispatch\n"
                "PROJECT_KEY=ia_conversacional\n"
                "CHAT_ID=8742621415\n"
                "FULL_MODE=never\n"
                "API_KEY=test-shared-secret\n"
            )
        # shared bot.env: API_KEY + DISPATCH_TOKEN
        self.shared_env = os.path.join(
            os.path.expanduser("~"), ".config", "notify-agent", "bot.env",
        )
        os.makedirs(os.path.dirname(self.shared_env), exist_ok=True)
        self._had_shared = os.path.exists(self.shared_env)
        if self._had_shared:
            with open(self.shared_env) as fh:
                self._shared_contents = fh.read()
        with open(self.shared_env, "w") as fh:
            fh.write(
                "API_KEY=test-shared-secret\n"
                "TELEGRAM_DISPATCH_TOKEN=%s\n" % self.DISPATCH_TOKEN
            )

        # Boot server (no_poll=True; only the HTTP endpoint is exercised)
        self.port = 30000 + (os.getpid() % 5000)
        self.server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        self.server.load_bots()
        self.server.start("127.0.0.1", self.port, allow_public=False, no_poll=True)
        self._http_thread = threading.Thread(
            target=self.server._server.serve_forever,
            daemon=True, name="test-dispatch",
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

    def _post_dispatch(self, payload, auth=None):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = auth
        req = urllib.request.Request(
            "http://127.0.0.1:%d/dispatch" % self.port,
            data=body, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _last_sendmessage_payload(self):
        for call in reversed(self.tg_calls):
            if call["method"] == "sendMessage":
                return call["payload"]
        return None

    def test_missing_auth_returns_401(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "hi"},
        }, auth=None)
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error"), "unauthorized")

    def test_wrong_auth_returns_401(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "hi"},
        }, auth="Bearer wrong-token")
        self.assertEqual(status, 401)

    def test_correct_auth_text_delivers(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "hello from test"},
            "thread_id": None,
            "metadata": {"session_id": "abc"},
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("telegram_message_id"), 100)
        self.assertEqual(body.get("metadata", {}).get("session_id"), "abc")
        # Confirm the Telegram sendMessage call carried the right text.
        payload = self._last_sendmessage_payload()
        self.assertIn("hello from test", payload["text"])

    def test_unknown_bot_returns_404(self):
        status, body = self._post_dispatch({
            "bot": "nope",
            "content": {"type": "text", "text": "hi"},
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error_code"), "unknown_bot")

    def test_text_with_inline_keyboard(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "Confirma?"},
            "inline_keyboard": {
                "rows": [
                    [{"text": "S\u00ed", "callback_data": "confirm:yes"},
                     {"text": "No", "callback_data": "confirm:no"}],
                ]
            },
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 200)
        payload = self._last_sendmessage_payload()
        self.assertIn("reply_markup", payload)
        kb = payload["reply_markup"]
        self.assertEqual(len(kb["inline_keyboard"]), 1)
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)
        self.assertEqual(kb["inline_keyboard"][0][0]["text"], "S\u00ed")

    def test_edit_returns_ok(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "edit", "message_id": 50, "text": "edited"},
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("telegram_message_id"), 50)

    def test_answer_callback_returns_ok(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {
                "type": "answer_callback",
                "callback_query_id": "cb-123",
                "text": "OK",
                "show_alert": False,
            },
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 200)
        # Confirm the right Telegram API was called.
        methods = [c["method"] for c in self.tg_calls]
        self.assertIn("answerCallbackQuery", methods)

    def test_unsupported_content_type_returns_400(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "photo", "media_url": "https://x/y.jpg"},
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "unsupported_in_phase_0")

    def test_missing_content_type_returns_400(self):
        status, body = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"text": "no type"},
        }, auth="Bearer %s" % self.DISPATCH_TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "missing content.type")

    def test_invalid_json_returns_400(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/dispatch" % self.port,
            data=b"{not json",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % self.DISPATCH_TOKEN},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)


class TestDispatchToken(unittest.TestCase):
    """Server refuses to dispatch if no token is configured (returns 503)."""

    def setUp(self):
        self._original_tg = notify_bot._tg_call_json

        def fake_tg_call(token, method, payload_obj, timeout=10):
            return 200, json.dumps({"ok": True, "result": {"message_id": 1}})

        notify_bot._tg_call_json = fake_tg_call

        self.tmpdir = tempfile.mkdtemp(prefix="notify-bot-notoken-")
        with open(os.path.join(self.tmpdir, "alexandria_ofelia.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN\n"
                "PROJECT_KEY=ia_conversacional\n"
                "CHAT_ID=8742621415\n"
                "API_KEY=test-shared-secret\n"
            )
        # shared bot.env with NO dispatch token
        self.shared_env = os.path.join(
            os.path.expanduser("~"), ".config", "notify-agent", "bot.env",
        )
        os.makedirs(os.path.dirname(self.shared_env), exist_ok=True)
        self._had_shared = os.path.exists(self.shared_env)
        if self._had_shared:
            with open(self.shared_env) as fh:
                self._shared_contents = fh.read()
        with open(self.shared_env, "w") as fh:
            fh.write("API_KEY=test-shared-secret\n")

        self.server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        self.server.load_bots()
        # Don't start() — just check that dispatch_token is empty.

    def tearDown(self):
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

    def test_dispatch_token_empty_when_unset(self):
        self.assertEqual(self.server.dispatch_token, "")


# ---------------------------------------------------------------------------
# Telegram-error simulation. Same scaffolding as TestDispatchAuth, but with
# a programmable fake_tg_call that returns whatever the test wants.
# ---------------------------------------------------------------------------

class _ProgrammableTelegramServerFixture(unittest.TestCase):
    """Boots a notify-bot server pointing at a fake Telegram API whose
    responses are programmable per test (via `set_tg_response`)."""

    DISPATCH_TOKEN = "test-dispatch-secret"

    def setUp(self):
        self.tg_calls = []
        self._original_tg = notify_bot._tg_call_json

        # Default: a success response. Tests override per-method.
        self._tg_responses = {}

        def fake_tg_call(token, method, payload_obj, timeout=10):
            self.tg_calls.append({"token": token, "method": method,
                                  "payload": dict(payload_obj)})
            if method == "getUpdates":
                return 200, json.dumps({"ok": True, "result": []})
            # Default OK for the rest, overridable per-method.
            if method in self._tg_responses:
                return self._tg_responses[method]
            return 200, json.dumps({"ok": True, "result": {
                "message_id": 100, "chat": {"id": 8742621415},
            }})

        notify_bot._tg_call_json = fake_tg_call

        self.tmpdir = tempfile.mkdtemp(prefix="notify-bot-prog-")
        with open(os.path.join(self.tmpdir, "alexandria_ofelia.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN-dispatch-prog\n"
                "PROJECT_KEY=ia_conversacional\n"
                "CHAT_ID=8742621415\n"
                "FULL_MODE=never\n"
                "API_KEY=test-shared-secret\n"
            )
        self.shared_env = os.path.join(
            os.path.expanduser("~"), ".config", "notify-agent", "bot.env",
        )
        os.makedirs(os.path.dirname(self.shared_env), exist_ok=True)
        self._had_shared = os.path.exists(self.shared_env)
        if self._had_shared:
            with open(self.shared_env) as fh:
                self._shared_contents = fh.read()
        with open(self.shared_env, "w") as fh:
            fh.write(
                "API_KEY=test-shared-secret\n"
                "TELEGRAM_DISPATCH_TOKEN=%s\n" % self.DISPATCH_TOKEN
            )

        self.port = 31000 + (os.getpid() % 5000)
        self.server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        self.server.load_bots()
        self.server.start("127.0.0.1", self.port, allow_public=False, no_poll=True)
        self._http_thread = threading.Thread(
            target=self.server._server.serve_forever,
            daemon=True, name="test-prog",
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

    def set_tg_response(self, method, code, body):
        self._tg_responses[method] = (code, body)

    def _post_dispatch(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/dispatch" % self.port,
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % self.DISPATCH_TOKEN},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestDispatchRateLimit(_ProgrammableTelegramServerFixture):
    def test_telegram_429_passes_through_with_retry_after(self):
        # Telegram returns 429 with retry_after in the parameters block.
        body = json.dumps({
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 30",
            "parameters": {"retry_after": 30},
        })
        self.set_tg_response("sendMessage", 429, body)
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "spam"},
        })
        self.assertEqual(status, 429)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error_code"), "rate_limited")
        self.assertEqual(resp.get("retry_after_seconds"), 30)


class TestDispatchBotBlocked(_ProgrammableTelegramServerFixture):
    def test_bot_blocked_returns_403(self):
        body = json.dumps({
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot was blocked by the user",
        })
        self.set_tg_response("sendMessage", 403, body)
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "blocked"},
        })
        self.assertEqual(status, 403)
        self.assertEqual(resp.get("error_code"), "bot_blocked")


class TestDispatchChatNotFound(_ProgrammableTelegramServerFixture):
    def test_chat_not_found_returns_404(self):
        body = json.dumps({
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        })
        self.set_tg_response("sendMessage", 400, body)
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "where?"},
        })
        self.assertEqual(status, 404)
        self.assertEqual(resp.get("error_code"), "chat_not_found")


class TestDispatchMessageTooLong(_ProgrammableTelegramServerFixture):
    def test_message_too_long_returns_502(self):
        body = json.dumps({
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message text is too long",
        })
        self.set_tg_response("sendMessage", 400, body)
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "x" * 5000},
        })
        self.assertEqual(status, 502)
        self.assertEqual(resp.get("error_code"), "message_too_long")


class TestDispatchPayloadEdges(_ProgrammableTelegramServerFixture):
    def test_huge_text_payload(self):
        """Server should handle long text without crashing. Telegram's text
        limit is 4096 chars; we send 3000 to stay within bounds but still
        exercise the path."""
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "x" * 3000},
        })
        self.assertEqual(status, 200)
        self.assertEqual(resp.get("telegram_message_id"), 100)

    def test_metadata_echo(self):
        meta = {"session_id": "xyz", "origin": "ofelia", "k": "v"}
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "text", "text": "hi"},
            "metadata": meta,
        })
        self.assertEqual(status, 200)
        self.assertEqual(resp.get("metadata"), meta)

    def test_unknown_content_type_returns_400(self):
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "content": {"type": "video", "media_url": "..."},
        })
        self.assertEqual(status, 400)
        self.assertEqual(resp.get("error_code"), "unknown_content_type")

    def test_chat_id_override_uses_payload_chat(self):
        """If the caller specifies chat_id in the body, the dispatch goes
        to that chat_id instead of the bot's default chat_id."""
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "chat_id": -1001234567890,
            "content": {"type": "text", "text": "override"},
        })
        self.assertEqual(status, 200)
        # Check the captured Telegram call hit the override chat_id.
        send_payload = next(c["payload"] for c in self.tg_calls
                            if c["method"] == "sendMessage")
        self.assertEqual(str(send_payload["chat_id"]), "-1001234567890")
        self.assertEqual(resp.get("telegram_chat_id"), "-1001234567890")
        # And that the bot's stored chat_id was NOT mutated permanently.
        bot = self.server.bots["alexandria_ofelia"]
        self.assertEqual(bot.chat_id, "8742621415")

    def test_thread_id_passes_through(self):
        status, resp = self._post_dispatch({
            "bot": "alexandria_ofelia",
            "thread_id": 42,
            "content": {"type": "text", "text": "hi topic 42"},
        })
        self.assertEqual(status, 200)
        send_payload = next(c["payload"] for c in self.tg_calls
                            if c["method"] == "sendMessage")
        self.assertEqual(send_payload.get("message_thread_id"), 42)
        self.assertEqual(resp.get("telegram_thread_id"), 42)


class TestParseTelegramError(unittest.TestCase):
    """Unit tests for _parse_telegram_error covering all known shapes."""

    def test_rate_limited_from_retry_after_param(self):
        body = json.dumps({
            "ok": False,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 5},
        })
        code, retry = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "rate_limited")
        self.assertEqual(retry, 5)

    def test_rate_limited_from_description(self):
        body = json.dumps({
            "ok": False,
            "description": "too many requests right now",
        })
        code, retry = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "rate_limited")
        self.assertIsNone(retry)

    def test_chat_not_found(self):
        body = json.dumps({"ok": False,
                           "description": "Bad Request: chat not found"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "chat_not_found")

    def test_thread_not_found(self):
        body = json.dumps({"ok": False,
                           "description": "Bad Request: message thread not found"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "thread_not_found")

    def test_message_too_long(self):
        body = json.dumps({"ok": False,
                           "description": "Bad Request: message is too long"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "message_too_long")

    def test_bot_blocked(self):
        body = json.dumps({"ok": False,
                           "description": "Forbidden: bot was blocked by the user"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "bot_blocked")

    def test_bot_kicked(self):
        body = json.dumps({"ok": False,
                           "description": "Forbidden: bot was kicked from the group"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "bot_blocked")

    def test_generic_forbidden(self):
        body = json.dumps({"ok": False,
                           "description": "Forbidden: something else"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "forbidden")

    def test_bad_request_fallback(self):
        body = json.dumps({"ok": False,
                           "description": "Bad Request: some unrecognised thing"})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertEqual(code, "bad_request")

    def test_ok_body_returns_none(self):
        body = json.dumps({"ok": True, "result": {}})
        code, _ = notify_bot._parse_telegram_error(body)
        self.assertIsNone(code)

    def test_invalid_json_returns_telegram_error(self):
        code, retry = notify_bot._parse_telegram_error("not json")
        self.assertEqual(code, "telegram_error")
        self.assertIsNone(retry)

    def test_empty_body_returns_telegram_error(self):
        code, retry = notify_bot._parse_telegram_error("")
        self.assertEqual(code, "telegram_error")
        self.assertIsNone(retry)


class TestPollingEnabledFlag(unittest.TestCase):
    """POLLING_ENABLED=false in .env skips the poller but keeps the bot
    loadable (so /dispatch still works for that bot). Used when an
    external webhook (e.g. ofelia-ui) is the sole handler for that bot's
    chat and we still want this server to handle outbound dispatch."""

    def setUp(self):
        self._original_tg = notify_bot._tg_call_json

        def fake_tg_call(token, method, payload_obj, timeout=10):
            if method == "getUpdates":
                return 200, json.dumps({"ok": True, "result": []})
            return 200, json.dumps({"ok": True, "result": {"message_id": 1}})

        notify_bot._tg_call_json = fake_tg_call

        self.tmpdir = tempfile.mkdtemp(prefix="notify-bot-nopoll-")
        with open(os.path.join(self.tmpdir, "alexandria_ofelia.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN\n"
                "BOT_USERNAME=Ofelia_sgm_agent_bot\n"
                "PROJECT_KEY=ia_conversacional\n"
                "CHAT_ID=8742621415\n"
                "FULL_MODE=always\n"
                "POLLING_ENABLED=false\n"
                "API_KEY=test-shared-secret\n"
            )

    def tearDown(self):
        notify_bot._tg_call_json = self._original_tg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bot_loads_without_polling(self):
        server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        server.load_bots()
        self.assertIn("alexandria_ofelia", server.bots)
        bot = server.bots["alexandria_ofelia"]
        self.assertEqual(bot._polling_enabled_raw, "false")
        # start_poller returns None immediately (no thread spawned).
        self.assertIsNone(bot.start_poller())
        # poller_thread was never set.
        self.assertIsNone(bot.poller_thread)
        # polling_active stays False.
        self.assertFalse(bot.polling_active)

    def test_polling_enabled_default_is_true(self):
        # Same env without POLLING_ENABLED → bot has default "true".
        with open(os.path.join(self.tmpdir, "other-bot.env"), "w") as fh:
            fh.write(
                "BOT_TOKEN=TESTTOKEN-OTHER\n"
                "PROJECT_KEY=other\n"
                "CHAT_ID=8742621415\n"
                "API_KEY=test-shared-secret\n"
            )
        server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        server.load_bots()
        bot = server.bots["other-bot"]
        self.assertEqual(bot._polling_enabled_raw, "true")

    def test_polling_enabled_accepts_truthy_aliases(self):
        # 0 / no / off should also disable.
        for value in ("0", "no", "off", "FALSE", "False"):
            with open(os.path.join(self.tmpdir, f"bot-{value}.env"), "w") as fh:
                fh.write(
                    f"BOT_TOKEN=TESTTOKEN-{value}\n"
                    f"PROJECT_KEY=p-{value}\n"
                    f"CHAT_ID=8742621415\n"
                    f"POLLING_ENABLED={value}\n"
                    f"API_KEY=test-shared-secret\n"
                )
        server = notify_bot.MultiBotServer(bots_dir=self.tmpdir)
        server.load_bots()
        for value in ("0", "no", "off", "FALSE", "False"):
            bot_name = f"bot-{value}"
            self.assertIn(bot_name, server.bots)
            self.assertIsNone(server.bots[bot_name].start_poller())


if __name__ == "__main__":
    unittest.main(verbosity=2)
