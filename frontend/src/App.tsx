import { useMemo, useState } from "react";
import { RunActivityProvider, TrackedRun, useRunActivity } from "./activity";
import { Link, useRouter } from "./router";
import SettingsScreen from "./screens/SettingsScreen";
import RunWorkspace from "./screens/RunWorkspace";
import { ClaimsNewPage, InvoiceNewPage, RunListPage } from "./screens/RunPages";

function pathMatch(pathname: string) {
  const parts = pathname.split("/").filter(Boolean);
  if (!parts.length) return { page: "redirect" as const };
  if ((parts[0] === "invoices" || parts[0] === "claims") && parts.length === 1) return { page: "list" as const, kind: parts[0] === "invoices" ? "invoice" as const : "claim" as const };
  if ((parts[0] === "invoices" || parts[0] === "claims") && parts[1] === "new") return { page: "new" as const, kind: parts[0] === "invoices" ? "invoice" as const : "claim" as const };
  if ((parts[0] === "invoices" || parts[0] === "claims") && parts[1]) return { page: "run" as const, kind: parts[0] === "invoices" ? "invoice" as const : "claim" as const, id: parts[1], view: parts[2] || "progress" };
  if (parts[0] === "settings") return { page: "settings" as const, section: (parts[1] || "workspace") as "workspace" | "claims" | "features" | "deployment" };
  return { page: "missing" as const };
}

function phaseCopy(run: TrackedRun) {
  const step = run.progress?.step?.replaceAll("_", " ") || run.status.replaceAll("_", " ");
  const count = run.progress?.total ? ` · ${run.progress.done || 0}/${run.progress.total}` : "";
  return `${step}${count}`;
}

function GlobalRunIndicator() {
  const { active, ready } = useRunActivity();
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem("ap-dismissed-ready-v2") || "[]") as string[]));
  const unseen = ready.filter((r) => !dismissed.has(`${r.kind}:${r.id}`));
  const shown = active.length ? active : unseen;
  const announce = useMemo(() => active.map((r) => `${r.id}:${r.progress?.phase}:${r.progress?.done}:${r.progress?.total}`).join("|"), [active]);
  if (!shown.length) return null;
  const dismiss = (run: TrackedRun) => {
    const next = new Set(dismissed).add(`${run.kind}:${run.id}`);
    setDismissed(next);
    localStorage.setItem("ap-dismissed-ready-v2", JSON.stringify([...next]));
  };
  return <div className="global-run">
    <span className="sr-only" aria-live="polite">{announce ? active.map((run) => `${run.progress?.phase || "working"}${run.progress?.total ? ` ${run.progress.done || 0} of ${run.progress.total}` : ""}`).join("; ") : ""}</span>
    {shown.length === 1 ? <><span className={`run-pulse ${active.length ? "" : "ready"}`} aria-hidden /><span>{active.length ? phaseCopy(shown[0]) : "Review ready"}</span><Link to={shown[0].href} onClick={() => !active.length && dismiss(shown[0])}>Return</Link>{!active.length ? <button aria-label="Dismiss ready run" onClick={() => dismiss(shown[0])}>×</button> : null}</> : <><button className="global-run-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>{active.length || shown.length} {active.length ? "runs active" : "runs ready"}</button>{open ? <div className="global-run-menu">{shown.map((run) => <div key={`${run.kind}:${run.id}`}><div><strong>{run.client}</strong><span>{active.includes(run) ? phaseCopy(run) : "Review ready"}</span></div><Link to={run.href} onClick={() => { setOpen(false); if (!active.includes(run)) dismiss(run); }}>Open</Link>{!active.includes(run) ? <button aria-label={`Dismiss ${run.client}`} onClick={() => dismiss(run)}>×</button> : null}</div>)}</div> : null}</>}
  </div>;
}

function AppShell() {
  const { location, navigate } = useRouter();
  const route = pathMatch(location.pathname);
  if (route.page === "redirect") { queueMicrotask(() => navigate("/invoices", true)); return null; }
  const invoiceCurrent = location.pathname.startsWith("/invoices"), claimCurrent = location.pathname.startsWith("/claims"), settingsCurrent = location.pathname.startsWith("/settings");
  return <><header className="topnav"><Link className="brand" to="/invoices"><span className="logo">AP</span><span>AP Agent</span></Link><nav aria-label="Primary navigation"><Link to="/invoices" ariaCurrent={invoiceCurrent ? "page" : undefined}>Invoices</Link><Link to="/claims" ariaCurrent={claimCurrent ? "page" : undefined}>Claims</Link><Link to="/settings/workspace" ariaCurrent={settingsCurrent ? "page" : undefined}>Settings</Link></nav><GlobalRunIndicator /></header><main>{route.page === "list" ? <RunListPage kind={route.kind} /> : route.page === "new" ? route.kind === "invoice" ? <InvoiceNewPage /> : <ClaimsNewPage /> : route.page === "run" ? <RunWorkspace kind={route.kind} runId={route.id} view={route.view} /> : route.page === "settings" ? <SettingsScreen section={route.section} /> : <section className="standard-page empty-state"><h1>Page not found</h1><Link className="btn primary" to="/invoices">Go to invoice runs</Link></section>}</main></>;
}

export default function App() { return <RunActivityProvider><AppShell /></RunActivityProvider>; }
