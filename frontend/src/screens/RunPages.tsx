import { useEffect, useRef, useState } from "react";
import { getSettings, getSharePointStatus, uploadBatch } from "../api";
import { useRunActivity } from "../activity";
import { Link, useRouter } from "../router";
import { claimsStatusLabel, runDestination, runOutcome, runPath } from "../runPresentation";
import { NewClaimsRunCard } from "./ClaimsList";

export function RunListPage({ kind }: { kind: "invoice" | "claim" }) {
  const { invoices, claims, failures } = useRunActivity();
  const rows = kind === "invoice" ? invoices : claims;
  const noun = kind === "invoice" ? "Invoice" : "Claim";
  return <section className="standard-page">
    <header className="page-header">
      <div><h1>{noun} runs</h1><p>Recent batches and their current outcome.</p></div>
      <Link className="btn primary" to={`/${kind === "invoice" ? "invoices" : "claims"}/new`}>New run</Link>
    </header>
    {failures >= 2 ? <div className="alert warning" role="status">Reconnecting—the runs continue on the server.</div> : null}
    <div className="data-list" role="list">
      {rows.map((run) => {
        const isClaim = "employee_count" in run;
        const source = isClaim ? run.folder : "Uploaded zip";
        const count = isClaim ? `${run.employees_done} of ${run.employee_count || "—"} claims` : `${run.documents_total} documents`;
        const destination = runDestination(kind, run, true);
        return <article className="run-row" role="listitem" key={run.id}>
          <div className="run-main"><strong>{run.client}</strong><span>{new Date(run.created_at).toLocaleString()}</span></div>
          <div className="run-source"><span className="meta-label">Source</span><span title={source}>{source}</span></div>
          <div className="run-count"><span className="meta-label">Progress</span><span>{count}{run.open_flags ? ` · ${run.open_flags} unresolved` : ""}</span></div>
          <div className="run-outcome"><span className={`status ${run.status === "failed" ? "failed" : run.status === "ready" ? "ready" : "working"}`}>{isClaim ? claimsStatusLabel(run) : run.status.replaceAll("_", " ")}</span></div>
          <Link className="btn" to={destination}>{runOutcome(run.status, run.open_flags, kind)}</Link>
          {run.status === "failed" && run.error ? <p className="run-error">{run.error}</p> : null}
        </article>;
      })}
      {!rows.length ? <div className="empty-state" role="listitem"><h2>No {noun.toLowerCase()} runs yet</h2><p>Start a run to process the first batch.</p><Link className="btn primary" to={`/${kind === "invoice" ? "invoices" : "claims"}/new`}>New run</Link></div> : null}
    </div>
  </section>;
}

export function InvoiceNewPage() {
  const { navigate } = useRouter();
  const { refresh } = useRunActivity();
  const input = useRef<HTMLInputElement>(null);
  const [client, setClient] = useState("");
  const [connectionBlocked, setConnectionBlocked] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { void Promise.all([getSettings(), getSharePointStatus()]).then(([settings, sharepoint]) => {
    setClient(settings.client_name); setConnectionBlocked(sharepoint.required && !sharepoint.connected);
  }).catch(() => setError("Could not load workspace settings.")); }, []);
  const start = async () => {
    if (!file) return;
    setBusy(true); setError("");
    try { const result = await uploadBatch(client, file); await refresh(); navigate(runPath("invoice", result.run_id, "progress")); }
    catch (e) { setError(e instanceof Error ? e.message : "Upload failed"); setBusy(false); }
  };
  return <section className="standard-page narrow-form">
    <header className="page-header"><div><h1>New invoice run</h1><p>Upload one zip containing the invoice batch.</p></div></header>
    {connectionBlocked ? <div className="alert danger" role="alert"><div><strong>SharePoint connection required</strong><p>Connect the workspace before starting this run.</p></div><Link className="btn" to="/settings/workspace">Open settings</Link></div> : null}
    <div className="upload-panel">
      <input ref={input} hidden type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      <button className="drop-target" type="button" onClick={() => input.current?.click()}><strong>{file ? file.name : "Choose invoice batch"}</strong><span>{file ? `${Math.round(file.size / 1024 / 1024 * 10) / 10} MB` : "ZIP files only"}</span></button>
    </div>
    {error ? <p className="error" role="alert">{error}</p> : null}
    <div className="decision-bar"><span>{client ? `Workspace: ${client}` : "Loading workspace…"}</span><button className="btn primary" disabled={!file || busy || connectionBlocked || !client} onClick={start}>{busy ? "Starting…" : "Start run"}</button></div>
  </section>;
}

export function ClaimsNewPage() {
  const { navigate } = useRouter();
  const { refresh } = useRunActivity();
  return <section className="standard-page"><NewClaimsRunCard onStarted={(id) => { void refresh(); navigate(runPath("claim", id, "progress")); }} /></section>;
}
