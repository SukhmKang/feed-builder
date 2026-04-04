import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Article, Feed, PipelineVersion } from "../types";
import { ArticleCard } from "./ArticleCard";

interface Props {
  feed: Feed;
  onPollTriggered: () => void;
  onFeedUpdated?: (feed: Feed) => void;
}

const PAGE_SIZE = 50;

export function ArticleList({ feed, onPollTriggered, onFeedUpdated }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [showFailed, setShowFailed] = useState(false);
  const [page, setPage] = useState(0);
  const [hasNextPage, setHasNextPage] = useState(false);
  const [lookbackHours, setLookbackHours] = useState("");
  const [replayDays, setReplayDays] = useState("30");
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(false);
  const [replaying, setReplaying] = useState(false);
  const [reverting, setReverting] = useState(false);
  const [versions, setVersions] = useState<PipelineVersion[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.pipelineVersions.list(feed.id).then(setVersions).catch(() => {});
  }, [feed.id]);

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

  function effectivePassed(article: Article): boolean {
    if (article.manual_verdict === "passed") return true;
    if (article.manual_verdict === "filtered") return false;
    return article.passed;
  }

  async function handleVerdictChange(articleId: string, verdict: "passed" | "filtered" | null) {
    try {
      const updated = await api.articles.setManualVerdict(feed.id, articleId, verdict);
      setArticles((prev) =>
        prev
          .map((a) => (a.id === articleId ? { ...a, manual_verdict: updated.manual_verdict } : a))
          .filter((article) => effectivePassed(article) === !showFailed),
      );
    } catch (e) {
      alert(`Failed to save verdict: ${e instanceof Error ? e.message : e}`);
    }
  }

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

  async function handleRevert(targetVersion: PipelineVersion) {
    const activeVersion = versions.find((v) => v.is_active);
    const confirmed = window.confirm(
      `Revert pipeline to v${targetVersion.version_number}` +
        (targetVersion.label ? ` "${targetVersion.label}"` : "") +
        `?\n\nThis will create a new version copying that config. ` +
        `Currently active: v${activeVersion?.version_number ?? "?"}.`,
    );
    if (!confirmed) return;
    setReverting(true);
    try {
      const result = await api.pipelineVersions.revert(feed.id, targetVersion.id);
      onFeedUpdated?.(result.feed);
      const updatedVersions = await api.pipelineVersions.list(feed.id);
      setVersions(updatedVersions);
      const updatedArticles = await api.articles.list(feed.id, showFailed ? false : true, 0, PAGE_SIZE);
      setArticles(updatedArticles);
      setPage(0);
      setHasNextPage(updatedArticles.length === PAGE_SIZE);
    } catch (e) {
      alert(`Revert failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setReverting(false);
    }
  }

  async function handleReplay() {
    const days = replayDays.trim();
    const lookback = days === "" ? undefined : Number.parseInt(days, 10);
    if (lookback !== undefined && (!Number.isFinite(lookback) || lookback < 1)) {
      alert("Lookback must be a whole number of days, or leave empty for all time.");
      return;
    }
    const confirmed = window.confirm(
      `This will re-evaluate all articles from current sources` +
        (lookback ? ` from the last ${lookback} days` : ` (all time)`) +
        ` against the current pipeline.\n\n` +
        `After replay, only articles from this pipeline version onwards will be shown. ` +
        `Articles from removed sources will no longer appear.\n\nProceed?`,
    );
    if (!confirmed) return;
    setReplaying(true);
    try {
      await api.feeds.replay(feed.id, lookback);
      await sleep(3000);
      const updated = await api.articles.list(feed.id, showFailed ? false : true, 0, PAGE_SIZE);
      setArticles(updated);
      setPage(0);
      setHasNextPage(updated.length === PAGE_SIZE);
    } catch (e) {
      alert(`Replay error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setReplaying(false);
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
        <button onClick={triggerPoll} disabled={polling || replaying} style={styles.pollBtn}>
          {polling ? "Polling…" : "Poll now"}
        </button>
        <label style={styles.lookbackLabel}>
          Replay (days)
          <input
            value={replayDays}
            onChange={(e) => setReplayDays(e.target.value.replace(/[^\d]/g, ""))}
            placeholder="all"
            inputMode="numeric"
            style={styles.lookbackInput}
          />
        </label>
        <button
          onClick={() => void handleReplay()}
          disabled={replaying || polling || feed.active_version_replayed}
          title={feed.active_version_replayed ? "Already replayed for this pipeline version" : undefined}
          style={styles.replayBtn}
        >
          {replaying ? "Replaying…" : feed.active_version_replayed ? "Replayed" : "Replay feed"}
        </button>
      </div>

      {versions.length > 0 && (() => {
        const active = versions.find((v) => v.is_active);
        const previous = versions.find((v) => !v.is_active);
        if (!active) return null;
        return (
          <div style={styles.versionBar}>
            <span style={styles.versionBadge}>Pipeline v{active.version_number}</span>
            {active.label && <span style={styles.versionLabel}>{active.label}</span>}
            {previous && (
              <button
                style={styles.revertBtn}
                disabled={reverting}
                onClick={() => void handleRevert(previous)}
              >
                {reverting ? "Reverting…" : `↩ Revert to v${previous.version_number}`}
              </button>
            )}
          </div>
        );
      })()}

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
            <ArticleCard
              article={a}
              showPipelineResult={showFailed}
              onVerdictChange={handleVerdictChange}
            />
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
  replayBtn: { background: "#ff9f0a", color: "#fff", fontSize: 13, padding: "6px 14px" },
  versionBar: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "6px 20px",
    borderBottom: "1px solid #f2f2f7",
    background: "#fafafa",
    flexShrink: 0,
  },
  versionBadge: {
    fontSize: 11,
    fontWeight: 600,
    color: "#4c6fff",
    background: "#f0f4ff",
    padding: "2px 8px",
    borderRadius: 6,
  },
  versionLabel: { fontSize: 12, color: "#6e6e73" },
  revertBtn: {
    marginLeft: "auto",
    fontSize: 12,
    padding: "4px 10px",
    background: "transparent",
    color: "#6e6e73",
    border: "1px solid #d1d1d6",
    borderRadius: 6,
    cursor: "pointer",
  },
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
