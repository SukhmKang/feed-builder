from typing import Any, TypedDict


class SourceStats(TypedDict):
    source_type: str
    source_feed: str
    source_name: str
    total_articles: int
    passed_count: int
    filtered_count: int
    pass_rate: float


class WeeklyBucket(TypedDict):
    week_label: str   # e.g. "2026-W10 (Mar 2)"
    week_start: str   # ISO date string
    total_articles: int
    passed_count: int
    pass_rate: float


class AggregateStats(TypedDict):
    feed_id: str
    audit_period_start: str
    audit_period_end: str
    total_articles: int
    passed_count: int
    filtered_count: int
    overall_pass_rate: float
    manual_override_count: int
    manual_passed_count: int
    manual_filtered_count: int
    per_source: list[SourceStats]
    weekly_trend: list[WeeklyBucket]


class ArticleSample(TypedDict):
    title: str
    url: str
    source_name: str
    source_type: str
    published_at: str
    passed: bool
    manual_verdict: str | None
    manually_overridden: bool
    content: str
    deciding_block: str   # which pipeline block filtered/passed; empty for replay articles


class AuditSummaryPayload(TypedDict):
    stats: AggregateStats
    passed_sample: list[ArticleSample]
    filtered_sample: list[ArticleSample]
    manual_override_sample: list[ArticleSample]


# --- LLM step output types ---

class AuditAssessment(TypedDict):
    passed_quality: str
    filtered_quality: str
    source_quality: str
    coverage_gaps: str
    noise_patterns: str
    volume_health: str


class ManualOverrideAssessment(TypedDict):
    summary: str
    false_positives: str
    false_negatives: str
    patterns: str
    suggested_focus: str


class PipelineRecommendation(TypedDict):
    satisfied: bool
    feedback: str
    issues: dict[str, str]
    suggested_changes: list[dict[str, str]]


class SourceChange(TypedDict):
    action: str                      # "remove" | "modify" | "add_needed"
    source_type: str
    source_feeds: list[str]
    reason: str
    coverage_gap_description: str    # only populated for "add_needed"


class SourceRecommendation(TypedDict):
    satisfied: bool
    feedback: str
    suggested_changes: list[SourceChange]


class AuditReport(TypedDict):
    feed_id: str
    topic: str
    audit_period_start: str
    audit_period_end: str
    stats: AggregateStats
    assessment: AuditAssessment
    manual_override_assessment: ManualOverrideAssessment
    pipeline_recommendations: PipelineRecommendation
    source_recommendations: SourceRecommendation
    proposed_new_sources: list[dict[str, str]]   # validated candidates from Step 4 discovery
    current_config_snapshot: dict[str, Any]
    generated_at: str


__all__ = [
    "AggregateStats",
    "ArticleSample",
    "AuditAssessment",
    "AuditReport",
    "AuditSummaryPayload",
    "ManualOverrideAssessment",
    "PipelineRecommendation",
    "SourceChange",
    "SourceRecommendation",
    "SourceStats",
    "WeeklyBucket",
]
