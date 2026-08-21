import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ClaimsRunDetail, RunDetailData, RunEvent, RunProgress,
  getClaimsRun, getClaimsRunEvents, getRun, getRunEvents,
} from "../api";
import CopyBlock from "../components/CopyBlock";
import { useRunActivity } from "../activity";
import { Link } from "../router";
import { runDestination, runPath, sentence } from "../runPresentation";
import ClaimsOrganize from "./ClaimsOrganize";
import ReviewWorkbench from "./ReviewWorkbench";

const WORKING = new Set(["queued", "sorting", "extracting", "checking", "surveying", "mapping", "verifying"]);
const STEP_COPY: Record<string, string> = {
  reading_references: "Reading the workspace references",
  sorting_documents: "Sorting the uploaded documents",
  reading_documents: "Reading invoice and claim details",
  running_checks: "Running policy and payment checks",
  building_output: "Building the output",
  preparing_files: "Preparing the submitted files",
  inspecting_contents: "Inspecting file contents",
  organizing_files: "Organizing files into claims",
  auditing_assignments: "Auditing uncertain assignments",
  reading_claim_summary: "Reading claim summaries",
  reading_evidence: "Reading supporting evidence",
  checking_mileage: "Checking mileage",
  matching_evidence: "Matching evidence to claim lines",
  deciding_category: "Deciding the payment category",
  finalizing_claim: "Finalizing the claim",
  organization_ready: "Organization ready",
  review_ready: "Review ready",
  claim_complete: "Claim complete",
  cancelled: "Cancelled",
  interrupted: "Interrupted by a server restart",
  failed: "Processing stopped",
};

function ageLabel(at?: string) {
  if (!at) return "No recent update";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 1000));
  if (seconds < 5) return "Updated just now";
  if (seconds < 60) return `Updated ${seconds} seconds ago`;
  return `Updated ${Math.floor(seconds / 60)} minutes ago`;
}

function elapsedLabel(start: string) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(start).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function ProgressScreen({ kind, run, onActivity }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail; onActivity: () => void }) {
  const progress = run.progress || {};
  const working = WORKING.has(run.status);
  const items = kind === "invoice"
    ? (run as RunDetailData).documents.map((d) => ({ id: d.id, name: d.filename, status: d.status, progress: undefined as RunProgress | undefined, error: d.error }))
    : (run as ClaimsRunDetail).employees.map((e) => ({ id: e.id, name: e.name || e.folder, status: e.status, progress: e.progress, error: e.error }));
  const [all, setAll] = useState(false);
  const now = Date.now();
  const changed = progress.updated_at ? new Date(progress.updated_at).getTime() : now;
  const staleSeconds = Math.max(0, Math.round((now - changed) / 1000));
  const done = Number(progress.done ?? 0), total = Number(progress.total ?? 0);
  const completed = items.filter((i) => ["checked", "verified", "failed", "skipped", "error"].includes(i.status)).length;
  const failed = items.filter((i) => ["failed", "error"].includes(i.status)).length;
  const active = items.filter((i) => ["extracting", "verifying"].includes(i.status));
  const displayItems = active.length ? [...active, ...items.filter((i) => !active.includes(i))] : items;
  const waiting = Math.max(0, items.length - completed - active.length);
  if (!working) {
    const organization = kind === "claim" && run.status === "map_ready";
    return <div className="completion-panel"><span className="completion-mark" aria-hidden>✓</span><h2>{organization ? "Organization ready" : run.status === "failed" ? "Run stopped" : "Review ready"}</h2><p>{run.status === "failed" ? run.error || "The run could not finish." : organization ? "The submitted files are organized into proposed claims and need confirmation." : "Background processing is complete. Nothing will redirect you away from this page."}</p>{run.status !== "failed" ? <Link className="btn primary" to={runDestination(kind, run)}>{organization ? "Organize claims" : "Open review"}</Link> : <button className="btn" onClick={onActivity}>Open activity</button>}</div>;
  }
  return <div className="progress-panel">
    <div className="progress-lead"><span className="status working">{sentence(progress.phase || "Working")}</span><h2>{STEP_COPY[progress.step || ""] || "Processing the run"}</h2><p>{total > 0 ? `${done} of ${total} ${progress.unit || (kind === "claim" ? "claims" : "documents")} complete` : "The run is moving through this step."}</p></div>
    <div className="progress-breakdown" aria-label="Run breakdown"><span><strong>{active.length}</strong> Active</span><span><strong>{completed - failed}</strong> Completed</span><span><strong>{waiting}</strong> Waiting</span><span><strong>{failed}</strong> Failed</span></div>
    <div className="progress-time"><span>Elapsed {elapsedLabel(run.created_at)} · {ageLabel(progress.updated_at)}</span>{staleSeconds >= 180 ? <strong>Taking longer than usual.</strong> : staleSeconds >= 60 ? <strong>Still working on this step.</strong> : null}</div>
    {displayItems.length ? <div className="active-items"><h3>{kind === "claim" ? "Claims" : "Documents"}</h3>{displayItems.slice(0, all ? displayItems.length : 5).map((item) => <div className="active-item" key={item.id}><span>{item.name}</span><span>{STEP_COPY[item.progress?.step || ""] || sentence(item.status)}</span></div>)}{displayItems.length > 5 ? <button className="text-button" onClick={() => setAll(!all)} aria-expanded={all}>{all ? "Show less" : `Show all ${displayItems.length}`}</button> : null}</div> : null}
    <p className="leave-note">You can leave this page; processing will continue.</p>
  </div>;
}

