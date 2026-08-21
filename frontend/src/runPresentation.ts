import { ClaimsRunSummary } from "./api";

export type RunKind = "invoice" | "claim";
export type RunView = "progress" | "organize" | "review" | "export";

/** The one place a run's URL is spelled: /invoices|claims/<id>/<view>. */
export function runPath(kind: RunKind, runId: string, view: RunView): string {
  return `/${kind === "invoice" ? "invoices" : "claims"}/${runId}/${view}`;
}

/** One authoritative mapping from a run outcome to its reviewer destination. */
export function runDestination(
  kind: RunKind,
  run: { id: string; status: string; open_flags?: number },
  openCompletedExport = false,
): string {
  if (kind === "claim" && run.status === "map_ready") return runPath(kind, run.id, "organize");
  if (run.status === "ready") return runPath(kind, run.id, openCompletedExport && !run.open_flags ? "export" : "review");
  return runPath(kind, run.id, "progress");
}

/** "map_ready" → "Map ready": status words read as a sentence, never a code or Title Case. */
export function sentence(text: string): string {
  const words = text.replaceAll("_", " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : words;
}

export function runOutcome(status: string, open: number, kind: RunKind): string {
  if (status === "failed") return "See what failed";
  if (kind === "claim" && status === "map_ready") return "Organize claims";
  if (status === "ready") return open ? "Continue review" : "Open export";
  return "View progress";
}

export function claimsStatusLabel(run: ClaimsRunSummary): string {
  switch (run.status) {
    case "queued": return "Queued";
    case "surveying": {
      const currentFile = run.progress?.file?.split(/[\\/]/).pop();
      return run.progress?.total
      ? `Preparing files ${run.progress.done || 0}/${run.progress.total}${currentFile ? ` · ${currentFile}` : ""}`
      : "Preparing files";
    }
    case "mapping": return "Organizing files";
    case "map_ready": return "Organization ready";
    case "verifying": return `Checking ${run.employees_done}/${run.employee_count}`;
    case "ready": return run.open_flags
      ? `${run.open_flags} to review${run.notes ? ` · ${run.notes} note${run.notes === 1 ? "" : "s"}` : ""}`
      : `Ready${run.notes ? ` · ${run.notes} note${run.notes === 1 ? "" : "s"}` : ""}`;
    case "failed": return "Failed";
    default: return run.status.replaceAll("_", " ");
  }
}
