import React, { useCallback, useEffect, useRef, useState } from "react";
import ReactDiffViewer, { DiffMethod } from "react-diff-viewer-continued";
import ReactMarkdown from "react-markdown";
import { api } from "../api/client";
import type { ApplyAuditResult, AuditDetail, AuditSummary, Feed, PipelineBlock, SourceSpec } from "../types";

interface Props {
  feed: Feed;
  onFeedUpdated: (feed: Feed) => void;
}

// ─── Diff helpers ─────────────────────────────────────────────────────────────

type DiffStatus = "unchanged" | "changed" | "added" | "removed";

const DIFF_COLORS: Record<DiffStatus, string> = {
  unchanged: "transparent",
  changed: "#fffbe6",
  added: "#f0faf0",
  removed: "#fff0f0",
};

const DIFF_BORDER: Record<DiffStatus, string> = {
  unchanged: "#e5e5ea",
  changed: "#f5a623",
  added: "#34c759",
  removed: "#ff3b30",
};


// ─── Collapsible section ─────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div style={{ marginBottom: 16 }}>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          background: "none",
          border: "none",
          padding: 0,
          cursor: "pointer",
          fontSize: 13,
          fontWeight: 600,
          color: "#1c1c1e",
          marginBottom: open ? 8 : 0,
        }}
      >
        <span style={{ fontSize: 10 }}>{open ? "▼" : "▶"}</span>
        {title}
      </button>
      {open && children}
    </div>
  );
}

// ─── Status badge ────────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, { bg: string; fg: string }> = {
  pending: { bg: "#f2f2f7", fg: "#8e8e93" },
  running: { bg: "#fff3cd", fg: "#856404" },
  complete: { bg: "#d4edda", fg: "#155724" },
  error: { bg: "#f8d7da", fg: "#721c24" },
};

function StatusBadge({ status }: { status: string }) {
  const colors = STATUS_COLORS[status] ?? STATUS_COLORS.pending;
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        padding: "2px 8px",
        borderRadius: 999,
        background: colors.bg,
        color: colors.fg,
        textTransform: "uppercase",
      }}
    >
      {status}
    </span>
  );
}

