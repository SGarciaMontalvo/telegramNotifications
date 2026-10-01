# POST /dispatch — Contract

`POST /dispatch` is the endpoint an upstream service (e.g. ofelia-ui) calls
to send messages, edits and callback-query answers back to Telegram through
the multi-bot registry in `notify-bot.py`.

This document is the source of truth for the wire protocol. Changes to
the contract go in `references/dispatch-contract.md` first, then the
implementation. The contract is **versioned implicitly** by the
`Contract-Version` field in `references/dispatch-contract.md` (v1.0 for
this revision).

## Endpoint

| Property | Value |
|---|---|
| URL | `POST /dispatch` |
| Default bind | `http://127.0.0.1:8765/dispatch` (loopback only) |
| Auth | `Authorization: Bearer <TELEGRAM_DISPATCH_TOKEN>` (header) |
| Content-Type | `application/json` |
| Body | JSON, ≤ 50 KB |
| Idempotency | None (caller is expected to handle retries itself) |

## Authentication

```
Authorization: Bearer d2676371001ae71c8225f675871f493357f26139bc74a2f323fd44ab8feed6ac
```

The server loads the token at startup from (in order):

1. Environment variable `NOTIFY_BOT_DISPATCH_TOKEN`.
2. The legacy `~/.config/notify-agent/bot.env` field `DISPATCH_TOKEN`
   (or `TELEGRAM_DISPATCH_TOKEN` — same thing).

If no token is configured, `POST /dispatch` returns **503** with body
`{"error": "server_not_initialized"}` (or, when the server is up but
not yet initialized, `{"status": "not_initialized"}`). The token is a
shared secret; rotate by changing the env var and restarting the server.

## Request body

```json
{
  "bot":                 "<required: bot name in notify-bot's registry>",
  "chat_id":             "<optional: int or string, defaults to bot.chat_id>",
  "thread_id":           "<optional: int, forum topic id (omit for non-forum)>",
  "content": {
    "type":              "<required: text|edit|answer_callback|photo|voice|document>",
    "...":               "<type-specific, see Content types below>"
  },
  "reply_to_message_id": "<optional: int>",
  "inline_keyboard":     "<optional: {rows: [[{text, callback_data}, ...], ...]}>",
  "parse_mode":          "<optional: 'plain' (default) | 'html' | 'http v1'>",
  "metadata":            "<optional: arbitrary JSON, echoed back in response>"
}
```

### Field rules

| Field | Type | Notes |
|---|---|---|
| `bot` | string | Must match a `~/.config/notify-agent/bots/<name>.env` filename. Unknown → `404 unknown_bot`. |
| `chat_id` | int/string | Override the bot's default `CHAT_ID` for this dispatch only. When omitted, the bot's `chat_id` from its `.env` is used. |
| `thread_id` | int or null | Forum topic id (Telegram `message_thread_id`). Omit / null → message lands in the chat General (only for non-forum groups; for forum groups this means no thread). |
| `content.type` | string | One of `text`, `edit`, `answer_callback`, `photo`, `voice`, `document`. Photo / voice / document return `400 unsupported_in_phase_0` in this revision; reserved for issue #18 Phase 4. |
| `reply_to_message_id` | int | Reserved (not wired yet). |
| `inline_keyboard` | object | Telegram inline-keyboard markup in the compact form. Converted server-side to the full `{inline_keyboard: [[{text, callback_data}, ...], ...]}` shape. |
| `parse_mode` | string | `plain` (default — server omits the field from the Telegram payload) or `html` (server passes `parse_mode=HTML` to Telegram). The alias `http v1` is accepted and treated as `html` for forward-compatibility with the issue #18 docs. |
| `metadata` | object | Arbitrary JSON, **always echoed back** in the response (`metadata` key). Used for tracing requests across hops (session id, source message id, etc.). |

## Content types

### `content.type = "text"`

Send a plain text message via Telegram `sendMessage`.

```json
"content": {
  "type": "text",
  "text": "<required: string, ≤ 4096 chars>"
}
```

Optional: `inline_keyboard` (top-level) attaches buttons.

Returns `{"ok": true, "telegram_message_id": <int>, "telegram_chat_id": "...", "telegram_thread_id": <int|null>, "metadata": {...}}`.

### `content.type = "edit"`

Edit an existing message via Telegram `editMessageText`. Note: Telegram's
`editMessageText` does NOT accept `message_thread_id`; the topic is
resolved implicitly from the chat.

```json
"content": {
  "type": "edit",
  "message_id": "<required: int, the Telegram message_id to edit>",
  "text": "<required: string, the new content>"
}
```

Returns same shape as `text` (with `telegram_message_id` echoed).

### `content.type = "answer_callback"`

Resolve an inline-keyboard callback (the user tapped a button on a
previous bot message). Required by Telegram within 30 seconds of the
callback — call this from the webhook handler the moment a `callback_query`
arrives.

```json
"content": {
  "type": "answer_callback",
  "callback_query_id": "<required: string>",
  "text":              "<optional: string, ≤ 200 chars, shown as toast>",
  "show_alert":        "<optional: bool, default false>"
}
```

