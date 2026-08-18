import { useEffect, useState } from "react";
import { RunEvent } from "../api";

// The run diary. Everything the system recorded about itself: which
// files it used, how long each stage took, and — the reason this exists —
// every failure it absorbed and carried on from. Shared by both run types;
// each passes its own fetcher and its own stage names.
export default function ActivityLog({
  runId,
  fetchEvents,
  stageLabels,
}: {
  runId: string;
  fetchEvents: (id: string, onlyProblems: boolean) => Promise<RunEvent[]>;
  stageLabels: Record<string, string>;
}) {
  const [events, setEvents] = useState<RunEvent[] | null>(null);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchEvents(runId, onlyProblems)
      .then((e) => alive && setEvents(e))
      .catch(() => alive && setError("Could not load the activity log"));
    return () => {
      alive = false;
    };
  }, [runId, onlyProblems, fetchEvents]);

  if (error) return <p className="error">{error}</p>;
  if (!events) return <p className="sub">Loading…</p>;

  return (
    <div>
      <p className="summary-line">
        <b>What the system did with this batch</b>
        <label className="filter">
          <input
            type="checkbox"
            checked={onlyProblems}
            onChange={(e) => setOnlyProblems(e.target.checked)}
          />
          Only show problems
        </label>
      </p>
      {events.length === 0 && (
        <p className="sub">
          {onlyProblems
            ? "Nothing went wrong — no warnings or errors were recorded."
            : "Nothing recorded for this run."}
        </p>
      )}
      {events.map((e) => (
        <div key={e.id} className={`card event ${e.level}`}>
          <div className="row" style={{ border: "none", padding: 0 }}>
            <div className="grow">
              <b>{stageLabels[e.stage] ?? e.stage}</b>
              <span className="sub">{new Date(e.at).toLocaleTimeString()}</span>
            </div>
            <span
              className={`chip ${e.level === "error" ? "flag" : e.level === "warning" ? "review" : "ok"}`}
            >
              {e.level}
            </span>
          </div>
          <p className="reason">{e.message}</p>
          {/* The engineer's version, folded away: a reviewer never needs
              it, and whoever is debugging always asks for it. */}
          {e.detail && (
            <details>
              <summary className="sub">Technical detail</summary>
              <pre>{e.detail}</pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
