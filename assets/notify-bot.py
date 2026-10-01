#!/usr/bin/env python3
"""notify-bot: multi-bot HTTP->Telegram bridge for the notify-agent skill v1.0.

Stdlib-only. Binds to 127.0.0.1:8765 by default, validates an X-API-Key header
(shared secret read from ~/.config/notify-agent/bot.env OR, as fallback, from
the first registered bot), then routes the incoming notification to the bot
whose PROJECT_KEY matches the payload's `project` field.

Per-bot isolation (v1.0):
  - each bot has its OWN decisions file: ~/.local/share/notify-agent/decisions-<project_key>.json
  - each bot has its OWN dedup dict (60s window per project/event/message)
  - each bot has its OWN daemon polling thread
  - each bot has its OWN FULL_MODE
  - /cancel, /list, /detail only see decisions for the bot that received the
    command (no cross-bot leaks)

Multi-bot registry:
  - server scans ~/.config/notify-agent/bots/*.env at startup
  - one env file = one bot; comma-separated PROJECT_KEY maps the same bot to
    multiple logical project keys

Endpoints:
  POST /notify   forward to Telegram; auth via X-API-Key; routes by `project`
  GET  /health   unauthenticated liveness probe + per-bot summary
  GET  /bots     unauthenticated list of registered bots (project_keys,
                 display_name, username, chat_id, full_mode, polling)

Legacy:
  v0.2's `bot.env` and `bot.toml` (single-bus-bot config) are IGNORED for
  bot_token / chat_id / projects. The legacy `bot.env` may still be read for
  the shared API_KEY (preferred). Legacy decisions.json is NOT loaded.
"""
import argparse
import hashlib
import http.server
import io
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"

BOTS_DIR_DEFAULT = os.path.expanduser("~/.config/notify-agent/bots")
LEGACY_ENV_PATH = os.path.expanduser("~/.config/notify-agent/bot.env")
LEGACY_TOML_PATH = os.path.expanduser("~/.config/notify-agent/bot.toml")  # noqa: F841 — documented, unused in v1.0
LOG_PATH = os.path.expanduser("~/.local/share/notify-agent/bot.log")
DECISIONS_DIR = os.path.expanduser("~/.local/share/notify-agent")

DEDUP_WINDOW_S = 60
DEDUP_MAX = 512
TELEGRAM_CHUNK = 4000  # safe margin under the 4096 char Telegram limit
DECISION_PRUNE_DAYS = 7
POLLER_TRANSIENT_BACKOFF_S = 2
POLLER_OTHER_BACKOFF_S = 5

CHAT_ID_PENDING = "PENDING_USER_START"  # marker for a not-yet-started 1:1 chat

EVENT_PREFIX = {
    "started": "\u25B6\uFE0F",
    "progress": "\u23F3",
    "blocked": "\U0001F6A8",
    "done": "\u2705",
    "failed": "\u274C",
    "cancelled": "\u23F9",
}

# Project emoji map. Keys are project_keys (logical ids used with
# --project); values are the emoji that prefixes the message so Sergio
# can identify the source of each push at a glance. Extend here when
# adding new OFELIA-unified projects. Bots that are NOT unified into
# OFELIA (e.g. Varian) keep their own chat identity.
PROJECT_EMOJI = {
    "ofelia": "\U0001F916",       # robot
    "ofelia-ui": "\U0001F3A8",    # artist palette
    "tareas": "\U0001F4CB",        # clipboard
    "bitacora": "\U0001F4D3",     # notebook
    "general": "\U0001F4AC",      # speech balloon (catch-all for non-project Q&A)
}

PROJECT_EMOJI_FALLBACK = "\U0001F4E6"  # incoming envelope (generic package)

FULL_MODE_NEVER = "never"
FULL_MODE_SUMMARY = "summary"
FULL_MODE_ALWAYS = "always"
FULL_MODE_FILE_ONLY = "file_only"
FULL_MODES = (FULL_MODE_NEVER, FULL_MODE_SUMMARY, FULL_MODE_ALWAYS, FULL_MODE_FILE_ONLY)

# Keys persisted to decisions-<project_key>.json (everything else stays in-memory only).
DURABLE_KEYS = (
    "project", "type", "options", "placeholder", "message",
    "chat_id", "message_id", "project_key",
    "created_at", "answered_at", "answered_value", "status",
)

logger = logging.getLogger("notify-bot")
start_ts = time.time()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def setup_logging():
    parent = os.path.dirname(LOG_PATH)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_env_file(path):
    """Tiny KEY=value env-style loader. Strips whitespace and matching quotes."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_topic_ids(raw):
    """Parse the TOPIC_IDS env value into a {project_key: message_thread_id} dict.

    Acceptable formats:
      ""                  → {} (no topic routing; falls back to General)
      "ofelia:4"          → {"ofelia": 4}
      "ofelia:4,ofelia-ui:4,brainstorm:6"
                          → {"ofelia": 4, "ofelia-ui": 4, "brainstorm": 6}

    Malformed entries (no colon, non-int value, empty key/value) are logged
    via the module-level logger and skipped. Whitespace around keys and
    values is stripped.

    Pure function (no I/O, no logger side-effects for valid input). Suitable
    for direct unit testing.
    """
    out = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            logger.warning("ignoring malformed TOPIC_IDS entry %r (missing ':')", part)
            continue
        k, v = part.split(":", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v:
            logger.warning("ignoring empty key/value in TOPIC_IDS entry %r", part)
            continue
        try:
            out[k] = int(v)
        except ValueError:
            logger.warning("TOPIC_IDS value %r for key %r is not an int; ignoring", v, k)
    return out


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Telegram API helpers (module-level so a Bot can call them with its own token)
# ---------------------------------------------------------------------------

def _tg_call_json(bot_token, method, payload_obj, timeout=10):
    """POST a JSON body to bot<token>/<method>. Returns (code, body_str)."""
    url = "https://api.telegram.org/bot%s/%s" % (bot_token, method)
    body = json.dumps(payload_obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8", "replace")
        except Exception:
            err = ""
        return e.code, err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, "transport: %s" % e


def _tg_extract_message_id(body_str):
    try:
        j = json.loads(body_str)
        return int(j.get("result", {}).get("message_id", 0) or 0)
    except Exception:
        return 0


def _parse_telegram_error(body_str):
    """Parse a Telegram error response body and return (error_code, retry_after).

    error_code is a short stable identifier (string) that callers can switch
    on, e.g. "rate_limited", "chat_not_found", "message_too_long",
    "bot_blocked", "forbidden". When the body has no parseable description,
    returns ("telegram_error", None).
    """
    if not body_str:
        return "telegram_error", None
    try:
        j = json.loads(body_str)
    except Exception:
        return "telegram_error", None
    if j.get("ok"):
        return None, None
    desc = (j.get("description") or "").lower()
    params = j.get("parameters") or {}
    retry = params.get("retry_after")
    if "too many requests" in desc or retry is not None:
        return "rate_limited", int(retry) if retry is not None else None
    if "chat not found" in desc:
        return "chat_not_found", None
    if "message is too long" in desc or "message text is too long" in desc:
        return "message_too_long", None
    if "message thread not found" in desc:
        return "thread_not_found", None
    if "bot was blocked" in desc or "bot was kicked" in desc:
        return "bot_blocked", None
    if "forbidden" in desc:
        return "forbidden", None
    if "bad request" in desc:
        return "bad_request", None
    return "telegram_error", None


def call_telegram_sendMessage(bot_token, chat_id, text, reply_markup=None,
                              parse_mode="HTML", disable_notification=False,
                              message_thread_id=None):
    """sendMessage wrapper. Returns (code, body_str).

    message_thread_id (optional): when set, Telegram posts the message into
    the corresponding forum topic of the chat. When None, the message lands
    in the chat's "General" (or the only chat for non-forum groups).

    parse_mode: passed to Telegram only when truthy. Telegram rejects an
    explicit `parse_mode: null` with 400 "unsupported parse_mode" — it
    expects either a valid mode string (HTML, MarkdownV2, Markdown) or
    the field absent.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": disable_notification,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)
    return _tg_call_json(bot_token, "sendMessage", payload)


