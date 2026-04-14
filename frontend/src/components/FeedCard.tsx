import { useState } from "react";
import { api } from "../api/client";
import { DEMO_MODE } from "../demoMode";
import type { Feed } from "../types";

interface Props {
  feed: Feed;
  selected: boolean;
  onSelect: () => void;
  onUpdated: (feed: Feed) => void;
  onDeleted: () => void;
}

export function FeedCard({ feed, selected, onSelect, onUpdated, onDeleted }: Props) {
  const [deleting, setDeleting] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(feed.name);
  const [savingName, setSavingName] = useState(false);

  async function handleDelete() {
    if (!confirm(`Delete feed "${feed.name}"?`)) return;
    setDeleting(true);
    try {
      await api.feeds.delete(feed.id);
      onDeleted();
    } catch (err) {
      alert(`Delete failed: ${err instanceof Error ? err.message : err}`);
      setDeleting(false);
    }
  }

  async function handleSaveName() {
    const normalized = draftName.trim();
    if (!normalized || normalized === feed.name || savingName) {
      if (normalized === feed.name) {
        setEditingName(false);
      }
      return;
    }

    setSavingName(true);
    try {
      const updated = await api.feeds.update(feed.id, { name: normalized });
      onUpdated(updated);
      setDraftName(updated.name);
      setEditingName(false);
    } catch (err) {
      alert(`Rename failed: ${err instanceof Error ? err.message : err}`);
    } finally {
      setSavingName(false);
    }
  }

  function handleCancelName() {
    setDraftName(feed.name);
    setEditingName(false);
  }

  const statusColor =
    feed.status === "ready" ? "#34c759" : feed.status === "error" ? "#ff3b30" : "#ff9500";
  const statusLabel =
    feed.status === "building" ? "Building…" : feed.status === "error" ? "Error" : "Ready";

  return (
    <div
      style={{
        ...styles.card,
        ...(selected ? styles.cardSelected : {}),
      }}
    >
      <div style={styles.header} onClick={onSelect}>
        <div style={{ minWidth: 0, flex: 1 }}>
          {editingName ? (
            <div style={styles.nameEditor} onClick={(event) => event.stopPropagation()}>
              <input
                value={draftName}
                onChange={(event) => setDraftName(event.target.value)}
                onKeyDown={(event) => {
                  if (DEMO_MODE) return;
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void handleSaveName();
                  }
                  if (event.key === "Escape") {
                    event.preventDefault();
                    handleCancelName();
                  }
                }}
                autoFocus
                style={styles.nameInput}
                maxLength={120}
              />
              <div style={styles.nameEditorActions}>
                <button onClick={() => void handleSaveName()} disabled={DEMO_MODE || savingName} style={styles.inlineSaveBtn}>
                  {savingName ? "…" : "Save"}
                </button>
                <button onClick={handleCancelName} disabled={DEMO_MODE || savingName} style={styles.inlineCancelBtn}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div style={styles.nameRow}>
              <div style={styles.name}>{feed.name}</div>
              <button
                onClick={(event) => {
                  event.stopPropagation();
                  setDraftName(feed.name);
                  setEditingName(true);
                }}
                disabled={DEMO_MODE}
                style={styles.editBtn}
              >
                Edit
              </button>
            </div>
          )}
          <div style={styles.topic}>{feed.topic}</div>
        </div>
        <span style={{ ...styles.badge, background: statusColor }}>{statusLabel}</span>
      </div>

      {feed.status === "error" && feed.error_message && (
        <div style={styles.errorMsg}>{feed.error_message}</div>
      )}

      <div style={styles.meta}>
        <span>
          Polls every {feed.poll_interval_hours}h
          {feed.last_polled_at && ` · Last: ${formatRelative(feed.last_polled_at)}`}
        </span>
      </div>

      <div style={styles.footer}>
        <button
          onClick={handleDelete}
          disabled={DEMO_MODE || deleting}
          style={styles.deleteBtn}
        >
          {deleting ? "…" : "Delete"}
        </button>
      </div>
    </div>
  );
}

function formatRelative(isoStr: string): string {
  const diff = Date.now() - new Date(isoStr).getTime();
  const h = Math.floor(diff / 3600000);
  if (h < 1) return "just now";
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const styles: Record<string, React.CSSProperties> = {
  card: {
    background: "#fff",
    borderRadius: 12,
    padding: 20,
    border: "2px solid transparent",
    boxShadow: "0 1px 4px rgba(0,0,0,0.08)",
    transition: "border-color 0.15s, box-shadow 0.15s",
    cursor: "default",
    outline: "none",
  },
  cardSelected: {
    borderColor: "#007aff",
    boxShadow: "0 4px 16px rgba(0,122,255,0.15)",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: 12,
    cursor: "pointer",
    marginBottom: 8,
  },
  nameRow: { display: "flex", alignItems: "center", gap: 8, marginBottom: 2 },
  name: { fontWeight: 600, fontSize: 15, marginBottom: 2 },
  nameEditor: { display: "flex", flexDirection: "column", gap: 8, marginBottom: 4 },
  nameInput: { fontSize: 14, fontWeight: 600, padding: "8px 10px" },
  nameEditorActions: { display: "flex", gap: 8 },
  inlineSaveBtn: { fontSize: 12, padding: "5px 10px", background: "#007aff", color: "#fff" },
  inlineCancelBtn: { fontSize: 12, padding: "5px 10px", background: "transparent", color: "#6e6e73" },
  editBtn: {
    fontSize: 11,
    padding: "2px 8px",
    background: "#eef4ff",
    color: "#007aff",
    borderRadius: 999,
  },
  topic: {
    fontSize: 12,
    color: "#6e6e73",
    lineHeight: 1.4,
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
    maxWidth: 200,
  },
  badge: {
    padding: "2px 8px",
    borderRadius: 20,
    fontSize: 11,
    fontWeight: 600,
    color: "#fff",
    whiteSpace: "nowrap",
    flexShrink: 0,
  },
  errorMsg: {
    fontSize: 12,
    color: "#ff3b30",
    background: "#fff2f0",
    borderRadius: 6,
    padding: "6px 10px",
    marginBottom: 8,
  },
  meta: { fontSize: 12, color: "#8e8e93", marginBottom: 12 },
  footer: { display: "flex", justifyContent: "flex-end", alignItems: "center" },
  deleteBtn: {
    fontSize: 12,
    padding: "6px 12px",
    background: "transparent",
    color: "#ff3b30",
    border: "1px solid #ff3b30",
  },
};
