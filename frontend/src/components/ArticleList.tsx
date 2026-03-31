import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Article, Feed } from "../types";
import { ArticleCard } from "./ArticleCard";

interface Props {
  feed: Feed;
  onPollTriggered: () => void;
}

export function ArticleList({ feed, onPollTriggered }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [showFailed, setShowFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api.articles
      .list(feed.id, showFailed ? false : true)
      .then(setArticles)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [feed.id, showFailed]);

  async function triggerPoll() {
    setPolling(true);
    try {
      await api.feeds.poll(feed.id);
      onPollTriggered();
      await sleep(2000);
      const updated = await api.articles.list(feed.id, showFailed ? false : true);
      setArticles(updated);
    } catch (e) {
      alert(`Poll error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setPolling(false);
    }
  }

  return (
    <div style={styles.container}>
      <div style={styles.toolbar}>
        <div style={styles.tabs}>
          <button
            style={{ ...styles.tab, ...(showFailed ? {} : styles.tabActive) }}
            onClick={() => setShowFailed(false)}
          >
            Passed
          </button>
          <button
            style={{ ...styles.tab, ...(showFailed ? styles.tabActive : {}) }}
            onClick={() => setShowFailed(true)}
          >
            Filtered out
          </button>
        </div>
        <button onClick={triggerPoll} disabled={polling} style={styles.pollBtn}>
          {polling ? "Polling…" : "Poll now"}
        </button>
      </div>

      {loading && <p style={styles.empty}>Loading…</p>}
      {error && <p style={{ ...styles.empty, color: "#ff3b30" }}>{error}</p>}
      {!loading && !error && articles.length === 0 && (
        <p style={styles.empty}>
          {showFailed ? "No filtered articles yet." : "No articles yet. Try polling now."}
        </p>
      )}

      <div style={styles.list}>
        {articles.map((a) => (
          <div key={a.id} style={styles.cardWrapper}>
            <ArticleCard article={a} showPipelineResult={showFailed} />
          </div>
        ))}
      </div>
    </div>
  );
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

const styles: Record<string, React.CSSProperties> = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  toolbar: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "12px 20px",
    borderBottom: "1px solid #e5e5ea",
    background: "#fff",
    flexShrink: 0,
  },
  tabs: { display: "flex", gap: 4 },
  tab: { background: "transparent", color: "#6e6e73", padding: "6px 14px", borderRadius: 20, fontSize: 13 },
  tabActive: { background: "#007aff", color: "#fff" },
  pollBtn: { marginLeft: "auto", background: "#34c759", color: "#fff", fontSize: 13, padding: "6px 14px" },
  empty: { textAlign: "center", color: "#8e8e93", padding: 40, fontSize: 14 },
  list: { flex: 1, overflowY: "auto" },
  cardWrapper: {
    padding: "16px 20px",
    borderBottom: "1px solid #f2f2f7",
    background: "#fff",
    transition: "background 0.1s",
  },
};

import React from "react";
