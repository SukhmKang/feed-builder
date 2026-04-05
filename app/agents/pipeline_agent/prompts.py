import json
from typing import Any

from app.pipeline.schema import PIPELINE_SCHEMA_PROMPT


PIPELINE_BUILDER_TOOL_NAMES = [
    "preview_feed",
    "preview_sources",
    "search_web",
    "search_custom_blocks",
    "list_custom_blocks",
    "read_custom_block",
    "get_custom_block_docs",
    "write_custom_block",
    "delete_custom_block",
    "test_custom_block",
    "validate_pipeline_json",
    "view_example_valid_pipeline",
    "list_env_vars",
    "install_package",
]

AUDIT_REMEDIATION_TOOL_NAMES = [
    "discover_feeds",
    "search_web",
    "get_google_news_feed",
    "search_rsscatalog",
    "get_rsscatalog_category_feeds",
    "preview_feed",
    "preview_sources",
    "search_youtube_videos",
    "search_youtube_channels",
    "get_channel_from_video",
    "get_channel_feed",
    "preview_youtube_channel",
    "preview_youtube_search",
    "preview_youtube_channels_by_topic",
    "preview_youtube_videos_by_topic",
    "search_subreddits",
    "search_reddit_posts",
    "get_subreddit_from_post",
    "get_subreddit_feed",
    "preview_reddit_subreddit",
    "preview_reddit_search",
    "preview_reddit_subreddits_by_topic",
    "preview_nitter_user",
    "preview_nitter_search",
    "preview_tavily_search",
    "search_custom_blocks",
    "list_custom_blocks",
    "read_custom_block",
    "get_custom_block_docs",
    "write_custom_block",
    "delete_custom_block",
    "test_custom_block",
    "validate_pipeline_json",
    "view_example_valid_pipeline",
    "list_env_vars",
    "install_package",
]

BLOCK_EDIT_TOOL_NAMES = [
    "search_custom_blocks",
    "list_custom_blocks",
    "read_custom_block",
    "get_custom_block_docs",
    "validate_pipeline_json",
    "view_example_valid_pipeline",
]


def build_dispatch_prompt(topic: str) -> str:
    return "\n".join(
        [
            "Decide which source-family specialist agents should run for this topic.",
            "Valid agent names: rss, youtube, reddit, nitter, tavily.",
            "Use rss when the topic is likely covered by stable, known publishers or official feeds.",
            "Use tavily when coverage is broad across many sites, when there may not be strong native feeds, or when web/news search style discovery is likely to add recall.",
            "If the topic is primarily news-centric, bias strongly toward rss and tavily.",
            "For news-centric topics, avoid reddit and youtube by default unless there is a clear reason the topic is inherently community-driven, creator-driven, or breaks first on those platforms.",
            "Use reddit when community discussion, niche enthusiast posts, or subreddit-native coverage is likely to add unique signal that standard news sources will miss.",
            "Use youtube when creator videos, reviews, commentary, or channel-native coverage are central to the topic rather than peripheral.",
            "It is often reasonable to run both rss and tavily for broad or fast-moving topics.",
            "Bias toward running multiple agents when the topic is broad or news-like.",
            "Return JSON only with this shape:",
            json.dumps(
                {
                    "agents": ["rss", "youtube", "reddit", "nitter", "tavily"],
                    "reasons": {
                        "rss": "why",
                        "youtube": "why",
                    },
                },
                indent=2,
            ),
            f"Topic: {topic}",
        ]
    )


def dispatch_system_prompt() -> str:
    return (
        "You are a dispatch agent. "
        "Return valid JSON only. "
        "Choose which specialist source agents should run for the topic."
    )


