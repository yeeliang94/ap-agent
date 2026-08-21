// All backend calls in one place, with the shapes the API returns.

export type RunPhase = "preparing" | "organizing" | "checking" | "finalizing";
export type ProgressUnit = "files" | "documents" | "claims" | "assignments" | "references" | "output" | "items" | "claim";
export interface RunProgress {
  phase?: RunPhase;
  step?: string;
  done?: number;
  total?: number;
  unit?: ProgressUnit | string;
  updated_at?: string;
  /** Compatibility fields emitted by older claims runs. */
  what?: string;
  file?: string;
  employees?: number;
}
export interface ItemProgress { id: string; name: string; status: string; progress?: RunProgress; error?: string; }
export interface ReviewGroup { id: string; name: string; unresolved: number; amountAtRisk?: string; complete: boolean; sourceOrder: number; }
export interface PreviewSource { file?: string; documentId?: string; page?: number; position?: string; sheet?: string; row?: number; summary?: string; }
export interface ReviewFinding { id: string; groupId: string; title: string; reason: string; basis?: string; status: string; blocking: boolean; amountAtRisk?: string; source?: PreviewSource; sourceOrder?: number; }

export interface RunSummary {
  id: string;
  client: string;
  status: string;
  error: string;
  progress: RunProgress;
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

/** One feature switch: its words, its live value, and its .env default. */
export interface SwitchInfo {
  key: string;
  label: string;
  description: string;
  value: boolean;
  default: boolean;
  /** True when a reviewer saved a choice; false = the .env default answers. */
  saved: boolean;
}

/** A read-only .env fact — whether a thing is set, never a secret's value. */
export interface DeploymentFact {
  label: string;
  value: string;
}

export interface SwitchBoard {
  switches: SwitchInfo[];
  deployment: DeploymentFact[];
}

export async function getSwitches(): Promise<SwitchBoard> {
  const r = await fetch("/api/settings/switches");
  if (!r.ok) throw new Error("Could not load the feature switches");
  return r.json();
}

export async function saveSwitches(changes: Record<string, boolean>): Promise<SwitchBoard> {
  const r = await fetch("/api/settings/switches", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? "Could not save the switch");
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
  page_count: number | null;
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
  /** The invoice pipeline sets this; a claims run's diary has no document. */
  document_id?: string;
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

export function documentFileUrl(runId: string, docId: string, page = 1): string {
  // The preview endpoint returns the first page as PNG for every file type,
  // so the review screen never depends on the browser's PDF plugin.
  return `/api/runs/${runId}/documents/${docId}/preview?page=${page}`;
}

// ---------------------------------------------------------------------------
// Claims module — a second run type with its own routes (/api/claims-runs).

/** The statuses in which the server is still working on a run (mirrors the
 *  backend's IN_PROGRESS_STATUSES): the screens poll while one is in
 *  progress, and only such a run can be cancelled. */
export const CLAIMS_IN_PROGRESS = ["queued", "surveying", "mapping", "verifying"] as const;
export function claimsRunWorking(r: { status: string }): boolean {
  return (CLAIMS_IN_PROGRESS as readonly string[]).includes(r.status);
}

export interface ClaimsRunSummary {
  id: string;
  client: string;
  status: string; // queued | surveying | mapping | map_ready | verifying | ready | failed
  error: string;
  progress: RunProgress;
  folder: string;
  employee_count: number;
  employees_done: number;
  open_flags: number;
  /** Info-level flags (never block) — "4 notes" on the list. */
  notes: number;
  errors: number;
  warnings: number;
  created_at: string;
}

/** One file's role in the claim map, with the AI's reason for it. */
export interface MapFile {
  path: string;
  role: "report" | "receipts" | "ignore" | "unplaced";
  reason: string;
}

/** One subfolder of the batch, as the AI proposed it and as the reviewer
 *  may correct it before confirming. */
export interface MapEmployee {
  folder: string;
  is_employee: boolean;
  name: string;
  er_code: string;
  report_file: string | null;
  report_tab: string | null;
  mileage_tab: string | null;
  no_report: boolean;
  skip?: boolean;
  files: MapFile[];
  reason: string;
}

export interface ClaimMap {
  employees: MapEmployee[];
  root_files: MapFile[];
  notes: string[];
  rounds?: number;
  confirmed?: boolean;
}

export interface SurveyFile {
  path: string;
  name: string;
  folder: string;
  type: "workbook" | "pdf" | "image" | "other";
  size: number | null;
  pages: number | null;
  er_code: string;
  peek: { tabs?: Record<string, string[]>; thumbnail?: string } | null;
  peek_error?: string;
}

export interface Survey {
  folders: { path: string; name: string; files: string[] }[];
  files: SurveyFile[];
  root_files: string[];
}

export interface ClaimEmployee {
  id: string;
  folder: string;
  name: string;
  er_code: string;
  roles: {
    report_file: string | null;
    report_tab: string | null;
    mileage_tab: string | null;
    no_report: boolean;
    receipt_files: string[];
    ignored: string[];
    unplaced: string[];
  };
  status: string; // pending | verifying | verified | failed | skipped
  error: string;
  report_total: string;
  category: string;
  gl: string;
  category_basis: string;
  summary: Record<string, unknown>;
  progress?: RunProgress;
}

export interface ClaimRow {
  id: string;
  employee_id: string;
  /** The Claim Case this line belongs to (hardening H2). */
  case_id?: string;
  /** Where the amount comes from (H7): reported | evidence_derived | reviewer_entered. */
  origin?: "reported" | "evidence_derived" | "reviewer_entered";
  kind: string;
  sheet: string;
  row: number;
  values: Record<string, unknown>;
  corrections: Record<string, { from: unknown; to: unknown; reason: string }>;
  matched_evidence_id: string;
  verdict: string;
}

export interface ClaimEvidence {
  id: string;
  employee_id: string;
  case_id?: string;
  kind: string;
  file: string;
  page: number;
  position: string;
  values: Record<string, unknown>;
  confidence: Record<string, string>;
  matched_row_id: string;
}

export interface ClaimFlag {
  id: string;
  employee_id: string;
  case_id?: string;
  row_id: string;
  evidence_id: string;
  /** A flag about a whole file (ARTIFACT_UNRESOLVED): the file's manifest id. */
  artifact_id?: string;
  code: string;
  reason: string;
  basis: string;
  cite: { file?: string; page?: number; position?: string; sheet?: string; row?: number };
  status: string;
  resolution: string;
}

// One flag code's words — from the backend catalogue, the single source
// of truth the Review screen, Settings and the tests share.
export interface FlagInfo {
  code: string;
  title: string;
  meaning: string;
  what_to_do: string;
  kind: "money" | "evidence" | "mileage" | "structure" | "note";
  blocks: "open" | "info";
  toggle: boolean;
}

export type FlagCatalogue = Record<string, FlagInfo>;

export interface ClaimsOutputs {
  header: string[];
  rows: string[][];
  tsv: string;
  totals: {
    total_myr: string;
    source_total: string;
    /** Calculated Lines Total of the lines to be paid (H9). */
    lines_total?: string;
    /** The Reported Totals added up, where a source states one. */
    reported_total?: string;
    reported_missing?: number;
    match: boolean;
    difference: string;
    differences?: { name: string; case_id?: string; expected: string | null; emitted: string; why: string }[];
  };
  included: { name: string; case_id?: string; er_code: string; amount: string; category: string; gl: string;
    reported_total?: string | null; lines_total?: string; derived?: boolean }[];
  not_included: { name: string; case_id?: string; why: string }[];
  exclusions: { name: string; case_id?: string; row: number; amount: string; why: string }[];
  /** Receipts and map trips no row used — what will NOT be paid, on the same screen as what will. */
  unused_evidence?: { name: string; what: string; where: string; amount: string; decision: string }[];
  header_fallback: boolean;
  header_note: string;
  received_date: string;
}

// ---- the case model (hardening H2/H6) --------------------------------------

export interface Claimant {
  name: string;
  identifier: string;
  state: "confirmed" | "proposed" | "unknown";
  basis: string;
  citations: unknown[];
}

/** One Claim Case: a proposed payment-listing decision. Mirrors an
 *  employee record 1:1 while both exist (employee_id). */
export interface ClaimCase {
  id: string;
  employee_id: string;
  label: string;
  claimant: Claimant;
  state: "proposed" | "confirmed" | "blocked" | "excluded";
  grouping_basis: string;
  citations: unknown[];
  artifact_ids: string[];
  roles: ClaimEmployee["roles"];
  status: string;
  error: string;
  category: string;
  gl: string;
  category_basis: string;
  reported_total: string;
  lines_total: string;
  summary: Record<string, unknown>;
  confidence: number;
  reason: string;
  // delivered aliases
  folder: string;
  name: string;
  er_code: string;
  report_total: string;
}

export type Disposition = "used" | "duplicate" | "irrelevant" | "unreadable" | "unresolved";
export type ArtifactRole =
  | "report" | "receipts" | "approval" | "report_copy" | "listing" | "roster" | "policy" | "other" | "unknown";

/** One submitted file, whatever it turns out to be. */
export interface ClaimArtifact {
  id: string;
  path: string;
  sha256: string;
  media_type: "workbook" | "pdf" | "image" | "other" | string;
  size: number;
  pages: number | null;
  sheets: string[];
  inspection_state: string;
  failure_reason: string;
  proposed_role: ArtifactRole;
  role_reason: string;
  disposition: Disposition;
  disposition_reason: string;
  disposition_by: "" | "adapter" | "reviewer";
  needs_confirmation: boolean;
  case_id: string;
  /** Identity signals found in this file (ER code, name, folder), each with where. */
  signals?: { kind: string; value: string; strength: "strong" | "weak"; cite: Record<string, unknown> }[];
}

export interface ClaimAssignment {
  id: string;
  evidence_id: string;
  artifact_id: string;
  case_id: string;
  line_id: string;
  state: "proposed" | "confirmed" | "rejected";
  basis: string;
  confidence: number;
  reason: string;
}

export interface Grouping {
  problems: string[];
  /** The problems that belong to one case, by case id (the rest are run-wide). */
  by_case?: Record<string, string[]>;
  /** Regrouping actions (create/merge/split/move/role) are on only when the
   *  server's CLAIMS_FULL_DUMP_GROUPING switch is on; the gate, claimant and
   *  dispositions always are. */
  actions_enabled?: boolean;
  counts: {
    artifacts: number;
    dispositioned: number;
    unresolved: number;
    material_unresolved: number;
    cases: number;
    to_verify: number;
    claimants_confirmed: number;
    conflicts: number;
  };
  ok: boolean;
}

export interface Investigation {
  id?: string;
  adapter?: string;
  strategy?: string;
  status?: string;
  plan?: { strategy?: string; steps?: string[]; assumptions?: string[]; questions?: string[]; rounds?: number; adapter?: string };
  summary?: Record<string, unknown>;
  rounds?: number;
}

export interface ClaimsRunDetail extends ClaimsRunSummary {
  /** Bumped on every reviewer change; sent back with every action so two
   *  screens never overwrite each other (409 = reload). */
  revision: number;
  cases?: ClaimCase[];
  artifacts?: ClaimArtifact[];
  assignments?: ClaimAssignment[];
  grouping?: Grouping;
  investigation?: Investigation;
  tool_summary?: Record<string, { calls: number; failed: number }>;
  artifact_counts?: { total: number; unresolved: number; needs_review: number };
  /** What keeps the Payment Listing locked, named by the server (H9). */
  output_blockers?: string[];
  folder_url: string;
  listing_url: string;
  received_date: string;
  instructions: string;
  survey: Survey | Record<string, never>;
  map: ClaimMap | Record<string, never>;
  map_warnings: string[];
  listing_headers: Record<string, unknown>;
  employees: ClaimEmployee[];
  rows: ClaimRow[];
  evidence: ClaimEvidence[];
  flags: ClaimFlag[];
  catalogue: FlagCatalogue;
  outputs: ClaimsOutputs | Record<string, never>;
}

async function fail(r: Response, fallback: string): Promise<never> {
  let detail = fallback;
  try {
    detail = (await r.json()).detail ?? fallback;
  } catch {
    /* body was not JSON */
  }
  throw new Error(typeof detail === "string" ? detail : fallback);
}

export async function listClaimsRuns(): Promise<ClaimsRunSummary[]> {
  const r = await fetch("/api/claims-runs");
  if (!r.ok) return fail(r, "Could not load claims runs");
  return r.json();
}

export async function getClaimsRun(id: string): Promise<ClaimsRunDetail> {
  const r = await fetch(`/api/claims-runs/${id}`);
  if (!r.ok) return fail(r, "Could not load the claims run");
  return r.json();
}

export async function getClaimsRunEvents(id: string, onlyProblems = false): Promise<RunEvent[]> {
  const r = await fetch(`/api/claims-runs/${id}/events${onlyProblems ? "?level=problems" : ""}`);
  if (!r.ok) return fail(r, "Could not load the run diary");
  return r.json();
}

export interface AuditItem {
  id: number;
  at: string;
  actor: string;
  action: string;
  detail: string;
}

export async function getClaimsRunAudit(id: string): Promise<AuditItem[]> {
  const r = await fetch(`/api/claims-runs/${id}/audit`);
  if (!r.ok) return fail(r, "Could not load the audit trail");
  return r.json();
}

export interface NewClaimsRun {
  received_date: string;
  folder_url?: string;
  listing_url?: string;
  instructions?: string;
  /** The batch: one zip, or loose files of the readable types. */
  batch?: File[];
  /** Each batch file's relative path (a picked/dropped folder), in order.
   *  Empty = the bare filenames, laid out flat. */
  batch_paths?: string[];
  listing?: File | null;
}

/** Starts a run. Uses XMLHttpRequest rather than fetch for the one thing
 *  fetch cannot do: report UPLOAD progress — a batch can be 200 MB, and a
 *  silent minute reads as a hang. onProgress gets 0..1. */
export function createClaimsRun(
  input: NewClaimsRun,
  onProgress?: (fraction: number) => void
): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("received_date", input.received_date);
  if (input.folder_url) form.append("folder_url", input.folder_url);
  if (input.listing_url) form.append("listing_url", input.listing_url);
  if (input.instructions) form.append("instructions", input.instructions);
  for (const file of input.batch ?? []) form.append("batch", file);
  if (input.batch_paths?.length) form.append("batch_paths", JSON.stringify(input.batch_paths));
  if (input.listing) form.append("listing", input.listing);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/claims-runs");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body: { detail?: unknown; run_id?: string } = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300 && body.run_id) {
        resolve({ run_id: body.run_id });
      } else {
        reject(new Error(typeof body.detail === "string" ? body.detail : "Could not start the claims run"));
      }
    };
    xhr.onerror = () => reject(new Error("Could not reach the server to start the run"));
    xhr.send(form);
  });
}

