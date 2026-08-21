import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ClaimArtifact, ClaimCase, ClaimFlag, ClaimsRunDetail, Doc, FlagItem, ReviewFinding, ReviewGroup,
  RunDetailData, RunEvent, RunProgress, claimsFileUrl, confirmGrouping, decideClaimFlag, decideFlag,
  documentFileUrl, getClaimsRun, getClaimsRunEvents, getRun, getRunEvents, setArtifactDisposition, setClaimant,
  correctFields, correctClaimRow, CORRECTABLE,
  moveArtifact,
} from "../api";
import CopyBlock from "../components/CopyBlock";
import { useRunActivity } from "../activity";
import { Link, setQuery, useRouter } from "../router";

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

function ProgressScreen({ kind, run }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail }) {
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
    return <div className="completion-panel"><span className="completion-mark" aria-hidden>✓</span><h2>{organization ? "Organization ready" : run.status === "failed" ? "Run stopped" : "Review ready"}</h2><p>{run.status === "failed" ? run.error || "The run could not finish." : organization ? "The submitted files are organized into proposed claims and need confirmation." : "Background processing is complete. Nothing will redirect you away from this page."}</p>{run.status !== "failed" ? <Link className="btn primary" to={`/${kind === "invoice" ? "invoices" : "claims"}/${run.id}/${organization ? "organize" : "review"}`}>{organization ? "Organize claims" : "Open review"}</Link> : <button className="btn" onClick={() => window.dispatchEvent(new CustomEvent("open-activity"))}>Open activity</button>}</div>;
  }
  return <div className="progress-panel">
    <div className="progress-lead"><span className="status working">{progress.phase || "Working"}</span><h2>{STEP_COPY[progress.step || ""] || "Processing the run"}</h2><p>{total > 0 ? `${done} of ${total} ${progress.unit || (kind === "claim" ? "claims" : "documents")} complete` : "The run is moving through this step."}</p></div>
    <div className="progress-breakdown" aria-label="Run breakdown"><span><strong>{active.length}</strong> Active</span><span><strong>{completed - failed}</strong> Completed</span><span><strong>{waiting}</strong> Waiting</span><span><strong>{failed}</strong> Failed</span></div>
    <div className="progress-time"><span>Elapsed {elapsedLabel(run.created_at)} · {ageLabel(progress.updated_at)}</span>{staleSeconds >= 180 ? <strong>Taking longer than usual.</strong> : staleSeconds >= 60 ? <strong>Still working on this step.</strong> : null}</div>
    {displayItems.length ? <div className="active-items"><h3>{kind === "claim" ? "Claims" : "Documents"}</h3>{displayItems.slice(0, all ? displayItems.length : 5).map((item) => <div className="active-item" key={item.id}><span>{item.name}</span><span>{STEP_COPY[item.progress?.step || ""] || item.status}</span></div>)}{displayItems.length > 5 ? <button className="text-button" onClick={() => setAll(!all)} aria-expanded={all}>{all ? "Show less" : `Show all ${displayItems.length}`}</button> : null}</div> : null}
    <p className="leave-note">You can leave this page; processing will continue.</p>
  </div>;
}

function RunHeader({ kind, run, view, onActivity }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail; view: string; onActivity: () => void }) {
  const base = `/${kind === "invoice" ? "invoices" : "claims"}/${run.id}`;
  const isClaim = kind === "claim";
  return <><header className="run-header"><div><Link className="back-link" to={kind === "invoice" ? "/invoices" : "/claims"}>← {isClaim ? "Claim" : "Invoice"} runs</Link><h1>{run.client}</h1><p>{new Date(run.created_at).toLocaleString()} · {run.id}</p></div><span className={`status ${run.status === "failed" ? "failed" : WORKING.has(run.status) ? "working" : "ready"}`}>{run.status.replaceAll("_", " ")}</span></header><nav className="run-destinations" aria-label="Run destinations">{isClaim ? <Link to={`${base}/organize`} className={view === "organize" ? "current" : ""} ariaCurrent={view === "organize" ? "page" : undefined}>Organize</Link> : null}<Link to={`${base}/review`} className={view === "review" ? "current" : ""} ariaCurrent={view === "review" ? "page" : undefined}>Review</Link><Link to={`${base}/export`} className={view === "export" ? "current" : ""} ariaCurrent={view === "export" ? "page" : undefined}>Export</Link><button onClick={onActivity}>Activity{run.errors + run.warnings ? ` (${run.errors + run.warnings})` : ""}</button></nav></>;
}

