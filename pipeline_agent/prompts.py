import json
from typing import Any

from pipeline_schema import PIPELINE_SCHEMA_PROMPT


PIPELINE_BUILDER_TOOL_NAMES = [
    "preview_feed",
    "preview_sources",
    "search_custom_blocks",
    "list_custom_blocks",
    "read_custom_block",
    "get_custom_block_docs",
    "write_custom_block",
    "delete_custom_block",
    "test_custom_block",
    "validate_pipeline_json",
    "list_env_vars",
    "install_package",
]


def build_dispatch_prompt(topic: str) -> str:
    return "\n".join(
        [
            "Decide which source-family specialist agents should run for this topic.",
            "Valid agent names: rss, youtube, reddit, nitter.",
            "Bias toward running multiple agents when the topic is broad or news-like.",
            "Return JSON only with this shape:",
            json.dumps(
                {
                    "agents": ["rss", "youtube", "reddit", "nitter"],
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
        "Assume source content will vary over weeks and months.",
        "Optimize for rules that will hold up across the typical range of content these sources produce, not just the current snapshot.",
        "Use the available tools to preview sources, inspect existing custom blocks, and create custom blocks only when needed.",
        "Before creating a new custom block, search existing custom blocks to find reusable ones.",
        "If you decide a new custom block is needed and you are unsure of the exact interface, call get_custom_block_docs before writing it.",
        "When the pipeline needs source-type-specific behavior, prefer a switch block over deeply nested conditionals.",
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
            ]
        )
    return "\n".join(prompt_parts)


def pipeline_builder_system_prompt() -> str:
    return (
        "You are a pipeline building agent. "
        "You are designing pipeline logic for the long run, not optimizing to today's sample. "
        "Assume the previewed articles are illustrative only, and build rules that will generalize across the future stream. "
        "Prefer reusable custom blocks over creating new ones. "
        "Prefer switch blocks for source-type-specific routing instead of deeply nested conditionals. "
        "Use tools as needed, and when you are satisfied call submit_pipeline with the final pipeline JSON."
    )


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
            "preview_sources",
        ]
    if agent_name == "reddit":
        return [
            "search_subreddits",
            "search_reddit_posts",
            "get_subreddit_from_post",
            "get_subreddit_feed",
            "preview_sources",
        ]
    if agent_name == "nitter":
        return [
            "search_web",
            "preview_nitter_user",
            "preview_nitter_search",
            "preview_sources",
        ]
    raise ValueError(f"Unsupported source agent: {agent_name}")


def source_agent_system_prompt(agent_name: str) -> str:
    if agent_name == "youtube":
        source_shape = {"sources": [{"feed": "UCKKGlGrWD1ZxicRrqF6K98A"}], "notes": "short explanation"}
    elif agent_name == "reddit":
        source_shape = {"sources": [{"feed": "AceAttorney"}], "notes": "short explanation"}
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
        ]
    )


def build_source_agent_prompt(agent_name: str, topic: str) -> str:
    source_hint: dict[str, str] = {
        "rss": (
            "Focus on official sites, publisher feeds, press blogs, and high-signal editorial feeds. "
            "Prefer native RSS feeds when available, and also use google_news_search when it adds useful coverage. "
            "Google News queries may include both site constraints and topic keywords, "
            'for example: site:bbc.com "Ace Attorney". '
            "If you have only a descriptive lead for a source, identify the actual site first before calling discover_feeds."
        ),
        "youtube": (
            "Focus on relevant channels or YouTube source types that are stable for ongoing coverage. "
            "Submit only the canonical channel identifier, channel URL, or search query as the feed value; "
            "the orchestrator will deterministically map it to the correct YouTube source type."
        ),
        "reddit": (
            "Focus on strong subreddit-based sources for ongoing topic coverage. "
            "Submit only the subreddit name, subreddit URL/RSS URL, or search query as the feed value; "
            "the orchestrator will deterministically map it to the correct Reddit source type."
        ),
        "nitter": (
            "Focus on official accounts, developers, publishers, and high-signal topic accounts. "
            "Use nitter_search only when specific accounts are not enough."
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