export function confirmClaimMap(
  runId: string,
  map: ClaimMap,
  remember: { pattern: string; role: string }[],
  revision?: number
): Promise<{ ok: boolean; employees: number; changes: string[] }> {
  return mutate(`/api/claims-runs/${runId}/confirm-map`, "POST",
    { map, remember, expected_revision: revision }, "Could not confirm the map");
}

/** Stop a run that is still working (H11): the workers stop, nothing partial
 *  becomes ready, the run is marked failed with the reason. Only a run in
 *  progress can be cancelled; the server refuses the rest. */
export function cancelClaimsRun(runId: string, revision?: number): Promise<{ ok: boolean; status: string; tools_cancelled: number }> {
  return mutate(`/api/claims-runs/${runId}/cancel`, "POST", { expected_revision: revision }, "Could not cancel the run");
}

/** A page of one of the run's files as an image; highlight names the
 *  third of the page (left/middle/right) a receipt sits in. */
export function claimsFileUrl(
  runId: string,
  path: string,
  page = 1,
  highlight = "",
  full = false
): string {
  const q = new URLSearchParams({ path, page: String(page) });
  if (highlight) q.set("highlight", highlight);
  if (full) q.set("full", "true");
  return `/api/claims-runs/${runId}/file?${q}`;
}