def edit_message_text(bot_token, chat_id, message_id, text, reply_markup=None,
                      parse_mode="HTML"):
    """editMessageText. Returns (code, body_str, ok_bool)."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    code, body = _tg_call_json(bot_token, "editMessageText", payload)
    ok = False
    if 200 <= code < 300:
        try:
            j = json.loads(body)
            ok = bool(j.get("ok"))
        except Exception:
            ok = True  # 2xx with unparseable body → assume success
    return code, body, ok


def answer_callback(bot_token, callback_query_id, text=""):
    """answerCallbackQuery. Telegram requires it within 30s. Best-effort."""
    if not callback_query_id:
        return
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]  # Telegram cap
    payload["show_alert"] = False
    code, body = _tg_call_json(bot_token, "answerCallbackQuery", payload)
    if not (200 <= code < 300):
        logger.warning("answerCallbackQuery failed code=%s body=%s",
                       code, (body or "")[:200])


def send_document(bot_token, chat_id, filename, content_bytes):
    """sendDocument via multipart/form-data. Returns (code, body_str, message_id)."""
    boundary = "----notifybot%d" % os.getpid()
    buf = io.BytesIO()
    sep = ("--" + boundary + "\r\n").encode("utf-8")

    def field(name, value):
        buf.write(sep)
        buf.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8"))
        buf.write(str(value).encode("utf-8"))
        buf.write(b"\r\n")

    field("chat_id", chat_id)
    buf.write(sep)
    buf.write(('Content-Disposition: form-data; name="document"; filename="%s"\r\n' % filename).encode("utf-8"))
    buf.write(b"Content-Type: text/plain; charset=utf-8\r\n\r\n")
    buf.write(content_bytes)
    buf.write(b"\r\n")
    buf.write(("--" + boundary + "--\r\n").encode("utf-8"))

    url = "https://api.telegram.org/bot%s/sendDocument" % bot_token
    req = urllib.request.Request(
        url,
        data=buf.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body, _tg_extract_message_id(body)
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8", "replace")
        except Exception:
            err = ""
        return e.code, err, 0
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, "transport: %s" % e, 0


# ---------------------------------------------------------------------------
# Decision rendering (shared logic — Bot instances call into these helpers)
# ---------------------------------------------------------------------------

def render_decision_keyboard(decision_type, decision_id, options, selected=None):
    """Build the inline_keyboard dict (with key 'inline_keyboard') or None."""
    if decision_type == "text":
        return None
    if not options:
        return None
    if decision_type == "binary":
        if len(options) != 2:
            return None
        return {"inline_keyboard": [[
            {"text": str(options[0]), "callback_data": "dec:%s:%d" % (decision_id, 0)},
            {"text": str(options[1]), "callback_data": "dec:%s:%d" % (decision_id, 1)},
        ]]}
    if decision_type == "choice":
        rows = [
            [{"text": str(opt), "callback_data": "dec:%s:%d" % (decision_id, i)}]
            for i, opt in enumerate(options)
        ]
        return {"inline_keyboard": rows}
    if decision_type == "multi":
        rows = []
        sel = selected if selected is not None else set()
        for i, opt in enumerate(options):
            marker = "\u2611" if i in sel else "\u25AB"  # ☑ / ▫
            rows.append([{
                "text": "%s %s" % (marker, str(opt)),
                "callback_data": "dec:%s:toggle:%d" % (decision_id, i),
            }])
        k = len(sel)
        rows.append([
            {"text": "\u2705 Confirmar (%d)" % k,
             "callback_data": "dec:%s:done" % decision_id},
            {"text": "Cancelar",
             "callback_data": "dec:%s:cancel" % decision_id},
        ])
        return {"inline_keyboard": rows}
    return None


def _build_text_body(payload, kind_label):
    """Build the prefix + body for a notification message.

    Format: "<project_emoji> <project_key>: <message>".

    The project_emoji acts like an avatar (similar to a WhatsApp contact
    photo) so Sergio can identify the source at a glance. The project_key
    is included for explicit routing when the emoji isn't distinctive enough.
    No event_emoji, no [project] brackets, no metadata footer (source/ts).
    The agent is responsible for tone (see MANDATORY notify-agent emission
    contract in /home/deploy/.pi/agent/AGENTS.md and
    ~/.config/opencode/AGENTS.md): Telegram messages should read like a
    chat, not a log dashboard.
    """
    project = payload.get("project", "unknown")
    message = payload.get("message", "")
    project_emoji = PROJECT_EMOJI.get(project, PROJECT_EMOJI_FALLBACK)
    parts = ["%s %s: %s" % (project_emoji, project, message)]
    du = payload.get("detail_url") or ""
    if du:
        parts.append("\n\U0001F517 %s" % du)
    return "".join(parts)


def format_message(payload, decision=None):
    """Build (text, keyboard_or_None, disable_notification)."""
    event = payload.get("event", "")
    if event in ("blocked", "failed"):
        kind_label = event.upper()
    else:
        kind_label = event
    text = _build_text_body(payload, kind_label)
    keyboard = None
    if decision is not None:
        dtype = decision.get("type")
        did = decision.get("decision_id")
        opts = decision.get("options") or []
        if dtype == "text":
            placeholder = decision.get("placeholder") or ""
            hint = "\n\n\U0001F4AC Respondé con el texto en el próximo mensaje"
            if placeholder:
                hint += " (%s)" % placeholder
            text = text + hint
        else:
            selected = decision.get("selected") if dtype == "multi" else None
            keyboard = render_decision_keyboard(dtype, did, opts, selected)
    disable_notification = (event != "blocked")
    return text, keyboard, disable_notification


# ---------------------------------------------------------------------------
# Verbosity (full_message + full_mode)
# ---------------------------------------------------------------------------

def deliver_full_message(bot_token, chat_id, full_text, full_mode, label="",
                         message_thread_id=None):
    """Deliver the full_message according to the bot's full_mode.

    Returns (mode_effective, status_code). status_code is the HTTP code from
    the last Telegram call (0 if no Telegram call was made).

    message_thread_id (optional): when set, full-message chunks (or the
    attached .txt) are posted into the same forum topic as the original
    notification. When None, full-message delivery targets the chat's
    "General" (or the only chat for non-forum groups).
    """
    mode = full_mode if full_mode in FULL_MODES else FULL_MODE_NEVER
    if not full_text:
        return mode, 0
    if mode == FULL_MODE_NEVER or mode == FULL_MODE_SUMMARY:
        # summary reserved for a future version; treat as never for now
        return mode, 0
    if mode == FULL_MODE_ALWAYS:
        n = len(full_text)
        if n <= TELEGRAM_CHUNK:
            code, _ = call_telegram_sendMessage(bot_token, chat_id,
                                                "\U0001F4C4 Detalle:\n" + full_text,
                                                message_thread_id=message_thread_id)
            return mode, code
        total = (n + TELEGRAM_CHUNK - 1) // TELEGRAM_CHUNK
        last_code = 0
        for i in range(total):
            chunk = full_text[i * TELEGRAM_CHUNK:(i + 1) * TELEGRAM_CHUNK]
            header = "\U0001F4C4 Detalle (parte %d/%d):\n" % (i + 1, total)
            code, _ = call_telegram_sendMessage(bot_token, chat_id, header + chunk,
                                                message_thread_id=message_thread_id)
            last_code = code
        return mode, last_code
    if mode == FULL_MODE_FILE_ONLY:
        safe_label = label or "detail"
        filename = "%s.txt" % safe_label.replace(" ", "_")
        # sendDocument does not accept message_thread_id in the current Bot API
        # surface used by this server; document delivery targets the chat's
        # "General" regardless. For forum groups this is acceptable: the .txt
        # is a fallback for very long payloads and the original notification
        # already lives in the topic.
        code, _, _ = send_document(bot_token, chat_id, filename,
                                   full_text.encode("utf-8"))
        return mode, code
    return FULL_MODE_NEVER, 0


# ---------------------------------------------------------------------------
# Bot class — one Telegram bot, fully isolated state
# ---------------------------------------------------------------------------

class Bot:
    """A single Telegram bot, isolated from all other bots in the registry."""

    def __init__(self, env_path, env):
        self.env_path = env_path
        # The bot's filesystem name (e.g. "ofelia" from bots/ofelia.env).
        # Used as a stable id in logs, /health and /bots responses.
        self.name = os.path.basename(env_path).rsplit(".", 1)[0]

        # Required
        self.bot_token = env.get("BOT_TOKEN") or ""
        if not self.bot_token:
            raise ValueError("%s: BOT_TOKEN missing" % env_path)
        self.api_key = env.get("API_KEY") or ""  # may be empty; server-wide key wins
        # POLLING_ENABLED controls the long-poll loop. Default true. Set
        # to "false" (or 0/no/off) to disable the poller while keeping
        # the bot loaded for /dispatch. Useful when an external webhook
        # (e.g. ofelia-ui) is the sole handler for that bot's chat.
        self._polling_enabled_raw = (env.get("POLLING_ENABLED") or "true").strip().lower()

        # Optional / display
        self.bot_username = env.get("BOT_USERNAME") or ""
        self.display_name = env.get("DISPLAY_NAME") or self.name

        # PROJECT_KEY may be comma-separated → list. Always >= 1 entry
        # (we substitute the bot's filesystem name when absent).
        project_keys_raw = env.get("PROJECT_KEY") or self.name
        self.project_keys = [k.strip() for k in project_keys_raw.split(",") if k.strip()]
        if not self.project_keys:
            self.project_keys = [self.name]

        # Primary project_key used as the persistence-file slug.
        self.primary_project_key = self.project_keys[0]

        # TOPIC_IDS maps each project_key this bot owns to its
        # message_thread_id in a Telegram forum group. Format:
        #   TOPIC_IDS="ofelia:4,ofelia-ui:4,brainstorm:6"
        # Project keys NOT listed here fall back to "no message_thread_id"
        # (i.e. messages land in the chat's General topic). This makes the
        # feature backwards-compatible: a bot without TOPIC_IDS in its .env
        # continues to behave exactly like v1.0.
        self.topic_ids = parse_topic_ids(env.get("TOPIC_IDS") or "")
        # Warn if a project_key has no topic mapping (operator may have
        # forgotten one). Not an error: it just means that key delivers to
        # General.
        for pk in self.project_keys:
            if pk not in self.topic_ids:
                logger.info(
                    "bot=%s project_key=%r has no TOPIC_IDS entry; "
                    "notifications for it will land in the chat General topic",
                    self.name, pk,
                )

        # chat_id can be missing or be the marker CHAT_ID_PENDING.
        # In both cases we start polling but /notify returns 503 until we
        # capture a real chat_id from the first inbound update.
        chat_id_raw = (env.get("CHAT_ID") or "").strip()
        self.chat_id = chat_id_raw
        self._chat_id_resolved = bool(chat_id_raw) and chat_id_raw != CHAT_ID_PENDING

        # FULL_MODE — validated; unknown values fall back to "never".
        fm_raw = (env.get("FULL_MODE") or FULL_MODE_NEVER).strip() or FULL_MODE_NEVER
        if fm_raw not in FULL_MODES:
            logger.warning("unknown FULL_MODE %r for bot %r; falling back to %r",
                           fm_raw, self.name, FULL_MODE_NEVER)
            fm_raw = FULL_MODE_NEVER
        self.full_mode = fm_raw

        # Per-bot mutable state
        self._lock = threading.RLock()
        self.decisions = {}        # decision_id -> dict
        self._selected = {}        # decision_id -> set[int] (multi only)
        self.dedup = {}            # (project, event, msg_hash) -> ts

        # Poller
        self._stop_event = threading.Event()
        self.last_update_id = 0
        self.poller_thread = None
        self.polling_active = False  # reflects thread is alive
        self.last_poller_error = ""  # last transient error string

    # -- Properties -----------------------------------------------------------

    @property
    def decisions_path(self):
        """Per-bot persistence file: ~/.local/share/notify-agent/decisions-<project_key>.json"""
        return os.path.join(DECISIONS_DIR,
                            "decisions-%s.json" % self.primary_project_key)

    def pending_decision_count(self):
        with self._lock:
            return sum(1 for d in self.decisions.values()
                       if d.get("status") == "pending")

    # -- Persistence ----------------------------------------------------------

    def load_decisions(self):
        """Load per-bot decisions file (durable subset only). Mutates self.decisions."""
        path = self.decisions_path
        with self._lock:
            self.decisions.clear()
            self._selected.clear()
            if not os.path.isfile(path):
                return
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    obj = json.loads(fh.read())
            except (OSError, ValueError, json.JSONDecodeError) as e:
                logger.warning("could not read %s: %s", path, e)
                return
            decisions = obj.get("decisions") if isinstance(obj, dict) else None
            if not isinstance(decisions, dict):
                return
            cutoff = time.time() - DECISION_PRUNE_DAYS * 86400
            for did, rec in decisions.items():
                if not isinstance(rec, dict):
                    continue
                status = rec.get("status")
                answered_at = rec.get("answered_at")
                if status in ("answered", "cancelled") and answered_at:
                    try:
                        ts_epoch = time.mktime(time.strptime(
                            answered_at, "%Y-%m-%dT%H:%M:%SZ"))
                    except Exception:
                        ts_epoch = 0
                    if ts_epoch and ts_epoch < cutoff:
                        continue
                rec2 = dict(rec)
                opts = rec2.get("options") or []
                if not isinstance(opts, list):
                    opts = list(opts)
                rec2["options"] = [str(o) for o in opts]
                if rec2.get("type") == "multi":
                    rec2.setdefault("selected", set())
                else:
                    rec2["selected"] = None
                self.decisions[did] = rec2
                if rec2.get("type") == "multi":
                    sel = rec2.get("selected") or set()
                    # selected stored as set on reload; json can't hold a set
                    # so persistence round-trips via list (see save_decisions).
                    if isinstance(sel, list):
                        sel = set(int(x) for x in sel)
                        rec2["selected"] = sel
                    self._selected[did] = sel

    def save_decisions(self):
        """Atomically write the durable subset of self.decisions to disk."""
        path = self.decisions_path
        with self._lock:
            snapshot = {}
            for did, rec in self.decisions.items():
                clean = {k: rec.get(k) for k in DURABLE_KEYS}
                # selected (set) is not JSON-serializable; convert to list
                sel = rec.get("selected")
                if isinstance(sel, set):
                    clean["selected"] = sorted(sel)
                snapshot[did] = clean
            obj = {"version": 1, "project_key": self.primary_project_key,
                   "decisions": snapshot}
            _ensure_parent(path)
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(obj, fh, indent=2, sort_keys=True)
                os.replace(tmp, path)
            except OSError as e:
                logger.warning("could not write %s: %s", path, e)

    # -- Decision mutation -----------------------------------------------------

    def record_decision(self, decision_id, project, dtype, options, placeholder,
                        message, chat_id, message_id):
        rec = {
            "project": project,
            "type": dtype,
            "options": list(options) if options else [],
            "placeholder": placeholder or "",
            "message": message or "",
            "chat_id": chat_id,
            "message_id": int(message_id or 0),
            "project_key": self.primary_project_key,
            "created_at": _now_iso(),
            "answered_at": None,
            "answered_value": None,
            "status": "pending",
        }
        if dtype == "multi":
            rec["selected"] = set()
        with self._lock:
            self.decisions[decision_id] = rec
            if dtype == "multi":
                self._selected[decision_id] = set()
        self.save_decisions()
        return rec

    def update_decision(self, decision_id, **kwargs):
        """Update fields and persist. Sets are stored as sets in-memory and as
        sorted lists on disk via save_decisions()."""
        with self._lock:
            rec = self.decisions.get(decision_id)
            if not rec:
                return None
            for k, v in kwargs.items():
                if k == "selected" and v is not None:
                    sel = set(v)
                    rec["selected"] = sel
                    self._selected[decision_id] = sel
                elif k in DURABLE_KEYS:
                    rec[k] = v
        self.save_decisions()
        return rec

    # -- /notify entrypoint ----------------------------------------------------

    def handle_notify(self, payload):
        """Process one /notify payload. Returns a dict suitable for the HTTP
        response (with HTTP status code in `_http_status`)."""
        out = {"_http_status": 200}
        if not self._chat_id_resolved:
            out["_http_status"] = 503
            out["error"] = "chat_id not resolved (user has not /start'ed this bot yet)"
            return out
        if not self.bot_token:
            out["_http_status"] = 503
            out["error"] = "bot_token missing"
            return out

        project = payload.get("project", "?")
        event = payload.get("event", "?")

        # Dedup per-bot
        msg_hash = hashlib.sha1(payload["message"].encode("utf-8")).hexdigest()
        key3 = (project, event, msg_hash)
        now = time.time()
        with self._lock:
            prev = self.dedup.get(key3)
            if prev is not None and (now - prev) < DEDUP_WINDOW_S:
                logger.info("deduped bot=%s project=%s event=%s",
                            self.name, project, event)
                out["ok"] = True
                out["deduped"] = True
                return out
            self.dedup[key3] = now
            if len(self.dedup) > DEDUP_MAX:
                cutoff = now - DEDUP_WINDOW_S
                for k3 in list(self.dedup.keys()):
                    if self.dedup[k3] < cutoff:
                        self.dedup.pop(k3, None)

        # Decision handling
        decision_id = payload.get("decision_id")
        decision_type = payload.get("decision_type")
        decision = None
        if decision_id and decision_type:
            options_in = payload.get("decision_options") or []
            if isinstance(options_in, list):
                options_clean = [str(o) for o in options_in]
            else:
                options_clean = [str(options_in)]
            placeholder = payload.get("decision_placeholder") or ""
            decision = {
                "decision_id": str(decision_id),
                "type": str(decision_type),
                "options": options_clean,
                "placeholder": placeholder,
                "message": payload.get("message", ""),
            }
            if decision_type == "multi":
                decision["selected"] = set()
            text, keyboard, disable = format_message(payload, decision=decision)
        else:
            text, keyboard, disable = format_message(payload)

        # Resolve message_thread_id for this project's forum topic.
        # None → Telegram posts to the chat General (default v1.0 behavior).
        thread_id = self.topic_ids.get(project)

        # Send to Telegram
        code, body = call_telegram_sendMessage(
            self.bot_token, self.chat_id, text,
            reply_markup=keyboard, disable_notification=disable,
            message_thread_id=thread_id,
        )
        if not (200 <= code < 300):
            logger.error("telegram failed bot=%s project=%s event=%s code=%s body=%s",
                         self.name, project, event, code, (body or "")[:500])
            out["_http_status"] = 503
            out["error"] = "telegram"
            out["code"] = code
            return out

        mid = _tg_extract_message_id(body)
        logger.info("delivered bot=%s project=%s event=%s mid=%s decision=%s",
                    self.name, project, event, mid, decision_id or "-")

        # Persist decision now that we know the message_id
        if decision is not None:
            try:
                self.record_decision(
                    decision_id=str(decision_id),
                    project=project,
                    dtype=str(decision_type),
                    options=decision.get("options") or [],
                    placeholder=decision.get("placeholder") or "",
                    message=payload.get("message", ""),
                    chat_id=self.chat_id,
                    message_id=mid,
                )
            except Exception as e:
                logger.exception("could not record decision %s: %s", decision_id, e)

        # Verbosity: full_message
        full_message = payload.get("full_message")
        if full_message:
            try:
                mode, fcode = deliver_full_message(
                    self.bot_token, self.chat_id,
                    full_message, self.full_mode,
                    label=decision_id or project,
                    message_thread_id=thread_id,
                )
                if 200 <= fcode < 300 or fcode == 0:
                    logger.info("full_message delivered bot=%s mode=%s code=%s",
                                self.name, mode, fcode)
                else:
                    logger.warning("full_message failed bot=%s mode=%s code=%s",
                                   self.name, mode, fcode)
            except Exception as e:
                logger.exception("full_message dispatch crashed: %s", e)

        out["ok"] = True
        out["message_id"] = mid
        out["bot"] = self.name
        if decision is not None:
            out["decision_id"] = str(decision_id)
        return out

    # -- Render helpers (exposed for tests / inspection) -----------------------

    def render_decision(self, decision_type, decision_id, options, selected=None):
        """Render the inline_keyboard for a decision. Returns dict or None."""
        return render_decision_keyboard(decision_type, decision_id, options, selected)

    # -- Telegram outbound wrappers ------------------------------------------

    def send_message(self, text, reply_markup=None, parse_mode=None,
                     disable_notification=False, message_thread_id=None):
        return call_telegram_sendMessage(
            self.bot_token, self.chat_id, text,
            reply_markup=reply_markup, parse_mode=parse_mode,
            disable_notification=disable_notification,
            message_thread_id=message_thread_id,
        )

    def edit_message(self, message_id, text, reply_markup=None, parse_mode=None,
                     message_thread_id=None):
        """editMessageText. Telegram's editMessageText does NOT accept
        message_thread_id, but the chat_id is the same as the original
        message so the edit lands in the right topic automatically."""
        return edit_message_text(
            self.bot_token, self.chat_id, message_id, text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )

    def answer_callback(self, callback_query_id, text=""):
        return answer_callback(self.bot_token, callback_query_id, text)

    def send_photo(self, photo_url, caption=None, message_thread_id=None,
                   reply_markup=None):
        """sendPhoto via URL. Returns (code, body, message_id, error_code)."""
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
        }
        if caption is not None:
            payload["caption"] = caption[:1024]
        if message_thread_id is not None:
            payload["message_thread_id"] = int(message_thread_id)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        code, body = _tg_call_json(self.bot_token, "sendPhoto", payload)
        mid = _tg_extract_message_id(body)
        err_code, retry = _parse_telegram_error(body)
        return code, body, mid, err_code, retry

    def send_voice(self, voice_url, duration_seconds=None, caption=None,
                   message_thread_id=None, reply_markup=None):
        """sendVoice via URL. duration_seconds is optional. Returns the same
        tuple shape as send_photo."""
        payload = {
            "chat_id": self.chat_id,
            "voice": voice_url,
        }
        if duration_seconds is not None:
            payload["duration"] = int(duration_seconds)
        if caption is not None:
            payload["caption"] = caption[:1024]
        if message_thread_id is not None:
            payload["message_thread_id"] = int(message_thread_id)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        code, body = _tg_call_json(self.bot_token, "sendVoice", payload)
        mid = _tg_extract_message_id(body)
        err_code, retry = _parse_telegram_error(body)
        return code, body, mid, err_code, retry

    def send_document(self, document_url, filename=None, caption=None,
                      message_thread_id=None, reply_markup=None):
        """sendDocument via URL. Telegram does NOT accept message_thread_id
        in sendDocument in the current Bot API surface; the document lands
        in the chat General regardless (callers should warn their users)."""
        payload = {
            "chat_id": self.chat_id,
            "document": document_url,
        }
        if filename is not None:
            payload["filename"] = filename
        if caption is not None:
            payload["caption"] = caption[:1024]
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        code, body = _tg_call_json(self.bot_token, "sendDocument", payload)
        mid = _tg_extract_message_id(body)
        err_code, retry = _parse_telegram_error(body)
        return code, body, mid, err_code, retry

    # -- chat_id resolution ----------------------------------------------------

    def maybe_resolve_chat_id_from_update(self, upd):
        """If our chat_id is pending, capture it from the first message."""
        if self._chat_id_resolved:
            return False
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id", "")) if chat else ""
        if not cid:
            return False
        # In a private chat (1:1 with the bot) chat.type == "private". In a
        # group chat chat.type == "group"|"supergroup". We accept either,
        # but warn if a group message "resolves" the chat_id (it means the
        # user added the bot to a group, which is a deployment mistake).
        chat_type = chat.get("type", "")
        if chat_type in ("group", "supergroup"):
            logger.warning("bot %r chat_id resolved from a %s (id=%s) — "
                           "did you mean a 1:1 chat?",
                           self.name, chat_type, cid)
        self.chat_id = cid
        self._chat_id_resolved = True
        logger.info("resolved chat_id for bot %r -> %s", self.name, cid)
        # Persist so the chat_id survives restarts (no need to re-/start).
        self._persist_chat_id(cid)
        return True

    def _persist_chat_id(self, chat_id):
        """Write the resolved chat_id back to the bot's env file (atomic)."""
        path = self.env_path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            logger.warning("could not read %s to persist chat_id: %s", path, e)
            return
        new_lines = []
        found = False
        for line in lines:
            if line.lstrip().startswith("CHAT_ID="):
                new_lines.append("CHAT_ID=%s" % chat_id)
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append("CHAT_ID=%s" % chat_id)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(new_lines) + "\n")
            os.replace(tmp_path, path)
            logger.info("persisted chat_id %s to %s", chat_id, path)
        except OSError as e:
            logger.warning("could not persist chat_id to %s: %s", path, e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # -- Polling ---------------------------------------------------------------

    def start_poller(self):
        if not self.bot_token:
            logger.warning("bot %r: no BOT_TOKEN, skipping poller", self.name)
            return None
        # POLLING_ENABLED=false in the .env disables the long-poll loop for
        # this bot. The bot stays loaded for /dispatch and /health but
        # receives no inbound updates on its own — an external service
        # (e.g. ofelia-ui with a webhook) is expected to handle the chat
        # side and call /dispatch to send replies. Backward-compatible:
        # absent or any value other than the exact lowercase string "false"
        # (or "0", "no", "off") keeps the poller on.
        polling_flag_raw = getattr(self, "_polling_enabled_raw", "true")
        if polling_flag_raw in ("false", "0", "no", "off"):
            logger.info(
                "bot %r: POLLING_ENABLED=%s in .env — poller disabled "
                "(this bot only serves /dispatch; inbound updates must be "
                "handled by an external webhook)",
                self.name, polling_flag_raw,
            )
            return None
        self._stop_event.clear()
        t = threading.Thread(
            target=self._poller_loop,
            name="notify-bot-poller-%s" % self.name,
            daemon=True,
        )
        t.start()
        self.poller_thread = t
        self.polling_active = True
        logger.info("poller started for bot %r", self.name)
        return t

    def stop_poller(self):
        if self.poller_thread:
            self._stop_event.set()
            self.polling_active = False
            # Don't join — daemon threads die with the process.

    def _poller_loop(self):
        """Long-poll getUpdates, dispatch each update to _handle_update."""
        token = self.bot_token
        offset = self.last_update_id
        backoff = POLLER_TRANSIENT_BACKOFF_S
        while not self._stop_event.is_set():
            try:
                url = (
                    "https://api.telegram.org/bot%s/getUpdates"
                    "?offset=%d&timeout=20&allowed_updates=%s"
                    % (token, offset,
                       "%5B%22message%22%2C%22callback_query%22%5D")
                )
                req = urllib.request.Request(
                    url, headers={"User-Agent": "notify-bot/%s" % VERSION},
                )
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                for upd in data.get("result", []) or []:
                    if self._stop_event.is_set():
                        break
                    try:
                        new_offset = int(upd.get("update_id", 0)) + 1
                    except (TypeError, ValueError):
                        new_offset = offset
                    offset = max(offset, new_offset)
                    self.last_update_id = offset
                    try:
                        self._handle_update(upd)
                    except Exception as e:
                        logger.exception("update handler crashed for %s: %s",
                                         self.name, e)
                backoff = POLLER_TRANSIENT_BACKOFF_S  # reset on success
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self.last_poller_error = str(e)
                logger.warning("poller transient bot=%s: %s", self.name, e)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)  # capped exponential backoff
            except Exception as e:
                self.last_poller_error = str(e)
                logger.exception("poller unexpected bot=%s: %s", self.name, e)
                self._stop_event.wait(POLLER_OTHER_BACKOFF_S)

    # -- Update handlers -------------------------------------------------------

    def _send_simple(self, text):
        if not self.bot_token or not self._chat_id_resolved:
            return
        code, body = call_telegram_sendMessage(self.bot_token, self.chat_id, text)
        if not (200 <= code < 300):
            logger.warning("reply failed bot=%s code=%s body=%s",
                           self.name, code, (body or "")[:200])

    def _handle_callback(self, cb):
        data = cb.get("data") or ""
        if not data.startswith("dec:"):
            self.answer_callback(cb.get("id", ""), "")
            return
        parts = data.split(":")
        if len(parts) < 3:
            self.answer_callback(cb.get("id", ""), "")
            return
        decision_id = parts[1]
        rest = ":".join(parts[2:])

        with self._lock:
            rec = self.decisions.get(decision_id)

        if not rec or rec.get("status") != "pending":
            self.answer_callback(cb.get("id", ""), "Decisión ya respondida")
            return

        options = rec.get("options") or []
        dtype = rec.get("type")
        msg_chat_id = rec.get("chat_id")
        msg_id = rec.get("message_id")
        answered_value = None
        new_status = "answered"
        ack_text = ""

        if dtype == "binary" or dtype == "choice":
            try:
                idx = int(rest)
            except ValueError:
                self.answer_callback(cb.get("id", ""), "")
                return
            if not (0 <= idx < len(options)):
                self.answer_callback(cb.get("id", ""), "")
                return
            answered_value = str(options[idx])
            ack_text = answered_value
        elif dtype == "multi":
            if rest == "cancel":
                new_status = "cancelled"
                ack_text = "Cancelado"
            elif rest == "done":
                sel = rec.get("selected") or set()
                answered_value = ", ".join(str(options[i]) for i in sorted(sel))
                if not answered_value:
                    self.answer_callback(cb.get("id", ""), "Nada seleccionado")
                    return
                ack_text = "Confirmado"
            elif rest.startswith("toggle:"):
                try:
                    idx = int(rest.split(":", 1)[1])
                except ValueError:
                    self.answer_callback(cb.get("id", ""), "")
                    return
                if not (0 <= idx < len(options)):
                    self.answer_callback(cb.get("id", ""), "")
                    return
                sel = set(rec.get("selected") or set())
                if idx in sel:
                    sel.discard(idx)
                else:
                    sel.add(idx)
                self.update_decision(decision_id, selected=sel)
                with self._lock:
                    rec2 = self.decisions.get(decision_id) or {}
                keyboard = render_decision_keyboard(
                    dtype, decision_id, options,
                    rec2.get("selected") or set(),
                )
                if msg_chat_id and msg_id:
                    _, body = _tg_call_json(
                        self.bot_token, "editMessageReplyMarkup", {
                            "chat_id": msg_chat_id,
                            "message_id": msg_id,
                            "reply_markup": keyboard or {"inline_keyboard": []},
                        })
                    logger.info("multi refresh decision=%s body=%s",
                                decision_id, (body or "")[:120])
                self.answer_callback(cb.get("id", ""),
                                     "%d seleccionadas" % len(rec.get("selected") or set()))
                return
            else:
                self.answer_callback(cb.get("id", ""), "")
                return
        elif dtype == "text":
            self.answer_callback(cb.get("id", ""), "")
            return
        else:
            self.answer_callback(cb.get("id", ""), "")
            return

        self.answer_callback(cb.get("id", ""), ack_text)

        updated = self.update_decision(
            decision_id,
            status=new_status,
            answered_at=_now_iso(),
            answered_value=answered_value,
        )

        if msg_chat_id and msg_id and updated:
            original = updated.get("message") or ""
            if new_status == "cancelled":
                footer = "\n\n\U0001F4AC Cancelado @ %s" % updated.get("answered_at", "")
            else:
                v = answered_value or ""
                if len(v) > 80:
                    v = v[:77] + "..."
                footer = "\n\n\U0001F4AC Respondido: %s @ %s" % (
                    v, updated.get("answered_at", ""),
                )
            edit_text = "\U0001F4AC %s \u2014 %s%s" % (
                updated.get("type", ""), original, footer,
            )
            code, body, ok = edit_message_text(
                self.bot_token, msg_chat_id, msg_id, edit_text,
                reply_markup={"inline_keyboard": []},
            )
            if not ok:
                logger.info("editMessageText no-op/fail decision=%s code=%s body=%s",
                            decision_id, code, (body or "")[:200])
            else:
                logger.info("decision=%s resolved=%s value=%r",
                            decision_id, new_status, answered_value)

    def _handle_text_message(self, msg):
        """Text reply → resolve the most-recent pending `text` decision."""
        text = msg.get("text") or ""
        if not text:
            return
        # find the most recent pending text decision for this bot
        with self._lock:
            pending_text = [
                (did, rec) for did, rec in self.decisions.items()
                if rec.get("status") == "pending" and rec.get("type") == "text"
            ]
            pending_text.sort(key=lambda kv: kv[1].get("created_at") or "",
                              reverse=True)
            if not pending_text:
                return
            decision_id, rec = pending_text[0]
        updated = self.update_decision(
            decision_id,
            status="answered",
            answered_at=_now_iso(),
            answered_value=text,
        )
        if not updated:
            return
        msg_chat_id = updated.get("chat_id")
        msg_id = updated.get("message_id")
        if msg_chat_id and msg_id:
            v = text if len(text) <= 80 else text[:77] + "..."
            edit_text = "%s\n\n\U0001F4AC Respondido: %s @ %s" % (
                updated.get("message") or "", v,
                updated.get("answered_at") or "",
            )
            edit_message_text(
                self.bot_token, msg_chat_id, msg_id, edit_text,
                reply_markup={"inline_keyboard": []},
            )
        logger.info("decision=%s resolved=text len=%d", decision_id, len(text))

    def _handle_command(self, text):
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/list":
            with self._lock:
                items = list(self.decisions.items())
            pending = [(did, d) for did, d in items if d.get("status") == "pending"]
            answered = sorted(
                [(did, d) for did, d in items if d.get("status") != "pending"],
                key=lambda kv: kv[1].get("answered_at") or kv[1].get("created_at") or "",
                reverse=True,
            )[:5]
            lines = ["\U0001F4CB Decisiones (%d pendientes, %d recientes):" %
                     (len(pending), len(answered))]
            if pending:
                lines.append("")
                lines.append("Pendientes:")
                for did, d in pending[:10]:
                    opts = ",".join(d.get("options") or [])[:40]
                    lines.append("  \u2022 %s [%s] %s%s" % (
                        did, d.get("type") or "?",
                        (d.get("message") or "")[:60],
                        " (%s)" % opts if opts else "",
                    ))
            if answered:
                lines.append("")
                lines.append("Recientes:")
                for did, d in answered:
                    v = (d.get("answered_value") or "")
                    if len(v) > 40:
                        v = v[:37] + "..."
                    lines.append("  \u2022 %s %s = %s" % (
                        did, d.get("status") or "?", v,
                    ))
            if not pending and not answered:
                lines.append("\n(nada registrado)")
            self._send_simple("\n".join(lines))
            return

        if cmd == "/detail":
            if len(parts) < 2:
                self._send_simple("Uso: /detail <decision-id>")
                return
            did = parts[1]
            with self._lock:
                d = self.decisions.get(did)
            if not d:
                self._send_simple("Decisión %s no encontrada" % did)
                return
            opts = d.get("options") or []
            opts_line = ""
            if opts:
                opts_line = "\nOpciones: " + ", ".join(str(o) for o in opts)
            status = d.get("status") or "?"
            v = d.get("answered_value")
            answered_line = ""
            if status == "answered":
                answered_line = "\nRespondido: %s @ %s" % (v, d.get("answered_at"))
            elif status == "cancelled":
                answered_line = "\nCancelado @ %s" % (d.get("answered_at"))
            self._send_simple(
                "\U0001F4CB %s [%s]\n%s%s%s" % (
                    did, d.get("type") or "?", d.get("message") or "",
                    opts_line, answered_line,
                )
            )
            return

        if cmd == "/cancel":
            if len(parts) < 2:
                self._send_simple("Uso: /cancel <decision-id>")
                return
            did = parts[1]
            with self._lock:
                d = self.decisions.get(did)
            if not d:
                self._send_simple("Decisión %s no encontrada" % did)
                return
            if d.get("status") != "pending":
                self._send_simple("Decisión %s ya está %s" % (did, d.get("status")))
                return
            # Per-bot isolation: this decision is already in this bot's
            # registry, so no cross-bot check is needed. In a 1:1 chat the
            # operator can only reach decisions for this bot.
            self.update_decision(
                did, status="cancelled", answered_at=_now_iso(),
                answered_value=None,
            )
            msg_chat_id = d.get("chat_id")
            msg_id = d.get("message_id")
            if msg_chat_id and msg_id:
                edit_text = "%s\n\n\U0001F4AC Cancelado @ %s" % (
                    d.get("message") or "", _now_iso(),
                )
                edit_message_text(
                    self.bot_token, msg_chat_id, msg_id, edit_text,
                    reply_markup={"inline_keyboard": []},
                )
            self._send_simple("Cancelada %s" % did)
            return

        # Unknown command: ignore silently (don't spam the chat).

    def _handle_update(self, upd):
        """Dispatch one getUpdates update for this bot.

        - callback_query → resolve a decision
        - message (text) → either a command or a text-decision reply
        - chat_id not yet resolved → capture it from the first message
        """
        # Capture chat_id from any inbound message that has one
        self.maybe_resolve_chat_id_from_update(upd)

        if "callback_query" in upd:
            cb = upd["callback_query"]
            try:
                self._handle_callback(cb)
            except Exception as e:
                logger.exception("callback handler crashed for %s: %s",
                                 self.name, e)
            return

        if "message" in upd:
            msg = upd["message"]
            # Only accept messages from the resolved chat (1:1).
            # If chat_id is still pending, accept any message and use it
            # as the resolution event (handled by maybe_resolve_chat_id_from_update).
            chat = msg.get("chat", {})
            if self._chat_id_resolved:
                if str(chat.get("id")) != str(self.chat_id):
                    return
            text = msg.get("text", "") or ""
            if text.startswith("/"):
                try:
                    self._handle_command(text)
                except Exception as e:
                    logger.exception("command handler crashed for %s: %s",
                                     self.name, e)
            else:
                try:
                    self._handle_text_message(msg)
                except Exception as e:
                    logger.exception("text handler crashed for %s: %s",
                                     self.name, e)


