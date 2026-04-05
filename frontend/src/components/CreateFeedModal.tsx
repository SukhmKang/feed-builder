import { useState } from "react";
import { api } from "../api/client";
import type { Feed } from "../types";

interface Props {
  onCreated: (feed: Feed) => void;
  onClose: () => void;
}

export function CreateFeedModal({ onCreated, onClose }: Props) {
  const [topic, setTopic] = useState("");
  const [pollInterval, setPollInterval] = useState(24);
  const [phase, setPhase] = useState<"input" | "building">("input");
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!topic.trim()) return;
    setPhase("building");
    setError(null);

    try {
      const feed = await api.feeds.create(topic.trim(), pollInterval);
      // Poll until status is no longer "building"
      const result = await pollUntilReady(feed.id);
      onCreated(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("input");
    }
  }

  async function pollUntilReady(id: string): Promise<Feed> {
    for (let i = 0; i < 120; i++) {
      await sleep(5000);
      const feed = await api.feeds.get(id);
      if (feed.status === "ready") return feed;
      if (feed.status === "error") throw new Error(feed.error_message ?? "Feed build failed");
    }
    throw new Error("Timed out waiting for feed to build");
  }

  return (
    <div style={styles.overlay} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <h2 style={styles.title}>New Feed</h2>

        {phase === "building" ? (
          <div style={styles.building}>
            <div style={styles.spinner} />
            <p style={styles.buildingText}>Building your feed…</p>
            <p style={styles.buildingSubtext}>
              The agent is discovering sources and generating filters. This can take up to 10 minutes.
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit}>
            <label style={styles.label}>
              Describe your feed
              <textarea
                style={{ ...styles.textarea }}
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                placeholder="e.g. Ace Attorney game updates, announcements, and fan community news"
                rows={4}
                autoFocus
              />
            </label>

            <label style={styles.label}>
              Poll interval
              <select
                value={pollInterval}
                onChange={(e) => setPollInterval(Number(e.target.value))}
                style={{ marginTop: 6 }}
              >
                <option value={1}>Every hour</option>
                <option value={6}>Every 6 hours</option>
                <option value={12}>Every 12 hours</option>
                <option value={24}>Once a day</option>
                <option value={168}>Once a week</option>
              </select>
            </label>

            {error && <p style={styles.error}>{error}</p>}

            <div style={styles.actions}>
              <button type="button" onClick={onClose} style={styles.cancelBtn}>
                Cancel
              </button>
              <button type="submit" disabled={!topic.trim()} style={styles.submitBtn}>
                Build Feed
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

const styles: Record<string, React.CSSProperties> = {
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.4)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 100,
  },
  modal: {
    background: "#fff",
    borderRadius: 16,
    padding: 32,
    width: "min(520px, 92vw)",
    boxShadow: "0 20px 60px rgba(0,0,0,0.2)",
  },
  title: { fontSize: 20, fontWeight: 600, marginBottom: 24 },
  label: {
    display: "flex",
    flexDirection: "column",
    gap: 6,
    fontSize: 14,
    fontWeight: 500,
    color: "#3a3a3c",
    marginBottom: 16,
  },
  textarea: { resize: "vertical", minHeight: 96 },
  building: { textAlign: "center", padding: "24px 0" },
  spinner: {
    width: 36,
    height: 36,
    border: "3px solid #e5e5ea",
    borderTopColor: "#007aff",
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
    margin: "0 auto 16px",
  },
  buildingText: { fontSize: 16, fontWeight: 600, marginBottom: 8 },
  buildingSubtext: { fontSize: 13, color: "#6e6e73", lineHeight: 1.5 },
  error: { color: "#ff3b30", fontSize: 13, marginBottom: 12 },
  actions: { display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 8 },
  cancelBtn: { background: "#e5e5ea", color: "#1d1d1f" },
  submitBtn: { background: "#007aff", color: "#fff" },
};

// Inject spinner keyframes once
const styleTag = document.createElement("style");
styleTag.textContent = "@keyframes spin { to { transform: rotate(360deg); } }";
document.head.appendChild(styleTag);