function ActivityDrawer({ open, onClose, events, run }: { open: boolean; onClose: () => void; events: RunEvent[]; run: RunDetailData | ClaimsRunDetail }) {
  const groups = useMemo(() => {
    const result = new Map<string, RunEvent[]>();
    events.forEach((event) => result.set(event.stage, [...(result.get(event.stage) || []), event]));
    return [...result.entries()];
  }, [events]);
  if (!open) return null;
  const claimRun = "investigation" in run;
  return <div className="drawer-layer"><button className="drawer-backdrop" aria-label="Close activity" onClick={onClose} /><aside className="activity-drawer" role="dialog" aria-modal="true" aria-labelledby="activity-title"><header><div><h2 id="activity-title">Activity</h2><p>Stages, warnings, and recorded events.</p></div><button className="icon-button" aria-label="Close activity" onClick={onClose}>×</button></header><div className="stage-timeline">{groups.map(([stage, stageEvents]) => { const errors = stageEvents.filter((e) => e.level === "error").length; const warnings = stageEvents.filter((e) => e.level === "warning").length; return <details key={stage} open={errors > 0}><summary><span className={`stage-dot ${errors ? "failed" : "done"}`} /><span><strong>{stage.replaceAll("_", " ")}</strong><small>{stageEvents.length} events{warnings ? ` · ${warnings} warnings` : ""}</small></span></summary><div className="stage-events">{stageEvents.map((e) => <div key={e.id}><time>{new Date(e.at).toLocaleTimeString()}</time><p>{e.message}</p>{e.detail ? <details><summary>Technical details</summary><pre>{e.detail}</pre></details> : null}</div>)}</div></details>; })}</div>{claimRun && ((run as ClaimsRunDetail).investigation || (run as ClaimsRunDetail).tool_summary) ? <details className="technical"><summary>Technical details</summary><pre>{JSON.stringify({ investigation: (run as ClaimsRunDetail).investigation, tools: (run as ClaimsRunDetail).tool_summary }, null, 2)}</pre></details> : null}</aside></div>;
}

