Scaffold a daily research-briefing agent in this repo, deployed as a GitHub Action.

ARCHITECTURE
- .github/workflows/briefing.yml — cron trigger (7 AM IST = 1:30 AM UTC), runs a Python script
- scripts/briefing.py — calls Anthropic API in a loop with web_search tool until it produces final markdown
- state/briefing.json — prior briefing + topic list, read at start of run, overwritten at end
- Email delivery via Resend API (direct HTTP call, no SDK)

AGENT LOOP (scripts/briefing.py)
- Load state/briefing.json (topics list + yesterday's summary per topic)
- For each topic, call Claude (model: claude-sonnet-4-6) with web_search tool enabled,
  prompt: "What's new on {topic} since: {yesterday's summary}. Search and summarize changes."
- Loop until no more tool_use blocks, extract final text
- Compose combined markdown briefing from all topic outputs
- Write new state/briefing.json with today's summaries (this becomes tomorrow's "yesterday")
- POST markdown to Resend API as HTML email

SECRETS (GitHub repo secrets, referenced in workflow env)
- ANTHROPIC_API_KEY
- RESEND_API_KEY
- BRIEFING_EMAIL_TO / BRIEFING_EMAIL_FROM

TOPICS
Start with 3, stored in state/briefing.json under a "topics" key, editable without touching code.

ERROR HANDLING
- If Claude API call fails for one topic, skip it, note "unavailable today" in briefing, continue others
- If Resend fails, fall back to writing briefing.md as a workflow artifact so it's not lost
- Workflow timeout: 10 min (3 topics × ~3 min worst case)

DELIVERABLES
1. .github/workflows/briefing.yml
2. scripts/briefing.py
3. state/briefing.json (seed with placeholder topics)
4. README section: how to set secrets, how to change topics, how to test locally