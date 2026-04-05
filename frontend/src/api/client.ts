import type { ApplyAuditResult, Article, AuditDetail, AuditSummary, CustomBlockOption, Feed, PipelineBlock, PipelineVersion, SourceSpec, StoryDetail, StorySummary } from "../types";

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
        poll_interval_hours?: number;
        blocks?: PipelineBlock[];
        sources?: SourceSpec[];
        version_label?: string;
      },
    ) =>
      request<Feed>(`/feeds/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    aiEditBlock: (
      id: string,
      block: PipelineBlock,
      sources: SourceSpec[],
      context: { blockPath: string; parentContext: string; siblingBlocks: PipelineBlock[] },
      instruction: string,
    ) =>
      request<{ replacement_blocks: PipelineBlock[] }>(`/feeds/${id}/ai-edit-block`, {
        method: "POST",
        body: JSON.stringify({
          block,
          sources,
          block_path: context.blockPath,
          parent_context: context.parentContext,
          sibling_blocks: context.siblingBlocks,
          instruction,
        }),
      }),
    replay: (id: string, lookbackDays?: number) =>
      request<{ status: string }>(`/feeds/${id}/replay`, {
        method: "POST",
        body: JSON.stringify({ lookback_days: lookbackDays ?? null }),
      }),
    listCustomBlocks: () => request<CustomBlockOption[]>("/feeds/custom-blocks"),
    delete: (id: string) => request<void>(`/feeds/${id}`, { method: "DELETE" }),
    poll: (id: string, lookbackHours?: number) => {
      const params = new URLSearchParams();
      if (lookbackHours !== undefined) params.set("lookback_hours", String(lookbackHours));
      const suffix = params.size > 0 ? `?${params.toString()}` : "";
      return request<{ status: string }>(`/feeds/${id}/poll${suffix}`, { method: "POST" });
    },
  },
  pipelineVersions: {
    list: (feedId: string) =>
      request<PipelineVersion[]>(`/feeds/${feedId}/pipeline-versions`),
    revert: (feedId: string, versionId: string) =>
      request<{ version: PipelineVersion; feed: Feed }>(
        `/feeds/${feedId}/pipeline-versions/${versionId}/revert`,
        { method: "POST" },
      ),
  },
  audits: {
    list: (feedId: string) => request<AuditSummary[]>(`/feeds/${feedId}/audits`),
    get: (feedId: string, auditId: string) => request<AuditDetail>(`/feeds/${feedId}/audits/${auditId}`),
    trigger: (
      feedId: string,
      data: { start: string; end: string; enable_replay?: boolean; enable_discovery?: boolean },
    ) =>
      request<{ status: string; message: string }>(`/feeds/${feedId}/audits`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    apply: (feedId: string, auditId: string, save: boolean, force = false) =>
      request<ApplyAuditResult>(`/feeds/${feedId}/audits/${auditId}/apply`, {
        method: "POST",
        body: JSON.stringify({ save, force }),
      }),
    delete: (feedId: string, auditId: string) =>
      request<void>(`/feeds/${feedId}/audits/${auditId}`, { method: "DELETE" }),
  },
  articles: {
    list: (feedId: string, passed?: boolean, offset = 0, limit = 50) => {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      if (passed !== undefined) params.set("passed", String(passed));
      return request<Article[]>(`/feeds/${feedId}/articles?${params}`);
    },
    setManualVerdict: (feedId: string, articleId: string, verdict: "passed" | "filtered" | null) =>
      request<Article>(`/feeds/${feedId}/articles/${articleId}/manual-verdict`, {
        method: "PATCH",
        body: JSON.stringify({ verdict }),
      }),
  },
  stories: {
    list: (feedId: string) => request<StorySummary[]>(`/feeds/${feedId}/stories`),
    get: (feedId: string, storyId: string) => request<StoryDetail>(`/feeds/${feedId}/stories/${storyId}`),
    update: (feedId: string, storyId: string, data: { title: string }) =>
      request<StorySummary>(`/feeds/${feedId}/stories/${storyId}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      }),
  },
};

export function getFeedRssUrl(feedId: string): string {
  const path = `/feeds/${feedId}/rss`;
  if (BASE) return `${BASE}${path}`;
  return `${window.location.origin}${path}`;
}