/** Per-client steering: the structured profile, the playbook paragraph,
 *  and the last confirmed map. */
export interface ClaimsSettings {
  client: string;
  local_mode: boolean;
  /** Whether the New-run form offers SharePoint link fields (a switch). */
  sharepoint_source: boolean;
  profile: {
    mileage_rates: Record<string, string>;
    km_tolerance: string;
    receipt_date_window_days: number;
    /** Unclaimed receipts at or above this amount (MYR) raise a flag. */
    unclaimed_receipt_threshold: string;
    receipt_optional_items: string[];
    mileage_item_pattern: string;
    categories: { item: string; gl: string }[];
    category_rule: string;
    file_role_patterns: { pattern: string; role: string }[];
    checks: Record<string, boolean>;
    set_by: Record<string, { by: string; at: string; evidence: string }>;
  };
  playbook: string;
  last_map: { run_id?: string; at?: string; map?: ClaimMap } | Record<string, never>;
}

export async function getFlagCatalogue(): Promise<{ codes: FlagCatalogue; kinds: string[]; toggleable: string[] }> {
  const r = await fetch("/api/claims-settings/catalogue");
  if (!r.ok) return fail(r, "Could not load the flag catalogue");
  return r.json();
}

export async function getClaimsSettings(): Promise<ClaimsSettings> {
  const r = await fetch("/api/claims-settings");
  if (!r.ok) return fail(r, "Could not load the claims settings");
  return r.json();
}

