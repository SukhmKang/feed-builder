import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { DEMO_MODE } from "../demoMode";
import type { Feed, ReportRecord } from "../types";

interface Props {
  feed: Feed;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ISO date string for <input type="date"> (YYYY-MM-DD)
function toDateInputValue(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

// ── Report row ────────────────────────────────────────────────────────────────

interface ReportRowProps {
  report: ReportRecord;
  feedId: string;
  onDeleted: (id: string) => void;
}

function ReportRow({ report, feedId, onDeleted }: ReportRowProps) {
  const [deleting, setDeleting] = useState(false);
  const [rowHover, setRowHover] = useState(false);

  const storyWord = report.story_count === 1 ? "story" : "stories";

  async function handleDelete() {
    if (DEMO_MODE) return;
    if (!window.confirm("Delete this report? This cannot be undone.")) return;
    setDeleting(true);
    try {
      await api.reports.delete(feedId, report.id);
      onDeleted(report.id);
    } catch (err) {
      alert(`Failed to delete report: ${err instanceof Error ? err.message : err}`);
    } finally {
      setDeleting(false);
    }
  }

  function handleDownload() {
    const url = api.reports.downloadUrl(feedId, report.id);
    window.open(url, "_blank", "noopener,noreferrer");
  }

  return (
    <div
      style={{ ...S.row, ...(rowHover ? S.rowHover : {}) }}
      onMouseEnter={() => setRowHover(true)}
      onMouseLeave={() => setRowHover(false)}
    >
      <div style={S.rowMain}>
        <span style={S.rowPeriod}>
          {fmtDate(report.date_from)} – {fmtDate(report.date_to)}
        </span>
        <span style={S.rowMeta}>
          {report.story_count} {storyWord} · Generated {fmtDateTime(report.created_at)}
        </span>
      </div>
      <div style={S.rowActions}>
        <button
          style={S.downloadBtn}
          onClick={handleDownload}
          disabled={!report.r2_key}
          title="Download PDF"
        >
          Download PDF
        </button>
        <button
          style={S.deleteBtn}
          onClick={() => void handleDelete()}
          disabled={DEMO_MODE || deleting}
          title="Delete report"
        >
          {deleting ? "…" : "Delete"}
        </button>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function ReportsTab({ feed }: Props) {
  const today = new Date();
  const sevenDaysAgo = new Date(today);
  sevenDaysAgo.setDate(today.getDate() - 7);

  const [reports, setReports] = useState<ReportRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [dateFrom, setDateFrom] = useState(toDateInputValue(sevenDaysAgo));
  const [dateTo, setDateTo] = useState(toDateInputValue(today));
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);

  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    setLoading(true);
    setError(null);
    api.reports
      .list(feed.id)
      .then((data) => { if (mountedRef.current) setReports(data); })
      .catch((err: Error) => { if (mountedRef.current) setError(err.message); })
      .finally(() => { if (mountedRef.current) setLoading(false); });
    return () => { mountedRef.current = false; };
  }, [feed.id]);

  async function handleGenerate() {
    if (DEMO_MODE || generating) return;
    setGenerateError(null);
    if (!dateFrom || !dateTo) {
      setGenerateError("Please select both a start and end date.");
      return;
    }
    if (dateFrom >= dateTo) {
      setGenerateError("Start date must be before end date.");
      return;
    }
    setGenerating(true);
    try {
      const record = await api.reports.generate(feed.id, dateFrom, dateTo);
      setReports((prev) => [record, ...prev]);
    } catch (err) {
      setGenerateError(err instanceof Error ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  }

  function handleDeleted(id: string) {
    setReports((prev) => prev.filter((r) => r.id !== id));
  }

  return (
    <div style={S.container}>
      {/* Generate panel */}
      <div style={S.panel}>
        <h3 style={S.panelTitle}>Generate Report</h3>
        <p style={S.panelDesc}>
          Creates a PDF of all stories (and their articles) whose dates overlap with the selected period.
        </p>
        <div style={S.formRow}>
          <label style={S.label}>
            From
            <input
              type="date"
              value={dateFrom}
              max={dateTo}
              onChange={(e) => setDateFrom(e.target.value)}
              style={S.dateInput}
              disabled={generating}
            />
          </label>
          <label style={S.label}>
            To
            <input
              type="date"
              value={dateTo}
              min={dateFrom}
              onChange={(e) => setDateTo(e.target.value)}
              style={S.dateInput}
              disabled={generating}
            />
          </label>
          <button
            style={{ ...S.generateBtn, ...(generating ? S.generateBtnDisabled : {}) }}
            onClick={() => void handleGenerate()}
            disabled={DEMO_MODE || generating}
          >
            {generating ? "Generating…" : "Generate PDF"}
          </button>
        </div>
        {generateError && <p style={S.errorText}>{generateError}</p>}
      </div>

      {/* Reports list */}
      <div style={S.listSection}>
        <h3 style={S.sectionTitle}>Past Reports</h3>

        {loading ? (
          <div style={S.center}>
            <div style={S.spinner} />
            <span style={S.centerText}>Loading…</span>
          </div>
        ) : error ? (
          <p style={S.errorText}>{error}</p>
        ) : reports.length === 0 ? (
          <p style={S.emptyText}>No reports yet. Generate one above.</p>
        ) : (
          <div style={S.list}>
            {reports.map((r) => (
              <ReportRow
                key={r.id}
                report={r}
                feedId={feed.id}
                onDeleted={handleDeleted}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const S: Record<string, React.CSSProperties> = {
  container: {
    flex: 1,
    overflowY: "auto",
    background: "#F8F9FA",
    padding: "24px",
    height: "100%",
  },
  panel: {
    background: "#fff",
    border: "1px solid #E8EAED",
    borderRadius: 12,
    padding: "20px 24px",
    marginBottom: 24,
    maxWidth: 760,
    boxShadow: "0 1px 2px rgba(60,64,67,0.04)",
  },
  panelTitle: {
    fontSize: 15,
    fontWeight: 600,
    color: "#202124",
    margin: "0 0 6px",
  },
  panelDesc: {
    fontSize: 13,
    color: "#5F6368",
    margin: "0 0 16px",
    lineHeight: 1.55,
  },
  formRow: {
    display: "flex",
    alignItems: "flex-end",
    gap: 16,
    flexWrap: "wrap",
  },
  label: {
    display: "flex",
    flexDirection: "column",
    gap: 5,
    fontSize: 12,
    fontWeight: 600,
    color: "#5F6368",
    letterSpacing: "0.02em",
    textTransform: "uppercase",
  },
  dateInput: {
    padding: "7px 10px",
    border: "1px solid #DADCE0",
    borderRadius: 8,
    fontSize: 13,
    color: "#202124",
    background: "#fff",
    fontFamily: "inherit",
    minWidth: 150,
  },
  generateBtn: {
    padding: "8px 20px",
    background: "#1558D6",
    color: "#fff",
    border: "none",
    borderRadius: 8,
    fontSize: 13,
    fontWeight: 600,
    cursor: "pointer",
    whiteSpace: "nowrap",
    fontFamily: "inherit",
  },
  generateBtnDisabled: {
    background: "#DADCE0",
    color: "#80868B",
    cursor: "default",
  },
  errorText: {
    fontSize: 12,
    color: "#D32F2F",
    marginTop: 10,
    marginBottom: 0,
  },

  listSection: {
    maxWidth: 760,
  },
  sectionTitle: {
    fontSize: 14,
    fontWeight: 600,
    color: "#3C4043",
    margin: "0 0 12px",
  },
  list: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
  },
  row: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 16,
    background: "#fff",
    border: "1px solid #E8EAED",
    borderRadius: 10,
    padding: "14px 18px",
    transition: "box-shadow 0.12s",
  },
  rowHover: {
    boxShadow: "0 2px 8px rgba(60,64,67,0.10)",
  },
  rowMain: {
    display: "flex",
    flexDirection: "column",
    gap: 3,
    minWidth: 0,
  },
  rowPeriod: {
    fontSize: 14,
    fontWeight: 600,
    color: "#202124",
    whiteSpace: "nowrap",
  },
  rowMeta: {
    fontSize: 12,
    color: "#70757A",
    whiteSpace: "nowrap",
  },
  rowActions: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    flexShrink: 0,
  },
  downloadBtn: {
    padding: "6px 14px",
    background: "#E8F0FE",
    color: "#1558D6",
    border: "none",
    borderRadius: 8,
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
    whiteSpace: "nowrap",
  },
  deleteBtn: {
    padding: "6px 12px",
    background: "transparent",
    color: "#D32F2F",
    border: "1px solid #FECDD2",
    borderRadius: 8,
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
    whiteSpace: "nowrap",
  },
  center: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "24px 0",
  },
  spinner: {
    width: 20,
    height: 20,
    border: "2px solid #DADCE0",
    borderTopColor: "#1967D2",
    borderRadius: "50%",
    animation: "spin 0.75s linear infinite",
  },
  centerText: {
    fontSize: 13,
    color: "#70757A",
  },
  emptyText: {
    fontSize: 13,
    color: "#70757A",
    margin: 0,
    padding: "16px 0",
  },
};
