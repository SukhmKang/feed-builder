---
name: Nitter self-hosted instance
description: Local Nitter instance setup for Twitter/X RSS feeds
type: project
---

Nitter is running locally via Docker at http://localhost:8080.

**Setup location:** `feed_builder/nitter/` contains docker-compose.yml, nitter.conf, sessions.jsonl
**Wrapper:** `feed_builder/nitter.py` — fetch_user_feed(username) and fetch_search_feed(query)

**Key facts:**
- Uses a burner Twitter account (@flashsassy) — cookies in sessions.jsonl
- When Nitter breaks: grab fresh auth_token + ct0 from DevTools (x.com), update sessions.jsonl, run `docker compose restart nitter` from the nitter/ directory
- RSS returns ~20 items max, no pagination support
- Don't log out of the burner account or cookies will invalidate immediately
- Machine is Apple Silicon Mac running under Rosetta — needed ARM64 Docker image (zedeus/nitter:latest-arm64)

**Why:** Feed builder needs Twitter/X content as RSS since Twitter has no public RSS.