function ClaimsOrganize({ run, reload }: { run: ClaimsRunDetail; reload: () => Promise<void> }) {
  const cases = run.cases || [];
  const artifacts = run.artifacts || [];
  const { location } = useRouter();
  const initial = new URLSearchParams(location.search).get("claim") || cases[0]?.id || "";
  const [selectedId, setSelectedId] = useState(initial);
  const selected = cases.find((c) => c.id === selectedId) || cases[0];
  const [drawer, setDrawer] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const problems = run.grouping?.by_case?.[selected?.id || ""] || [];
  const assigned = artifacts.filter((a) => a.case_id === selected?.id);
  const unassigned = artifacts.filter((a) => !a.case_id);
  const choose = (id: string) => { setSelectedId(id); window.history.replaceState({}, "", setQuery({ claim: id })); };
  const settle = async (artifact: ClaimArtifact, disposition: "irrelevant" | "duplicate" | "unreadable") => { setBusy(true); setError(""); try { await setArtifactDisposition(run.id, run.revision, artifact.id, disposition, `${disposition} during organization`); await reload(); } catch (e) { setError(e instanceof Error ? e.message : "Could not update file"); } finally { setBusy(false); } };
  const assign = async (artifact: ClaimArtifact) => { setBusy(true); setError(""); try { await moveArtifact(run.id, run.revision, artifact.id, selected.id); await reload(); } catch (e) { setError(e instanceof Error ? e.message : "Could not assign file"); } finally { setBusy(false); } };
  const confirm = async () => { setBusy(true); setError(""); try { await confirmGrouping(run.id, run.revision); await reload(); } catch (e) { setError(e instanceof Error ? e.message : "Could not confirm claims"); setBusy(false); } };
  if (!selected) return <div className="empty-state"><h2>No proposed claims</h2><p>Open Activity to inspect why no claims were found.</p></div>;
  return <div className="organize-workbench">
    <aside className="claim-index" aria-label="Claims"><label className="mobile-claim-select">Claim<select value={selected.id} onChange={(e) => choose(e.target.value)}>{cases.map((c) => <option value={c.id} key={c.id}>{c.label}</option>)}</select></label><div className="desktop-claim-index"><h2>Claims</h2>{cases.map((c) => { const p = run.grouping?.by_case?.[c.id]?.length || 0; return <button key={c.id} onClick={() => choose(c.id)} className={c.id === selected.id ? "selected" : ""} aria-current={c.id === selected.id ? "true" : undefined}><span>{c.label}</span><small>{p ? `${p} blocking` : c.claimant.state === "confirmed" ? "Ready" : "Needs confirmation"}</small></button>; })}</div></aside>
    <main className="claim-editor"><header><div><span className="eyebrow">Selected claim</span><h2>{selected.label}</h2></div><button className="btn files-button" onClick={() => setDrawer(true)}>Submitted files ({artifacts.length})</button></header>{problems.length ? <div className="alert danger"><div><strong>{problems.length} blocking problem{problems.length === 1 ? "" : "s"}</strong>{problems.map((p) => <p key={p}>{p}</p>)}</div></div> : null}<ClaimIdentity run={run} claim={selected} reload={reload} /><section className="editor-section"><h3>Claim summary</h3><dl><div><dt>Reported total</dt><dd>{selected.reported_total || "Not stated"}</dd></div><div><dt>Lines total</dt><dd>{selected.lines_total || "Not available"}</dd></div><div><dt>Grouping basis</dt><dd>{selected.grouping_basis || selected.reason || "Proposed from submitted files"}</dd></div></dl></section><section className="editor-section"><h3>Mileage source</h3><p>{selected.roles?.mileage_tab || "No separate mileage source assigned"}</p></section><section className="editor-section"><h3>Assigned evidence</h3><div className="compact-files">{assigned.map((a) => <div key={a.id}><span>{a.path}</span><small>{a.proposed_role.replaceAll("_", " ")} · {a.disposition}</small></div>)}</div></section>{run.grouping?.actions_enabled ? <details className="advanced-actions"><summary>Advanced actions</summary><p>Merge, split, and move actions are available from each submitted file’s context menu.</p></details> : null}</main>
    <SubmittedFiles open={drawer} onClose={() => setDrawer(false)} artifacts={artifacts} unassigned={unassigned} busy={busy} settle={settle} assign={assign} />
    <div className="decision-bar organize-decision"><span>{run.grouping?.ok ? `${cases.length} claims ready to check` : `${run.grouping?.problems.length || 0} blocking problems must be resolved`}</span><button className="btn primary" disabled={!run.grouping?.ok || busy || run.status !== "map_ready"} onClick={confirm}>{busy ? "Starting…" : "Confirm claims and start checks"}</button>{error ? <span className="error">{error}</span> : null}</div>
  </div>;
}

function ClaimIdentity({ run, claim, reload }: { run: ClaimsRunDetail; claim: ClaimCase; reload: () => Promise<void> }) {
  const [name, setName] = useState(claim.claimant.name || ""), [identifier, setIdentifier] = useState(claim.claimant.identifier || "");
  const [busy, setBusy] = useState(false);
  useEffect(() => { setName(claim.claimant.name || ""); setIdentifier(claim.claimant.identifier || ""); }, [claim.id, claim.claimant.name, claim.claimant.identifier]);
  const save = async () => { setBusy(true); try { await setClaimant(run.id, run.revision, claim.id, name, identifier); await reload(); } finally { setBusy(false); } };
  return <section className="editor-section"><h3>Claimant</h3><div className="field-grid"><label>Name<input value={name} onChange={(e) => setName(e.target.value)} /></label><label>Identifier<input value={identifier} onChange={(e) => setIdentifier(e.target.value)} /></label></div><div className="section-actions"><span className={`status ${claim.claimant.state === "confirmed" ? "ready" : "working"}`}>{claim.claimant.state}</span><button className="btn" disabled={busy || !name.trim()} onClick={save}>{busy ? "Saving…" : "Save claimant"}</button></div></section>;
}