def build_pipeline_builder_prompt(
    topic: str,
    selected_sources: list[dict[str, str]],
    *,
    feedback: dict[str, Any] | None,
    previous_blocks_json: list[dict[str, Any]] | None,
) -> str:
    prompt_parts = [
        "Build a pipeline for the given topic and source set.",
        "You are designing pipeline logic, not tuning to today's sample.",
        "The selected source list is fixed for this task.",
        "Do not add, remove, replace, or suggest edits to sources.",
        "You may only edit the pipeline logic that processes the provided sources.",
        "Assume source content will vary over weeks and months.",
        "Optimize for rules that will hold up across the typical range of content these sources produce, not just the current snapshot.",
        "Use the available tools to preview sources, inspect existing custom blocks, and create custom blocks only when needed.",
        "Before creating a new custom block, search existing custom blocks to find reusable ones.",
        "When social sources such as Nitter are leaking non-English content, prefer the reusable custom block `drop_non_english` before expensive LLM filters.",
        "If you decide a new custom block is needed and you are unsure of the exact interface, call get_custom_block_docs before writing it.",
        "When the pipeline needs source-type-specific behavior, prefer a switch block over deeply nested conditionals.",
        "Keep llm_filter prompts concise and operational. Avoid long policy memos or restating the full topic brief inside each prompt.",
        "Every llm_filter prompt must stay under 2500 characters.",
        "Use validate_pipeline_json to sanity-check candidate pipeline JSON before final submission when helpful.",
        "Return only the pipeline JSON array.",
        PIPELINE_SCHEMA_PROMPT,
        f"Topic: {topic}",
        "Selected sources:",
        json.dumps(selected_sources, indent=2),
    ]
    if previous_blocks_json is not None:
        prompt_parts.extend(
            [
                "Previous pipeline JSON:",
                json.dumps(previous_blocks_json, indent=2),
            ]
        )
    if feedback is not None:
        prompt_parts.extend(
            [
                "Critic feedback from the previous iteration:",
                json.dumps(feedback, indent=2),
                "Revise the pipeline to address the feedback.",
                "If any feedback mentions changing sources, ignore that part and address the issue only with pipeline logic.",
            ]
        )
    return "\n".join(prompt_parts)


def pipeline_builder_system_prompt() -> str:
    return (
        "You are a pipeline building agent. "
        "You are designing pipeline logic for the long run, not optimizing to today's sample. "
        "Assume the previewed articles are illustrative only, and build rules that will generalize across the future stream. "
        "The source list is fixed and out of scope. "
        "Do not add, remove, replace, or suggest edits to sources; only edit the pipeline. "
        "Prefer reusable custom blocks over creating new ones. "
        "If non-English social posts are getting through, prefer the existing custom block `drop_non_english` before LLM filters. "
        "Prefer switch blocks for source-type-specific routing instead of deeply nested conditionals. "
        "Keep llm_filter prompts concise, practical, and under 2500 characters. "
        "Use tools as needed, and when you are satisfied call submit_pipeline with the final pipeline JSON."
    )


def build_audit_remediation_prompt(
    topic: str,
    current_sources: list[dict[str, str]],
    current_blocks_json: list[dict[str, Any]],
    audit_report: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "Apply the audit report to the current feed configuration.",
            "You may edit both sources and pipeline blocks.",
            "Use the audit report as the primary driver of change.",
            "Prefer the smallest set of changes that addresses the audit findings.",
            "Preserve parts of the current config that the audit does not indicate should change.",
            "When changing sources, return canonical source specs only.",
            "When changing pipeline logic, keep llm_filter prompts concise and under 2500 characters.",
            "Prefer reusable custom blocks over writing new ones unless the audit clearly requires new logic.",
            "Use tools to preview or verify revised sources when needed.",
            "Return the complete revised source list and the complete revised pipeline block list.",
            PIPELINE_SCHEMA_PROMPT,
            f"Topic: {topic}",
            "Current sources:",
            json.dumps(current_sources, indent=2),
            "Current pipeline blocks:",
            json.dumps(_strip_agent_internal_fields(current_blocks_json), indent=2),
            "Audit report:",
            json.dumps(_slim_audit_report(audit_report), indent=2),
        ]
    )


def _slim_audit_report(report: dict[str, Any]) -> dict[str, Any]:
    """Compact an audit report down to actionable instructions for the remediation agent.

    The full report is for humans to read. The agent only needs:
    - pass rate + rough verdict
    - concise assessment signal (coverage gaps + noise, 1-2 sentences each)
    - pipeline changes list
    - source changes list (action + feed + one-line reason)
    - proposed new sources
    """
    stats = report.get("stats", {})
    pass_rate = stats.get("overall_pass_rate") or 0.0

    # Derive a plain-English verdict from the pass rate
    if pass_rate < 0.01:
        verdict = "critical over-filtering"
    elif pass_rate < 0.05:
        verdict = "over-filtering"
    elif pass_rate < 0.15:
        verdict = "moderate filtering"
    elif pass_rate <= 0.50:
        verdict = "healthy"
    else:
        verdict = "under-filtering"

    assessment = report.get("assessment") or {}
    pipeline_recs = report.get("pipeline_recommendations") or {}
    source_recs = report.get("source_recommendations") or {}

    # Trim source changes — keep action + feed, truncate reason to first sentence,
    # drop coverage_gap_description entirely
    source_changes = [
        {
            "action": c.get("action"),
            "source_type": c.get("source_type"),
            "source_feed": c.get("source_feed") or (c.get("source_feeds") or [""])[0],
            "reason": _first_sentence(c.get("reason", "")),
        }
        for c in (source_recs.get("suggested_changes") or [])
    ]

    slim: dict[str, Any] = {
        "pass_rate": pass_rate,
        "verdict": verdict,
        "coverage_gaps": _first_sentences(assessment.get("coverage_gaps", ""), n=2),
        "noise_patterns": _first_sentences(assessment.get("noise_patterns", ""), n=2),
        "pipeline_satisfied": pipeline_recs.get("satisfied"),
        "pipeline_changes": pipeline_recs.get("suggested_changes") or [],
        "source_satisfied": source_recs.get("satisfied"),
        "source_changes": source_changes,
    }
    if report.get("proposed_new_sources"):
        slim["proposed_new_sources"] = report["proposed_new_sources"]
    return slim


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    end = text.find(". ")
    return text[: end + 1].strip() if end != -1 else text.strip()


