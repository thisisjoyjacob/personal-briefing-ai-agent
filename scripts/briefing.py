#!/usr/bin/env python3
"""Daily research-briefing agent.

For each topic in state/briefing.json, ask a model via OpenRouter (with web
search grounding) what's changed since yesterday's summary, compose a combined
markdown briefing, persist today's summaries as tomorrow's "yesterday", and
deliver it via email (Resend) and/or Telegram.

Topics can be set three ways:
  1. Edit the `topics` array in state/briefing.json directly (the 3 slots).
  2. Send the bot a `/topics` message (see apply_telegram_commands).
  3. Both — a Telegram `/topics` command overrides the file on the next run.

The model can be changed live by sending the bot a `/model <id>` message; it
persists in state. Resolution order: state (Telegram) > env > DEFAULT_MODEL.

Run locally:  python scripts/briefing.py
In CI:        invoked by .github/workflows/briefing.yml on a cron schedule.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# OpenRouter is OpenAI-compatible.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Strong general-purpose free model on OpenRouter. Change it live with a
# Telegram `/model <id>` command, or pin OPENROUTER_MODEL in the environment.
# Resolution order: state (Telegram) > env > this default.
DEFAULT_MODEL = "deepseek/deepseek-chat-v3-0324:free"
MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "1500"))


def resolve_model(state):
    return state.get("model") or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL

# Web search grounding via OpenRouter's `web` plugin. On by default; set
# WEB_SEARCH=0 to disable (then summaries rely only on the model's knowledge).
# Note: the web plugin has a small per-search cost even with a free model.
WEB_SEARCH = os.environ.get("WEB_SEARCH", "1").lower() not in ("0", "false", "no", "")
WEB_MAX_RESULTS = int(os.environ.get("WEB_MAX_RESULTS", "5"))

MAX_TOPICS = 3

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "briefing.json"
ARTIFACT_PATH = Path(__file__).resolve().parent.parent / "briefing.md"

IST = timezone(timedelta(hours=5, minutes=30))

SYSTEM_PROMPT = (
    "You are a research briefing assistant. Using the most current information "
    "available (web search results are provided when relevant), report what has "
    "genuinely changed on the given topic since the provided prior summary. "
    "Write a tight, skimmable markdown summary (a few bullet points) of only the "
    "new or changed developments. Lead with the most important item. Cite "
    "sources inline as markdown links. If nothing meaningful has changed, say so "
    "in one line. Do not include a heading — just the body."
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Telegram topic setting (poll getUpdates for a /topics command)
# ---------------------------------------------------------------------------

def _parse_topics(text):
    """Pull up to MAX_TOPICS topics out of a /topics message body.

    Accepts newline- or pipe-separated topics, e.g.:
        /topics AI policy | air quality | rockets
        /topics
        AI policy
        air quality
        rockets
    """
    body = text.strip()[len("/topics"):]
    parts = re.split(r"[\n|]+", body)
    topics = [p.strip() for p in parts if p.strip()]
    return topics[:MAX_TOPICS]


def apply_telegram_commands(state):
    """Poll Telegram and apply any /topics and /model commands.

    Returns a dict of what changed, e.g. {"topics": [...], "model": "..."}.
    Uses a persisted offset so each update is consumed once (and so a stale
    message can't keep clobbering manual edits to the file). Within a batch the
    newest command of each kind wins.

    Commands:
        /topics AI policy | air quality | rockets
        /model deepseek/deepseek-chat-v3-0324:free
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {}

    params = {"timeout": 0, "allowed_updates": '["message","channel_post"]'}
    offset = state.get("telegram_offset")
    if offset:
        params["offset"] = offset

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=30
        )
        updates = resp.json().get("result", [])
    except Exception as exc:  # never let command-polling sink the whole run
        print(f"getUpdates failed: {exc}", file=sys.stderr)
        return {}

    if not updates:
        return {}

    # Ack everything we just read so it won't be returned again.
    state["telegram_offset"] = max(u["update_id"] for u in updates) + 1

    changes = {}
    for upd in updates:  # iterate in order; newest of each kind overwrites
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = msg.get("text", "").strip()
        low = text.lower()
        if low.startswith("/topics"):
            parsed = _parse_topics(text)
            if parsed:
                state["topics"] = parsed
                changes["topics"] = parsed
        elif low.startswith("/model"):
            model = text[len("/model"):].strip()
            if model:
                state["model"] = model
                changes["model"] = model

    if changes:
        print(f"Applied Telegram commands: {changes}")
    return changes


# ---------------------------------------------------------------------------
# Research one topic (OpenRouter, direct HTTP)
# ---------------------------------------------------------------------------

def research_topic(topic, prior_summary, model):
    """Return today's markdown summary for a single topic.

    Raises on API failure so the caller can mark the topic unavailable.
    """
    prior = prior_summary.strip() or "(no prior summary — this is the first run)"
    user_prompt = (
        f"Topic: {topic}\n\n"
        f"Yesterday's summary:\n{prior}\n\n"
        "Summarize what has changed since then."
    )

    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    if WEB_SEARCH:
        payload["plugins"] = [{"id": "web", "max_results": WEB_MAX_RESULTS}]

    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
            "X-Title": "daily-briefing-agent",
        },
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])

    content = data["choices"][0]["message"]["content"]
    return (content or "").strip() or "_No summary returned._"


# ---------------------------------------------------------------------------
# Briefing composition
# ---------------------------------------------------------------------------

