import type { Article, NitterMedia, NitterQuoteTweet, NitterRaw, YouTubeRaw } from "../types";

interface Props {
  article: Article;
  showPipelineResult: boolean;
}

export function ArticleCard({ article, showPipelineResult }: Props) {
  const a = article.article;
  const type = a.source_type;

  let card: React.ReactNode;
  if (type === "nitter") {
    card = <TweetCard article={article} />;
  } else if (type === "youtube") {
    card = <YouTubeCard article={article} />;
  } else if (type === "reddit") {
    card = <RedditCard article={article} />;
  } else {
    card = <RSSCard article={article} />;
  }

  return (
    <div>
      {card}
      {showPipelineResult && article.pipeline_result?.block_results?.length > 0 && (
        <PipelineTrace result={article.pipeline_result} />
      )}
      {a.tags?.length > 0 && (
        <div style={cardStyles.tags}>
          {a.tags.map((t) => (
            <span key={t} style={cardStyles.tag}>{t}</span>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Tweet ────────────────────────────────────────────────────────────────────

function TweetCard({ article }: { article: Article }) {
  const a = article.article;
  const raw = a.raw as NitterRaw | undefined;
  const username = raw?.username ?? a.source_name;
  const tweetUrl = a.url;

  return (
    <div style={tweetStyles.card}>
      <div style={tweetStyles.header}>
        <div style={tweetStyles.avatar}>{username[0]?.toUpperCase() ?? "T"}</div>
        <div>
          <div style={tweetStyles.username}>@{username}</div>
          <div style={tweetStyles.date}>{formatDate(a.published_at)}</div>
        </div>
        <a href={tweetUrl} target="_blank" rel="noreferrer" style={tweetStyles.xLink}>
          𝕏
        </a>
      </div>

      <p style={tweetStyles.text}>{raw?.text ?? a.content}</p>

      {raw?.media && raw.media.length > 0 && (
        <MediaGrid media={raw.media} />
      )}

      {raw?.quote_tweet && <QuoteTweet qt={raw.quote_tweet} />}
    </div>
  );
}

function MediaGrid({ media }: { media: NitterMedia[] }) {
  const items = media.slice(0, 4);
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${Math.min(items.length, 2)}, minmax(0, 1fr))`,
        gap: 4,
        marginTop: 10,
        borderRadius: 10,
        overflow: "hidden",
        maxWidth: 360,
      }}
    >
      {items.map((m, i) => (
        <div key={i} style={{ position: "relative", background: "#000", aspectRatio: "4/3", maxHeight: 140 }}>
          {m.thumbnail_url && (
            <img
              src={m.thumbnail_url}
              alt=""
              style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
              onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
            />
          )}
          {(m.content_type === "video" || m.content_type === "gif") && (
            <div style={tweetStyles.playOverlay}>▶</div>
          )}
          {m.duration && (
            <span style={tweetStyles.duration}>{m.duration}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function QuoteTweet({ qt }: { qt: NitterQuoteTweet }) {
  return (
    <a href={qt.url} target="_blank" rel="noreferrer" style={tweetStyles.quoteTweet}>
      <div style={tweetStyles.quoteHeader}>
        <strong>@{qt.username}</strong>
        {qt.display_name && qt.display_name !== qt.username && (
          <span style={{ color: "#536471", fontWeight: 400 }}> · {qt.display_name}</span>
        )}
      </div>
      <p style={tweetStyles.quoteText}>{qt.text}</p>
    </a>
  );
}

const tweetStyles: Record<string, React.CSSProperties> = {
  card: {
    borderLeft: "3px solid #1d9bf0",
    paddingLeft: 14,
  },
  header: { display: "flex", alignItems: "center", gap: 10, marginBottom: 8 },
  avatar: {
    width: 36, height: 36, borderRadius: "50%",
    background: "#1d9bf0", color: "#fff",
    display: "flex", alignItems: "center", justifyContent: "center",
    fontWeight: 700, fontSize: 15, flexShrink: 0,
  },
  username: { fontWeight: 700, fontSize: 14, color: "#0f1419" },
  date: { fontSize: 12, color: "#536471" },
  xLink: { marginLeft: "auto", color: "#0f1419", textDecoration: "none", fontSize: 17, fontWeight: 900 },
  text: { fontSize: 15, lineHeight: 1.5, color: "#0f1419", margin: 0, whiteSpace: "pre-wrap" },
  playOverlay: {
    position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
    background: "rgba(0,0,0,0.35)", color: "#fff", fontSize: 28,
  },
  duration: {
    position: "absolute", bottom: 6, right: 8,
    background: "rgba(0,0,0,0.7)", color: "#fff", fontSize: 11,
    padding: "2px 5px", borderRadius: 4,
  },
  quoteTweet: {
    display: "block", marginTop: 10, padding: 12,
    border: "1px solid #cfd9de", borderRadius: 12,
    color: "inherit", textDecoration: "none",
  },
  quoteHeader: { fontSize: 13, fontWeight: 700, marginBottom: 4 },
  quoteText: { fontSize: 13, color: "#536471", margin: 0, lineHeight: 1.4 },
};

// ─── YouTube ──────────────────────────────────────────────────────────────────

function YouTubeCard({ article }: { article: Article }) {
  const a = article.article;
  const raw = a.raw as YouTubeRaw | undefined;

  const videoId = raw?.video_id ?? extractYouTubeId(a.url);
  const thumbnailUrl = videoId
    ? `https://img.youtube.com/vi/${videoId}/mqdefault.jpg`
    : null;
  const channelName = raw?.channel_name ?? a.source_name;
  const transcript = raw?.transcript?.text ?? a.full_text ?? "";

  return (
    <div style={ytStyles.card}>
      {thumbnailUrl && (
        <a href={a.url} target="_blank" rel="noreferrer" style={ytStyles.thumbLink}>
          <img src={thumbnailUrl} alt={a.title} style={ytStyles.thumb} />
          <div style={ytStyles.playBtn}>▶</div>
        </a>
      )}
      <div style={ytStyles.body}>
        <a href={a.url} target="_blank" rel="noreferrer" style={ytStyles.title}>
          {a.title}
        </a>
        <div style={ytStyles.channel}>
          <span style={ytStyles.channelBadge}>▶</span>
          {channelName}
          <span style={ytStyles.dot}>·</span>
          <span style={ytStyles.date}>{formatDate(a.published_at)}</span>
        </div>
        {transcript && (
          <Collapsible label="Transcript">
            <p style={ytStyles.transcript}>{truncate(transcript, 600)}</p>
          </Collapsible>
        )}
      </div>
    </div>
  );
}

function extractYouTubeId(url: string): string | null {
  try {
    const u = new URL(url);
    return u.searchParams.get("v");
  } catch {
    return null;
  }
}

const ytStyles: Record<string, React.CSSProperties> = {
  card: { display: "flex", gap: 14 },
  thumbLink: { position: "relative", flexShrink: 0, display: "block", width: 160, height: 90, borderRadius: 8, overflow: "hidden", background: "#000" },
  thumb: { width: "100%", height: "100%", objectFit: "cover", display: "block" },
  playBtn: {
    position: "absolute", inset: 0,
    display: "flex", alignItems: "center", justifyContent: "center",
    background: "rgba(0,0,0,0.3)", color: "#fff", fontSize: 24,
    opacity: 0,
    transition: "opacity 0.15s",
  },
  body: { flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 },
  title: { fontSize: 14, fontWeight: 600, color: "#0f0f0f", textDecoration: "none", lineHeight: 1.4 },
  channel: { display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#606060" },
  channelBadge: { color: "#ff0000", fontSize: 10 },
  dot: { color: "#ccc" },
  date: {},
  transcript: { fontSize: 12, color: "#3a3a3c", lineHeight: 1.6, whiteSpace: "pre-wrap" },
};

// ─── Reddit ───────────────────────────────────────────────────────────────────

function RedditCard({ article }: { article: Article }) {
  const a = article.article;
  const subreddit = a.source_name.startsWith("r/") ? a.source_name : `r/${a.source_name}`;

  return (
    <div style={redditStyles.card}>
      <div style={redditStyles.header}>
        <span style={redditStyles.subreddit}>{subreddit}</span>
        <span style={redditStyles.dot}>·</span>
        <span style={redditStyles.date}>{formatDate(a.published_at)}</span>
      </div>
      <a href={a.url} target="_blank" rel="noreferrer" style={redditStyles.title}>
        {a.title}
      </a>
      {a.content && a.content !== a.title && (
        <Collapsible label="Post body">
          <p style={redditStyles.body}>{truncate(stripHtml(a.content), 400)}</p>
        </Collapsible>
      )}
    </div>
  );
}

const redditStyles: Record<string, React.CSSProperties> = {
  card: { borderLeft: "3px solid #ff4500", paddingLeft: 14 },
  header: { display: "flex", alignItems: "center", gap: 6, marginBottom: 6 },
  subreddit: { fontWeight: 700, fontSize: 12, color: "#ff4500" },
  dot: { color: "#ccc", fontSize: 10 },
  date: { fontSize: 12, color: "#878a8c" },
  title: { fontSize: 15, fontWeight: 600, color: "#1c1c1c", textDecoration: "none", lineHeight: 1.4, display: "block" },
  body: { fontSize: 13, color: "#3c3c3c", lineHeight: 1.5, marginTop: 8 },
};

// ─── RSS / Google News (default) ─────────────────────────────────────────────

function RSSCard({ article }: { article: Article }) {
  const a = article.article;
  const isGoogleNews = a.source_type === "google_news";

  return (
    <div style={rssStyles.card}>
      <div style={rssStyles.header}>
        <span style={{ ...rssStyles.badge, background: isGoogleNews ? "#4285f4" : "#e5e5ea", color: isGoogleNews ? "#fff" : "#6e6e73" }}>
          {isGoogleNews ? "Google News" : a.source_name}
        </span>
        <span style={rssStyles.date}>{formatDate(a.published_at)}</span>
      </div>
      <a href={a.url} target="_blank" rel="noreferrer" style={rssStyles.title}>
        {a.title}
      </a>
      {a.content && a.content !== a.title && (
        <p style={rssStyles.excerpt}>{truncate(stripHtml(a.content), 300)}</p>
      )}
      {!isGoogleNews && (
        <span style={rssStyles.sourceName}>{a.source_name}</span>
      )}
    </div>
  );
}

const rssStyles: Record<string, React.CSSProperties> = {
  card: {},
  header: { display: "flex", alignItems: "center", gap: 8, marginBottom: 8 },
  badge: { fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 4 },
  date: { fontSize: 12, color: "#8e8e93", marginLeft: "auto" },
  title: { fontSize: 15, fontWeight: 600, color: "#1d1d1f", textDecoration: "none", lineHeight: 1.4, display: "block", marginBottom: 6 },
  excerpt: { fontSize: 13, color: "#3a3a3c", lineHeight: 1.5, margin: 0 },
  sourceName: { fontSize: 11, color: "#8e8e93", display: "block", marginTop: 6 },
};

// ─── Shared sub-components ────────────────────────────────────────────────────

function Collapsible({ label, children }: { label: string; children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div style={{ marginTop: 6 }}>
      <button onClick={() => setOpen((v) => !v)} style={collapsibleStyles.btn}>
        {open ? "▾" : "▸"} {label}
      </button>
      {open && <div style={collapsibleStyles.body}>{children}</div>}
    </div>
  );
}

const collapsibleStyles: Record<string, React.CSSProperties> = {
  btn: { background: "transparent", color: "#007aff", fontSize: 12, padding: "2px 0", borderRadius: 0 },
  body: { marginTop: 6 },
};

function PipelineTrace({ result }: { result: Article["pipeline_result"] }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div style={pipelineStyles.container}>
      <button onClick={() => setOpen((v) => !v)} style={pipelineStyles.btn}>
        {open ? "▾" : "▸"} Pipeline trace
        {result.dropped_at && (
          <span style={pipelineStyles.droppedBadge}>dropped at {result.dropped_at}</span>
        )}
      </button>
      {open && (
        <div style={pipelineStyles.results}>
          {result.block_results.map((br, i) => (
            <div key={i} style={pipelineStyles.row}>
              <span style={{ color: br.passed ? "#34c759" : "#ff3b30", marginRight: 6 }}>
                {br.passed ? "✓" : "✗"}
              </span>
              {br.reason}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const pipelineStyles: Record<string, React.CSSProperties> = {
  container: { marginTop: 10, paddingTop: 10, borderTop: "1px solid #f2f2f7" },
  btn: { background: "transparent", color: "#8e8e93", fontSize: 12, padding: "2px 0", borderRadius: 0 },
  droppedBadge: { marginLeft: 8, background: "#fff2f0", color: "#ff3b30", fontSize: 11, padding: "1px 6px", borderRadius: 4 },
  results: { marginTop: 8, display: "flex", flexDirection: "column", gap: 4, background: "#f9f9f9", borderRadius: 8, padding: 10 },
  row: { fontSize: 12, color: "#3a3a3c", lineHeight: 1.4 },
};

// ─── Shared helpers ───────────────────────────────────────────────────────────

import React from "react";

const cardStyles: Record<string, React.CSSProperties> = {
  tags: { display: "flex", flexWrap: "wrap", gap: 4, marginTop: 10 },
  tag: { fontSize: 10, background: "#f2f2f7", color: "#6e6e73", padding: "2px 7px", borderRadius: 4 },
};

function formatDate(iso: string): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const diffMs = Date.now() - d.getTime();
    const diffH = diffMs / 3600000;
    if (diffH < 1) return "just now";
    if (diffH < 24) return `${Math.floor(diffH)}h ago`;
    if (diffH < 24 * 7) return `${Math.floor(diffH / 24)}d ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

function truncate(text: string, max: number): string {
  const t = text.trim();
  return t.length <= max ? t : t.slice(0, max - 1).trimEnd() + "…";
}

function stripHtml(html: string): string {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}
