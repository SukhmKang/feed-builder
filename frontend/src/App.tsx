import { useEffect, useRef, useState } from "react";
import { api, getFeedRssUrl } from "./api/client";
import { ArticleList } from "./components/ArticleList";
import { AuditTab } from "./components/AuditTab";
import { CreateFeedModal } from "./components/CreateFeedModal";
import { FeedCard } from "./components/FeedCard";
import { PipelineEditor } from "./components/PipelineEditor";
import { StoriesList } from "./components/StoriesList";
import { DEMO_MODE } from "./demoMode";
import type { Feed, PipelineBlock, SourceSpec } from "./types";

export default function App() {
  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"articles" | "stories" | "pipeline" | "audits">("articles");
  const [showCreate, setShowCreate] = useState(false);
  const [loadingFeeds, setLoadingFeeds] = useState(true);
  const [copyingRss, setCopyingRss] = useState(false);
  const [topicExpanded, setTopicExpanded] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    loadFeeds();
  }, []);

  useEffect(() => {
    setTopicExpanded(false);
  }, [selectedId]);

  // Poll building feeds every 5s until they resolve
  useEffect(() => {
    const hasBuildingFeeds = feeds.some((f) => f.status === "building");
    if (hasBuildingFeeds) {
      if (!pollingRef.current) {
        pollingRef.current = setInterval(loadFeeds, 5000);
      }
    } else {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    }
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [feeds]);

  async function loadFeeds() {
    try {
      const data = await api.feeds.list();
      setFeeds(data);
    } catch {
      // ignore transient errors
    } finally {
      setLoadingFeeds(false);
    }
  }

  function handleFeedCreated(feed: Feed) {
    setFeeds((prev) => [feed, ...prev.filter((f) => f.id !== feed.id)]);
    setSelectedId(feed.id);
    setActiveTab("articles");
    setShowCreate(false);
  }

  function handleFeedUpdated(updated: Feed) {
    setFeeds((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
  }

  function handleFeedDeleted(id: string) {
    setFeeds((prev) => prev.filter((f) => f.id !== id));
    if (selectedId === id) setSelectedId(null);
  }

  async function handleCopyRss(feedId: string) {
    try {
      await navigator.clipboard.writeText(getFeedRssUrl(feedId));
      setCopyingRss(true);
      window.setTimeout(() => setCopyingRss(false), 1200);
    } catch (err) {
      alert(`Copy RSS failed: ${err instanceof Error ? err.message : err}`);
    }
  }

  const selectedFeed = feeds.find((f) => f.id === selectedId) ?? null;

  return (
    <div style={{ ...styles.root, ...(DEMO_MODE ? { paddingTop: 30 } : {}) }}>
      {DEMO_MODE && (
        <div style={{ position: "fixed", top: 0, left: 0, right: 0, zIndex: 9999, background: "#f59e0b", color: "#1c1917", textAlign: "center", padding: "6px", fontSize: 13, fontWeight: 600 }}>
          Demo mode — all actions are disabled
        </div>
      )}
      {/* Sidebar */}
      <aside style={styles.sidebar}>
        <div style={styles.sidebarHeader}>
          <h1 style={styles.logo}>Feed Builder</h1>
          <button onClick={() => setShowCreate(true)} disabled={DEMO_MODE} style={styles.newBtn}>
            + New
          </button>
        </div>

        {loadingFeeds ? (
          <p style={styles.empty}>Loading…</p>
        ) : feeds.length === 0 ? (
          <p style={styles.empty}>No feeds yet. Create one!</p>
        ) : (
          <div style={styles.feedList}>
            {feeds.map((feed) => (
              <FeedCard
                key={feed.id}
                feed={feed}
                selected={feed.id === selectedId}
                onSelect={() => setSelectedId(feed.id)}
                onUpdated={handleFeedUpdated}
                onDeleted={() => handleFeedDeleted(feed.id)}
              />
            ))}
          </div>
        )}
      </aside>

      {/* Main panel */}
      <main style={styles.main}>
        {selectedFeed ? (
          <>
            <div style={styles.mainHeader}>
              <div>
                <h2 style={styles.feedTitle}>{selectedFeed.name}</h2>
                <div style={styles.feedTopicRow}>
                  <p
                    style={{
                      ...styles.feedTopic,
                      ...(topicExpanded ? styles.feedTopicExpanded : {}),
                    }}
                  >
                    {selectedFeed.topic}
                  </p>
                  {selectedFeed.topic.length > 120 ? (
                    <button
                      type="button"
                      style={styles.topicToggle}
                      onClick={() => setTopicExpanded((value) => !value)}
                    >
                      {topicExpanded ? "Show less" : "Show more"}
                    </button>
                  ) : null}
                </div>
              </div>
            </div>
            {selectedFeed.status === "ready" ? (
              <>
                <div style={styles.tabs}>
                  <button
                    style={{ ...styles.tab, ...(activeTab === "articles" ? styles.tabActive : {}) }}
                    onClick={() => setActiveTab("articles")}
                  >
                    Articles
                  </button>
                  <button
                    style={{ ...styles.tab, ...(activeTab === "stories" ? styles.tabActive : {}) }}
                    onClick={() => setActiveTab("stories")}
                  >
                    Stories
                  </button>
                  <button
                    style={{ ...styles.tab, ...(activeTab === "pipeline" ? styles.tabActive : {}) }}
                    onClick={() => setActiveTab("pipeline")}
                  >
                    Edit Pipeline
                  </button>
                  <button
                    style={{ ...styles.tab, ...(activeTab === "audits" ? styles.tabActive : {}) }}
                    onClick={() => setActiveTab("audits")}
                  >
                    Audits
                  </button>
                  <button
                    style={styles.toolbarBtn}
                    onClick={() => void handleCopyRss(selectedFeed.id)}
                    disabled={copyingRss}
                  >
                    {copyingRss ? "Copied RSS" : "Copy RSS"}
                  </button>
                </div>
                {activeTab === "articles" ? (
                  <ArticleList feed={selectedFeed} onPollTriggered={loadFeeds} onFeedUpdated={handleFeedUpdated} />
                ) : activeTab === "stories" ? (
                  <StoriesList feed={selectedFeed} />
                ) : activeTab === "audits" ? (
                  <AuditTab feed={selectedFeed} onFeedUpdated={handleFeedUpdated} />
                ) : (
                  <PipelineEditor
                    key={selectedFeed.id}
                    sources={(selectedFeed.config?.sources ?? []) as SourceSpec[]}
                    pipeline={(selectedFeed.config?.blocks ?? []) as PipelineBlock[]}
                    onSave={async ({ sources, pipeline, versionLabel }) => {
                      const updated = await api.feeds.update(selectedFeed.id, {
                        blocks: pipeline,
                        sources,
                        version_label: versionLabel,
                      });
                      handleFeedUpdated(updated);
                    }}
                    onFeedConfigChanged={(config) => {
                      handleFeedUpdated({ ...selectedFeed, config });
                    }}
                    feedId={selectedFeed.id}
                  />
                )}
              </>
            ) : selectedFeed.status === "building" ? (
              <div style={styles.centerMsg}>
                <div style={styles.spinner} />
                <p>Building feed…</p>
              </div>
            ) : (
              <div style={styles.centerMsg}>
                <p style={{ color: "#ff3b30" }}>
                  {selectedFeed.error_message ?? "Feed build failed"}
                </p>
              </div>
            )}
          </>
        ) : (
          <div style={styles.centerMsg}>
            <p style={{ color: "#8e8e93" }}>
              {feeds.length === 0
                ? 'Click "+ New" to create your first feed.'
                : "Select a feed to view articles."}
            </p>
          </div>
        )}
      </main>

      {showCreate && (
        <CreateFeedModal
          onCreated={handleFeedCreated}
          onClose={() => setShowCreate(false)}
        />
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  root: { display: "flex", height: "100vh", overflow: "hidden" },
  sidebar: {
    width: 320,
    flexShrink: 0,
    background: "#f5f5f7",
    borderRight: "1px solid #e5e5ea",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  sidebarHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "16px 16px 12px",
    borderBottom: "1px solid #e5e5ea",
    background: "#f5f5f7",
  },
  logo: { fontSize: 17, fontWeight: 700 },
  newBtn: { background: "#007aff", color: "#fff", fontSize: 13, padding: "6px 14px" },
  feedList: { flex: 1, overflowY: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 10 },
  empty: { padding: 24, color: "#8e8e93", fontSize: 13, textAlign: "center" },
  main: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: "#fff" },
  mainHeader: {
    padding: "20px 24px 16px",
    borderBottom: "1px solid #e5e5ea",
    background: "#fff",
  },
  feedTitle: { fontSize: 18, fontWeight: 600, marginBottom: 2 },
  feedTopicRow: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    gap: 4,
  },
  feedTopic: {
    fontSize: 13,
    color: "#6e6e73",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
    maxWidth: "100%",
    margin: 0,
  },
  feedTopicExpanded: {
    whiteSpace: "normal",
    overflow: "visible",
    textOverflow: "clip",
  },
  topicToggle: {
    border: "none",
    background: "transparent",
    padding: 0,
    color: "#007aff",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
  },
  tabs: {
    display: "flex",
    alignItems: "center",
    gap: 4,
    borderBottom: "1px solid #e5e5ea",
    padding: "0 24px",
    flexShrink: 0,
  },
  tab: {
    padding: "10px 16px",
    fontSize: 13,
    fontWeight: 500,
    border: "none",
    borderBottom: "2px solid transparent",
    background: "transparent",
    color: "#6e6e73",
    cursor: "pointer",
    marginBottom: -1,
    fontFamily: "inherit",
  },
  tabActive: {
    color: "#007aff",
    borderBottomColor: "#007aff",
  },
  toolbarBtn: {
    marginLeft: "auto",
    fontSize: 12,
    padding: "6px 12px",
    background: "#eef4ff",
    color: "#007aff",
    borderRadius: 999,
  },
  centerMsg: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: 16,
    color: "#3a3a3c",
    fontSize: 14,
  },
  spinner: {
    width: 32,
    height: 32,
    border: "3px solid #e5e5ea",
    borderTopColor: "#007aff",
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
  },
};
