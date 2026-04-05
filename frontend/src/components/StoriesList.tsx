import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { DEMO_MODE } from "../demoMode";
import type { Feed, StoryDetail, StorySummary } from "../types";

interface Props {
  feed: Feed;
}

// ── helpers ──────────────────────────────────────────────────────────────────

const SOURCE_PALETTE = [
  "#1967D2", "#0D7377", "#5E35B1", "#C62828",
  "#2E7D32", "#E65100", "#00838F", "#6A1B9A",
  "#AD1457", "#37474F",
];

function sourceColor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
  return SOURCE_PALETTE[Math.abs(h) % SOURCE_PALETTE.length];
}

function timeAgo(value: string | null | undefined): string {
  if (!value) return "";
  try {
    const ms = Date.now() - new Date(value).getTime();
    const m = Math.floor(ms / 60_000);
    if (m < 2) return "just now";
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} hr ago`;
    const d = Math.floor(h / 24);
    if (d < 7) return `${d}d ago`;
    return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

// ── sub-components ────────────────────────────────────────────────────────────

function SourceBadge({ name }: { name: string }) {
  const bg = sourceColor(name);
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 18,
        height: 18,
        borderRadius: 4,
        background: bg,
        color: "#fff",
        fontSize: 10,
        fontWeight: 700,
        flexShrink: 0,
        letterSpacing: 0,
      }}
    >
      {(name || "?")[0].toUpperCase()}
    </span>
  );
}

interface SourceLineProps {
  name: string;
  time?: string | null;
}

function SourceLine({ name, time }: SourceLineProps) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
      <SourceBadge name={name} />
      <span style={{ fontSize: 13, fontWeight: 600, color: "#3C4043" }}>{name}</span>
      {time && (
        <>
          <span style={{ color: "#DADCE0", fontSize: 11 }}>·</span>
          <span style={{ fontSize: 12, color: "#70757A" }}>{time}</span>
        </>
      )}
    </div>
  );
}

// ── story card ────────────────────────────────────────────────────────────────

interface StoryCardProps {
  story: StorySummary;
  detail: StoryDetail | null;
  isLoadingDetail: boolean;
  onToggle: () => void;
  onRename: (title: string) => Promise<void>;
}

function StoryCard({ story, detail, isLoadingDetail, onToggle, onRename }: StoryCardProps) {
  const rep = story.representative_article;
  const isExpanded = !!detail;
  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState(story.title);
  const [savingTitle, setSavingTitle] = useState(false);

  const relatedArticles = detail
    ? detail.articles.filter((a) => a.id !== rep?.id)
    : [];

  const [footerHover, setFooterHover] = useState(false);

  useEffect(() => {
    setDraftTitle(story.title);
  }, [story.title]);

  async function saveTitle() {
    const normalized = draftTitle.trim();
    if (!normalized || normalized === story.title || savingTitle) {
      if (normalized === story.title) setEditingTitle(false);
      return;
    }
    setSavingTitle(true);
    try {
      await onRename(normalized);
      setEditingTitle(false);
    } finally {
      setSavingTitle(false);
    }
  }

  return (
    <article style={S.card}>
      {/* Header: story title */}
      <div style={S.cardHeader}>
        {editingTitle ? (
          <div style={S.titleEditor}>
            <input
              value={draftTitle}
              onChange={(event) => setDraftTitle(event.target.value)}
              onKeyDown={(event) => {
                if (DEMO_MODE) return;
                if (event.key === "Enter") {
                  event.preventDefault();
                  void saveTitle();
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setDraftTitle(story.title);
                  setEditingTitle(false);
                }
              }}
              autoFocus
              maxLength={160}
              style={S.titleInput}
            />
            <div style={S.titleEditorActions}>
              <button type="button" style={S.titleActionPrimary} onClick={() => void saveTitle()} disabled={DEMO_MODE || savingTitle}>
                {savingTitle ? "…" : "Save"}
              </button>
              <button
                type="button"
                style={S.titleActionSecondary}
                onClick={() => {
                  setDraftTitle(story.title);
                  setEditingTitle(false);
                }}
                disabled={DEMO_MODE || savingTitle}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div style={S.titleRow}>
            {rep ? (
              <a href={rep.url} target="_blank" rel="noreferrer" style={S.storyTitleLink}>
                {story.title}
                <span style={S.arrow}> ›</span>
              </a>
            ) : (
              <span style={S.storyTitlePlain}>{story.title}</span>
            )}
            <button
              type="button"
              style={S.editTitleButton}
              onClick={() => setEditingTitle(true)}
              disabled={DEMO_MODE}
            >
              Edit
            </button>
          </div>
        )}
        <span style={S.headerTime}>
          {timeAgo(story.last_published_at ?? story.updated_at ?? null)}
        </span>
      </div>

      {/* Body */}
      {rep ? (
        <div style={S.cardBody}>
          {/* Featured article — left */}
          <div style={S.featuredCol}>
            <SourceLine name={rep.source_name} time={timeAgo(rep.published_at)} />
            <FeaturedHeadline url={rep.url} title={rep.title} />
          </div>

          {/* Right column: summary or related articles */}
          <div style={S.sideCol}>
            {isExpanded && relatedArticles.length > 0 ? (
              relatedArticles.slice(0, 3).map((a, i) => (
                <RelatedRow
                  key={a.id + i}
                  url={a.url}
                  title={a.title}
                  sourceName={a.source_name}
                  time={timeAgo(a.published_at)}
                  divider={i < Math.min(relatedArticles.length, 3) - 1}
                />
              ))
            ) : (
              <p style={S.summary}>{story.summary}</p>
            )}
          </div>
        </div>
      ) : (
        <div style={{ padding: "12px 20px 4px" }}>
          <p style={S.summary}>{story.summary}</p>
        </div>
      )}

      {/* Overflow articles when expanded */}
      {isExpanded && relatedArticles.length > 3 && (
        <OverflowSection articles={relatedArticles.slice(3)} storyId={story.id} />
      )}

      {/* Footer expand/collapse — hidden for single-article stories */}
      {(story.article_count > 1 || isExpanded) && (
        <button
          style={{ ...S.footer, ...(footerHover ? S.footerHover : {}) }}
          onClick={onToggle}
          disabled={isLoadingDetail}
          onMouseEnter={() => setFooterHover(true)}
          onMouseLeave={() => setFooterHover(false)}
        >
          <NewsIcon />
          <span>
            {isLoadingDetail
              ? "Loading…"
              : isExpanded
              ? "Collapse"
              : "See more headlines & perspectives"}
          </span>
          {!isExpanded && !isLoadingDetail && (
            <span style={S.countBadge}>{story.article_count}</span>
          )}
        </button>
      )}
    </article>
  );
}

function FeaturedHeadline({ url, title }: { url: string; title: string }) {
  const [hover, setHover] = useState(false);
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      style={{ ...S.featuredHeadline, ...(hover ? S.featuredHeadlineHover : {}) }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {title}
    </a>
  );
}

interface RelatedRowProps {
  url: string;
  title: string;
  sourceName: string;
  time: string;
  divider: boolean;
}

function RelatedRow({ url, title, sourceName, time, divider }: RelatedRowProps) {
  const [hover, setHover] = useState(false);
  return (
    <div style={{ ...(divider ? S.relatedDivider : {}) }}>
      <SourceLine name={sourceName} time={time} />
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        style={{ ...S.relatedHeadline, ...(hover ? S.relatedHeadlineHover : {}) }}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
      >
        {title}
      </a>
    </div>
  );
}

interface OverflowSectionProps {
  articles: StoryDetail["articles"];
  storyId: string;
}

function OverflowSection({ articles, storyId }: OverflowSectionProps) {
  return (
    <div style={S.overflow}>
      {articles.map((a, i) => {
        const isLast = i === articles.length - 1;
        return (
          <OverflowRow
            key={`${storyId}:${a.id}:${i}`}
            url={a.url}
            title={a.title}
            sourceName={a.source_name}
            time={timeAgo(a.published_at)}
            last={isLast}
          />
        );
      })}
    </div>
  );
}

function OverflowRow({
  url,
  title,
  sourceName,
  time,
  last,
}: {
  url: string;
  title: string;
  sourceName: string;
  time: string;
  last: boolean;
}) {
  const [hover, setHover] = useState(false);
  return (
    <div style={{ ...S.overflowRow, ...(last ? {} : S.overflowRowDivider) }}>
      <SourceLine name={sourceName} time={time} />
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        style={{ ...S.overflowHeadline, ...(hover ? S.relatedHeadlineHover : {}) }}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
      >
        {title}
      </a>
    </div>
  );
}

function NewsIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="#1558D6"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0 }}
    >
      <path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2" />
      <path d="M18 14h-8" />
      <path d="M15 18h-5" />
      <path d="M10 6h8v4h-8V6Z" />
    </svg>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export function StoriesList({ feed }: Props) {
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [expanded, setExpanded] = useState<Record<string, StoryDetail | null>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    setLoading(true);
    setError(null);
    setExpanded({});
    api.stories
      .list(feed.id)
      .then((data) => { if (mountedRef.current) setStories(data); })
      .catch((err: Error) => { if (mountedRef.current) setError(err.message); })
      .finally(() => { if (mountedRef.current) setLoading(false); });
    return () => { mountedRef.current = false; };
  }, [feed.id]);

  async function toggleStory(story: StorySummary) {
    if (expanded[story.id]) {
      setExpanded((prev) => {
        const next = { ...prev };
        delete next[story.id];
        return next;
      });
      return;
    }
    setLoadingId(story.id);
    try {
      const detail = await api.stories.get(feed.id, story.id);
      setExpanded((prev) => ({ ...prev, [story.id]: detail }));
    } catch (err) {
      alert(`Failed to load story: ${err instanceof Error ? err.message : err}`);
    } finally {
      setLoadingId(null);
    }
  }

  async function renameStory(storyId: string, title: string) {
    try {
      const updated = await api.stories.update(feed.id, storyId, { title });
      setStories((prev) => prev.map((story) => (story.id === storyId ? { ...story, title: updated.title } : story)));
      setExpanded((prev) => {
        const current = prev[storyId];
        if (!current) return prev;
        return { ...prev, [storyId]: { ...current, title: updated.title } };
      });
    } catch (err) {
      alert(`Failed to rename story: ${err instanceof Error ? err.message : err}`);
      throw err;
    }
  }

  if (loading) {
    return (
      <div style={S.center}>
        <div style={S.spinner} />
        <p style={S.centerText}>Loading stories…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div style={S.center}>
        <p style={{ ...S.centerText, color: "#D32F2F" }}>{error}</p>
      </div>
    );
  }

  if (stories.length === 0) {
    return (
      <div style={S.center}>
        <p style={S.centerText}>No stories yet. Poll again after new articles come in.</p>
      </div>
    );
  }

  return (
    <div style={S.container}>
      <div style={S.feed}>
        {stories.map((story) => (
          <StoryCard
            key={story.id}
            story={story}
            detail={expanded[story.id] ?? null}
            isLoadingDetail={loadingId === story.id}
            onToggle={() => void toggleStory(story)}
            onRename={(title) => renameStory(story.id, title)}
          />
        ))}
      </div>
    </div>
  );
}

// ── styles ────────────────────────────────────────────────────────────────────

const S: Record<string, React.CSSProperties> = {
  container: {
    flex: 1,
    overflowY: "auto",
    background: "#F8F9FA",
    height: "100%",
  },
  feed: {
    maxWidth: 880,
    margin: "0 auto",
    padding: "20px 16px 48px",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  center: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: 12,
    background: "#F8F9FA",
    height: "100%",
  },
  centerText: {
    color: "#70757A",
    fontSize: 14,
    margin: 0,
  },
  spinner: {
    width: 28,
    height: 28,
    border: "2.5px solid #DADCE0",
    borderTopColor: "#1967D2",
    borderRadius: "50%",
    animation: "spin 0.75s linear infinite",
  },

  // Card
  card: {
    background: "#FFFFFF",
    borderRadius: 12,
    overflow: "hidden",
    border: "1px solid #E8EAED",
    boxShadow: "0 1px 2px rgba(60,64,67,0.04), 0 2px 6px rgba(60,64,67,0.06)",
  },
  cardHeader: {
    padding: "16px 20px 0",
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
  },
  titleRow: {
    display: "flex",
    alignItems: "flex-start",
    gap: 10,
    flex: 1,
    minWidth: 0,
  },
  titleEditor: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    flex: 1,
    minWidth: 0,
  },
  titleInput: {
    fontSize: 16,
    lineHeight: 1.35,
    padding: "8px 10px",
    border: "1px solid #DADCE0",
    borderRadius: 8,
    width: "100%",
  },
  titleEditorActions: {
    display: "flex",
    gap: 8,
  },
  titleActionPrimary: {
    fontSize: 12,
    padding: "5px 10px",
    background: "#1558D6",
    color: "#fff",
  },
  titleActionSecondary: {
    fontSize: 12,
    padding: "5px 10px",
    background: "transparent",
    color: "#5F6368",
  },
  storyTitleLink: {
    fontSize: 18,
    fontWeight: 400,
    color: "#1558D6",
    textDecoration: "none",
    lineHeight: 1.35,
    letterSpacing: "-0.01em",
    flex: 1,
    minWidth: 0,
  },
  storyTitlePlain: {
    fontSize: 18,
    fontWeight: 400,
    color: "#202124",
    lineHeight: 1.35,
    letterSpacing: "-0.01em",
    flex: 1,
    minWidth: 0,
  },
  editTitleButton: {
    fontSize: 11,
    padding: "2px 8px",
    background: "#EEF4FF",
    color: "#1558D6",
    borderRadius: 999,
    flexShrink: 0,
  },
  arrow: {
    fontSize: 19,
  },
  headerTime: {
    fontSize: 12,
    color: "#70757A",
    flexShrink: 0,
    paddingTop: 3,
    whiteSpace: "nowrap",
  },

  // Card body
  cardBody: {
    padding: "14px 20px 6px",
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 0,
    alignItems: "start",
  },
  featuredCol: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    paddingRight: 20,
    borderRight: "1px solid #E8EAED",
  },
  sideCol: {
    paddingLeft: 20,
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },

  featuredHeadline: {
    fontSize: 16,
    fontWeight: 400,
    color: "#202124",
    textDecoration: "none",
    lineHeight: 1.45,
    letterSpacing: "-0.01em",
    display: "block",
    transition: "color 0.1s",
  },
  featuredHeadlineHover: {
    color: "#1558D6",
  },

  relatedDivider: {
    paddingTop: 12,
    borderTop: "1px solid #E8EAED",
  },
  relatedHeadline: {
    fontSize: 14,
    fontWeight: 400,
    color: "#202124",
    textDecoration: "none",
    lineHeight: 1.4,
    display: "block",
    marginTop: 4,
    transition: "color 0.1s",
  },
  relatedHeadlineHover: {
    color: "#1558D6",
  },

  summary: {
    margin: 0,
    fontSize: 13,
    color: "#5F6368",
    lineHeight: 1.65,
  },

  // Overflow section
  overflow: {
    borderTop: "1px solid #E8EAED",
    padding: "14px 20px 2px",
    display: "flex",
    flexDirection: "column",
  },
  overflowRow: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
    paddingBottom: 12,
  },
  overflowRowDivider: {
    borderBottom: "1px solid #E8EAED",
    marginBottom: 12,
  },
  overflowHeadline: {
    fontSize: 14,
    fontWeight: 400,
    color: "#202124",
    textDecoration: "none",
    lineHeight: 1.4,
    display: "block",
    transition: "color 0.1s",
  },

  // Footer
  footer: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    width: "100%",
    padding: "10px 20px 10px",
    background: "none",
    border: "none",
    borderTop: "1px solid #E8EAED",
    cursor: "pointer",
    color: "#5F6368",
    fontSize: 13,
    fontWeight: 500,
    textAlign: "left",
    transition: "background 0.15s",
    fontFamily: "inherit",
    marginTop: 6,
  },
  footerHover: {
    background: "#F8F9FA",
  },

  countBadge: {
    marginLeft: "auto",
    fontSize: 11,
    background: "#E8F0FE",
    color: "#1558D6",
    borderRadius: 999,
    padding: "2px 9px",
    fontWeight: 600,
  },
};
