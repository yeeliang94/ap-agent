import { useCallback, useEffect, useRef, useState } from "react";
import { ClaimsRunDetail, cancelClaimsRun, getClaimsRun, getClaimsRunEvents } from "../api";
import ActivityLog from "../components/ActivityLog";
import { InlineConfirm } from "../components/Inline";
import { useAction } from "../hooks/useAction";
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

// Where the run stands, as a quiet horizontal stepper (the redesign's
// run-detail spec). Done steps are soft accent, the current one outlined.
const STEPS = ["Ingest", "Map", "Confirm", "Verify", "Review"] as const;
function stepIndex(status: string, openFlags: number): number {
  switch (status) {
    case "queued":
    case "surveying":
      return 0;
    case "mapping":
      return 1;
    case "map_ready":
      return 2;
    case "verifying":
      return 3;
    case "ready":
      return openFlags ? STEPS.length - 1 : STEPS.length; // past the end = all done
    default:
      return 0;
  }
}

function StageStepper({ status, openFlags }: { status: string; openFlags: number }) {
  const now = stepIndex(status, openFlags);
  return (
    <div className="stepper" aria-label="Run stages">
      {STEPS.map((label, i) => (
        <span key={label} className={`step ${i < now ? "done" : i === now ? "now" : ""}`}>
          <span className="n">{i < now ? "✓" : i + 1}</span>
          {label}
          <span className="connector" />
        </span>
      ))}
    </div>
  );
}

/** The run is still working (the server's IN_PROGRESS_STATUSES): the
 *  screen polls, and only such a run can be cancelled. */
const WORKING_STATUSES = ["queued", "surveying", "mapping", "verifying"];

// One claims run: Map & Rules → Verifying → Review → Output, plus Activity.
export default function ClaimsRunDetailScreen({ runId }: { runId: string }) {
  const [run, setRun] = useState<ClaimsRunDetail | null>(null);
  const [chosenTab, setTab] = useState<Tab | null>(null);
  const [error, setError] = useState("");
  const [confirmingCancel, setConfirmingCancel] = useState(false);
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
  // Cancelling goes through the shared action hook like every other
  // mutation: the run is re-read (and the re-read awaited) before the
  // button is released, and a stale run reloads before it says so.
  const cancel = useAction(reload, "Could not cancel the run");

  // Poll while the system is working (mapping, verifying) so the chips
  // move without a refresh; stop once the run is at rest. A long run backs
  // off — every 3 s for the first minute (when steps land quickly), every
  // 5 s after that — so an hour-long batch is not re-read 1200 times.
  const working = !!run && WORKING_STATUSES.includes(run.status);
  useEffect(() => {
    if (!working) return;
    const startedAt = Date.now();
    let timer = 0;
    const tick = () => {
      reload();
      timer = window.setTimeout(tick, Date.now() - startedAt > 60_000 ? 5000 : 3000);
    };
    timer = window.setTimeout(tick, 3000);
    return () => clearTimeout(timer);
  }, [working, reload]);

  // Review and Output only exist while the run is `ready`. A re-verify
  // takes it back to `verifying`: the tab that was chosen is dropped so
  // the screen falls back to the default for the new status, instead of
  // leaving an active-but-empty tab behind a disabled button.
  const status = run?.status;
  useEffect(() => {
    if (status && status !== "ready" && (chosenTab === "review" || chosenTab === "output")) setTab(null);
  }, [status, chosenTab]);

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
      <div className="hero" style={{ alignItems: "flex-start", marginBottom: 6 }}>
        <div>
          <h1>{run.client}</h1>
          <span className="sub">
            {new Date(run.created_at).toLocaleString()} ·{" "}
            <span title={run.folder}>{run.folder.length > 60 ? "…" + run.folder.slice(-60) : run.folder}</span>
            {run.received_date ? ` · received ${run.received_date}` : ""}
          </span>
        </div>
        <span className={`chip ${failed ? "flag" : run.status === "ready" && !openFlags.length ? "ok" : "review"}`}>
          {claimsStatusLabel(run)}
        </span>
      </div>
      {!failed && <StageStepper status={run.status} openFlags={openFlags.length} />}
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
      {/* Stopping a run that is still working (H11). It is destructive —
          the run is marked failed and nothing from it may be used — so it
          is asked in place, never on the first click. */}
      {working && (
        <div className="actions">
          {confirmingCancel ? (
            <InlineConfirm
              question="Stop this run? The workers stop where they are, the run is marked failed, and nothing from it may be used. Starting again means a new run."
              confirmLabel="Yes — stop this run"
              busy={!!cancel.busy}
              onConfirm={async () => {
                if (await cancel.run(() => cancelClaimsRun(run.id, run.revision))) setConfirmingCancel(false);
              }}
              onCancel={() => setConfirmingCancel(false)}
            />
          ) : (
            <button className="btn warn" disabled={!!cancel.busy} onClick={() => setConfirmingCancel(true)}>
              Stop this run
            </button>
          )}
        </div>
      )}
      {cancel.error && <p className="error">{cancel.error}</p>}
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
          key={run.status}
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
          onChanged={reload}
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