function SubmittedFiles({ open, onClose, artifacts, unassigned, busy, settle, assign }: { open: boolean; onClose: () => void; artifacts: ClaimArtifact[]; unassigned: ClaimArtifact[]; busy: boolean; settle: (a: ClaimArtifact, d: "irrelevant" | "duplicate" | "unreadable") => void; assign: (a: ClaimArtifact) => void }) {
  return <aside className={`submitted-files ${open ? "open" : ""}`} aria-label="Submitted files"><header><div><h2>Submitted files</h2><p>{unassigned.length} unassigned</p></div><button className="icon-button files-button" onClick={onClose} aria-label="Close submitted files">×</button></header>{artifacts.map((a) => <div className="submitted-file" key={a.id}><strong>{a.path.split("/").pop()}</strong><span>{a.case_id ? "Assigned to claim" : "Unassigned file"} · {a.disposition}</span>{!a.case_id || a.disposition === "unresolved" ? <details><summary>File actions</summary>{!a.case_id ? <button disabled={busy} onClick={() => assign(a)}>Assign to claim</button> : null}<button disabled={busy} onClick={() => settle(a, "irrelevant")}>Mark irrelevant</button><button disabled={busy} onClick={() => settle(a, "duplicate")}>Mark duplicate</button><button disabled={busy} onClick={() => settle(a, "unreadable")}>Mark unreadable</button></details> : null}</div>)}</aside>;
}

type ReviewModel = { groups: ReviewGroup[]; findings: ReviewFinding[]; docs: Map<string, Doc>; flags: Map<string, FlagItem | ClaimFlag>; claimRun?: ClaimsRunDetail };
function buildReview(kind: "invoice" | "claim", run: RunDetailData | ClaimsRunDetail): ReviewModel {
  if (kind === "invoice") {
    const invoice = run as RunDetailData; const docs = new Map(invoice.documents.map((d) => [d.id, d])); const flags = new Map<string, FlagItem | ClaimFlag>();
    const findings = invoice.flags.map((f, i) => { flags.set(f.id, f); return { id: f.id, groupId: f.document_id || "run", title: f.code.replaceAll("_", " "), reason: f.reason, basis: f.basis, status: f.status, blocking: true, source: f.document_id ? { documentId: f.document_id, page: 1 } : { summary: f.basis }, sourceOrder: i } as ReviewFinding & { sourceOrder: number }; });
    const groupIds = new Set(findings.map((f) => f.groupId)); const groups = [...groupIds].map((id, i) => { const fs = findings.filter((f) => f.groupId === id); return { id, name: docs.get(id)?.filename || "Run checks", unresolved: fs.filter((f) => f.status === "open").length, complete: fs.every((f) => f.status !== "open"), sourceOrder: i }; });
    return { groups, findings, docs, flags };
  }
  const claims = run as ClaimsRunDetail; const cases = claims.cases?.length ? claims.cases : claims.employees.map((e) => ({ id: e.id, employee_id: e.id, label: e.name || e.folder } as ClaimCase)); const docs = new Map<string, Doc>(); const flags = new Map<string, FlagItem | ClaimFlag>();
  const groupFor = (flag: ClaimFlag) => flag.case_id || flag.employee_id || "run";
  const findings = claims.flags.map((f, i) => { const row = claims.rows.find((r) => r.id === f.row_id); const amount = row?.values?.total || row?.values?.amount; flags.set(f.id, f); return { id: f.id, groupId: groupFor(f), title: claims.catalogue?.[f.code]?.title || f.code.replaceAll("_", " "), reason: f.reason, basis: f.basis, status: f.status, blocking: f.status !== "info", amountAtRisk: amount ? String(amount) : undefined, source: f.cite ? { file: f.cite.file, page: f.cite.page || 1, position: f.cite.position, sheet: f.cite.sheet, row: f.cite.row } : undefined, sourceOrder: i } as ReviewFinding & { sourceOrder: number }; });
  const ids = new Set(findings.map((f) => f.groupId)); const groups = [...ids].map((id, i) => { const fs = findings.filter((f) => f.groupId === id); const c = cases.find((x) => x.id === id || x.employee_id === id); return { id, name: c?.label || c?.name || "Run checks", unresolved: fs.filter((f) => f.status === "open").length, amountAtRisk: c?.lines_total, complete: fs.every((f) => f.status !== "open"), sourceOrder: i }; });
  return { groups, findings, docs, flags, claimRun: claims };
}