export async function saveClaimsSettings(body: {
  profile?: Partial<ClaimsSettings["profile"]>;
  playbook?: string;
  forget_last_map?: boolean;
}): Promise<ClaimsSettings> {
  const r = await fetch("/api/claims-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) return fail(r, "Could not save the claims settings");
  return r.json();
}

// Every review mutation sends the revision the screen last saw (H9/H10;
// expected_revision); the server answers 409 when the run moved on. ONE
// helper turns that into a StaleRunError so every screen handles it the
// same way (useAction: reload, then show the message) — no per-call
// status checks to get wrong.

export class StaleRunError extends Error {
  constructor() {
    super("This run changed since your screen loaded — it has been reloaded; please try again.");
    this.name = "StaleRunError";
  }
}

/** A JSON mutation: 409 → StaleRunError, any other failure → Error with
 *  the server's detail (or the fallback), success → the parsed body
 *  (an empty / non-JSON body reads as {}). */
export async function mutate<T = Record<string, unknown>>(
  url: string,
  method: "POST" | "PUT" | "DELETE",
  body: Record<string, unknown>,
  fallback: string
): Promise<T> {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 409) throw new StaleRunError();
  if (!r.ok) return fail(r, fallback);
  try {
    return (await r.json()) as T;
  } catch {
    return {} as T;
  }
}