def _first_sentences(text: str, n: int = 2) -> str:
    if not text:
        return ""
    parts = text.split(". ")
    return ". ".join(parts[:n]).strip().rstrip(".") + ("." if len(parts) > 0 else "")


def audit_remediation_system_prompt() -> str:
    return (
        "You are an audit remediation agent. "
        "You revise a feed configuration based on an audit report. "
        "You may change both sources and pipeline logic, but only insofar as the audit justifies. "
        "Return the full revised configuration by calling submit_audit_remediation."
    )


def build_block_edit_prompt(
    topic: str,
    current_sources: list[dict[str, str]],
    selected_block_json: dict[str, Any],
    *,
    block_path: str,
    parent_context: str,
    sibling_blocks_json: list[dict[str, Any]],
    instruction: str,
) -> str:
    cleaned_selected_block = _strip_agent_internal_fields(selected_block_json)
    cleaned_sibling_blocks = _strip_agent_internal_fields(sibling_blocks_json)
    return "\n".join(
        [
            "Edit a single selected pipeline block according to the user's instruction.",
            "You may only replace the selected block with one or more pipeline blocks.",
            "Do not edit sources.",
            "Do not assume you can change unrelated parts of the pipeline.",
            "Make the smallest effective change that satisfies the instruction.",
            "Keep llm_filter prompts concise and under 2500 characters.",
            "Use the available tools to inspect reusable custom blocks when helpful.",
            "Before creating a new custom block, search existing custom blocks to find reusable ones.",
            "Return only the replacement block sequence for the selected block.",
            "Submit the final result as replacement_blocks.",
            PIPELINE_SCHEMA_PROMPT,
            f"Topic: {topic}",
            "Feed sources:",
            json.dumps(current_sources, indent=2),
            f"Selected block path: {block_path}",
            f"Parent container context: {parent_context}",
            "Sibling blocks in the same container:",
            json.dumps(cleaned_sibling_blocks, indent=2),
            "Selected block JSON:",
            json.dumps(cleaned_selected_block, indent=2),
            "User instruction:",
            instruction.strip(),
        ]
    )


def block_edit_system_prompt() -> str:
    return (
        "You are a block editing agent. "
        "You transform one selected pipeline block into one or more replacement blocks based on a user instruction. "
        "Keep the change narrow and local. "
        "Return the replacement block sequence by calling submit_block_edit."
    )


def _strip_agent_internal_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_agent_internal_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_agent_internal_fields(item)
            for key, item in value.items()
            if key not in {"batch_prompt", "batch_prompt_source_hash"}
        }
    return value


def source_agent_tool_names(agent_name: str) -> list[str]:
    if agent_name == "rss":
        return [
            "discover_feeds",
            "search_web",
            "get_google_news_feed",
            "search_rsscatalog",
            "get_rsscatalog_category_feeds",
            "preview_feed",
        ]
    if agent_name == "youtube":
        return [
            "search_web",
            "search_youtube_videos",
            "search_youtube_channels",
            "get_channel_from_video",
            "get_channel_feed",
            "preview_youtube_channel",
            "preview_youtube_search",
            "preview_youtube_channels_by_topic",
            "preview_youtube_videos_by_topic",
        ]
    if agent_name == "reddit":
        return [
            "search_subreddits",
            "search_reddit_posts",
            "get_subreddit_from_post",
            "get_subreddit_feed",
            "preview_reddit_subreddit",
            "preview_reddit_search",
            "preview_reddit_subreddits_by_topic",
        ]
    if agent_name == "nitter":
        return [
            "search_web",
            "preview_nitter_user",
            "preview_nitter_search",
        ]
    if agent_name == "tavily":
        return [
            "search_web",
            "preview_tavily_search",
        ]
    raise ValueError(f"Unsupported source agent: {agent_name}")


