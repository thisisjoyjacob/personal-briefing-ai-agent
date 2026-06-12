# CLAUDE.md

## Stack
- Python (stdlib + anthropic SDK + requests only — no extra deps unless essential)
- GitHub Actions for scheduling, no Cloudflare Worker
- Resend for email (direct HTTP, no SDK)

## Locked decisions
- State lives in state/briefing.json, committed to repo (git-native history)
- No database, no Redis
- 3 topics max for now

## Current phase
Initial scaffold — see prompt in docs/PROJECT_PROMPT.md