export async function retryClaimEmployee(runId: string, employeeId: string, revision?: number): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/employees/${employeeId}/retry`, "POST",
    { expected_revision: revision }, "Could not retry this employee");
}

export async function retryCase(runId: string, caseId: string, revision?: number): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/cases/${caseId}/retry`, "POST",
    { expected_revision: revision }, "Could not re-verify this case");
}

export async function decideClaimFlag(
  runId: string,
  flagId: string,
  decision: "accepted" | "dismissed",
  note: string,
  revision?: number,
  disposition?: Disposition
): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/flags/${flagId}/decide`, "POST",
    { decision, note, expected_revision: revision, ...(disposition ? { disposition } : {}) },
    "Could not record the decision");
}

export async function correctClaimRow(
  runId: string,
  rowId: string,
  fields: Record<string, string>,
  reason: string,
  revision?: number
): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/rows/${rowId}/correct`, "POST",
    { fields, reason, expected_revision: revision }, "Correction failed");
}

export async function setEmployeeCategory(
  runId: string,
  employeeId: string,
  category: string,
  gl: string,
  reason: string,
  revision?: number
): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/employees/${employeeId}/category`, "PUT",
    { category, gl, reason, expected_revision: revision }, "Could not set the category");
}

export async function setCaseCategory(
  runId: string, caseId: string, category: string, gl: string, reason: string, revision?: number
): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/cases/${caseId}/category`, "PUT",
    { category, gl, reason, expected_revision: revision }, "Could not set the category");
}