# ---------------------------------------------------------------------------
# MultiBotServer — owns the registry + HTTP server + all pollers
# ---------------------------------------------------------------------------

class MultiBotServer:
    """Multi-bot HTTP->Telegram server. Scans BOTS_DIR on construction."""

    def __init__(self, bots_dir=None):
        self.bots = {}                  # bot_name -> Bot
        self.project_to_bot = {}        # project_key -> bot_name (1:1 in v1.0)
        self.api_key = ""
        self.dispatch_token = ""        # Bearer token for POST /dispatch (ofelia-ui)
        self._server = None
        self.start_ts = time.time()
        self.bots_dir = bots_dir or BOTS_DIR_DEFAULT

    def load_bots(self):
        """Discover bots/*.env, build registry, resolve API key.

        api_key priority:
          1. ~/.config/notify-agent/bot.env  (legacy file, preserved)
          2. First bot's API_KEY  (new v1.0 way)

        dispatch_token: read from env var NOTIFY_BOT_DISPATCH_TOKEN or from
        legacy bot.env. If empty, the /dispatch endpoint is disabled (returns
        503 to all callers).
        """
        # Re-resolve paths each call so HOME changes (tests, sudo contexts)
        # are picked up correctly.
        legacy_env_path = os.path.expanduser("~/.config/notify-agent/bot.env")
        legacy_env = {}
        if os.path.isfile(legacy_env_path):
            legacy_env = load_env_file(legacy_env_path)
        # Only API_KEY is taken from the legacy file in v1.0 — bot_token,
        # chat_id, [projects] are all ignored.
        legacy_api_key = legacy_env.get("API_KEY") or ""

        bots = []
        if os.path.isdir(self.bots_dir):
            for name in sorted(os.listdir(self.bots_dir)):
                if not name.endswith(".env"):
                    continue
                path = os.path.join(self.bots_dir, name)
                env = load_env_file(path)
                if not env.get("BOT_TOKEN"):
                    logger.warning("skipping %s: missing BOT_TOKEN", path)
                    continue
                try:
                    bot = Bot(path, env)
                except ValueError as e:
                    logger.warning("skipping %s: %s", path, e)
                    continue
                bots.append(bot)
        else:
            logger.warning("bots dir %s does not exist; server will start with 0 bots",
                           self.bots_dir)

        # api_key resolution
        if legacy_api_key:
            self.api_key = legacy_api_key
        elif bots:
            self.api_key = bots[0].api_key
            if not self.api_key:
                logger.warning("no API_KEY in legacy %s and bot %r has empty API_KEY",
                               LEGACY_ENV_PATH, bots[0].name)

        # dispatch_token resolution:
        #   1. env var NOTIFY_BOT_DISPATCH_TOKEN (preferred; never committed)
        #   2. legacy bot.env's DISPATCH_TOKEN (or TELEGRAM_DISPATCH_TOKEN) field
        self.dispatch_token = os.environ.get("NOTIFY_BOT_DISPATCH_TOKEN") or \
            legacy_env.get("DISPATCH_TOKEN") or \
            legacy_env.get("TELEGRAM_DISPATCH_TOKEN") or ""
        if not self.dispatch_token:
            logger.warning(
                "no dispatch_token configured — POST /dispatch will return 503. "
                "Set NOTIFY_BOT_DISPATCH_TOKEN or TELEGRAM_DISPATCH_TOKEN in %s.",
                LEGACY_ENV_PATH,
            )
        else:
            logger.info("dispatch_token configured (length=%d); POST /dispatch enabled",
                        len(self.dispatch_token))

        # Index
        for bot in bots:
            if bot.name in self.bots:
                logger.warning("duplicate bot name %r; ignoring %s",
                               bot.name, bot.env_path)
                continue
            self.bots[bot.name] = bot
            for pk in bot.project_keys:
                if pk in self.project_to_bot:
                    logger.warning(
                        "project_key %r already mapped to bot %r; "
                        "duplicate mapping in bot %r ignored (v1.0 is 1:1)",
                        pk, self.project_to_bot[pk], bot.name,
                    )
                else:
                    self.project_to_bot[pk] = bot.name

    def load_all_decisions(self):
        for bot in self.bots.values():
            try:
                bot.load_decisions()
            except Exception as e:
                logger.exception("load_decisions failed for %s: %s", bot.name, e)

    def find_bot(self, project_key):
        """Return the Bot that owns `project_key`, or None."""
        bot_name = self.project_to_bot.get(project_key)
        if not bot_name:
            return None
        return self.bots.get(bot_name)

    def find_bot_by_name(self, bot_name):
        """Return the Bot registered under `bot_name`, or None.

        Used by POST /dispatch where the caller names the bot explicitly
        (rather than via a project_key like POST /notify does)."""
        return self.bots.get(bot_name)

    def dispatch(self, payload):
        """Process one POST /dispatch payload from an upstream service
        (e.g. ofelia-ui calling this server to send a reply back to
        Telegram via the multi-bot registry).

        payload shape (see references/setup.md in the telegramNotifications
        repo for the full contract):

          {
            "bot":                 "<bot name in this server's registry>",
            "chat_id":             int (overrides the bot's default chat_id),
            "thread_id":           int or null (forum topic id),
            "content": {
              "type":              "text" | "edit" | "answer_callback"
                                   | "photo" | "voice" | "document",
              ...type-specific fields...
            },
            "reply_to_message_id": int (optional),
            "inline_keyboard":     {"rows": [[{...}, ...]]} (optional),
            "parse_mode":          "plain" | "html" (default "plain"),
            "metadata":            dict (optional, echoed back as-is)
          }

        Returns a dict ready to be JSON-encoded as the HTTP response:
          on success: {"ok": true, "telegram_message_id", "telegram_chat_id",
                       "telegram_thread_id", "metadata"}
          on failure: {"ok": false, "error_code", "retry_after_seconds"?,
                       "metadata"}

        Auth has already been verified by the caller (Handler._handle_dispatch).
        """
        meta = payload.get("metadata") or {}

        bot_name = payload.get("bot") or ""
        if not bot_name:
            return {"ok": False, "error_code": "missing_bot", "metadata": meta}
        bot = self.find_bot_by_name(bot_name)
        if bot is None:
            return {"ok": False, "error_code": "unknown_bot",
                    "bot": bot_name, "metadata": meta}

        target_chat_id = payload.get("chat_id") or bot.chat_id
        target_thread_id = payload.get("thread_id")

        content = payload.get("content") or {}
        ctype = content.get("type") or ""
        parse_mode = (payload.get("parse_mode") or "plain").lower()
        if parse_mode == "http v1":
            parse_mode = "html"
        elif parse_mode == "plain" or parse_mode == "":
            parse_mode = None

        reply_markup = None
        kb = payload.get("inline_keyboard")
        if isinstance(kb, dict) and kb.get("rows"):
            try:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": btn.get("text", ""),
                          "callback_data": btn.get("callback_data", "")}
                         for btn in row]
                        for row in kb["rows"]
                    ]
                }
            except Exception as e:
                logger.warning("malformed inline_keyboard, ignoring: %s", e)
                reply_markup = None

        # Temporarily swap chat_id for this dispatch call only. Restored in
        # finally so concurrent dispatchers don't clobber each other.
        original_chat_id = bot.chat_id
        bot.chat_id = str(target_chat_id)
        try:
            try:
                if ctype == "text":
                    text = content.get("text") or ""
                    if not text:
                        return {"ok": False, "error_code": "missing_text",
                                "metadata": meta}
                    code, body = bot.send_message(
                        text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                        message_thread_id=target_thread_id,
                    )
                    mid = _tg_extract_message_id(body)
                    if 200 <= code < 300:
                        return {"ok": True, "telegram_message_id": mid,
                                "telegram_chat_id": str(bot.chat_id),
                                "telegram_thread_id": target_thread_id,
                                "metadata": meta}
                    err, retry = _parse_telegram_error(body)
                    out = {"ok": False, "error_code": err or "telegram_error",
                           "metadata": meta}
                    if retry is not None:
                        out["retry_after_seconds"] = retry
                    return out

                if ctype == "edit":
                    mid_in = content.get("message_id")
                    text = content.get("text") or ""
                    if not mid_in or not text:
                        return {"ok": False,
                                "error_code": "missing_message_id_or_text",
                                "metadata": meta}
                    code, body, ok = edit_message_text(
                        bot.bot_token, bot.chat_id, int(mid_in), text,
                        reply_markup=reply_markup, parse_mode=parse_mode,
                    )
                    if ok:
                        return {"ok": True, "telegram_message_id": int(mid_in),
                                "telegram_chat_id": str(bot.chat_id),
                                "telegram_thread_id": target_thread_id,
                                "metadata": meta}
                    err, retry = _parse_telegram_error(body)
                    out = {"ok": False, "error_code": err or "telegram_error",
                           "metadata": meta}
                    if retry is not None:
                        out["retry_after_seconds"] = retry
                    return out

                if ctype == "answer_callback":
                    cb_id = content.get("callback_query_id") or ""
                    alert_text = content.get("text") or ""
                    show_alert = bool(content.get("show_alert", False))
                    if not cb_id:
                        return {"ok": False,
                                "error_code": "missing_callback_query_id",
                                "metadata": meta}
                    payload_cb = {"callback_query_id": cb_id}
                    if alert_text:
                        payload_cb["text"] = alert_text[:200]
                    payload_cb["show_alert"] = show_alert
                    code, body = _tg_call_json(
                        bot.bot_token, "answerCallbackQuery", payload_cb,
                    )
                    if 200 <= code < 300:
                        return {"ok": True, "telegram_message_id": 0,
                                "telegram_chat_id": str(bot.chat_id),
                                "telegram_thread_id": target_thread_id,
                                "metadata": meta}
                    err, retry = _parse_telegram_error(body)
                    out = {"ok": False, "error_code": err or "telegram_error",
                           "metadata": meta}
                    if retry is not None:
                        out["retry_after_seconds"] = retry
                    return out

                if ctype in ("photo", "voice", "document"):
                    return {"ok": False, "error_code": "unsupported_in_phase_0",
                            "requested_type": ctype, "metadata": meta}

                return {"ok": False, "error_code": "unknown_content_type",
                        "content_type": ctype, "metadata": meta}
            except Exception as e:
                logger.exception("dispatch crashed for bot=%s type=%s: %s",
                                 bot.name, ctype, e)
                return {"ok": False, "error_code": "internal_error",
                        "metadata": meta}
        finally:
            bot.chat_id = original_chat_id

    def health(self):
        bot_list = []
        for bot in self.bots.values():
            bot_list.append({
                "name": bot.name,
                "display_name": bot.display_name,
                "username": bot.bot_username,
                "project_keys": list(bot.project_keys),
                "primary_project_key": bot.primary_project_key,
                "chat_id": bot.chat_id,
                "chat_id_resolved": bot._chat_id_resolved,
                "full_mode": bot.full_mode,
                "pending_decisions": bot.pending_decision_count(),
                "polling": bot.polling_active,
                "last_update_id": bot.last_update_id,
            })
        return {
            "status": "ok",
            "version": VERSION,
            "uptime_s": int(time.time() - self.start_ts),
            "bots_loaded": len(self.bots),
            "projects_registered": len(self.project_to_bot),
            "bots": bot_list,
        }

    def bots_summary(self):
        return {
            "version": VERSION,
            "bots": [
                {
                    "name": bot.name,
                    "display_name": bot.display_name,
                    "username": bot.bot_username,
                    "project_keys": list(bot.project_keys),
                    "primary_project_key": bot.primary_project_key,
                    "chat_id": bot.chat_id,
                    "full_mode": bot.full_mode,
                }
                for bot in self.bots.values()
            ],
        }

    def start(self, host, port, allow_public, no_poll):
        if not self.api_key:
            sys.stderr.write("FAIL api_key missing — set API_KEY in %s "
                             "or in the first %s/*.env\n" %
                             (LEGACY_ENV_PATH, BOTS_DIR))
            sys.exit(2)
        if not allow_public and host not in ("127.0.0.1", "::1", "localhost"):
            sys.stderr.write(
                "FAIL refusing to bind non-loopback host %r without --allow-public\n"
                % host
            )
            sys.exit(2)

        for bot in self.bots.values():
            if not bot.full_mode:
                bot.full_mode = FULL_MODE_NEVER
            if not bot._chat_id_resolved:
                logger.warning(
                    "bot %r: chat_id is %r — /notify will return 503 until "
                    "user /start's the bot; poller will start anyway",
                    bot.name, bot.chat_id,
                )

        self.load_all_decisions()

        Handler.server_ref = self
        self._server = ThreadingHTTPServer((host, port), Handler)
        if not no_poll:
            for bot in self.bots.values():
                bot.start_poller()