function RunHeader({ kind, run, view, onActivity }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail; view: string; onActivity: () => void }) {
  const isClaim = kind === "claim";
  return <><header className="run-header"><div><Link className="back-link" to={kind === "invoice" ? "/invoices" : "/claims"}>← {isClaim ? "Claim" : "Invoice"} runs</Link><h1>{run.client}</h1><p>{new Date(run.created_at).toLocaleString()} · {run.id}</p></div><span className={`status ${run.status === "failed" ? "failed" : WORKING.has(run.status) ? "working" : "ready"}`}>{sentence(run.status)}</span></header><nav className="run-destinations" aria-label="Run destinations">{isClaim ? <Link to={runPath(kind, run.id, "organize")} className={view === "organize" ? "current" : ""} ariaCurrent={view === "organize" ? "page" : undefined}>Organize</Link> : null}<Link to={runPath(kind, run.id, "review")} className={view === "review" ? "current" : ""} ariaCurrent={view === "review" ? "page" : undefined}>Review</Link><Link to={runPath(kind, run.id, "export")} className={view === "export" ? "current" : ""} ariaCurrent={view === "export" ? "page" : undefined}>Export</Link><button onClick={onActivity}>Activity{run.errors + run.warnings ? ` (${run.errors + run.warnings})` : ""}</button></nav></>;
}

function ActivityDrawer({ open, onClose, events, run }: { open: boolean; onClose: () => void; events: RunEvent[]; run: RunDetailData | ClaimsRunDetail }) {
  const groups = useMemo(() => {
    const result = new Map<string, RunEvent[]>();
    events.forEach((event) => result.set(event.stage, [...(result.get(event.stage) || []), event]));
    return [...result.entries()];
  }, [events]);
  if (!open) return null;
  const claimRun = "investigation" in run;
  return <div className="drawer-layer"><button className="drawer-backdrop" aria-label="Close activity" onClick={onClose} /><aside className="activity-drawer" role="dialog" aria-modal="true" aria-labelledby="activity-title"><header><div><h2 id="activity-title">Activity</h2><p>Stages, warnings, and recorded events.</p></div><button className="icon-button" aria-label="Close activity" onClick={onClose}>×</button></header><div className="stage-timeline">{groups.map(([stage, stageEvents]) => { const errors = stageEvents.filter((e) => e.level === "error").length; const warnings = stageEvents.filter((e) => e.level === "warning").length; return <details key={stage} open={errors > 0}><summary><span className={`stage-dot ${errors ? "failed" : "done"}`} /><span><strong>{sentence(stage)}</strong><small>{stageEvents.length} events{warnings ? ` · ${warnings} warnings` : ""}</small></span></summary><div className="stage-events">{stageEvents.map((e) => <div key={e.id}><time>{new Date(e.at).toLocaleTimeString()}</time><p>{e.message}</p>{e.detail ? <details><summary>Technical details</summary><pre>{e.detail}</pre></details> : null}</div>)}</div></details>; })}</div>{claimRun && ((run as ClaimsRunDetail).investigation || (run as ClaimsRunDetail).tool_summary) ? <details className="technical"><summary>Technical details</summary><pre>{JSON.stringify({ investigation: (run as ClaimsRunDetail).investigation, tools: (run as ClaimsRunDetail).tool_summary }, null, 2)}</pre></details> : null}</aside></div>;
}