def source_agent_system_prompt(agent_name: str) -> str:
    if agent_name == "youtube":
        source_shape = {"sources": [{"type": "channel", "feed": "UCKKGlGrWD1ZxicRrqF6K98A"}], "notes": "short explanation"}
    elif agent_name == "reddit":
        source_shape = {"sources": [{"type": "subreddit", "feed": "AceAttorney"}], "notes": "short explanation"}
    elif agent_name == "nitter":
        source_shape = {"sources": [{"type": "user", "feed": "SteamDeckHQ"}], "notes": "short explanation"}
    elif agent_name == "tavily":
        source_shape = {"sources": [{"type": "search", "feed": "Ace Attorney announcement"}], "notes": "short explanation"}
    else:
        source_shape = {
            "sources": [{"type": "rss", "feed": "https://example.com/feed"}],
            "notes": "short explanation",
        }
    return "\n".join(
        [
            f"You are the {agent_name.upper()} source agent.",
            "You may use your own knowledge of good sources, and you may also use the provided tools to discover or verify them.",
            "If you already know a strong source URL or feed URL, use it directly instead of searching for it again.",
            "Discovery tools are helpers for when you do not already know the right URL or need extra candidates.",
            "Use discover_feeds only once you have a concrete publication/site name, domain, or homepage URL. If you only have a fuzzy description of a source, use search_web first to identify the actual site.",
            "Do not rely only on tools if you already know strong sources for the topic.",
            "Find strong candidate sources for the topic.",
            "Preview candidates before recommending them when possible.",
            "Prefer recurring, high-signal sources over broad noisy searches.",
            "When you are ready, call submit_source_candidates with this shape:",
            json.dumps(source_shape, indent=2),
            "IMPORTANT: Your final submit_source_candidates call must include ALL sources you want to keep — it fully replaces any previous submission. If you submitted sources earlier and are now adding replacements, re-include the previously accepted sources too.",
        ]
    )


def build_source_agent_prompt(agent_name: str, topic: str) -> str:
    source_hint: dict[str, str] = {
        "rss": (
            "Focus on official sites, publisher feeds, press blogs, and high-signal editorial feeds. "
            "Prefer native RSS feeds when available, and also use google_news_search when it adds useful coverage. "
            "Do not use Reddit URLs, subreddit feeds, or Reddit search as RSS sources; Reddit belongs to the dedicated reddit agent. "
            "Google News queries may include both site constraints and topic keywords, "
            'for example: site:bbc.com "Ace Attorney". '
            "When a source is broad or generic, do not submit a site-only Google News query such as site:example.com by itself; "
            "add topic keywords so the RSS stays narrow and relevant. "
            "If you have only a descriptive lead for a source, identify the actual site first before calling discover_feeds."
        ),
        "youtube": (
            "Focus on relevant channels or YouTube source types that are stable for ongoing coverage. "
            "Use preview_youtube_channel for specific channels or channel URLs, preview_youtube_search for direct video search, "
            "preview_youtube_channels_by_topic for channel discovery by topic, and preview_youtube_videos_by_topic for video-led topic discovery. "
            "Submit explicit types such as channel, channel_url, search, channels_by_topic, or videos_by_topic."
        ),
        "reddit": (
            "Focus on strong subreddit-based sources for ongoing topic coverage. "
            "Use preview_reddit_subreddit for concrete communities, preview_reddit_search for broad search queries, "
            "and preview_reddit_subreddits_by_topic for topic-led subreddit discovery. "
            "Submit explicit types such as subreddit, search, or subreddits_by_topic."
        ),
        "nitter": (
            "Focus on official accounts, developers, publishers, and high-signal topic accounts. "
            "Use preview_nitter_user for specific accounts and preview_nitter_search for broader search queries. "
            "Submit explicit types user or search."
        ),
        "tavily": (
            "Focus on broad web/news discovery queries that Tavily can keep refreshing over time. "
            "Use preview_tavily_search to inspect query quality before submitting. "
            "Submit explicit type search with the Tavily query as the feed value. "
            "Prefer concise news-oriented queries over long natural-language instructions."
        ),
    }
    return "\n".join(
        [
            f"Topic: {topic}",
            f"Agent type: {agent_name}",
            source_hint[agent_name],
            "Recommend 5 to 12 sources when the topic supports it.",
            "Favor breadth over premature narrowing.",
        ]
    )
