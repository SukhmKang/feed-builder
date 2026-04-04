// ─── Pipeline types ───────────────────────────────────────────────────────────

export type PipelineTier = "mini" | "medium" | "high";

export type PipelineCondition =
  | { type: "and"; conditions: PipelineCondition[] }
  | { type: "or"; conditions: PipelineCondition[] }
  | { type: "not"; condition: PipelineCondition }
  | { type: "source_type"; value: string }
  | { type: "source_name"; value: string }
  | { type: "source_url"; value: string }
  | { type: "domain"; value: string }
  | { type: "source_domain"; value: string }
  | { type: "field_equals"; field: string; value: string }
  | { type: "field_contains"; field: string; value: string }
  | { type: "field_exists"; field: string }
  | { type: "field_matches_regex"; field: string; pattern: string }
  | { type: "tag_exists"; tag: string }
  | { type: "tag_condition"; tag: string; operator: "has" | "not_has" }
  | { type: "tag_matches"; pattern: string }
  | { type: "keyword"; terms: string[]; operator: "any" | "all" }
  | { type: "length"; field: string; min: number; max: number }
  | { type: "published_after"; days_ago: number }
  | { type: "published_before"; days_ago: number }
  | { type: "llm"; prompt: string; tier?: PipelineTier };

export interface SwitchBranch {
  condition: PipelineCondition;
  blocks: PipelineBlock[];
}

export interface SourceSpec {
  type: string;
  feed: string;
}

export interface CustomBlockOption {
  name: string;
  title: string | null;
  description: string | null;
}

export type PipelineBlock =
  | { type: "keyword_filter"; include: string[]; exclude: string[] }
  | { type: "semantic_similarity"; query: string; field: string; threshold: number }
  | { type: "llm_filter"; prompt: string; tier: PipelineTier }
  | { type: "conditional"; condition: PipelineCondition; if_true: PipelineBlock[]; if_false: PipelineBlock[] }
  | { type: "switch"; branches: SwitchBranch[]; default: PipelineBlock[] }
  | { type: "custom_block"; name: string };

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
