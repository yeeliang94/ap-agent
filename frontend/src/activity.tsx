import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ClaimsRunSummary, RunSummary, claimsRunWorking, listClaimsRuns, listRuns } from "./api";

type RunKind = "invoice" | "claim";
export type TrackedRun = { kind: RunKind; id: string; client: string; status: string; progress: RunSummary["progress"]; ready: boolean; href: string };
type ActivityValue = { invoices: RunSummary[]; claims: ClaimsRunSummary[]; active: TrackedRun[]; ready: TrackedRun[]; refresh: () => Promise<void>; failures: number };
const ActivityContext = createContext<ActivityValue | null>(null);
const INVOICE_WORKING = new Set(["queued", "sorting", "extracting", "checking"]);

export function RunActivityProvider({ children }: { children: ReactNode }) {
  const [invoices, setInvoices] = useState<RunSummary[]>([]);
  const [claims, setClaims] = useState<ClaimsRunSummary[]>([]);
  const [failures, setFailures] = useState(0);
  const alive = useRef(true);
  const previous = useRef<Map<string, string>>(new Map());
  const [readyKeys, setReadyKeys] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem("ap-ready-runs-v2") || "[]") as string[]));
  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([listRuns(), listClaimsRuns()]);
    if (!alive.current) return;
    const rejected = results.filter((r) => r.status === "rejected").length;
    setFailures((n) => rejected ? n + 1 : 0);
    const nextInvoices = results[0].status === "fulfilled" ? results[0].value : null;
    const nextClaims = results[1].status === "fulfilled" ? results[1].value : null;
    const newlyReady: string[] = [];
    nextInvoices?.forEach((run) => { const key = `invoice:${run.id}`; if (INVOICE_WORKING.has(previous.current.get(key) || "") && run.status === "ready") newlyReady.push(key); previous.current.set(key, run.status); });
    nextClaims?.forEach((run) => { const key = `claim:${run.id}`; if (claimsRunWorking({ status: previous.current.get(key) || "" }) && (run.status === "map_ready" || run.status === "ready")) newlyReady.push(key); previous.current.set(key, run.status); });
    if (newlyReady.length) setReadyKeys((current) => { const next = new Set([...current, ...newlyReady]); localStorage.setItem("ap-ready-runs-v2", JSON.stringify([...next])); return next; });
    if (nextInvoices) setInvoices(nextInvoices);
    if (nextClaims) setClaims(nextClaims);
  }, []);

  const anyWorking = invoices.some((r) => INVOICE_WORKING.has(r.status)) || claims.some(claimsRunWorking);
  useEffect(() => {
    alive.current = true;
    void refresh();
    const focus = () => void refresh();
    window.addEventListener("focus", focus);
    return () => { alive.current = false; window.removeEventListener("focus", focus); };
  }, [refresh]);
  useEffect(() => {
    if (!anyWorking) return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [anyWorking, refresh]);

  const value = useMemo<ActivityValue>(() => {
    const invoiceRuns = invoices.map<TrackedRun>((r) => ({ kind: "invoice", id: r.id, client: r.client, status: r.status, progress: r.progress, ready: r.status === "ready", href: `/invoices/${r.id}/${r.status === "ready" ? "review" : "progress"}` }));
    const claimRuns = claims.map<TrackedRun>((r) => ({ kind: "claim", id: r.id, client: r.client, status: r.status, progress: r.progress, ready: r.status === "map_ready" || r.status === "ready", href: `/claims/${r.id}/${r.status === "map_ready" ? "organize" : r.status === "ready" ? "review" : "progress"}` }));
    return { invoices, claims, active: [...invoiceRuns.filter((r) => INVOICE_WORKING.has(r.status)), ...claimRuns.filter((r) => claimsRunWorking(r))], ready: [...invoiceRuns, ...claimRuns].filter((r) => r.ready && readyKeys.has(`${r.kind}:${r.id}`)), refresh, failures };
  }, [invoices, claims, failures, refresh, readyKeys]);
  return <ActivityContext.Provider value={value}>{children}</ActivityContext.Provider>;
}

export function useRunActivity() {
  const value = useContext(ActivityContext);
  if (!value) throw new Error("useRunActivity must be used inside RunActivityProvider");
  return value;
}