function ReviewWorkbench({ kind, run, reload }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail; reload: () => Promise<void> }) {
  const { location, navigate } = useRouter(); const model = useMemo(() => buildReview(kind, run), [kind, run]); const query = new URLSearchParams(location.search);
  const ordered = [...model.findings].sort((a, b) => Number(b.status === "open" && b.blocking) - Number(a.status === "open" && a.blocking));
  const first = ordered[0]; const groupId = query.get("group") || first?.groupId || model.groups[0]?.id; const groupFindings = model.findings.filter((f) => f.groupId === groupId); const findingId = query.get("finding") || groupFindings.find((f) => f.status === "open")?.id || groupFindings[0]?.id; const finding = model.findings.find((f) => f.id === findingId) || first; const group = model.groups.find((g) => g.id === groupId);
  const [view, setView] = useState<"preview" | "findings">("findings"); const [busy, setBusy] = useState(false); const [note, setNote] = useState(""); const [error, setError] = useState(""); const [page, setPage] = useState(finding?.source?.page || 1); const [correcting, setCorrecting] = useState(false); const [linesOpen, setLinesOpen] = useState(false);
  useEffect(() => setPage(finding?.source?.page || 1), [finding?.id, finding?.source?.page]);
  const select = (g: string, f?: string) => navigate(setQuery({ group: g, finding: f }));
  const decide = async (outcome: "keep" | "exclude") => { if (!finding) return; setBusy(true); setError(""); try { if (kind === "invoice") await decideFlag(run.id, finding.id, outcome === "keep" ? "accepted" : "rejected", note); else await decideClaimFlag(run.id, finding.id, outcome === "keep" ? "dismissed" : "accepted", note || (outcome === "exclude" ? "Excluded during review" : ""), (run as ClaimsRunDetail).revision); await reload(); const rest = model.findings.filter((f) => f.status === "open" && f.id !== finding.id); const next = rest.find((f) => f.groupId === finding.groupId) || rest[0]; if (next) select(next.groupId, next.id); setNote(""); } catch (e) { setError(e instanceof Error ? e.message : "Could not record decision"); } finally { setBusy(false); } };
  if (!model.findings.length) return <div className="completion-panel"><span className="completion-mark">✓</span><h2>Review complete</h2><p>There are no unresolved findings.</p><Link className="btn primary" to={`/${kind === "invoice" ? "invoices" : "claims"}/${run.id}/export`}>Open export</Link></div>;
  const claimSource = finding?.source?.file; const doc = finding?.source?.documentId ? model.docs.get(finding.source.documentId) : undefined;
  return <div className="review-workbench"><aside className="group-index"><h2>{kind === "claim" ? "Claims" : "Documents"}</h2>{model.groups.map((g) => <button className={g.id === groupId ? "selected" : ""} key={g.id} onClick={() => select(g.id)}><span>{g.name}</span><small>{g.unresolved ? `${g.unresolved} unresolved` : "Complete"}{g.amountAtRisk ? ` · RM ${g.amountAtRisk}` : ""}</small></button>)}</aside><div className="mobile-review-nav"><label>{kind === "claim" ? "Claim" : "Document"}<select value={groupId} onChange={(e) => select(e.target.value)}>{model.groups.map((g) => <option value={g.id} key={g.id}>{g.name} · {g.unresolved} unresolved</option>)}</select></label><div role="tablist" aria-label="Review view"><button role="tab" aria-selected={view === "preview"} onClick={() => setView("preview")}>Preview</button><button role="tab" aria-selected={view === "findings"} onClick={() => setView("findings")}>Findings</button></div></div><section className={`preview-pane ${view === "preview" ? "mobile-current" : ""}`}><header><div><span className="eyebrow">Source</span><h2>{doc?.filename || claimSource || group?.name}</h2></div>{doc && (doc.page_count || 1) > 1 ? <div className="page-controls"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>←</button><span>Page {page} of {doc.page_count}</span><button disabled={page >= (doc.page_count || 1)} onClick={() => setPage(page + 1)}>→</button></div> : null}</header>{doc ? <img src={documentFileUrl(run.id, doc.id, page)} alt={`Page ${page} of ${doc.filename}`} /> : claimSource ? <img src={claimsFileUrl(run.id, claimSource, page, finding?.source?.position || "")} alt={`Cited page ${page} from ${claimSource}`} /> : <div className="source-summary"><h3>Source summary</h3><p>{finding?.source?.summary || finding?.basis || "This finding does not have a previewable citation."}</p>{finding?.source?.sheet ? <p>{finding.source.sheet}{finding.source.row ? `, row ${finding.source.row}` : ""}</p> : null}</div>}</section><aside className={`findings-pane ${view === "findings" ? "mobile-current" : ""}`}><header><div><span className="eyebrow">{group?.name}</span><h2>Findings</h2></div><div className="finding-head-actions"><span className="status working">{group?.unresolved || 0} unresolved</span>{kind === "claim" && groupId !== "run" ? <button className="btn sm" onClick={() => setLinesOpen(true)}>Claim lines</button> : null}</div></header><div className="finding-list">{groupFindings.map((f) => <button key={f.id} className={f.id === finding?.id ? "selected" : ""} onClick={() => select(f.groupId, f.id)}><span>{f.title}</span><small>{f.status === "open" ? f.blocking ? "Needs decision" : "Information" : "Reviewed"}</small></button>)}</div>{finding ? <article className="finding-detail"><h3>{finding.title}</h3><p>{finding.reason}</p>{finding.basis ? <div className="finding-basis"><strong>Basis</strong>{finding.basis}</div> : null}</article> : null}{correcting && finding ? <CorrectionPanel kind={kind} run={run} finding={finding} doc={doc} onDone={async () => { setCorrecting(false); await reload(); }} onCancel={() => setCorrecting(false)} /> : null}</aside><div className="decision-bar review-decisions"><input aria-label="Decision note" placeholder="Decision note" value={note} onChange={(e) => setNote(e.target.value)} />{finding?.status === "open" ? <>{kind === "claim" && finding.groupId === "run" ? <button className="btn primary" disabled={busy} onClick={() => decide("exclude")}>Mark reviewed</button> : <><button className="btn primary" disabled={busy || (kind === "claim" && !note.trim())} onClick={() => decide("keep")}>{kind === "invoice" ? "Include in output" : "Keep in payment"}</button><button className="btn danger" disabled={busy} onClick={() => decide("exclude")}>{kind === "invoice" ? "Exclude and query" : `Exclude${finding.amountAtRisk ? ` RM ${finding.amountAtRisk}` : ""} from payment`}</button><button className="btn" onClick={() => setCorrecting(true)}>Correct value</button></>}</> : <span>Decision recorded</span>}{error ? <span className="error">{error}</span> : null}</div>{kind === "claim" && linesOpen ? <ClaimLinesDrawer run={run as ClaimsRunDetail} groupId={groupId} onClose={() => setLinesOpen(false)} /> : null}</div>;
}

