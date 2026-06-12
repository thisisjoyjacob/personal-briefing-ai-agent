# claude-briefing-agent

A daily research-briefing agent. Every morning a GitHub Action wakes up, asks a
free model via **OpenRouter** (with web-search grounding) what's changed on each
of your topics since yesterday, composes a combined markdown briefing, and
delivers it by **email (Resend)** and/or **Telegram**.

State lives in `state/briefing.json`, committed back to the repo each run, so
the history is git-native: no database, no Redis.

## How it works

```
.github/workflows/briefing.yml   cron (7 AM IST / 1:30 AM UTC) → runs the script
scripts/briefing.py              per topic: ask OpenRouter + web search, summarize
state/briefing.json              topics, model, summaries, telegram offset
                                 (read at start, overwritten at end)
```

Each run: read any `/topics` or `/model` commands you've sent the bot, read
yesterday's per-topic summaries, ask the model for what's new, write today's
summaries back as tomorrow's "yesterday", and deliver the briefing.

## Setup

### 1. Repository secrets

**Settings → Secrets and variables → Actions → New repository secret.**

Required:

| Secret               | Purpose                                              |
| -------------------- | --------------------------------------------------- |
| `OPENROUTER_API_KEY` | OpenRouter API key (used for research + web search) |

Optional — email channel (add all three or none):

| Secret                | Purpose                                              |
| --------------------- | --------------------------------------------------- |
| `RESEND_API_KEY`      | Resend API key                                      |
| `BRIEFING_EMAIL_TO`   | Recipient address                                   |
| `BRIEFING_EMAIL_FROM` | Sender address (must be on a Resend-verified domain) |

Optional — Telegram channel (add both or none). Telegram is also how you set
topics and the model by chatting (see below):

| Secret                | Purpose                                              |
| --------------------- | --------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | Bot token from [@BotFather](https://t.me/BotFather)  |
| `TELEGRAM_CHAT_ID`    | Numeric chat ID to send to (see below)              |

To get a chat ID: message your bot once, open
`https://api.telegram.org/bot<token>/getUpdates`, and read
`result[].message.chat.id` (a number like `123456789`). The bot *username* is
not the chat ID.

If neither email nor Telegram is configured, the briefing is uploaded as a
workflow artifact (`briefing.md`) instead.

### 2. Workflow permissions

The workflow commits the updated `state/briefing.json` back to the repo. It
declares `permissions: contents: write`, but the repository must also allow it:
**Settings → Actions → General → Workflow permissions → Read and write
permissions**.

## The model

Defaults to a strong general-purpose free OpenRouter model
(`deepseek/deepseek-chat-v3-0324:free`). Change it any of these ways
(resolution order: Telegram > env > default):

- **Telegram:** `/model deepseek/deepseek-chat-v3-0324:free` — persists in state.
- **Env:** set `OPENROUTER_MODEL` (e.g. as a repo variable/secret).
- **Default:** edit `DEFAULT_MODEL` in `scripts/briefing.py`.

Browse free model IDs at <https://openrouter.ai/models?max_price=0>.

> **Web search cost:** grounding uses OpenRouter's `web` plugin, which has a
> small per-search cost (~$0.02/request at 5 results) even with a free model.
> Set the `WEB_SEARCH=0` env var to disable it and rely only on model knowledge.

## Setting topics

Keep it to **3 topics** (the workflow has a 10-minute budget). Two ways:

**A. Edit the file** — change the `topics` array in `state/briefing.json`:

```json
{
  "topics": ["Your first topic", "Your second topic", "Your third topic"],
  "model": null,
  "summaries": {},
  "telegram_offset": null,
  "last_run": null
}
```

**B. Chat with the bot** — send a `/topics` message (newline- or pipe-separated):

```
/topics AI policy | air-quality sensors | reusable rockets
```

A Telegram `/topics` command overrides the file on the next run. Because the
agent runs on a cron, changes take effect at the next scheduled run — or trigger
it now from the **Actions** tab via **Run workflow**. The bot replies with a
confirmation when topics or the model change.

## Testing locally

```bash
pip install -r requirements.txt

export OPENROUTER_API_KEY=sk-or-...

# optional email channel
export RESEND_API_KEY=re_...
export BRIEFING_EMAIL_TO=you@example.com
export BRIEFING_EMAIL_FROM=briefing@your-verified-domain.com

# optional Telegram channel (and topic/model commands)
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=123456789

python scripts/briefing.py
```

A run researches each topic, **overwrites `state/briefing.json`** with today's
summaries, and delivers the briefing. To test without keeping state changes, run
on a throwaway branch or `git checkout state/briefing.json` afterward.

## Error handling

- If the model call fails for a single topic, that topic is marked
  _"Unavailable today"_, its previous summary is retained, and the others run.
- The briefing is sent over every configured channel (email + Telegram).
  Telegram messages auto-split to stay under the 4096-char limit.
- If **no** channel delivers, the briefing is written to `briefing.md` and
  uploaded as a workflow artifact so it's never lost.