// ─── Date formatting ─────────────────────────────────────────────────────────

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return iso;
  }
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function toDatetimeLocal(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// ─── Run Audit Modal ─────────────────────────────────────────────────────────

interface RunModalProps {
  feedId: string;
  onClose: () => void;
  onTriggered: (newAuditId: string) => void;
}

function RunAuditModal({ feedId, onClose, onTriggered }: RunModalProps) {
  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
  const [start, setStart] = useState(toDatetimeLocal(thirtyDaysAgo));
  const [end, setEnd] = useState(toDatetimeLocal(now));
  const [enableReplay, setEnableReplay] = useState(true);
  const [enableDiscovery, setEnableDiscovery] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.audits.trigger(feedId, {
        start: new Date(start).toISOString(),
        end: new Date(end).toISOString(),
        enable_replay: enableReplay,
        enable_discovery: enableDiscovery,
      });
      // Fetch the list to find the newly created audit (it'll be first, newest)
      const audits = await api.audits.list(feedId);
      const newAudit = audits[0];
      onTriggered(newAudit?.id ?? "");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={modalStyles.overlay} onClick={onClose}>
      <div style={modalStyles.box} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 600 }}>Run Audit</h3>
        <form onSubmit={handleSubmit}>
          <label style={modalStyles.label}>
            Start
            <input
              type="datetime-local"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              style={modalStyles.input}
              required
            />
          </label>
          <label style={modalStyles.label}>
            End
            <input
              type="datetime-local"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              style={modalStyles.input}
              required
            />
          </label>
          <label style={modalStyles.checkLabel}>
            <input
              type="checkbox"
              checked={enableReplay}
              onChange={(e) => setEnableReplay(e.target.checked)}
            />
            Re-evaluate all historical articles against current pipeline (replay)
          </label>
          <label style={modalStyles.checkLabel}>
            <input
              type="checkbox"
              checked={enableDiscovery}
              onChange={(e) => setEnableDiscovery(e.target.checked)}
            />
            Enable source discovery (AI finds new sources)
          </label>
          {error && <p style={{ color: "#ff3b30", fontSize: 12, margin: "8px 0" }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, marginTop: 16, justifyContent: "flex-end" }}>
            <button type="button" onClick={onClose} style={modalStyles.cancelBtn}>Cancel</button>
            <button type="submit" disabled={loading} style={modalStyles.submitBtn}>
              {loading ? "Starting…" : "Run Audit"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

const modalStyles: Record<string, React.CSSProperties> = {
  overlay: {
    position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
  },
  box: {
    background: "#fff", borderRadius: 12, padding: 24, width: 420,
    boxShadow: "0 8px 32px rgba(0,0,0,0.18)",
  },
  label: { display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: "#6e6e73", marginBottom: 12 },
  input: { padding: "7px 10px", borderRadius: 8, border: "1px solid #d1d1d6", fontSize: 13, color: "#1c1c1e" },
  checkLabel: { display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "#3a3a3c", marginBottom: 8 },
  cancelBtn: { background: "#f2f2f7", color: "#3a3a3c", fontSize: 13, padding: "7px 16px" },
  submitBtn: { background: "#007aff", color: "#fff", fontSize: 13, padding: "7px 16px" },
};

// ─── Main AuditTab component ─────────────────────────────────────────────────

export function AuditTab({ feed, onFeedUpdated }: Props) {
  const [audits, setAudits] = useState<AuditSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditDetail | null>(null);
  const [diffResult, setDiffResult] = useState<ApplyAuditResult | null>(null);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [generatingDiff, setGeneratingDiff] = useState(false);
  const [applying, setApplying] = useState(false);
  const [showRunModal, setShowRunModal] = useState(false);
  const [applySuccess, setApplySuccess] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pendingAuditIdRef = useRef<string | null>(null);

  // Load an audit and populate diffResult from stored proposed_config if available,
  // otherwise call apply(save=false) to generate and persist it.
  const loadAndSelectAudit = useCallback(async (id: string) => {
    setSelectedId(id);
    setDetail(null);
    setDiffResult(null);
    setApplySuccess(false);
    setLoadingDetail(true);
    let d: AuditDetail | null = null;
    try {
      d = await api.audits.get(feed.id, id);
      setDetail(d);
    } catch (e) {
      alert(`Failed to load audit: ${e instanceof Error ? e.message : e}`);
      setLoadingDetail(false);
      return;
    }
    setLoadingDetail(false);

    if (d.proposed_config) {
      setDiffResult({ saved: false, summary: d.proposed_config._summary ?? "", proposed_config: d.proposed_config });
    }
  }, [feed.id]);

  const loadAudits = useCallback(async () => {
    try {
      const data = await api.audits.list(feed.id);
      setAudits(data);

      // If we were waiting for a newly triggered audit, check if it's done
      const pendingId = pendingAuditIdRef.current;
      if (pendingId) {
        const audit = data.find((a) => a.id === pendingId);
        if (audit && (audit.status === "complete" || audit.status === "error")) {
          pendingAuditIdRef.current = null;
          if (audit.status === "complete") {
            await loadAndSelectAudit(pendingId);
          }
        }
      }
    } catch {
      // ignore transient errors
    } finally {
      setLoadingList(false);
    }
  }, [feed.id, loadAndSelectAudit]);

  useEffect(() => {
    setLoadingList(true);
    setSelectedId(null);
    setDetail(null);
    setDiffResult(null);
    loadAudits();
  }, [feed.id, loadAudits]);

  // Poll when there are pending/running audits
  useEffect(() => {
    const hasActive = audits.some((a) => a.status === "pending" || a.status === "running");
    if (hasActive) {
      if (!pollingRef.current) {
        pollingRef.current = setInterval(loadAudits, 5000);
      }
    } else {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    }
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [audits, loadAudits]);

  async function handleSelectAudit(id: string) {
    if (id === selectedId) return;
    await loadAndSelectAudit(id);
  }

  async function handleDelete(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    if (!confirm("Delete this audit? This cannot be undone.")) return;
    setDeletingId(id);
    try {
      await api.audits.delete(feed.id, id);
      setAudits((prev) => prev.filter((a) => a.id !== id));
      if (selectedId === id) {
        setSelectedId(null);
        setDetail(null);
        setDiffResult(null);
      }
    } catch (err) {
      alert(`Failed to delete: ${err instanceof Error ? err.message : err}`);
    } finally {
      setDeletingId(null);
    }
  }

  async function handleGenerateDiff(force = false) {
    if (!selectedId) return;
    setGeneratingDiff(true);
    setDiffResult(null);
    try {
      const result = await api.audits.apply(feed.id, selectedId, false, force);
      setDiffResult(result);
    } catch (e) {
      alert(`Failed to generate diff: ${e instanceof Error ? e.message : e}`);
    } finally {
      setGeneratingDiff(false);
    }
  }

  async function handleApply() {
    if (!selectedId) return;
    setApplying(true);
    try {
      const result = await api.audits.apply(feed.id, selectedId, true);
      if (result.feed) onFeedUpdated(result.feed);
      setApplySuccess(true);
    } catch (e) {
      alert(`Failed to apply: ${e instanceof Error ? e.message : e}`);
    } finally {
      setApplying(false);
    }
  }

  const currentBlocks = (feed.config?.blocks ?? []) as PipelineBlock[];
  const proposedBlocks = (diffResult?.proposed_config?.blocks ?? []) as PipelineBlock[];
  const currentSources = (feed.config?.sources ?? []) as SourceSpec[];
  const proposedSources = (diffResult?.proposed_config?.sources ?? []) as SourceSpec[];

  return (
    <div style={styles.root}>
      {/* Left panel: audit list */}
      <div style={styles.listPanel}>
        <div style={styles.listHeader}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Audit History</span>
          <button style={styles.runBtn} onClick={() => setShowRunModal(true)}>
            + Run Audit
          </button>
        </div>
        {loadingList ? (
          <p style={styles.empty}>Loading…</p>
        ) : audits.length === 0 ? (
          <p style={styles.empty}>No audits yet.</p>
        ) : (
          <div style={styles.auditList}>
            {audits.map((a) => (
              <div
                key={a.id}
                onClick={() => handleSelectAudit(a.id)}
                style={{
                  ...styles.auditItem,
                  ...(a.id === selectedId ? styles.auditItemActive : {}),
                  cursor: "pointer",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6, marginBottom: 4 }}>
                  <span style={{ fontSize: 11, color: "#6e6e73" }}>
                    {fmtDate(a.audit_period_start)} – {fmtDate(a.audit_period_end)}
                  </span>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    {a.pipeline_version_number != null && (
                      <span style={{ fontSize: 10, fontWeight: 600, color: "#4c6fff", background: "#f0f4ff", padding: "1px 6px", borderRadius: 6 }}>
                        v{a.pipeline_version_number}
                      </span>
                    )}
                    <StatusBadge status={a.status} />
                    <button
                      onClick={(e) => handleDelete(a.id, e)}
                      disabled={deletingId === a.id}
                      title="Delete audit"
                      style={styles.deleteBtn}
                    >
                      {deletingId === a.id ? "…" : "✕"}
                    </button>
                  </div>
                </div>
                {(a.status === "running" || a.status === "pending") && (
                  <p style={{ fontSize: 11, color: "#8e8e93", margin: 0 }}>
                    Started {fmtDateTime(a.started_at)}
                  </p>
                )}
                {a.status === "complete" && (
                  <p style={{ fontSize: 11, color: "#8e8e93", margin: 0 }}>
                    Completed {fmtDateTime(a.completed_at)}
                  </p>
                )}
                {a.status === "error" && (
                  <p style={{ fontSize: 11, color: "#ff3b30", margin: 0 }}>
                    {a.error_message ?? "Unknown error"}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Right panel: detail */}
      <div style={styles.detailPanel}>
        {!selectedId ? (
          <div style={styles.placeholder}>
            <p style={{ color: "#8e8e93", fontSize: 14 }}>Select an audit to view details.</p>
          </div>
        ) : loadingDetail ? (
          <div style={styles.placeholder}>
            <p style={{ color: "#8e8e93", fontSize: 14 }}>Loading…</p>
          </div>
        ) : !detail ? null : (
          <div style={styles.detailScroll}>
            {/* Header */}
            <div style={styles.detailHeader}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>
                    {fmtDate(detail.audit_period_start)} – {fmtDate(detail.audit_period_end)}
                  </h3>
                  <StatusBadge status={detail.status} />
                  {detail.pipeline_version_number != null && (
                    <span style={{ fontSize: 11, fontWeight: 600, color: "#4c6fff", background: "#f0f4ff", padding: "2px 8px", borderRadius: 8 }}>
                      Pipeline v{detail.pipeline_version_number}
                    </span>
                  )}
                </div>
                {detail.completed_at && (
                  <p style={{ margin: "4px 0 0", fontSize: 12, color: "#8e8e93" }}>
                    Completed {fmtDateTime(detail.completed_at)}
                  </p>
                )}
              </div>
            </div>

            {detail.status === "error" && (
              <div style={{ padding: "12px 20px" }}>
                <p style={{ color: "#ff3b30", fontSize: 13 }}>{detail.error_message}</p>
              </div>
            )}

            {detail.status === "running" || detail.status === "pending" ? (
              <div style={styles.placeholder}>
                <p style={{ color: "#8e8e93", fontSize: 14 }}>Audit in progress… refreshing automatically.</p>
              </div>
            ) : null}

            {detail.report && (
              <div style={{ padding: "16px 20px" }}>
                {/* Stats */}
                <Section title="Overview">
                  <div style={styles.statsGrid}>
                    <StatBox label="Total" value={detail.report.stats.total_articles} />
                    <StatBox label="Passed" value={detail.report.stats.passed_count} color="#34c759" />
                    <StatBox label="Filtered" value={detail.report.stats.filtered_count} color="#ff3b30" />
                    <StatBox
                      label="Pass Rate"
                      value={`${(detail.report.stats.overall_pass_rate * 100).toFixed(1)}%`}
                      color="#007aff"
                    />
                    {detail.report.stats.manual_override_count > 0 && (
                      <StatBox label="Manual Overrides" value={detail.report.stats.manual_override_count} color="#f28f3b" />
                    )}
                  </div>
                </Section>

                {/* Source breakdown */}
                {detail.report.stats.per_source.length > 0 && (
                  <Section title="Sources">
                    <div style={{ overflowX: "auto" }}>
                      <table style={styles.table}>
                        <thead>
                          <tr>
                            <th style={styles.th}>Source</th>
                            <th style={styles.th}>Type</th>
                            <th style={styles.th}>Total</th>
                            <th style={styles.th}>Passed</th>
                            <th style={styles.th}>Filtered</th>
                            <th style={styles.th}>Pass %</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.report.stats.per_source.map((src, i) => (
                            <tr key={i} style={{ background: i % 2 === 0 ? "#fff" : "#f9f9fb" }}>
                              <td style={styles.td}>{src.source_name || src.source_feed || "—"}</td>
                              <td style={styles.td}>{src.source_type}</td>
                              <td style={styles.td}>{src.total_articles}</td>
                              <td style={{ ...styles.td, color: "#34c759" }}>{src.passed_count}</td>
                              <td style={{ ...styles.td, color: "#ff3b30" }}>{src.filtered_count}</td>
                              <td style={styles.td}>{(src.pass_rate * 100).toFixed(1)}%</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Section>
                )}

                {/* Assessment */}
                <Section title="Assessment">
                  <AssessmentGrid assessment={detail.report.assessment} />
                </Section>

                {/* Manual override assessment */}
                {detail.report.manual_override_assessment && (
                  <Section title="Manual Override Analysis">
                    <AssessmentField label="Summary" text={detail.report.manual_override_assessment.summary} />
                    <AssessmentField label="False Positives" text={detail.report.manual_override_assessment.false_positives} />
                    <AssessmentField label="False Negatives" text={detail.report.manual_override_assessment.false_negatives} />
                    <AssessmentField label="Patterns" text={detail.report.manual_override_assessment.patterns} />
                    <AssessmentField label="Suggested Focus" text={detail.report.manual_override_assessment.suggested_focus} />
                  </Section>
                )}

                {/* Pipeline recommendations */}
                <Section title="Pipeline Recommendations">
                  <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
                    <span
                      style={{
                        fontSize: 11,
                        fontWeight: 700,
                        padding: "2px 8px",
                        borderRadius: 4,
                        background: detail.report.pipeline_recommendations.satisfied ? "#d4edda" : "#f8d7da",
                        color: detail.report.pipeline_recommendations.satisfied ? "#155724" : "#721c24",
                      }}
                    >
                      {detail.report.pipeline_recommendations.satisfied ? "Satisfied" : "Needs Changes"}
                    </span>
                  </div>
                  <p style={styles.recText}>{detail.report.pipeline_recommendations.feedback}</p>
                  {detail.report.pipeline_recommendations.suggested_changes.length > 0 && (
                    <ul style={styles.changeList}>
                      {detail.report.pipeline_recommendations.suggested_changes.map((c, i) => (
                        <li key={i} style={styles.changeItem}>
                          <strong>{c.action ?? c.type ?? "change"}:</strong>{" "}
                          {c.description ?? c.reason ?? JSON.stringify(c)}
                        </li>
                      ))}
                    </ul>
                  )}
                </Section>

                {/* Source recommendations */}
                <Section title="Source Recommendations">
                  <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
                    <span
                      style={{
                        fontSize: 11,
                        fontWeight: 700,
                        padding: "2px 8px",
                        borderRadius: 4,
                        background: detail.report.source_recommendations.satisfied ? "#d4edda" : "#f8d7da",
                        color: detail.report.source_recommendations.satisfied ? "#155724" : "#721c24",
                      }}
                    >
                      {detail.report.source_recommendations.satisfied ? "Satisfied" : "Needs Changes"}
                    </span>
                  </div>
                  <p style={styles.recText}>{detail.report.source_recommendations.feedback}</p>
                  {detail.report.source_recommendations.suggested_changes.length > 0 && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      {detail.report.source_recommendations.suggested_changes.map((c, i) => (
                        <div key={i} style={styles.sourceChange}>
                          <ActionBadge action={c.action} />
                          <span style={{ fontSize: 12, color: "#3a3a3c", flex: 1 }}>
                            <strong>{c.source_type}</strong>
                            {c.source_feeds.length > 0 ? `: ${c.source_feeds.join(", ")}` : ""} — {c.reason}
                            {c.coverage_gap_description ? ` (${c.coverage_gap_description})` : ""}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </Section>

                {/* Proposed new sources from discovery */}
                {detail.report.proposed_new_sources.length > 0 && (
                  <Section title="Proposed New Sources">
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      {detail.report.proposed_new_sources.map((src, i) => (
                        <div key={i} style={styles.sourceChange}>
                          <ActionBadge action="add" />
                          <span style={{ fontSize: 12, color: "#3a3a3c" }}>
                            <strong>{src.type}</strong>
                            {src.feed ? `: ${src.feed}` : ""}
                            {src.reason ? ` — ${src.reason}` : ""}
                          </span>
                        </div>
                      ))}
                    </div>
                  </Section>
                )}

                {/* Pipeline diff */}
                <Section title="Pipeline Diff">
                  {!diffResult ? (
                    <div>
                      <p style={{ fontSize: 12, color: "#6e6e73", marginBottom: 10 }}>
                        Generate a proposed pipeline based on this audit's recommendations. The AI will rewrite your pipeline — you can review and apply.
                      </p>
                      <button
                        onClick={() => handleGenerateDiff(false)}
                        disabled={generatingDiff}
                        style={styles.generateBtn}
                      >
                        {generatingDiff ? "Generating…" : "Generate Proposed Pipeline"}
                      </button>
                    </div>
                  ) : (
                    <div>
                      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
                        <button
                          onClick={() => handleGenerateDiff(true)}
                          disabled={generatingDiff}
                          style={{ ...styles.generateBtn, padding: "5px 12px", fontSize: 12 }}
                        >
                          {generatingDiff ? "Regenerating…" : "Regenerate"}
                        </button>
                      </div>

                      <SourceDiffView currentSources={currentSources} proposedSources={proposedSources} />
                      <BlocksJSONDiffView currentBlocks={currentBlocks} proposedBlocks={proposedBlocks} />

                      {/* Summary */}
                      {diffResult.summary && (
                        <div style={styles.summaryBox}>
                          <strong style={{ fontSize: 12 }}>Summary of changes:</strong>
                          <div style={styles.summaryMarkdown}>
                            <ReactMarkdown>{diffResult.summary}</ReactMarkdown>
                          </div>
                        </div>
                      )}

                      {/* Apply */}
                      {applySuccess ? (
                        <div style={styles.successMsg}>
                          Pipeline applied successfully. View changes in the Edit Pipeline tab.
                        </div>
                      ) : (
                        <button
                          onClick={handleApply}
                          disabled={applying}
                          style={styles.applyBtn}
                        >
                          {applying ? "Applying…" : "Apply to Pipeline"}
                        </button>
                      )}
                    </div>
                  )}
                </Section>
              </div>
            )}
          </div>
        )}
      </div>

      {showRunModal && (
        <RunAuditModal
          feedId={feed.id}
          onClose={() => setShowRunModal(false)}
          onTriggered={(newAuditId) => {
            if (newAuditId) pendingAuditIdRef.current = newAuditId;
            loadAudits();
          }}
        />
      )}
    </div>
  );
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function StatBox({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={styles.statBox}>
      <span style={{ fontSize: 20, fontWeight: 700, color: color ?? "#1c1c1e" }}>{value}</span>
      <span style={{ fontSize: 11, color: "#8e8e93" }}>{label}</span>
    </div>
  );
}

function AssessmentField({ label, text }: { label: string; text: string }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <p style={{ fontSize: 12, fontWeight: 600, color: "#3a3a3c", margin: "0 0 2px" }}>{label}</p>
      <p style={{ fontSize: 12, color: "#6e6e73", margin: 0, lineHeight: 1.6 }}>{text}</p>
    </div>
  );
}

function AssessmentGrid({ assessment }: { assessment: NonNullable<AuditDetail["report"]>["assessment"] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <AssessmentField label="Passed Article Quality" text={assessment.passed_quality} />
      <AssessmentField label="Filtered Article Quality" text={assessment.filtered_quality} />
      <AssessmentField label="Source Quality" text={assessment.source_quality} />
      <AssessmentField label="Coverage Gaps" text={assessment.coverage_gaps} />
      <AssessmentField label="Noise Patterns" text={assessment.noise_patterns} />
      <AssessmentField label="Volume Health" text={assessment.volume_health} />
    </div>
  );
}

function ActionBadge({ action }: { action: string }) {
  const colors: Record<string, { bg: string; fg: string }> = {
    remove: { bg: "#f8d7da", fg: "#721c24" },
    modify: { bg: "#fff3cd", fg: "#856404" },
    add_needed: { bg: "#d4edda", fg: "#155724" },
    add: { bg: "#d4edda", fg: "#155724" },
  };
  const c = colors[action] ?? { bg: "#f2f2f7", fg: "#8e8e93" };
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        padding: "2px 8px",
        borderRadius: 4,
        background: c.bg,
        color: c.fg,
        textTransform: "uppercase",
        flexShrink: 0,
      }}
    >
      {action.replace("_needed", "")}
    </span>
  );
}


function SourceDiffRow({ source, status }: { source: SourceSpec; status: DiffStatus }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "7px 10px",
        borderRadius: 6,
        marginBottom: 4,
        background: DIFF_COLORS[status],
        border: `1px solid ${DIFF_BORDER[status]}`,
      }}
    >
      <span style={{ fontSize: 11, fontWeight: 600, color: "#6e6e73", minWidth: 60 }}>{source.type}</span>
      <span style={{ fontSize: 12, color: "#1c1c1e", flex: 1, wordBreak: "break-all" }}>{source.feed}</span>
      {status !== "unchanged" && (
        <span
          style={{
            fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4,
            background: DIFF_BORDER[status], color: "#fff", textTransform: "uppercase", flexShrink: 0,
          }}
        >
          {status}
        </span>
      )}
    </div>
  );
}

function computeSourceDiff(current: SourceSpec[], proposed: SourceSpec[]) {
  const currentKeys = new Set(current.map((s) => `${s.type}::${s.feed}`));
  const proposedKeys = new Set(proposed.map((s) => `${s.type}::${s.feed}`));
  const removed = current.filter((s) => !proposedKeys.has(`${s.type}::${s.feed}`));
  const added = proposed.filter((s) => !currentKeys.has(`${s.type}::${s.feed}`));
  const unchanged = current.filter((s) => proposedKeys.has(`${s.type}::${s.feed}`));
  return { removed, added, unchanged };
}

function SourceDiffView({ currentSources, proposedSources }: { currentSources: SourceSpec[]; proposedSources: SourceSpec[] }) {
  const sourceDiff = computeSourceDiff(currentSources, proposedSources);
  const hasChanges = sourceDiff.removed.length > 0 || sourceDiff.added.length > 0;
  return (
    <div style={{ marginBottom: 16 }}>
      <p style={{ ...styles.diffColHeader, marginBottom: 8 }}>
        Sources {!hasChanges && <span style={{ color: "#34c759", fontWeight: 400 }}>— no changes</span>}
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div>
          <p style={{ ...styles.diffColHeader, fontSize: 11, color: "#8e8e93" }}>Current ({currentSources.length})</p>
          {currentSources.map((s, i) => {
            const isRemoved = sourceDiff.removed.some((r) => r.type === s.type && r.feed === s.feed);
            return <SourceDiffRow key={i} source={s} status={isRemoved ? "removed" : "unchanged"} />;
          })}
        </div>
        <div>
          <p style={{ ...styles.diffColHeader, fontSize: 11, color: "#8e8e93" }}>Proposed ({proposedSources.length})</p>
          {proposedSources.map((s, i) => {
            const isAdded = sourceDiff.added.some((a) => a.type === s.type && a.feed === s.feed);
            return <SourceDiffRow key={i} source={s} status={isAdded ? "added" : "unchanged"} />;
          })}
        </div>
      </div>
    </div>
  );
}

function BlocksJSONDiffView({ currentBlocks, proposedBlocks }: { currentBlocks: PipelineBlock[]; proposedBlocks: PipelineBlock[] }) {
  const oldStr = JSON.stringify(currentBlocks, null, 2);
  const newStr = JSON.stringify(proposedBlocks, null, 2);
  return (
    <div style={{ marginBottom: 16 }}>
      <p style={{ ...styles.diffColHeader, marginBottom: 8 }}>Pipeline blocks</p>
      <div style={{ fontSize: 12, overflowX: "auto", border: "1px solid #e5e5ea", borderRadius: 8 }}>
        <ReactDiffViewer
          oldValue={oldStr}
          newValue={newStr}
          splitView={true}
          compareMethod={DiffMethod.LINES}
          leftTitle="Current"
          rightTitle="Proposed"
          useDarkTheme={false}
          styles={{ variables: { light: { codeFoldGutterBackground: "#f9f9fb" } } }}
        />
      </div>
    </div>
  );
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  root: { display: "flex", flex: 1, overflow: "hidden", height: "100%" },
  listPanel: {
    width: 280,
    flexShrink: 0,
    borderRight: "1px solid #e5e5ea",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    background: "#f9f9fb",
  },
  listHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 14px",
    borderBottom: "1px solid #e5e5ea",
    background: "#fff",
    flexShrink: 0,
  },
  runBtn: { background: "#007aff", color: "#fff", fontSize: 12, padding: "5px 12px" },
  auditList: { flex: 1, overflowY: "auto", padding: 10, display: "flex", flexDirection: "column", gap: 6 },
  auditItem: {
    background: "#fff",
    border: "1px solid #e5e5ea",
    borderRadius: 8,
    padding: "10px 12px",
    textAlign: "left",
    cursor: "pointer",
    width: "100%",
  },
  auditItemActive: {
    borderColor: "#007aff",
    background: "#eef4ff",
  },
  deleteBtn: {
    background: "none",
    border: "none",
    color: "#8e8e93",
    fontSize: 11,
    cursor: "pointer",
    padding: "1px 4px",
    borderRadius: 4,
    lineHeight: 1,
  },
  empty: { padding: 20, color: "#8e8e93", fontSize: 13, textAlign: "center" },
  detailPanel: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  placeholder: { flex: 1, display: "flex", alignItems: "center", justifyContent: "center" },
  detailScroll: { flex: 1, overflowY: "auto" },
  detailHeader: {
    padding: "16px 20px",
    borderBottom: "1px solid #e5e5ea",
    background: "#fff",
    flexShrink: 0,
  },
  statsGrid: { display: "flex", flexWrap: "wrap", gap: 10, marginBottom: 4 },
  statBox: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    background: "#f9f9fb",
    border: "1px solid #e5e5ea",
    borderRadius: 8,
    padding: "10px 16px",
    minWidth: 80,
  },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  th: {
    textAlign: "left",
    padding: "6px 10px",
    borderBottom: "1px solid #e5e5ea",
    fontWeight: 600,
    color: "#6e6e73",
    fontSize: 11,
    background: "#f9f9fb",
  },
  td: { padding: "6px 10px", color: "#3a3a3c", borderBottom: "1px solid #f2f2f7" },
  recText: { fontSize: 12, color: "#3a3a3c", lineHeight: 1.6, margin: "0 0 8px" },
  changeList: { margin: "0 0 0 16px", padding: 0, fontSize: 12, color: "#3a3a3c", lineHeight: 1.8 },
  changeItem: { marginBottom: 4 },
  sourceChange: { display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 10px", background: "#f9f9fb", borderRadius: 6 },
  generateBtn: {
    background: "#5856d6",
    color: "#fff",
    fontSize: 13,
    padding: "8px 16px",
    borderRadius: 8,
  },
  modeToggle: {
    display: "flex",
    background: "#f2f2f7",
    borderRadius: 8,
    padding: 2,
  },
  modeBtn: {
    background: "transparent",
    border: "none",
    padding: "5px 12px",
    fontSize: 12,
    borderRadius: 6,
    cursor: "pointer",
    color: "#6e6e73",
  },
  modeBtnActive: { background: "#fff", color: "#1c1c1e", fontWeight: 600, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" },
  diffColHeader: { fontSize: 12, fontWeight: 600, color: "#6e6e73", margin: "0 0 8px" },
  emptyCol: { fontSize: 12, color: "#8e8e93", fontStyle: "italic" },
  emptySlot: { height: 60, marginBottom: 8, border: "1px dashed #e5e5ea", borderRadius: 8 },
  summaryBox: {
    background: "#f9f9fb",
    border: "1px solid #e5e5ea",
    borderRadius: 8,
    padding: "12px 14px",
    margin: "12px 0",
  },
  summaryMarkdown: {
    fontSize: 12,
    color: "#3a3a3c",
    lineHeight: 1.7,
    marginTop: 6,
  },
  successMsg: {
    background: "#d4edda",
    color: "#155724",
    fontSize: 13,
    padding: "10px 14px",
    borderRadius: 8,
    marginTop: 12,
  },
  applyBtn: {
    background: "#34c759",
    color: "#fff",
    fontSize: 13,
    padding: "8px 18px",
    borderRadius: 8,
    marginTop: 12,
  },
};
