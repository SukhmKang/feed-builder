import type { Article, CustomBlockOption, Feed, PipelineBlock, SourceSpec, StoryDetail, StorySummary } from "../types";

// In dev, leave empty so Vite's proxy handles it.
// In production, set VITE_API_URL=https://api.yourdomain.com
const BASE = import.meta.env.VITE_API_URL ?? "";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  feeds: {
    list: () => request<Feed[]>("/feeds"),
    get: (id: string) => request<Feed>(`/feeds/${id}`),
    create: (topic: string, poll_interval_hours: number) =>
      request<Feed>("/feeds", {
        method: "POST",
        body: JSON.stringify({ topic, poll_interval_hours }),
      }),
    update: (
      id: string,
      data: {
        name?: string;
        notifications_enabled?: boolean;
        poll_interval_hours?: number;
        blocks?: PipelineBlock[];
        sources?: SourceSpec[];
      },
    ) =>
      request<Feed>(`/feeds/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    listCustomBlocks: () => request<CustomBlockOption[]>("/feeds/custom-blocks"),
    delete: (id: string) => request<void>(`/feeds/${id}`, { method: "DELETE" }),
    poll: (id: string, lookbackHours?: number) => {
      const params = new URLSearchParams();
      if (lookbackHours !== undefined) params.set("lookback_hours", String(lookbackHours));
      const suffix = params.size > 0 ? `?${params.toString()}` : "";
      return request<{ status: string }>(`/feeds/${id}/poll${suffix}`, { method: "POST" });
    },
  },
  articles: {
    list: (feedId: string, passed?: boolean, offset = 0, limit = 50) => {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      if (passed !== undefined) params.set("passed", String(passed));
      return request<Article[]>(`/feeds/${feedId}/articles?${params}`);
    },
  },
  stories: {
    list: (feedId: string) => request<StorySummary[]>(`/feeds/${feedId}/stories`),
    get: (feedId: string, storyId: string) => request<StoryDetail>(`/feeds/${feedId}/stories/${storyId}`),
  },
  push: {
    getPublicKey: () => request<{ publicKey: string }>("/push/vapid-public-key"),
    subscribe: (feed_id: string, subscription: PushSubscriptionJSON) =>
      request<{ status: string }>("/push/subscribe", {
        method: "POST",
        body: JSON.stringify({ feed_id, subscription }),
      }),
    unsubscribe: (feed_id: string) =>
      request<void>(`/push/subscribe/${feed_id}`, { method: "DELETE" }),
  },
};

export function getFeedRssUrl(feedId: string): string {
  const path = `/feeds/${feedId}/rss`;
  if (BASE) return `${BASE}${path}`;
  return `${window.location.origin}${path}`;
}

export function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}
