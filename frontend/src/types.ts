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
  | { type: "similarity_score"; threshold: number; operator: "gt" | "lt" }
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

// ─── Feed / Article types ─────────────────────────────────────────────────────

export interface Feed {
  id: string;
  name: string;
  topic: string;
  status: "building" | "ready" | "error";
  notifications_enabled: boolean;
  poll_interval_hours: number;
  created_at: string | null;
  last_polled_at: string | null;
  error_message: string | null;
  config: { sources: SourceSpec[]; blocks: PipelineBlock[] } | null;
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
