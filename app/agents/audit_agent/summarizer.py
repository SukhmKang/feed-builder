"""Stratified sampling and stats table formatting for the audit agent."""

import json
from typing import Any

from .types import AggregateStats, ArticleSample, AuditSummaryPayload

AUDIT_SAMPLE_PASSED = 20
AUDIT_SAMPLE_FILTERED = 20
AUDIT_SAMPLE_MANUAL_OVERRIDES = 12
AUDIT_CONTENT_LIMIT = 300


def build_audit_summary_payload(
    stats: AggregateStats,
    passed: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
) -> AuditSummaryPayload:
    passed_sample = _build_stratified_sample(passed, passed=True, target_n=AUDIT_SAMPLE_PASSED)
    filtered_sample = _build_stratified_sample(filtered, passed=False, target_n=AUDIT_SAMPLE_FILTERED)
    manual_override_sample = _build_manual_override_sample(
        passed + filtered,
        target_n=AUDIT_SAMPLE_MANUAL_OVERRIDES,
    )
    return AuditSummaryPayload(
        stats=stats,
        passed_sample=passed_sample,
        filtered_sample=filtered_sample,
        manual_override_sample=manual_override_sample,
    )


def format_stats_table(stats: AggregateStats) -> str:
    """Return a compact plain-text stats table for inclusion in LLM prompts."""
    lines: list[str] = []

    start = stats["audit_period_start"][:10]
    end = stats["audit_period_end"][:10]
    lines.append(f"AUDIT PERIOD: {start} → {end}")
    lines.append("")

    total = stats["total_articles"]
    passed = stats["passed_count"]
    filtered = stats["filtered_count"]
    rate = stats["overall_pass_rate"] * 100
    lines.append(
        f"OVERALL: {total} articles | {passed} passed ({rate:.1f}%) | {filtered} filtered ({100 - rate:.1f}%)"
    )
    if stats["manual_override_count"] > 0:
        lines.append(
            "MANUAL OVERRIDES: "
            f"{stats['manual_override_count']} total "
            f"({stats['manual_passed_count']} forced passed, {stats['manual_filtered_count']} forced filtered)"
        )
    lines.append("")

    if stats["per_source"]:
        lines.append("PER-SOURCE BREAKDOWN:")
        col_w = [20, 32, 7, 8, 10, 7]
        header = (
            f"{'source_type':<{col_w[0]}} | {'source_name':<{col_w[1]}} | "
            f"{'total':>{col_w[2]}} | {'passed':>{col_w[3]}} | {'filtered':>{col_w[4]}} | {'pass%':>{col_w[5]}}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for src in stats["per_source"]:
            src_type = src["source_type"][:col_w[0]]
            src_name = src["source_name"][:col_w[1]]
            lines.append(
                f"{src_type:<{col_w[0]}} | {src_name:<{col_w[1]}} | "
                f"{src['total_articles']:>{col_w[2]}} | {src['passed_count']:>{col_w[3]}} | "
                f"{src['filtered_count']:>{col_w[4]}} | {src['pass_rate'] * 100:>{col_w[5] - 1}.1f}%"
            )
        lines.append("")

    if stats["weekly_trend"]:
        lines.append("WEEKLY TREND:")
        lines.append(f"{'week':<22} | {'total':>6} | {'passed':>7} | {'pass%':>6}")
        lines.append("-" * 50)
        for bucket in stats["weekly_trend"]:
            lines.append(
                f"{bucket['week_label']:<22} | {bucket['total_articles']:>6} | "
                f"{bucket['passed_count']:>7} | {bucket['pass_rate'] * 100:>5.1f}%"
            )
        lines.append("")

    return "\n".join(lines)


def _build_stratified_sample(
    articles: list[dict[str, Any]],
    *,
    passed: bool,
    target_n: int,
) -> list[ArticleSample]:
    if not articles:
        return []

    # Group by (source_type, source_name)
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for article in articles:
        src_type = str(article.get("source_type", "")).strip()
        src_name = str(article.get("source_name", "")).strip()
        key = (src_type, src_name)
        buckets.setdefault(key, []).append(article)

    # Sort each bucket by published_at for even time distribution
    for key in buckets:
        buckets[key].sort(key=lambda a: str(a.get("published_at", "")))

    # Allocate slots proportionally (min 1 per bucket)
    n_buckets = len(buckets)
    total_articles = len(articles)
    slots: dict[tuple[str, str], int] = {}
    remaining = target_n

    bucket_list = list(buckets.items())
    for key, bucket_articles in bucket_list:
        proportional = max(1, round(len(bucket_articles) / total_articles * target_n))
        slots[key] = proportional

    # Trim or expand to hit target_n
    total_slots = sum(slots.values())
    if total_slots > target_n:
        # Trim from largest buckets
        sorted_keys = sorted(slots, key=lambda k: slots[k], reverse=True)
        for k in sorted_keys:
            if total_slots <= target_n:
                break
            if slots[k] > 1:
                cut = min(slots[k] - 1, total_slots - target_n)
                slots[k] -= cut
                total_slots -= cut
    elif total_slots < target_n:
        # Add to largest buckets
        sorted_keys = sorted(slots, key=lambda k: len(buckets[k]), reverse=True)
        i = 0
        while total_slots < target_n:
            k = sorted_keys[i % len(sorted_keys)]
            max_for_bucket = len(buckets[k])
            if slots[k] < max_for_bucket:
                slots[k] += 1
                total_slots += 1
            i += 1
            if i > target_n * 2:
                break

    # Pick evenly spaced articles from each bucket
    result: list[ArticleSample] = []
    for key, bucket_articles in bucket_list:
        n = slots.get(key, 1)
        picked = _evenly_spaced(bucket_articles, n)
        for article in picked:
            result.append(_to_article_sample(article, passed=passed))

    # Shuffle slightly to avoid source clustering in prompt
    import random
    random.shuffle(result)
    return result[:target_n]


def _build_manual_override_sample(
    articles: list[dict[str, Any]],
    *,
    target_n: int,
) -> list[ArticleSample]:
    overridden = [
        article for article in articles
        if article.get("_manual_verdict") in {"passed", "filtered"}
    ]
    if not overridden:
        return []

    forced_passed = [article for article in overridden if article.get("_manual_verdict") == "passed"]
    forced_filtered = [article for article in overridden if article.get("_manual_verdict") == "filtered"]

    half = max(1, target_n // 2)
    result: list[ArticleSample] = []
    result.extend(_build_stratified_sample(forced_passed, passed=True, target_n=min(half, len(forced_passed))))
    remaining = max(0, target_n - len(result))
    result.extend(
        _build_stratified_sample(
            forced_filtered,
            passed=False,
            target_n=min(remaining, len(forced_filtered)),
        )
    )

    if len(result) < target_n:
        used_urls = {sample["url"] for sample in result}
        leftovers = [
            article for article in overridden
            if str(article.get("url", "")).strip() not in used_urls
        ]
        leftovers.sort(key=lambda a: str(a.get("published_at", "")), reverse=True)
        for article in leftovers:
            if len(result) >= target_n:
                break
            result.append(
                _to_article_sample(
                    article,
                    passed=article.get("_manual_verdict") == "passed",
                )
            )

    return result[:target_n]


def _evenly_spaced(items: list[Any], n: int) -> list[Any]:
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _to_article_sample(article: dict[str, Any], *, passed: bool) -> ArticleSample:
    deciding_block = ""
    pipeline_result = article.get("_pipeline_result") or {}
    if isinstance(pipeline_result, dict):
        deciding_block = str(pipeline_result.get("deciding_block", "")).strip()

    content = str(article.get("content", "") or article.get("full_text", "")).strip()
    if len(content) > AUDIT_CONTENT_LIMIT:
        content = content[: AUDIT_CONTENT_LIMIT - 3].rstrip() + "..."

    return ArticleSample(
        title=str(article.get("title", "")).strip(),
        url=str(article.get("url", "")).strip(),
        source_name=str(article.get("source_name", "")).strip(),
        source_type=str(article.get("source_type", "")).strip(),
        published_at=str(article.get("published_at", "")).strip(),
        passed=passed,
        manual_verdict=(
            str(article.get("_manual_verdict")).strip()
            if article.get("_manual_verdict") is not None
            else None
        ),
        manually_overridden=article.get("_manual_verdict") in {"passed", "filtered"},
        content=content,
        deciding_block=deciding_block,
    )


__all__ = [
    "build_audit_summary_payload",
    "format_stats_table",
    "AUDIT_SAMPLE_PASSED",
    "AUDIT_SAMPLE_FILTERED",
    "AUDIT_SAMPLE_MANUAL_OVERRIDES",
]