function ClaimLinesDrawer({ run, groupId, onClose }: { run: ClaimsRunDetail; groupId: string; onClose: () => void }) {
  const employeeIds = new Set((run.cases || []).filter((c) => c.id === groupId).flatMap((c) => [c.id, c.employee_id])); employeeIds.add(groupId);
  const rows = run.rows.filter((row) => row.case_id === groupId || employeeIds.has(row.employee_id));
  return <div className="drawer-layer"><button className="drawer-backdrop" aria-label="Close claim lines" onClick={onClose} /><aside className="activity-drawer claim-lines-drawer" role="dialog" aria-modal="true" aria-labelledby="claim-lines-title"><header><div><h2 id="claim-lines-title">Claim lines</h2><p>{rows.length} lines in the selected Claim.</p></div><button className="icon-button" aria-label="Close claim lines" onClick={onClose}>×</button></header><div className="claim-lines-list">{rows.map((row) => <article key={row.id}><strong>{String(row.values.item_name || row.values.item || row.values.reason || "Claim line")}</strong><span>{String(row.values.date || "No date")} · {String(row.values.currency || "MYR")} {String(row.values.total || row.values.amount || "—")}</span><small>{row.verdict.replaceAll("_", " ")} · {row.origin?.replaceAll("_", " ")}</small></article>)}</div></aside></div>;
}