Returns `{"ok": true, "telegram_message_id": 0, "telegram_chat_id": "...", "telegram_thread_id": <int|null>, "metadata": {...}}`. `telegram_message_id` is 0 because no new message was sent.

### `content.type = "photo" | "voice" | "document"` (NOT YET WIRED)

Reserved for issue #18 Phase 4 (media support). Calling now returns:

```json
{"ok": false, "error_code": "unsupported_in_phase_0", "requested_type": "photo", "metadata": {...}}
```

with HTTP 400.

## Response shapes

### Success

```json
{
  "ok": true,
  "telegram_message_id": <int or 0>,
  "telegram_chat_id":    "<string, echoes the resolved chat_id>",
  "telegram_thread_id":  <int or null>,
  "metadata":            <echo of the request metadata, may be empty {}>
}
```

### Error

```json
{
  "ok": false,
  "error_code":            "<stable string identifier>",
  "retry_after_seconds":   <int, only for rate_limited>,
  "metadata":              <echo>
}
```

## Error code → HTTP status mapping

| `error_code` | HTTP status | Meaning |
|---|---|---|
| `missing_bot` | 404 | `bot` field absent or empty. |
| `unknown_bot` | 404 | No `.env` matches `bot`. |
| `chat_not_found` | 404 | Telegram says the chat doesn't exist. |
| `thread_not_found` | 404 | Telegram says the topic doesn't exist (or the bot isn't in it). |
| `missing_text` | 400 | `content.type=text` without `text`. |
| `missing_message_id_or_text` | 400 | `content.type=edit` without one of those. |
| `missing_callback_query_id` | 400 | `content.type=answer_callback` without that field. |
| `missing_content_type` | 400 | `content.type` absent. |
| `unknown_content_type` | 400 | `content.type` not one of the supported values. |
| `unsupported_in_phase_0` | 400 | `content.type` in `photo\|voice\|document`. |
| `bad_request` | 502 | Telegram returned 400 for some other reason (e.g. text too long for the chat type, malformed HTML). |
| `message_too_long` | 502 | Telegram returned 400 "message text is too long". |
| `rate_limited` | 429 | Telegram returned 429. The body includes `retry_after_seconds`. |
| `forbidden` / `bot_blocked` | 403 | The bot was blocked or kicked. |
| `internal_error` | 502 | Server-side crash (logged with stack trace). |
| `server_not_initialized` | 503 | Server up but `dispatch_token` not configured. |
| `unauthorized` (auth) | 401 | Missing or wrong `Authorization` header. |

HTTP status mapping is in `Handler._handle_dispatch` in `assets/notify-bot.py`. When in doubt, treat any 5xx as transient (retry) and any 4xx as permanent (don't retry).

## Rate limiting

The server **does not** apply internal rate limiting. It forwards
Telegram's `retry_after` verbatim when Telegram returns 429. Callers
should:

1. Honour `retry_after_seconds` in the response body.
2. Use exponential backoff with jitter on repeated 429s.
3. Per-user rate limits (the Telegram global per-bot-per-chat limit) are
   not exposed — only what Telegram returns.

## Examples

### Send a text message with inline buttons

```bash
curl -X POST http://127.0.0.1:8765/dispatch \
  -H "Authorization: Bearer $TELEGRAM_DISPATCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "bot": "ofelia-vps",
    "thread_id": 4,
    "content": {"type": "text", "text": "¿Apruebas el plan?"},
    "inline_keyboard": {
      "rows": [[
        {"text": "Sí", "callback_data": "confirm:yes"},
        {"text": "No", "callback_data": "confirm:no"}
      ]]
    },
    "metadata": {"session_id": "abc-123"}
  }'
```

### Edit a previous message

```bash
curl -X POST http://127.0.0.1:8765/dispatch \
  -H "Authorization: Bearer $TELEGRAM_DISPATCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "bot": "ofelia-vps",
    "content": {"type": "edit", "message_id": 12345, "text": "editado"}
  }'
```

### Resolve a callback (button tap)

```bash
curl -X POST http://127.0.0.1:8765/dispatch \
  -H "Authorization: Bearer $TELEGRAM_DISPATCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "bot": "ofelia-vps",
    "content": {
      "type": "answer_callback",
      "callback_query_id": "abc-def-ghi",
      "text": "OK",
      "show_alert": false
    }
  }'
```

## Versioning

Contract is **v1.0** as of 2026-10-01. Breaking changes require a new
endpoint (e.g. `POST /dispatch/v2`) — never change the semantics of
`/dispatch` in-place. Backwards-compatible additions (new optional
fields, new `content.type`) may be added without bumping.

| Field | Since |
|---|---|
| `bot`, `chat_id`, `thread_id`, `content`, `inline_keyboard`, `parse_mode`, `metadata` | v1.0 |
| `content.type` = `text`, `edit`, `answer_callback` | v1.0 |
| `content.type` = `photo`, `voice`, `document` | reserved (Phase 4) |

## Change log

- **2026-10-01 v1.0**: initial contract, drafted jointly with the
  ofelia-ui agent. Signed off by both sides on Telegram chat.
  Implementation merged in commit `ba6432a` of
  `github.com/SGarciaMontalvo/telegramNotifications`.