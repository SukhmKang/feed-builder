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
  config: { sources: unknown[]; pipeline: unknown[] } | null;
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
