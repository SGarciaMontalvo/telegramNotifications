# Setup: putting a notify-agent bot into a Telegram forum group

This is the setup flow for **one** bot that posts into a specific Telegram
forum group, with one topic per `project_key`. It builds on the v1.0
multi-bot contract (one bot per Telegram bot token, multiple project keys
per bot); the only new thing is the `TOPIC_IDS` mapping.

## Prerequisites

- A Telegram supergroup with **Topics enabled** (Settings → Topics → On).
- An `@BotFather` bot (one bot per agent / project family).
- The bot added to the group as a member (admin recommended if you want it
  to pin / manage topics; member is enough for plain `sendMessage`).
- Stdlib Python on the host that will run `notify-bot.py`.

## Step 1 — Create the topic for this bot

In the Telegram client (mobile, desktop, or web):

1. Open the supergroup.
2. Tap the group name → **Topics** → **Create Topic**.
3. Name the topic after the agent (e.g. "Ofelia", "Brainstorm").
4. Optional: pick a colour / icon to make it easy to spot at a glance.

Repeat for each topic you need.

## Step 2 — Capture each topic's `message_thread_id`

The notify-bot server needs the numeric `message_thread_id` Telegram assigns
to each topic. There are three ways to get it:

**Option A — From any message in the topic (easiest)**

1. Open the topic.
2. Right-click (desktop) or long-press (mobile) on any message in the topic.
3. Choose **"Copy Link"**.
4. The link has the form `https://t.me/c/<group_id>/<topic_id>/<message_id>`.
   - `<group_id>` is the bare group id (no `-100` prefix).
   - `<topic_id>` is the integer you need.
   - `<message_id>` is irrelevant — drop it.

Example: `https://t.me/c/4320132331/4/22` → topic_id = **4**.

**Option B — From a service message via the Bot API**

If you have already added the bot to the group, the bot can call
`getUpdates` and read the `message_thread_id` and the topic name from the
`forum_topic_created` service message. See `../scripts/dump_topics.py` for
a small helper that prints all topic ids and names that the bot can see.

**Option C — Just publish a test message and read the reply**

1. Post "test" in the topic from your account.
2. From the bot, reply to it via the API (or call `forwardMessage` from the
   group back to the bot's own 1:1 chat). The reply carries the
   `message_thread_id` in its metadata.

## Step 3 — Drop the per-bot `.env`

```bash
mkdir -p ~/.config/notify-agent/bots

cat > ~/.config/notify-agent/bots/ofelia-vps.env <<EOF
BOT_TOKEN=REPLACE_WITH_BOTFATHER_TOKEN
BOT_USERNAME=ofelia_sgm_vpsagent_bot
DISPLAY_NAME=Ofelia VPS
PROJECT_KEY=ofelia,ofelia-ui
CHAT_ID=-1004320132331
TOPIC_IDS=ofelia:4,ofelia-ui:4
FULL_MODE=always
API_KEY=REPLACE_WITH_SHARED_SECRET
EOF
chmod 600 ~/.config/notify-agent/bots/ofelia-vps.env
```

Notes:
- `CHAT_ID` is the **supergroup** id (negative, starts with `-100`). This is
  NOT your private chat with the bot.
- `TOPIC_IDS` lists every project_key this bot owns, paired with its
  `message_thread_id`. Multiple keys can share one topic (e.g. `ofelia` and
  `ofelia-ui` both point at topic 4).
- If a bot owns a `general` catch-all key, simply OMIT it from `TOPIC_IDS`:
  messages for that key fall back to the chat General topic (no
  `message_thread_id` is sent). This is intentional.
- The bot **must already be a member of the group** before it can post.
  If the bot tries to send before being added, Telegram returns
  `403 Forbidden: bot was kicked` or `400 chat not found`.

## Step 4 — Install / update the server

Replace the upstream server with this fork:

```bash
SKILL_DIR=/home/deploy/.local/worktrees/telegram-notifications  # or your checkout path
install -m 0755 ${SKILL_DIR}/assets/notify-bot.py ~/.local/share/notify-agent/notify-bot.py
```

Or run it directly from the repo during development:

```bash
python3 /home/deploy/.local/worktrees/telegram-notifications/assets/notify-bot.py \
  --bots-dir ~/.config/notify-agent/bots \
  --port 8765 \
  --host 127.0.0.1
```

Restart the server (systemd unit, `at now` fallback, or whatever you use):

```bash
# systemd example
systemctl --user restart notify-bot
systemctl --user status notify-bot
journalctl --user -u notify-bot -e -n 30

# or, if you start it via `at now`:
echo "pkill -f notify-bot.py; /usr/bin/env python3 -u /home/deploy/.local/worktrees/telegram-notifications/assets/notify-bot.py --bots-dir ~/.config/notify-agent/bots --port 8765 --host 127.0.0.1 >> ~/.local/share/notify-agent/longterm.log 2>&1" | at now + 1 minute
```

## Step 5 — Verify with /health and a smoke notify

```bash
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
# expect: bots_loaded >= N, each bot has a "topic_ids" entry showing the mapping

curl -s http://127.0.0.1:8765/bots | python3 -m json.tool
# expect: every bot lists its topic_ids dict (or empty dict if it has none)

notify-agent --project ofelia --event done --message "primer test" --no-retry
# expect: push arrives in the "Ofelia" topic of the supergroup
```

## Step 6 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `notify-agent` returns 503 "chat_id not resolved" | Bot was never `/start`-ed by an admin, or its `.env` has no `CHAT_ID` and it's not picking up group messages | Add `CHAT_ID` to the bot's `.env` and restart; OR `/start` the bot from your private chat with it (but for group-only bots this doesn't help — set `CHAT_ID` explicitly) |
| Push arrives but in the wrong topic | `TOPIC_IDS` has the wrong `message_thread_id` for that key | Re-fetch the topic id via Option A/B/C and update the `.env` |
| `400 Bad Request: message thread not found` | The topic was deleted, or the bot lost access | Recreate the topic (you'll get a new id) and update `.env` |
| `403 Forbidden: bot is not a member of the channel chat` | Bot was kicked or never added | Re-add the bot to the group; wait 5s; retry |
| `403 Forbidden: bot was blocked by the user` (only for 1:1 bots) | User blocked the bot | Not applicable for group-only bots; for legacy 1:1 bots ask user to `/start` again |
| `TestLegacyBotBackwardCompat` fails after upgrade | One of the new code paths regressed v1.0 behaviour | Run `python3 assets/test_notify_bot.py` and check `TestLegacyBotBackwardCompat` output |
| `parse_topic_ids` warns about an entry | A `TOPIC_IDS` entry is malformed | Check `.env` for typos, missing colons, non-integer values |

## When you don't need topics at all

A bot whose project lives in a 1:1 private chat (the v1.0 default) doesn't
need `TOPIC_IDS` at all — just omit it from the `.env` and the server
behaves exactly like upstream v1.0. The `TestLegacyBotBackwardCompat`
integration test pins this behaviour.