function CorrectionPanel({ kind, run, finding, doc, onDone, onCancel }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail; finding: ReviewFinding; doc?: Doc; onDone: () => Promise<void>; onCancel: () => void }) {
  const claimRun = run as ClaimsRunDetail; const flag = kind === "claim" ? claimRun.flags.find((f) => f.id === finding.id) : undefined; const row = flag ? claimRun.rows.find((r) => r.id === flag.row_id) : undefined; const fields = kind === "invoice" ? CORRECTABLE[doc?.kind || ""] || [] : row ? Object.keys(row.values).filter((key) => ["date", "item", "reason", "amount", "currency", "rate", "total"].includes(key)) : [];
  const source = kind === "invoice" ? doc?.fields || {} : row?.values || {}; const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f, String(source[f] ?? "")]))); const [reason, setReason] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const save = async () => { setBusy(true); setError(""); try { if (kind === "invoice" && doc) await correctFields(run.id, doc.id, values, reason); else if (row) await correctClaimRow(run.id, row.id, values, reason, claimRun.revision); await onDone(); } catch (e) { setError(e instanceof Error ? e.message : "Correction failed"); setBusy(false); } };
  if (!fields.length) return <div className="correction-panel"><p>No directly correctable value is attached to this finding.</p><button className="btn" onClick={onCancel}>Close</button></div>;
  return <div className="correction-panel"><h3>Correct value</h3>{fields.map((field) => <label key={field}>{field.replaceAll("_", " ")}<input value={values[field]} onChange={(e) => setValues({ ...values, [field]: e.target.value })} /></label>)}<label>Reason<input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Required audit reason" /></label><div className="actions"><button className="btn primary" disabled={busy || !reason.trim()} onClick={save}>{busy ? "Saving…" : "Save correction"}</button><button className="btn" disabled={busy} onClick={onCancel}>Cancel</button></div>{error ? <p className="error">{error}</p> : null}</div>;
}

