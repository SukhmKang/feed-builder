import { useState } from "react";
import { api, urlBase64ToUint8Array } from "../api/client";
import type { Feed } from "../types";

interface Props {
  feed: Feed;
  selected: boolean;
  onSelect: () => void;
  onUpdated: (feed: Feed) => void;
  onDeleted: () => void;
}

export function FeedCard({ feed, selected, onSelect, onUpdated, onDeleted }: Props) {
  const [togglingNotifs, setTogglingNotifs] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function toggleNotifications() {
    if (togglingNotifs) return;
    setTogglingNotifs(true);
    try {
      const enabling = !feed.notifications_enabled;
      if (enabling) {
        await requestAndSubscribePush(feed.id);
      } else {
        await api.push.unsubscribe(feed.id);
      }
      const updated = await api.feeds.update(feed.id, { notifications_enabled: enabling });
      onUpdated(updated);
    } catch (err) {
      alert(`Notification error: ${err instanceof Error ? err.message : err}`);
    } finally {
      setTogglingNotifs(false);
    }
  }

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
        <div>
          <div style={styles.name}>{feed.name}</div>
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
          onClick={toggleNotifications}
          disabled={togglingNotifs || feed.status !== "ready"}
          style={{
            ...styles.notifBtn,
            background: feed.notifications_enabled ? "#007aff" : "#e5e5ea",
            color: feed.notifications_enabled ? "#fff" : "#1d1d1f",
          }}
        >
          {togglingNotifs
            ? "…"
            : feed.notifications_enabled
              ? "🔔 Notifications on"
              : "🔕 Notifications off"}
        </button>

        <button
          onClick={handleDelete}
          disabled={deleting}
          style={styles.deleteBtn}
        >
          {deleting ? "…" : "Delete"}
        </button>
      </div>
    </div>
  );
}

async function requestAndSubscribePush(feedId: string) {
  if (!("Notification" in window)) throw new Error("Notifications not supported");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Notification permission denied");

  if (!("serviceWorker" in navigator)) throw new Error("Service workers not supported");
  const reg = await navigator.serviceWorker.register("/sw.js");
  const { publicKey } = await api.push.getPublicKey();
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey).buffer as ArrayBuffer,
  });
  await api.push.subscribe(feedId, sub.toJSON() as PushSubscriptionJSON);
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
  name: { fontWeight: 600, fontSize: 15, marginBottom: 2 },
  topic: { fontSize: 12, color: "#6e6e73", lineHeight: 1.4 },
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
  footer: { display: "flex", gap: 8, alignItems: "center" },
  notifBtn: { fontSize: 12, padding: "6px 12px", borderRadius: 20 },
  deleteBtn: {
    marginLeft: "auto",
    fontSize: 12,
    padding: "6px 12px",
    background: "transparent",
    color: "#ff3b30",
    border: "1px solid #ff3b30",
  },
};
