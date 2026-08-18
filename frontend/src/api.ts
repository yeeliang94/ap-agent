// All backend calls in one place, with the shapes the API returns.

export interface RunSummary {
  id: string;
  client: string;
  status: string;
  error: string;
  progress: { done?: number; total?: number };
  documents_total: number;
  open_flags: number;
  /** Diary counts. A run can be "ready" AND have errors — that pairing is
   *  the one worth showing, so it rides on every summary. */
  errors: number;
  warnings: number;
  created_at: string;
}

export interface AppSettings {
  client_name: string;
  sharepoint_folder_url: string;
  /** Names on the drafted listing tab's Prepared by / Reviewed by line. */
  draft_prepared_by: string;
  draft_reviewed_by: string;
  /** Estimated bank charge per payment (RM), used in the draft's fund figures. */
  draft_bank_charge: string;
}

export async function getSettings(): Promise<AppSettings> {
  const r = await fetch("/api/settings");
  if (!r.ok) throw new Error("Could not load settings");
  return r.json();
}

export async function saveSettings(s: AppSettings): Promise<AppSettings> {
  const r = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(s),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? "Could not save settings");
  return r.json();
}

/** Whether this deployment needs a SharePoint sign-in, and whether it has one. */
export interface SharePointStatus {
  required: boolean;
  connected: boolean;
}

export async function getSharePointStatus(): Promise<SharePointStatus> {
  const r = await fetch("/api/sharepoint/status");
  if (!r.ok) throw new Error("Could not check the SharePoint connection");
  return r.json();
}

/** Opens a browser window on the server machine — only ever from a click. */
export async function connectSharePoint(): Promise<{ signed_in_as: string }> {
  const r = await fetch("/api/sharepoint/connect", { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail ?? "Could not connect");
  return r.json();
}

export async function disconnectSharePoint(): Promise<void> {
  const r = await fetch("/api/sharepoint/disconnect", { method: "POST" });
  if (!r.ok) throw new Error("Could not disconnect");
}

export interface Doc {
  id: string;
  filename: string;
  kind: string;
  fields: Record<string, unknown>;
  confidence: Record<string, string>;
  corrections: Record<string, { from: unknown; to: unknown; reason: string }>;
  status: string;
  error: string;
}

// Fields a human may correct, mirroring the backend's rule.
export const CORRECTABLE: Record<string, string[]> = {
  invoice: ["vendor", "invoice_number", "date", "amount", "currency"],
  claim: ["claimant", "description", "amount", "currency"],
};

export async function correctFields(
  runId: string,
  docId: string,
  fields: Record<string, string>,
  reason: string
): Promise<void> {
  const r = await fetch(`/api/runs/${runId}/documents/${docId}/correct`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields, reason }),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? "Correction failed");
}

export interface FlagItem {
  id: string;
  document_id: string;
  code: string;
  reason: string;
  basis: string;
  status: string;
  resolution: string;
}

export interface Outputs {
  bank_header: string;
  bank_rows: string[];
  /** True when the reference folder had no bank template, so no block exists. */
  bank_skipped: boolean;
  excluded_non_myr: number;
  filenames: string[];
  /** Vendors the payment listing has never paid — likely not yet Maybank beneficiaries. */
  new_vendors: string[];
  totals: { bank: number; match: boolean };
  /** Next month's listing entries, drafted as a new tab on a copy of the
   *  client's workbook — or {skipped: why} when it could not be written. */
  listing_draft: ListingDraft | { skipped: string };
}

export interface ListingDraft {
  tab: string;
  source_tab: string;
  month: string;
  file: string;
  entries: {
    voucher: string;
    payee: string;
    total: string;
    invoices: { number: string; description: string; amount: string }[];
    cells: { row: number; total: string };
  }[];
  invoice_count: number;
  excluded_non_myr: number;
  opening_balance: string;
  net_payment: string;
  bank_charges: string;
  fund_to_request: string;
  closing_balance: string;
  prepared_by: string;
  reviewed_by: string;
  has_bank_block: boolean;
}

export function listingDraftUrl(runId: string): string {
  return `/api/runs/${runId}/draft`;
}

export interface RunDetailData extends RunSummary {
  documents: Doc[];
  flags: FlagItem[];
  outputs: Outputs | Record<string, never>;
}

export async function listRuns(): Promise<RunSummary[]> {
  const r = await fetch("/api/runs");
  if (!r.ok) throw new Error("Could not load runs");
  return r.json();
}

export async function getRun(id: string): Promise<RunDetailData> {
  const r = await fetch(`/api/runs/${id}`);
  if (!r.ok) throw new Error("Could not load run");
  return r.json();
}

/** One moment in a run's life, as recorded by the backend's telemetry. */
export interface RunEvent {
  id: number;
  at: string;
  stage: string;
  level: "info" | "warning" | "error";
  code: string;
  message: string;
  detail: string;
  document_id: string;
}

export async function getRunEvents(
  id: string,
  onlyProblems = false
): Promise<RunEvent[]> {
  const query = onlyProblems ? "?level=problems" : "";
  const r = await fetch(`/api/runs/${id}/events${query}`);
  if (!r.ok) throw new Error("Could not load the run diary");
  return r.json();
}

export async function uploadBatch(client: string, file: File): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("client", client);
  form.append("batch", file);
  const r = await fetch("/api/runs", { method: "POST", body: form });
  if (!r.ok) throw new Error((await r.json()).detail ?? "Upload failed");
  return r.json();
}

export async function decideFlag(
  runId: string,
  flagId: string,
  decision: "accepted" | "rejected",
  note: string
): Promise<void> {
  const r = await fetch(`/api/runs/${runId}/flags/${flagId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note }),
  });
  if (!r.ok) throw new Error("Could not record decision");
}

export function documentFileUrl(runId: string, docId: string): string {
  // The preview endpoint returns the first page as PNG for every file type,
  // so the review screen never depends on the browser's PDF plugin.
  return `/api/runs/${runId}/documents/${docId}/preview`;
}