// ---- Map & Group actions (hardening H6). Every one sends the revision the
// screen last saw; a 409 means someone (or another tab) changed the run —
// reload and try again.

type GroupingReply = { ok: boolean; revision: number; grouping?: Grouping };

export function createCase(runId: string, revision: number, label: string, artifactIds: string[]) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/cases`, "POST",
    { label, artifact_ids: artifactIds, expected_revision: revision }, "Could not create the case");
}

export function updateCase(runId: string, revision: number, caseId: string,
  patch: { label?: string; roles?: Partial<ClaimEmployee["roles"]>; state?: "excluded" | "proposed" }) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/cases/${caseId}`, "PUT",
    { ...patch, expected_revision: revision }, "Could not update the case");
}

export function setClaimant(runId: string, revision: number, caseId: string, name: string, identifier: string) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/cases/${caseId}/claimant`, "PUT",
    { name, identifier, expected_revision: revision }, "Could not set the claimant");
}

export function confirmClaimant(runId: string, revision: number, caseId: string) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/cases/${caseId}/claimant`, "PUT",
    { confirm: true, expected_revision: revision }, "Could not confirm the claimant");
}

export function mergeCase(runId: string, revision: number, caseId: string, into: string) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/cases/${caseId}/merge`, "POST",
    { into, expected_revision: revision }, "Could not merge the cases");
}

export function splitCase(runId: string, revision: number, caseId: string, artifactIds: string[], label: string) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/cases/${caseId}/split`, "POST",
    { artifact_ids: artifactIds, label, expected_revision: revision }, "Could not split the case");
}

export function moveArtifact(runId: string, revision: number, artifactId: string, caseId: string) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/artifacts/${artifactId}/move`, "POST",
    { case_id: caseId, expected_revision: revision }, "Could not move the file");
}

export function setArtifactRole(runId: string, revision: number, artifactId: string, role: ArtifactRole, remember: boolean) {
  return mutate<GroupingReply>(`/api/claims-runs/${runId}/artifacts/${artifactId}/role`, "PUT",
    { role, remember, expected_revision: revision }, "Could not set the file's role");
}

export async function setArtifactDisposition(runId: string, revision: number, artifactId: string, disposition: Disposition, reason: string): Promise<void> {
  await mutate(`/api/claims-runs/${runId}/artifacts/${artifactId}/disposition`, "POST",
    { disposition, reason, expected_revision: revision }, "Could not settle the file");
}

export function confirmGrouping(runId: string, revision: number): Promise<{ ok: boolean; cases: number; revision: number }> {
  return mutate(`/api/claims-runs/${runId}/confirm-grouping`, "POST",
    { expected_revision: revision }, "Could not confirm the grouping");
}
