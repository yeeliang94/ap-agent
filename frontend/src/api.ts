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

// ---------------------------------------------------------------------------
// Claims module — a second run type with its own routes (/api/claims-runs).

export interface ClaimsRunSummary {
  id: string;
  client: string;
  status: string; // queued | surveying | mapping | map_ready | verifying | ready | failed
  error: string;
  progress: { done?: number; total?: number; what?: string; employees?: number };
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
  batch?: File | null;
  listing?: File | null;
}

export async function createClaimsRun(input: NewClaimsRun): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("received_date", input.received_date);
  if (input.folder_url) form.append("folder_url", input.folder_url);
  if (input.listing_url) form.append("listing_url", input.listing_url);
  if (input.instructions) form.append("instructions", input.instructions);
  if (input.batch) form.append("batch", input.batch);
  if (input.listing) form.append("listing", input.listing);
  const r = await fetch("/api/claims-runs", { method: "POST", body: form });
  if (!r.ok) return fail(r, "Could not start the claims run");
  return r.json();
}

export async function confirmClaimMap(
  runId: string,
  map: ClaimMap,
  remember: { pattern: string; role: string }[]
): Promise<{ ok: boolean; employees: number; changes: string[] }> {
  const r = await fetch(`/api/claims-runs/${runId}/confirm-map`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ map, remember }),
  });
  if (!r.ok) return fail(r, "Could not confirm the map");
  return r.json();
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
  profile: {
    mileage_rates: Record<string, string>;
    km_tolerance: string;
    receipt_date_window_days: number;
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

// Every review mutation sends the revision the screen last saw (H9/H10);
// the server answers 409 when the run moved on — the screen reloads.
function staleOr(r: Response, fallback: string): Promise<never> {
  if (r.status === 409) throw new StaleRunError();
  return fail(r, fallback);
}

export async function retryClaimEmployee(runId: string, employeeId: string, revision?: number): Promise<void> {
  const r = await fetch(`/api/claims-runs/${runId}/employees/${employeeId}/retry`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: revision }),
  });
  if (!r.ok) return staleOr(r, "Could not retry this employee");
}

export async function retryCase(runId: string, caseId: string, revision?: number): Promise<void> {
  const r = await fetch(`/api/claims-runs/${runId}/cases/${caseId}/retry`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: revision }),
  });
  if (!r.ok) return staleOr(r, "Could not re-verify this case");
}

export async function decideClaimFlag(
  runId: string,
  flagId: string,
  decision: "accepted" | "dismissed",
  note: string,
  revision?: number,
  disposition?: Disposition
): Promise<void> {
  const r = await fetch(`/api/claims-runs/${runId}/flags/${flagId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note, expected_revision: revision, ...(disposition ? { disposition } : {}) }),
  });
  if (!r.ok) return staleOr(r, "Could not record the decision");
}

export async function correctClaimRow(
  runId: string,
  rowId: string,
  fields: Record<string, string>,
  reason: string,
  revision?: number
): Promise<void> {
  const r = await fetch(`/api/claims-runs/${runId}/rows/${rowId}/correct`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields, reason, expected_revision: revision }),
  });
  if (!r.ok) return staleOr(r, "Correction failed");
}

export async function setEmployeeCategory(
  runId: string,
  employeeId: string,
  category: string,
  gl: string,
  reason: string,
  revision?: number
): Promise<void> {
  const r = await fetch(`/api/claims-runs/${runId}/employees/${employeeId}/category`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category, gl, reason, expected_revision: revision }),
  });
  if (!r.ok) return staleOr(r, "Could not set the category");
}


// ---- Map & Group actions (hardening H6). Every one sends the revision the
// screen last saw; a 409 means someone (or another tab) changed the run —
// reload and try again.

async function groupingCall(url: string, method: string, body: Record<string, unknown>, fallback: string) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 409) throw new StaleRunError();
  if (!r.ok) return fail(r, fallback);
  return r.json() as Promise<{ ok: boolean; revision: number; grouping?: Grouping }>;
}

export class StaleRunError extends Error {
  constructor() {
    super("This run changed since your screen loaded — it has been reloaded; please try again.");
    this.name = "StaleRunError";
  }
}

export function createCase(runId: string, revision: number, label: string, artifactIds: string[]) {
  return groupingCall(`/api/claims-runs/${runId}/cases`, "POST",
    { label, artifact_ids: artifactIds, expected_revision: revision }, "Could not create the case");
}

export function updateCase(runId: string, revision: number, caseId: string,
  patch: { label?: string; roles?: Partial<ClaimEmployee["roles"]>; state?: "excluded" | "proposed" }) {
  return groupingCall(`/api/claims-runs/${runId}/cases/${caseId}`, "PUT",
    { ...patch, expected_revision: revision }, "Could not update the case");
}

export function setClaimant(runId: string, revision: number, caseId: string, name: string, identifier: string) {
  return groupingCall(`/api/claims-runs/${runId}/cases/${caseId}/claimant`, "PUT",
    { name, identifier, expected_revision: revision }, "Could not set the claimant");
}

export function confirmClaimant(runId: string, revision: number, caseId: string) {
  return groupingCall(`/api/claims-runs/${runId}/cases/${caseId}/claimant`, "PUT",
    { confirm: true, expected_revision: revision }, "Could not confirm the claimant");
}

export function mergeCase(runId: string, revision: number, caseId: string, into: string) {
  return groupingCall(`/api/claims-runs/${runId}/cases/${caseId}/merge`, "POST",
    { into, expected_revision: revision }, "Could not merge the cases");
}

export function splitCase(runId: string, revision: number, caseId: string, artifactIds: string[], label: string) {
  return groupingCall(`/api/claims-runs/${runId}/cases/${caseId}/split`, "POST",
    { artifact_ids: artifactIds, label, expected_revision: revision }, "Could not split the case");
}

export function moveArtifact(runId: string, revision: number, artifactId: string, caseId: string) {
  return groupingCall(`/api/claims-runs/${runId}/artifacts/${artifactId}/move`, "POST",
    { case_id: caseId, expected_revision: revision }, "Could not move the file");
}

export function setArtifactRole(runId: string, revision: number, artifactId: string, role: ArtifactRole, remember: boolean) {
  return groupingCall(`/api/claims-runs/${runId}/artifacts/${artifactId}/role`, "PUT",
    { role, remember, expected_revision: revision }, "Could not set the file's role");
}

export async function setArtifactDisposition(runId: string, revision: number, artifactId: string, disposition: Disposition, reason: string) {
  const r = await fetch(`/api/claims-runs/${runId}/artifacts/${artifactId}/disposition`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ disposition, reason, expected_revision: revision }),
  });
  if (r.status === 409) throw new StaleRunError();
  if (!r.ok) return fail(r, "Could not settle the file");
}

export async function setCaseCategory(
  runId: string, caseId: string, category: string, gl: string, reason: string, revision?: number
): Promise<void> {
  const r = await fetch(`/api/claims-runs/${runId}/cases/${caseId}/category`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category, gl, reason, expected_revision: revision }),
  });
  if (!r.ok) return staleOr(r, "Could not set the category");
}

export async function confirmGrouping(runId: string, revision: number): Promise<{ ok: boolean; cases: number; revision: number }> {
  const r = await fetch(`/api/claims-runs/${runId}/confirm-grouping`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: revision }),
  });
  if (r.status === 409) throw new StaleRunError();
  if (!r.ok) return fail(r, "Could not confirm the grouping");
  return r.json();
}
