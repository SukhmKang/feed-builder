import { useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import { ArticleList } from "./components/ArticleList";
import { CreateFeedModal } from "./components/CreateFeedModal";
import { FeedCard } from "./components/FeedCard";
import type { Feed } from "./types";

export default function App() {
  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [loadingFeeds, setLoadingFeeds] = useState(true);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    loadFeeds();
  }, []);

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
    setShowCreate(false);
  }

  function handleFeedUpdated(updated: Feed) {
    setFeeds((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
  }

  function handleFeedDeleted(id: string) {
    setFeeds((prev) => prev.filter((f) => f.id !== id));
    if (selectedId === id) setSelectedId(null);
  }

  const selectedFeed = feeds.find((f) => f.id === selectedId) ?? null;

  return (
    <div style={styles.root}>
      {/* Sidebar */}
      <aside style={styles.sidebar}>
        <div style={styles.sidebarHeader}>
          <h1 style={styles.logo}>Feed Builder</h1>
          <button onClick={() => setShowCreate(true)} style={styles.newBtn}>
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
                <p style={styles.feedTopic}>{selectedFeed.topic}</p>
              </div>
            </div>
            {selectedFeed.status === "ready" ? (
              <ArticleList
                feed={selectedFeed}
                onPollTriggered={loadFeeds}
              />
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
  feedTopic: { fontSize: 13, color: "#6e6e73" },
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
