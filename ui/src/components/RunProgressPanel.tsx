import { useEffect, useMemo, useState } from "react";
import { connectRunEvents } from "../run-events/connectRunEvents";
import { collectEvidenceBadges } from "../run-events/evidenceBadges";
import { normalizeRunEvent } from "../run-events/normalizeRunEvent";
import type { NormalizedRunEvent, RunMeta } from "../run-events/types";
import "./RunProgressPanel.css";

type Props = {
  runId: string;
  baseUrl?: string;
};

function statusClass(status: string): string {
  if (status === "finished") return "pill pill-finished";
  if (status === "error") return "pill pill-error";
  if (status === "running") return "pill pill-running";
  return "pill";
}

function isTimelineEvent(ev: NormalizedRunEvent): boolean {
  return ["thinking", "task", "tool_call", "status"].includes(ev.type);
}

function timelineIcon(ev: NormalizedRunEvent): string {
  if (ev.type === "thinking") return "…";
  if (ev.type === "task" && ev.status === "final.ready") return "✓";
  if (ev.type === "tool_call" && ev.status === "completed") return "✓";
  if (ev.type === "tool_call" && ev.status === "running") return "→";
  if (ev.type === "status" && ev.status === "finished") return "■";
  return "·";
}

export function RunProgressPanel({ runId, baseUrl = "" }: Props) {
  const [meta, setMeta] = useState<RunMeta | null>(null);
  const [events, setEvents] = useState<NormalizedRunEvent[]>([]);
  const [runStatus, setRunStatus] = useState("loading");
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [snapshotMaxSeq, setSnapshotMaxSeq] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const prefix = baseUrl.replace(/\/$/, "");

    fetch(`${prefix}/router/agent/runs/${encodeURIComponent(runId)}`)
      .then((r) => {
        if (!r.ok) throw new Error(`run not found (${r.status})`);
        return r.json();
      })
      .then((data) => {
        if (cancelled) return;
        setMeta({
          run_id: data.run_id,
          status: data.status,
          turn_index: data.turn_index,
          parent_run_id: data.parent_run_id,
          query: data.query,
          intent: data.intent,
          phase: data.phase,
        });
        setRunStatus(data.status ?? "unknown");
        const snapshot = (data.events ?? []).map((raw: unknown) =>
          normalizeRunEvent(raw as Parameters<typeof normalizeRunEvent>[0]),
        );
        setEvents(snapshot);
        const maxSeq = snapshot.reduce(
          (m: number, ev: NormalizedRunEvent) => Math.max(m, ev.seq),
          0,
        );
        setSnapshotMaxSeq(maxSeq);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });

    return () => {
      cancelled = true;
      setSnapshotMaxSeq(null);
    };
  }, [runId, baseUrl]);

  useEffect(() => {
    if (!runId || snapshotMaxSeq === null) return;

    const disconnect = connectRunEvents(
      runId,
      (ev) => {
        setEvents((prev) => {
          if (prev.some((p) => p.seq === ev.seq && p.type === ev.type)) {
            return prev;
          }
          return [...prev, ev].sort((a, b) => a.seq - b.seq);
        });
        if (ev.type === "status" && ev.status === "finished") {
          setRunStatus("finished");
        }
      },
      (status) => {
        if (status) setRunStatus(status);
      },
      { baseUrl, initialAfterSeq: snapshotMaxSeq },
    );

    return disconnect;
  }, [runId, baseUrl, snapshotMaxSeq]);

  const thinking = useMemo(
    () => events.filter((ev) => ev.type === "thinking").map((ev) => ev.detail).filter(Boolean),
    [events],
  );

  const timeline = useMemo(
    () =>
      events.filter(isTimelineEvent).filter((ev) => {
        if (ev.type === "thinking") return false;
        if (ev.type === "status" && ev.status === "running") return false;
        return true;
      }),
    [events],
  );

  const badges = useMemo(() => collectEvidenceBadges(events), [events]);
  const finalReady = events.some(
    (ev) => ev.type === "task" && ev.status === "final.ready",
  );

  if (error) {
    return (
      <div className="run-progress panel-error">
        <p>{error}</p>
      </div>
    );
  }

  return (
    <div className="run-progress">
      <header className="run-header">
        <div className="run-header-top">
          <span className={statusClass(runStatus)}>{runStatus}</span>
          {finalReady && <span className="pill pill-ready">Ready to answer</span>}
        </div>
        <div className="run-meta">
          <code>{meta?.run_id ?? runId}</code>
          {meta?.turn_index != null && meta.turn_index > 0 && (
            <span>turn {meta.turn_index}</span>
          )}
          {meta?.parent_run_id && (
            <span className="parent">parent {meta.parent_run_id}</span>
          )}
        </div>
        {meta?.query && <p className="run-query">{meta.query}</p>}
      </header>

      {thinking.length > 0 && (
        <section className="thinking-block">
          <button
            type="button"
            className="thinking-toggle"
            onClick={() => setThinkingOpen((v) => !v)}
          >
            Thinking {thinkingOpen ? "▾" : "▸"}
          </button>
          {thinkingOpen && (
            <pre className="thinking-body">{thinking.join("\n\n")}</pre>
          )}
        </section>
      )}

      <section className="evidence-badges">
        <h3>Evidence</h3>
        <div className="badge-row">
          {badges.map((b) => (
            <span
              key={b.key}
              className={`badge ${b.collected ? "badge-on" : "badge-off"}`}
              title={b.detail ?? ""}
            >
              {b.label}
            </span>
          ))}
        </div>
      </section>

      <section className="timeline">
        <h3>Timeline</h3>
        <ol>
          {timeline.map((ev) => (
            <li key={`${ev.seq}-${ev.type}-${ev.status ?? ""}`} className={`tl-${ev.type}`}>
              <span className="tl-icon">{timelineIcon(ev)}</span>
              <div className="tl-body">
                <span className="tl-label">{ev.label}</span>
                {ev.detail && <span className="tl-detail">{ev.detail}</span>}
              </div>
              <span className="tl-seq">{ev.seq}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
