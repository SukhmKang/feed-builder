// ─── Pipeline types (auto-generated from Pydantic schema models) ──────────────
// Source of truth: app/pipeline/schema_models.py
// To regenerate: python scripts/generate_pipeline_types.py

import type { PipelineBlock, PipelineCondition, SwitchBranchSchema } from "./pipeline_types.generated";
export type { PipelineBlock, PipelineCondition };
export type SwitchBranch = SwitchBranchSchema;
export { PIPELINE_ARTICLE_FIELDS, PIPELINE_SOURCE_TYPE_VALUES } from "./pipeline_types.generated";
export type { Value as SourceTypeValue } from "./pipeline_types.generated";

// PipelineTier is a convenience alias kept here since it's used directly in UI code.
export type PipelineTier = "mini" | "medium" | "high";

export interface SourceSpec {
  type: string;
  feed: string;
}

export interface CustomBlockOption {
  name: string;
  title: string | null;
  description: string | null;
}

export interface PipelineVersion {
  id: string;
  feed_id: string;
  version_number: number;
  is_active: boolean;
  has_been_replayed: boolean;
  label: string | null;
  created_at: string | null;
  config: { sources: SourceSpec[]; blocks: PipelineBlock[]; topic?: string } | null;
}

// ─── Feed / Article types ─────────────────────────────────────────────────────

export interface Feed {
  id: string;
  name: string;
  topic: string;
  status: "building" | "ready" | "error";
  poll_interval_hours: number;
  created_at: string | null;
  last_polled_at: string | null;
  error_message: string | null;
  config: { sources: SourceSpec[]; blocks: PipelineBlock[] } | null;
  active_version_replayed: boolean;
}

export interface NitterMedia {
  content_type: "image" | "video" | "gif";
  thumbnail_url: string;
  content_url: string | null;
  duration: string | null;
  media_text: string | null;
}

export interface NitterQuoteTweet {
  username: string;
  display_name: string;
  text: string;
  url: string;
  media: NitterMedia[];
}

export interface NitterRaw {
  username: string;
  text: string;
  media: NitterMedia[];
  quote_tweet: NitterQuoteTweet | null;
}

export interface YouTubeRaw {
  video_id: string;
  video_title: string;
  channel_name: string;
  description: string;
  transcript?: { text: string };
}

export interface Article {
  id: string;
  feed_id: string;
  passed: boolean;
  manual_verdict: "passed" | "filtered" | null;
  fetched_at: string | null;
  article: {
    id: string;
    title: string;
    url: string;
    published_at: string;
    content: string;
    full_text: string;
    source_name: string;
    source_type: string;
    tags: string[];
    raw?: NitterRaw | YouTubeRaw | Record<string, unknown>;
  };
  pipeline_result: {
    passed: boolean;
    dropped_at: string | null;
    block_results: { passed: boolean; reason: string }[];
  };
}

export interface StorySummary {
  id: string;
  feed_id: string;
  title: string;
  summary: string;
  status: "active" | "merged" | string;
  canonical_article_id: string | null;
  article_count: number;
  first_published_at: string | null;
  last_published_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  provenance: Record<string, unknown> | null;
  representative_article: Article["article"] | null;
}

export interface StoryDetail extends StorySummary {
  articles: Article["article"][];
}

// ─── Audit types ──────────────────────────────────────────────────────────────

export interface AuditSummary {
  id: string;
  feed_id: string;
  status: "pending" | "running" | "complete" | "error";
  audit_period_start: string | null;
  audit_period_end: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  error_message: string | null;
  pipeline_version_id: string | null;
  pipeline_version_number: number | null;
}

export interface AuditReport {
  feed_id: string;
  topic: string;
  audit_period_start: string;
  audit_period_end: string;
  stats: {
    total_articles: number;
    passed_count: number;
    filtered_count: number;
    overall_pass_rate: number;
    manual_override_count: number;
    per_source: Array<{
      source_type: string;
      source_name: string;
      source_feed: string;
      total_articles: number;
      passed_count: number;
      filtered_count: number;
      pass_rate: number;
    }>;
    weekly_trend: Array<{
      week_label: string;
      total_articles: number;
      passed_count: number;
      pass_rate: number;
    }>;
  };
  assessment: {
    passed_quality: string;
    filtered_quality: string;
    source_quality: string;
    coverage_gaps: string;
    noise_patterns: string;
    volume_health: string;
  };
  manual_override_assessment: {
    summary: string;
    false_positives: string;
    false_negatives: string;
    patterns: string;
    suggested_focus: string;
  } | null;
  pipeline_recommendations: {
    satisfied: boolean;
    feedback: string;
    issues: Record<string, string>;
    suggested_changes: Array<Record<string, string>>;
  };
  source_recommendations: {
    satisfied: boolean;
    feedback: string;
    suggested_changes: Array<{
      action: string;
      source_type: string;
      source_feeds: string[];
      reason: string;
      coverage_gap_description: string;
    }>;
  };
  proposed_new_sources: Array<Record<string, string>>;
  current_config_snapshot: { sources: SourceSpec[]; blocks: PipelineBlock[] };
  generated_at: string;
}

export interface AuditDetail extends AuditSummary {
  report: AuditReport | null;
  proposed_config: { sources: SourceSpec[]; blocks: PipelineBlock[]; _summary?: string } | null;
}

export interface ApplyAuditResult {
  saved: boolean;
  summary: string;
  proposed_config: { sources: SourceSpec[]; blocks: PipelineBlock[] };
  feed?: Feed;
}
