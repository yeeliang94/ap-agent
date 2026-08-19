import { useCallback, useEffect, useRef, useState } from "react";
import { ClaimsRunDetail, getClaimsRun, getClaimsRunEvents } from "../api";
import ActivityLog from "../components/ActivityLog";
import MapView from "./claims/MapView";
import GroupView from "./claims/GroupView";
import VerifyingView from "./claims/VerifyingView";
import ReviewView from "./claims/ReviewView";
import OutputView from "./claims/OutputView";
import { claimsStatusLabel } from "./ClaimsList";

const STAGE_LABEL: Record<string, string> = {
  run: "Run",
  source: "Reading the folder",
  survey: "Survey",
  map: "Map",
  verify: "Verify",
  output: "Output",
};

type Tab = "map" | "verifying" | "review" | "output" | "activity";

// One claims run: Map & Rules → Verifying → Review → Output, plus Activity.
export default function ClaimsRunDetailScreen({ runId }: { runId: string }) {
  const [run, setRun] = useState<ClaimsRunDetail | null>(null);
  const [chosenTab, setTab] = useState<Tab | null>(null);
  const [error, setError] = useState("");
  // Each poll is numbered; a reply from an older poll that lands after a
  // newer one is dropped, so the screen never steps backwards.
  const seq = useRef(0);

  const reload = useCallback(() => {
    const mine = ++seq.current;
    return getClaimsRun(runId)
      .then((r) => {
        if (mine !== seq.current) return;
        setRun(r);
        setError("");
      })
      .catch(() => {
        if (mine !== seq.current) return;
        setError("Could not load the claims run");
      });
  }, [runId]);
  useEffect(() => {
    reload();
  }, [reload]);

  // Poll every 3 s while the system is working (mapping, verifying), so
  // chips move without a refresh; stop once the run is at rest.
  const working = !!run && ["queued", "surveying", "mapping", "verifying"].includes(run.status);
  useEffect(() => {
    if (!working) return;
    const t = setInterval(reload, 3000);
    return () => clearInterval(t);
  }, [working, reload]);

  // A failed poll shows a notice above the last good screen; only when
  // nothing has ever loaded does it replace the screen.
  if (error && !run) return <p className="error">{error}</p>;
  if (!run) return <p className="sub">Loading…</p>;

  const failed = run.status === "failed";
  const openFlags = run.flags.filter((f) => f.status === "open");
  const defaultTab: Tab = failed
    ? "activity"
    : run.status === "ready"
      ? "review"
      : run.status === "verifying"
        ? "verifying"
        : "map";
  const tab = chosenTab ?? defaultTab;
  const mapExists = "employees" in run.map;
  // The case model (H6): when the server sends cases, Map & Group is the
  // one map screen; the delivered MapView is the fallback while
  // CLAIMS_CASE_MODEL is off.
  const caseModel = Array.isArray(run.cases) && !!run.grouping;

  return (
    <section>
      {error && <p className="error">{error} — showing the last good state; retrying.</p>}
      <p className="summary-line">
        <b>{run.client}</b> · <span className="mono">{run.folder}</span> ·{" "}
        {new Date(run.created_at).toLocaleString()} ·{" "}
        <span className={`chip ${failed ? "flag" : run.status === "ready" && !openFlags.length ? "ok" : "review"}`}>
          {claimsStatusLabel(run)}
        </span>
      </p>
      {failed ? (
        <div className="card banner bad">
          <b>This run stopped before it finished</b>
          <span className="sub">{run.error || "No reason was recorded."}</span>
          <span className="sub">
            Nothing from this run may be used. Fix the cause (see Activity below), then start a
            new run.
          </span>
        </div>
      ) : run.errors > 0 ? (
        <div className="card banner bad">
          <b>{run.errors} error{run.errors === 1 ? "" : "s"} happened while this run was processed</b>
          <span className="sub">
            Some employees may be incomplete. Open Activity to see what failed before you trust
            this run's output.
          </span>
        </div>
      ) : null}
      {(run.status === "queued" || run.status === "surveying" || run.status === "mapping") && (
        <div className="card">
          <b>{claimsStatusLabel(run)}…</b>
          <span className="sub">
            {run.status === "mapping"
              ? "The agent is looking inside every file to work out which is which. This takes about a minute."
              : "Copying the batch into this run's own workspace and looking at every file."}
          </span>
          <div className="skeleton" aria-hidden />
        </div>
      )}
      <div className="tabs">
        <button className={tab === "map" ? "tab active" : "tab"} disabled={!mapExists && !caseModel} onClick={() => setTab("map")}
          title={mapExists || caseModel ? "" : "The map is not ready yet"}>
          Map &amp; Group {(run.map_warnings.length + (run.grouping?.problems.length ?? 0)) > 0 && <em>{run.map_warnings.length + (run.grouping?.problems.length ?? 0)}</em>}
        </button>
        <button className={tab === "verifying" ? "tab active" : "tab"} disabled={run.employees.length === 0}
          onClick={() => setTab("verifying")} title={run.employees.length ? "" : "Verification has not started"}>
          Verifying
        </button>
        <button className={tab === "review" ? "tab active" : "tab"} disabled={run.status !== "ready"}
          onClick={() => setTab("review")} title={run.status === "ready" ? "" : "Review opens once every employee is verified"}>
          Review {openFlags.length > 0 && <em>{openFlags.length}</em>}
        </button>
        <button className={tab === "output" ? "tab active" : "tab"} disabled={run.status !== "ready"}
          onClick={() => setTab("output")} title={run.status === "ready" ? "" : "Output opens once review is complete"}>
          Output
        </button>
        <button className={tab === "activity" ? "tab active" : "tab"} onClick={() => setTab("activity")}>
          Activity {run.errors + run.warnings > 0 && <em className={run.errors > 0 ? "bad" : ""}>{run.errors + run.warnings}</em>}
        </button>
      </div>
      {tab === "map" && caseModel && (
        <GroupView
          key={`${run.status}-${run.revision}`}
          run={run}
          onChanged={reload}
          onConfirmed={() => {
            setTab("verifying");
            reload();
          }}
        />
      )}
      {tab === "map" && !caseModel && mapExists && (
        <MapView
          key={run.status}
          run={run}
          onConfirmed={() => {
            setTab("verifying");
            reload();
          }}
        />
      )}
      {tab === "verifying" && <VerifyingView run={run} onChanged={reload} />}
      {tab === "review" && <ReviewView run={run} onChanged={reload} />}
      {tab === "output" && <OutputView run={run} onGoReview={() => setTab("review")} />}
      {tab === "activity" && (
        <ActivityLog runId={run.id} fetchEvents={getClaimsRunEvents} stageLabels={STAGE_LABEL} />
      )}
    </section>
  );
}
