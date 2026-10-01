# telegramNotifications

Multi-bot HTTP→Telegram bridge for the `notify-agent` skill, **extended with
forum-group topic support**. When a project lives in a Telegram supergroup
with Topics enabled (one topic per agent/project), this server routes each
notification to the right topic instead of dropping everything into a single
1:1 chat.

This repository is a **scratchpad / work-in-progress** of the feature
`dual-channel-notifications`. The "production" notify-agent server (v1.0
multi-bot) still lives in
[`SGarciaMontalvo/sgm-ai-skills`](https://github.com/SGarciaMontalvo/sgm-ai-skills)
under `skills/notify-agent/`. The plan is to feed the changes back into that
repo once the feature is validated end-to-end.

## What this fork adds on top of sgm-ai-skills v1.0

A single new env variable per bot: `TOPIC_IDS=key:thread_id,key:thread_id,...`.
When a notification is delivered for a `project_key` listed in `TOPIC_IDS`,
the server sends the message with `message_thread_id=<thread_id>` so Telegram
drops it into the right forum topic of the supergroup. Project keys not
listed fall back to the chat's "General" topic (i.e. behave exactly like
v1.0, no `message_thread_id` in the payload).

The change is fully backwards-compatible: a bot whose `.env` has no
`TOPIC_IDS` keeps working identically to the upstream v1.0. See
`test_notify_bot.py` for the explicit `TestLegacyBotBackwardCompat` case.

## Repository layout

```
assets/
  notify-bot.py        HTTP server (multi-bot registry, topic-aware)
  test_notify_bot.py   stdlib-only test suite (19 tests, 4.3s)
references/
  setup.md             one-time setup for a bot in a forum group
  bots-env.example     .env template showing TOPIC_IDS
bin/                   (reserved for systemd unit / CLI wrappers; empty)
```

## Running the tests

The server and tests are stdlib-only (no `pip install`).

```bash
python3 assets/test_notify_bot.py
# expect: Ran 19 tests in ~4s, OK
```

The integration tests boot a real `MultiBotServer` on an ephemeral port
with `_tg_call_json` stubbed to capture payloads instead of calling
Telegram — so no network access is required.

## Configuration example

```ini
# ~/.config/notify-agent/bots/ofelia-vps.env
BOT_TOKEN=...
BOT_USERNAME=ofelia_sgm_vpsagent_bot
DISPLAY_NAME=Ofelia VPS
PROJECT_KEY=ofelia,ofelia-ui
CHAT_ID=-1004320132331        # the Agentes VPS supergroup
FULL_MODE=always
TOPIC_IDS=ofelia:4,ofelia-ui:4
API_KEY=<shared-secret>
```

A bot whose `PROJECT_KEY` lists `general` (or any key with no entry in
`TOPIC_IDS`) sends to the chat's "General" topic — useful as a catch-all
for messages that don't have a dedicated topic.

## Status

| Step | Status |
|---|---|
| Server extended with `TOPIC_IDS` | ✅ done |
| Unit + integration tests passing (19/19) | ✅ done |
| Docs (`references/setup.md`, `bots-env.example`) | ⏳ pending |
| Backed up production bots to `bots/legacy/` | ⏳ pending |
| Production `.env` files created | ⏳ pending |
| Server restarted with new config | ⏳ pending |
| End-to-end smoke (push to all topics) | ⏳ pending |
| Merge back into `sgm-ai-skills` | ⏳ pending |

See `/home/deploy/.pi/odd/tasks/dual-channel-notifications.md` (on the VPS,
not in this repo) for the full task plan and acceptance criteria.

## Why a separate repo?

The notify-agent skill is shared across all of Sergio's projects and lives
in `sgm-ai-skills`. The forum-topics extension is still experimental — it
touches the server's hot path and changes the on-disk layout (each bot now
needs a `TOPIC_IDS` line). Keeping the WIP in a dedicated repo lets us
iterate quickly, run the tests in isolation, and feed the change back to
`sgm-ai-skills` only when it's production-ready and Sergio signs off.
