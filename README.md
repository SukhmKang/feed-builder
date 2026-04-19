## What it does

**Feed Builder** lets you create custom news feeds from multiple source types (RSS, YouTube, Reddit, Twitter/Nitter, Tavily web search). Each feed consists of a list of sources and filters.

### Key features

- **Multi-source feeds** — aggregate RSS feeds, YouTube channels, Reddit, Twitter/Nitter, and web search results into a single feed
- **AI filtering pipeline** — build a pipeline of blocks (keyword filters, LLM scoring, deduplication, relevance checks) that each article must pass through
- **Stories view** — articles are clustered into stories, giving a digest-style view of what's happening
- **Pipeline versioning** — every pipeline change is versioned; revert to any previous version in one click
- **Replay** — re-evaluate all historical articles against an updated pipeline without re-fetching
- **Audits** — run a full AI audit of a feed's performance over a time window; get a structured report on pass rates, source quality, and pipeline recommendations, with a proposed pipeline diff you can review and apply
- **Manual verdicts** — override the pipeline's pass/filter decision on individual articles
- **RSS output** — every feed exposes a standard RSS endpoint for use in any reader
- **Demo mode** — deploy a read-only frontend where all mutating actions are disabled

## Environment variables

See `.env.example` for the full list. Required:

- `DATABASE_URL` — Postgres connection string (or SQLite path for dev)
- `ANTHROPIC_API_KEY` — used for all pipeline LLM calls
- `FRONTEND_URL` — comma-separated list of allowed frontend origins for CORS

Optional:
- `OPENAI_API_KEY`, `YOUTUBE_API_KEY`, `TAVILY_API_KEY`, `NITTER_BASE`, proxy credentials