# ---------------------------------------------------------------------------
# HTTP handler — routes /notify to the right Bot
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "notify-bot/%s" % VERSION
    server_ref = None  # injected at start() — MultiBotServer instance

    def log_message(self, fmt, *args):
        try:
            logger.info("%s - %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            try:
                logger.warning("could not write response: %s", e)
            except Exception:
                pass

    def do_GET(self):
        path = self.path.split("?")[0]
        server = self.server_ref
        if path == "/health":
            if server is None:
                self._send_json(503, {"status": "not_initialized"})
                return
            self._send_json(200, server.health())
            return
        if path == "/bots":
            if server is None:
                self._send_json(503, {"status": "not_initialized"})
                return
            self._send_json(200, server.bots_summary())
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        server = self.server_ref
        if path == "/dispatch":
            self._handle_dispatch(server)
            return
        if path != "/notify":
            self._send_json(404, {"error": "endpoint not found"})
            return
        if server is None:
            self._send_json(503, {"status": "not_initialized"})
            return

        expected_key = server.api_key or ""
        got_key = self.headers.get("X-API-Key", "")
        if (not expected_key or not got_key
                or len(got_key) != len(expected_key)
                or got_key != expected_key):
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            payload = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return

        for k in ("project", "event", "message"):
            if not payload.get(k):
                self._send_json(400, {"error": "missing field: %s" % k})
                return

        project = payload["project"]
        bot = server.find_bot(project)
        if bot is None:
            self._send_json(404, {"error": "unknown project: %s" % project})
            return

        result = bot.handle_notify(payload)
        status = int(result.pop("_http_status", 200))
        self._send_json(status, result)

    def _handle_dispatch(self, server):
        """Handle POST /dispatch — ofelia-ui calls this to send replies
        back to Telegram via the multi-bot registry."""
        if server is None:
            self._send_json(503, {"error": "server_not_initialized"})
            return

        expected = server.dispatch_token or ""
        auth = self.headers.get("Authorization", "")
        if not expected or not auth or auth != "Bearer %s" % expected:
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            payload = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be a JSON object"})
            return

        content = payload.get("content") or {}
        if not isinstance(content, dict) or not content.get("type"):
            self._send_json(400, {"error": "missing content.type"})
            return

        result = server.dispatch(payload)
        # dispatch() never raises and always returns a JSON-friendly dict.
        # Map failure codes to HTTP statuses (caller sees the raw error_code
        # in the body for fine-grained handling).
        if result.get("ok"):
            self._send_json(200, result)
            return
        err = result.get("error_code") or ""
        if err == "rate_limited":
            self._send_json(429, result)
        elif err in ("chat_not_found", "thread_not_found", "unknown_bot",
                     "missing_bot"):
            self._send_json(404, result)
        elif err in ("missing_text", "missing_message_id_or_text",
                     "missing_callback_query_id", "unknown_content_type",
                     "unsupported_in_phase_0", "missing_content_type",
                     "malformed_payload", "invalid_json"):
            self._send_json(400, result)
        elif err in ("forbidden", "bot_blocked"):
            self._send_json(403, result)
        else:
            self._send_json(502, result)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="notify-bot",
        description="Multi-bot HTTP->Telegram bridge for the notify-agent skill v1.0.",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="Bind port (default %d)" % DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="Bind host (default %s, must be loopback unless --allow-public)"
                    % DEFAULT_HOST)
    ap.add_argument("--allow-public", action="store_true",
                    help="Allow binding non-loopback addresses. DANGEROUS. Off by default.")
    ap.add_argument("--no-poll", action="store_true",
                    help="Skip starting the Telegram polling threads (test only).")
    ap.add_argument("--bots-dir", default=BOTS_DIR_DEFAULT,
                    help="Directory holding per-bot env files (default %s)"
                    % BOTS_DIR_DEFAULT)
    args = ap.parse_args()

    setup_logging()

    server = MultiBotServer(bots_dir=args.bots_dir)
    server.load_bots()
    server.start(args.host, args.port, args.allow_public, args.no_poll)

    stop_state = {"flag": False}

    def _stop(*_):
        if stop_state["flag"]:
            return
        stop_state["flag"] = True
        try:
            logger.info("shutting down")
        except Exception:
            pass
        try:
            for bot in server.bots.values():
                bot.stop_poller()
        except Exception:
            pass
        try:
            if server._server is not None:
                server._server.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    sys.stderr.write("notify-bot v%s listening on %s:%d (bots=%d, projects=%d, pid=%d)\n"
                     % (VERSION, args.host, args.port,
                        len(server.bots), len(server.project_to_bot),
                        os.getpid()))
    sys.stderr.flush()

    try:
        server._server.serve_forever()
    finally:
        try:
            server._server.server_close()
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