function ExportView({ kind, run }: { kind: "invoice" | "claim"; run: RunDetailData | ClaimsRunDetail }) {
  const unlocked = run.status === "ready" && run.open_flags === 0; const blockers = kind === "claim" ? (run as ClaimsRunDetail).output_blockers || [] : run.open_flags ? [`${run.open_flags} unresolved findings`] : [];
  if (!unlocked) return <div className="export-layout"><header><span className="status working">Export locked</span><h2>Resolve the review gate</h2><p>The export remains visible so you can see exactly what is blocking it.</p></header><div className="alert danger"><div><strong>Export is not ready</strong>{(blockers.length ? blockers : ["Background processing is not complete."]).map((b) => <p key={b}>{b.replaceAll("_", " ")}</p>)}</div></div><Link className="btn primary" to={`/${kind === "invoice" ? "invoices" : "claims"}/${run.id}/review`}>Review unresolved findings</Link></div>;
  if (kind === "claim") { const output = (run as ClaimsRunDetail).outputs; if (!("tsv" in output)) return <p>No output was built.</p>; return <div className="export-layout"><header><span className="status ready">Gate passed</span><h2>Payment listing</h2><p>{output.included.length} claims included · {output.exclusions.length + output.not_included.length} excluded or not included</p></header><div className="export-totals"><div><span>Total payable</span><strong>RM {output.totals.total_myr}</strong></div><div><span>Reconciliation</span><strong>{output.totals.match ? "Matched" : `Difference RM ${output.totals.difference}`}</strong></div></div><CopyBlock title="Payment listing rows" hint="Paste into the reviewed client workbook" text={output.tsv} preview={output.rows.map((r) => r.join(" · "))} /><details><summary>Excluded and not included ({output.exclusions.length + output.not_included.length})</summary>{[...output.exclusions.map((x) => `${x.name}: RM ${x.amount} — ${x.why}`), ...output.not_included.map((x) => `${x.name}: ${x.why}`)].map((x) => <p key={x}>{x}</p>)}</details></div>; }
  const output = (run as RunDetailData).outputs; if (!("bank_rows" in output)) return <p>No output was built.</p>; return <div className="export-layout"><header><span className="status ready">Gate passed</span><h2>Invoice output</h2><p>{output.bank_rows.length} payment rows are ready.</p></header><div className="export-totals"><div><span>Bank total</span><strong>RM {output.totals.bank.toFixed(2)}</strong></div><div><span>Reconciliation</span><strong>{output.totals.match ? "Matched" : "Mismatch"}</strong></div></div><CopyBlock title="Bank entry rows" hint="Copy into the reviewed bank upload template" text={[output.bank_header, ...output.bank_rows].join("\n")} preview={output.bank_rows} /><CopyBlock title="Proposed file names" hint="Apply when filing invoices" text={output.filenames.join("\n")} preview={output.filenames} /></div>;
}

export default function RunWorkspace({ kind, runId, view }: { kind: "invoice" | "claim"; runId: string; view: string }) {
  const [run, setRun] = useState<RunDetailData | ClaimsRunDetail | null>(null), [events, setEvents] = useState<RunEvent[]>([]), [error, setError] = useState(""), [activity, setActivity] = useState(false); const failures = useRef(0); const { refresh: refreshLists } = useRunActivity();
  const reload = useCallback(async () => { try { const result = kind === "invoice" ? await getRun(runId) : await getClaimsRun(runId); setRun(result); failures.current = 0; setError(""); return; } catch { failures.current += 1; if (failures.current >= 2) setError("Reconnecting—the run continues on the server"); } }, [kind, runId]);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { if (!run || !WORKING.has(run.status)) return; const timer = window.setInterval(() => void reload(), 3000); return () => clearInterval(timer); }, [run?.status, reload]);
  const openActivity = useCallback(() => { setActivity(true); const request = kind === "invoice" ? getRunEvents(runId) : getClaimsRunEvents(runId); void request.then(setEvents); }, [kind, runId]);
  useEffect(() => { const handler = () => openActivity(); window.addEventListener("open-activity", handler); return () => window.removeEventListener("open-activity", handler); }, [openActivity]);
  const refresh = useCallback(async () => { await reload(); await refreshLists(); }, [reload, refreshLists]);
  if (!run) return <section className="standard-page"><p>{error || "Loading run…"}</p></section>;
  const workbench = view === "organize" || view === "review";
  return <section className={workbench ? "workbench-page" : "standard-page"}>{error ? <div className="alert warning" role="status">{error}</div> : null}<RunHeader kind={kind} run={run} view={view} onActivity={openActivity} /><div className="run-content">{view === "progress" ? <ProgressScreen kind={kind} run={run} /> : view === "organize" && kind === "claim" ? <ClaimsOrganize run={run as ClaimsRunDetail} reload={refresh} /> : view === "review" ? <ReviewWorkbench kind={kind} run={run} reload={refresh} /> : view === "export" ? <ExportView kind={kind} run={run} /> : <ProgressScreen kind={kind} run={run} />}</div><ActivityDrawer open={activity} onClose={() => setActivity(false)} events={events} run={run} /></section>;
}
