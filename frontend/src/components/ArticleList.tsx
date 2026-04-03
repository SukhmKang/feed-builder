import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Article, Feed } from "../types";
import { ArticleCard } from "./ArticleCard";

interface Props {
  feed: Feed;
  onPollTriggered: () => void;
}

const PAGE_SIZE = 50;

export function ArticleList({ feed, onPollTriggered }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [showFailed, setShowFailed] = useState(false);
  const [page, setPage] = useState(0);
  const [hasNextPage, setHasNextPage] = useState(false);
  const [lookbackHours, setLookbackHours] = useState("");
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPage(0);
  }, [feed.id, showFailed]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api.articles
      .list(feed.id, showFailed ? false : true, page * PAGE_SIZE, PAGE_SIZE)
      .then((items) => {
        setArticles(items);
        setHasNextPage(items.length === PAGE_SIZE);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [feed.id, showFailed, page]);

  async function triggerPoll() {
    setPolling(true);
    try {
      const normalizedLookback = lookbackHours.trim();
      const lookbackOverride =
        normalizedLookback === "" ? undefined : Number.parseInt(normalizedLookback, 10);

      if (lookbackOverride !== undefined && (!Number.isFinite(lookbackOverride) || lookbackOverride < 1)) {
        throw new Error("Lookback must be a whole number of hours");
      }

      await api.feeds.poll(feed.id, lookbackOverride);
      onPollTriggered();
      await sleep(2000);
      const updated = await api.articles.list(feed.id, showFailed ? false : true, page * PAGE_SIZE, PAGE_SIZE);
      setArticles(updated);
      setHasNextPage(updated.length === PAGE_SIZE);
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
        <label style={styles.lookbackLabel}>
          Lookback (h)
          <input
            value={lookbackHours}
            onChange={(event) => setLookbackHours(event.target.value.replace(/[^\d]/g, ""))}
            placeholder={String(feed.poll_interval_hours * 3)}
            inputMode="numeric"
            style={styles.lookbackInput}
          />
        </label>
        <button onClick={triggerPoll} disabled={polling} style={styles.pollBtn}>
          {polling ? "Polling…" : "Poll now"}
        </button>
      </div>

      <div style={styles.pagination}>
        <button onClick={() => setPage((value) => Math.max(0, value - 1))} disabled={loading || page === 0}>
          Previous
        </button>
        <span style={styles.pageLabel}>Page {page + 1}</span>
        <button onClick={() => setPage((value) => value + 1)} disabled={loading || !hasNextPage}>
          Next
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
  lookbackLabel: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    fontSize: 12,
    color: "#6e6e73",
    marginLeft: "auto",
  },
  lookbackInput: {
    width: 72,
    padding: "6px 8px",
    fontSize: 13,
    border: "1px solid #d1d1d6",
    borderRadius: 8,
    background: "#fff",
  },
  pollBtn: { background: "#34c759", color: "#fff", fontSize: 13, padding: "6px 14px" },
  pagination: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
    padding: "10px 20px",
    borderBottom: "1px solid #f2f2f7",
    background: "#fff",
    flexShrink: 0,
  },
  pageLabel: { fontSize: 13, color: "#6e6e73", minWidth: 60, textAlign: "center" },
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