def compose_markdown(date_str, sections):
    """sections: list of (topic, body) tuples."""
    parts = [f"# Daily Briefing — {date_str}", ""]
    for topic, body in sections:
        parts.append(f"## {topic}")
        parts.append("")
        parts.append(body)
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def markdown_to_html(md):
    """Minimal markdown -> HTML for email (stdlib-only, no markdown dep).

    Handles headings, unordered lists, bold, inline links, and paragraphs.
    Good enough for a briefing email; not a full markdown implementation.
    """
    import html

    def inline(text):
        text = html.escape(text)
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        return text

    out = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line:
            close_list()
            continue
        if line.startswith("### "):
            close_list()
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            close_list()
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            close_list()
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = line.lstrip()[2:]
            out.append(f"<li>{inline(item)}</li>")
        else:
            close_list()
            out.append(f"<p>{inline(line)}</p>")

    close_list()
    body = "\n".join(out)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
        'sans-serif;max-width:680px;margin:0 auto;line-height:1.5;color:#222;">'
        f"{body}</div>"
    )


# ---------------------------------------------------------------------------
# Email delivery (Resend, direct HTTP) — optional
# ---------------------------------------------------------------------------

def send_email(subject, markdown):
    """Send the briefing via Resend. Returns True on success.

    No-op returning False when Resend env vars are unset, so email is opt-in.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    email_to = os.environ.get("BRIEFING_EMAIL_TO")
    email_from = os.environ.get("BRIEFING_EMAIL_FROM")
    if not (api_key and email_to and email_from):
        return False  # not configured — skip silently

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": email_from,
            "to": [email_to],
            "subject": subject,
            "html": markdown_to_html(markdown),
            "text": markdown,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"Resend failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        return False
    print(f"Email sent via Resend: {resp.json().get('id', '(no id)')}")
    return True


# ---------------------------------------------------------------------------
# Telegram delivery (Bot API, direct HTTP) — optional
# ---------------------------------------------------------------------------

# Telegram caps a message at 4096 chars; stay under it with headroom.
TELEGRAM_CHUNK_LIMIT = 3500


def _markdown_to_telegram_html(md):
    """Convert briefing markdown to Telegram's HTML subset.

    Telegram has no headings/lists, so headings become bold lines and bullets
    become "• ". Output is line-based and each line's tags are self-contained,
    so it can be safely split on newlines for chunking.
    """
    import html

    def inline(text):
        text = html.escape(text)
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
        return text

    out = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line:
            out.append("")
        elif line.startswith("### "):
            out.append(f"<b>{inline(line[4:])}</b>")
        elif line.startswith("## "):
            out.append(f"<b>{inline(line[3:])}</b>")
        elif line.startswith("# "):
            out.append(f"<b>{inline(line[2:])}</b>")
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(f"• {inline(line.lstrip()[2:])}")
        else:
            out.append(inline(line))
    return "\n".join(out)


def _chunk_by_lines(text, limit):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = line if not cur else f"{cur}\n{line}"
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(markdown):
    """Send markdown to Telegram. Returns True on success.

    No-op returning False when TELEGRAM_* env vars are unset, so Telegram is
    purely opt-in.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False  # not configured — skip silently

    html = _markdown_to_telegram_html(markdown)
    ok = True
    for chunk in _chunk_by_lines(html, TELEGRAM_CHUNK_LIMIT):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.status_code >= 300:
            print(f"Telegram failed ({resp.status_code}): {resp.text}", file=sys.stderr)
            ok = False
    if ok:
        print("Briefing sent via Telegram.")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()

    # Apply any /topics and /model commands sent to the bot since the last run.
    changes = apply_telegram_commands(state)

    topics = state.get("topics", [])
    prior_summaries = state.get("summaries", {})
    model = resolve_model(state)

    if "topics" in changes:
        send_telegram("✅ Topics updated:\n" + "\n".join(f"- {t}" for t in topics))
    if "model" in changes:
        send_telegram(f"✅ Model set to: {changes['model']}")

    if not topics:
        print("No topics configured — nothing to do.")
        save_state(state)  # persist the telegram offset even with no topics
        return 0

    print(f"Using model: {model}")

    sections = []
    new_summaries = {}

    for topic in topics:
        prior = prior_summaries.get(topic, "")
        print(f"Researching: {topic}")
        try:
            body = research_topic(topic, prior, model)
            sections.append((topic, body))
            new_summaries[topic] = body
        except Exception as exc:  # one topic failing must not sink the rest
            print(f"  topic failed: {exc}", file=sys.stderr)
            sections.append((topic, "_Unavailable today._"))
            # Keep yesterday's summary so we don't lose the thread tomorrow.
            new_summaries[topic] = prior

    date_str = datetime.now(IST).strftime("%A, %d %B %Y")
    markdown = compose_markdown(date_str, sections)
    subject = f"Daily Briefing — {date_str}"

    # Persist today's summaries (this becomes tomorrow's "yesterday").
    state["summaries"] = new_summaries
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    emailed = False
    try:
        emailed = send_email(subject, markdown)
    except Exception as exc:
        print(f"Email step errored: {exc}", file=sys.stderr)

    telegrammed = False
    try:
        telegrammed = send_telegram(markdown)
    except Exception as exc:
        print(f"Telegram step errored: {exc}", file=sys.stderr)

    if not (emailed or telegrammed):
        # Fallback: write the briefing as a workflow artifact so it's not lost.
        ARTIFACT_PATH.write_text(markdown, encoding="utf-8")
        print(f"Wrote fallback artifact: {ARTIFACT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
