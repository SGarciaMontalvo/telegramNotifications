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
| Docs (`references/setup.md`, `bots-env.example`) | ✅ done |
| Backed up production bots to `bots/legacy/` | ✅ done |
| Production `.env` files created | ✅ done |
| Server restarted with new config | ✅ done |
| End-to-end smoke (push to all topics) | ✅ done |
| Merge back into `sgm-ai-skills` | ❌ not planned — see below |

**Deployed to production on 2026-10-01.** Sergio confirmed visually that
all five smoke-test messages landed in their respective topics (Ofelia,
Brainstorm, Bitácora, General) inside the "Agentes VPS" supergroup
(chat_id=-1004320132331).

### Production topology

| Bot | Project keys | Chat | Topic |
|---|---|---|---|
| `ofelia_sgm_vpsagent_bot` | `ofelia`, `ofelia-ui` | Agentes VPS | Topic 4 |
| `brainstorm_sgm_vpsagent_bot` | `brainstorm` | Agentes VPS | Topic 6 |
| `bitacora_sgm_vpsagent_bot` | `bitacora` | Agentes VPS | Topic 8 |
| `general_sgm_vpsagent_bot` | `general` | Agentes VPS | General (no thread) |
| (placeholder) | `tareas` | Agentes VPS | Topic 12 (BotFather limit) |
| `Ofelia_sgm_agent_bot` | `ia_conversacional` | 1:1 with Sergio | — |
| `varian_sgm_agent_bot` | `varian` | 1:1 with Sergio | — |

The production binary lives at
`/home/deploy/.local/share/notify-agent/notify-bot.py`, distinct from
the upstream skill path under `~/.config/opencode/skills/`.

### Decision: stay in this repo, do not merge back

Sergio chose to keep this as the canonical repository for the feature
rather than feeding it back into `sgm-ai-skills`. The upstream skill
remains at v1.0 (no topic support); the multi-bot topology in
`sgm-ai-skills` continues to work for any consumer that doesn't want
forum groups. Future development of the topic-aware version lives here.

See `/home/deploy/.pi/odd/tasks/dual-channel-notifications.md` on the
VPS for the full task plan, acceptance criteria, and closure notes.

## Why a separate repo?

The notify-agent skill is shared across all of Sergio's projects and lives
in `sgm-ai-skills`. The forum-topics extension is still experimental — it
touches the server's hot path and changes the on-disk layout (each bot now
needs a `TOPIC_IDS` line). Keeping the WIP in a dedicated repo lets us
iterate quickly, run the tests in isolation, and feed the change back to
`sgm-ai-skills` only when it's production-ready and Sergio signs off.