function ExportView({ kind, run }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail }) {
  const unlocked = run.status === "ready" && run.open_flags === 0; const blockers = kind === "claim" ? (run as ClaimsRunDetail).output_blockers || [] : run.open_flags ? [`${run.open_flags} unresolved findings`] : [];
  if (!unlocked) return <div className="export-layout"><header><span className="status working">Export locked</span><h2>Resolve the review gate</h2><p>The export remains visible so you can see exactly what is blocking it.</p></header><div className="alert danger"><div><strong>Export is not ready</strong>{(blockers.length ? blockers : ["Background processing is not complete."]).map((b) => <p key={b}>{b.replaceAll("_", " ")}</p>)}</div></div><Link className="btn primary" to={runPath(kind, run.id, "review")}>Review unresolved findings</Link></div>;
  if (kind === "claim") { const output = (run as ClaimsRunDetail).outputs; if (!("tsv" in output)) return <p>No output was built.</p>; return <div className="export-layout"><header><span className="status ready">Gate passed</span><h2>Payment listing</h2><p>{output.included.length} claims included · {output.exclusions.length + output.not_included.length} excluded or not included</p></header><div className="export-totals"><div><span>Total payable</span><strong>RM {output.totals.total_myr}</strong></div><div><span>Reconciliation</span><strong>{output.totals.match ? "Matched" : `Difference RM ${output.totals.difference}`}</strong></div></div><CopyBlock title="Payment listing rows" hint="Paste into the reviewed client workbook" text={output.tsv} preview={output.rows.map((r) => r.join(" · "))} /><details><summary>Excluded and not included ({output.exclusions.length + output.not_included.length})</summary>{[...output.exclusions.map((x) => `${x.name}: RM ${x.amount} — ${x.why}`), ...output.not_included.map((x) => `${x.name}: ${x.why}`)].map((x) => <p key={x}>{x}</p>)}</details></div>; }
  const output = (run as RunDetailData).outputs; if (!("bank_rows" in output)) return <p>No output was built.</p>; return <div className="export-layout"><header><span className="status ready">Gate passed</span><h2>Invoice output</h2><p>{output.bank_rows.length} payment rows are ready.</p></header><div className="export-totals"><div><span>Bank total</span><strong>RM {output.totals.bank.toFixed(2)}</strong></div><div><span>Reconciliation</span><strong>{output.totals.match ? "Matched" : "Mismatch"}</strong></div></div><CopyBlock title="Bank entry rows" hint="Copy into the reviewed bank upload template" text={[output.bank_header, ...output.bank_rows].join("\n")} preview={output.bank_rows} /><CopyBlock title="Proposed file names" hint="Apply when filing invoices" text={output.filenames.join("\n")} preview={output.filenames} /></div>;
}

export default function RunWorkspace({ kind, runId, view }: { kind: "invoice" | "claim"; runId: string; view: string }) {
  const [run, setRun] = useState<RunDetailData | ClaimsRunDetail | null>(null), [events, setEvents] = useState<RunEvent[]>([]), [error, setError] = useState(""), [activity, setActivity] = useState(false); const failures = useRef(0); const { refresh: refreshLists } = useRunActivity();
  const reload = useCallback(async () => { try { const result = kind === "invoice" ? await getRun(runId) : await getClaimsRun(runId); setRun(result); failures.current = 0; setError(""); return; } catch { failures.current += 1; if (failures.current >= 2) setError("Reconnecting—the run continues on the server"); } }, [kind, runId]);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { if (!run || !WORKING.has(run.status)) return; const timer = window.setInterval(() => void reload(), 3000); return () => clearInterval(timer); }, [run?.status, reload]);
  const openActivity = useCallback(() => { setActivity(true); const request = kind === "invoice" ? getRunEvents(runId) : getClaimsRunEvents(runId); void request.then(setEvents); }, [kind, runId]);
  const refresh = useCallback(async () => { await reload(); await refreshLists(); }, [reload, refreshLists]);
  if (!run) return <section className="standard-page" aria-busy={!error}>{error ? <div className="alert warning" role="status">{error}</div> : <><p className="sr-only" role="status">Loading run…</p><div className="skeleton-page" aria-hidden><span className="skeleton w-25" /><span className="skeleton tall w-40" /><span className="skeleton w-60" /><span className="skeleton block" /></div></>}</section>;
  const workbench = view === "organize" || view === "review";
  return <section className={workbench ? "workbench-page" : "standard-page"}>{error ? <div className="alert warning" role="status">{error}</div> : null}<RunHeader kind={kind} run={run} view={view} onActivity={openActivity} /><div className="run-content">{view === "progress" ? <ProgressScreen kind={kind} run={run} onActivity={openActivity} /> : view === "organize" && kind === "claim" ? <ClaimsOrganize run={run as ClaimsRunDetail} reload={refresh} /> : view === "review" ? <ReviewWorkbench kind={kind} run={run} reload={refresh} /> : view === "export" ? <ExportView kind={kind} run={run} /> : <ProgressScreen kind={kind} run={run} onActivity={openActivity} />}</div><ActivityDrawer open={activity} onClose={() => setActivity(false)} events={events} run={run} /></section>;
}
