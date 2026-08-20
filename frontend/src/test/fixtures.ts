import {
  ClaimCase,
  ClaimEmployee,
  ClaimFlag,
  ClaimRow,
  ClaimsRunDetail,
  ClaimsRunSummary,
  FlagCatalogue,
} from "../api";

// Fixtures for the frontend tests: the smallest run detail the screens
// accept, with helpers to add the pieces a test cares about. Everything
// here mirrors the wire shape the backend sends (routes.py), so a test
// that passes here is a test about the real payload.

export function makeRun(over: Partial<ClaimsRunDetail> = {}): ClaimsRunDetail {
  return {
    id: "run1",
    client: "Acme",
    status: "ready",
    error: "",
    progress: {},
    folder: "/batches/aug",
    employee_count: 0,
    employees_done: 0,
    open_flags: 0,
    notes: 0,
    errors: 0,
    warnings: 0,
    created_at: "2026-08-19T02:00:00Z",
    revision: 7,
    folder_url: "",
    listing_url: "",
    received_date: "2026-08-19",
    instructions: "",
    survey: {},
    map: {},
    map_warnings: [],
    listing_headers: {},
    employees: [],
    rows: [],
    evidence: [],
    flags: [],
    catalogue: {},
    outputs: {},
    ...over,
  };
}

// The list screen is sent the summary, not the whole detail.
export function makeRunSummary(
  over: Partial<ClaimsRunSummary> = {},
): ClaimsRunSummary {
  const { id, client, status, error, progress, folder, employee_count,
    employees_done, open_flags, notes, errors, warnings, created_at } = makeRun();
  return {
    id, client, status, error, progress, folder, employee_count,
    employees_done, open_flags, notes, errors, warnings, created_at,
    ...over,
  };
}

export function makeEmployee(over: Partial<ClaimEmployee> = {}): ClaimEmployee {
  return {
    id: "e1",
    folder: "Aegene Ong",
    name: "Aegene Ong",
    er_code: "ER001",
    roles: {
      report_file: null, report_tab: null, mileage_tab: null,
      no_report: false, receipt_files: [], ignored: [], unplaced: [],
    },
    status: "verified",
    error: "",
    report_total: "100.00",
    category: "Travel",
    gl: "6100",
    category_basis: "",
    summary: { rows: 3 },
    ...over,
  };
}

export function makeCase(over: Partial<ClaimCase> = {}): ClaimCase {
  return {
    id: "c1",
    employee_id: "e1",
    label: "Aegene Ong",
    claimant: { name: "Aegene Ong", identifier: "ER001", state: "proposed", basis: "header", citations: [] },
    state: "proposed",
    grouping_basis: "folder:Aegene Ong",
    citations: [],
    artifact_ids: [],
    roles: {
      report_file: null, report_tab: null, mileage_tab: null,
      no_report: false, receipt_files: [], ignored: [], unplaced: [],
    },
    status: "verified",
    error: "",
    category: "Travel",
    gl: "6100",
    category_basis: "",
    reported_total: "100.00",
    lines_total: "100.00",
    summary: { rows: 3 },
    confidence: 0.9,
    reason: "one folder, one name",
    folder: "Aegene Ong",
    name: "Aegene Ong",
    er_code: "ER001",
    report_total: "100.00",
    ...over,
  };
}

export function makeFlag(over: Partial<ClaimFlag> = {}): ClaimFlag {
  return {
    id: "f1",
    employee_id: "e1",
    case_id: "c1",
    row_id: "",
    evidence_id: "",
    code: "CLAIM_AMOUNT_UNCONFIRMED",
    reason: "no Reported Total was found",
    basis: "",
    cite: {},
    status: "open",
    resolution: "",
    ...over,
  };
}

export function makeRow(over: Partial<ClaimRow> = {}): ClaimRow {
  return {
    id: "r1",
    employee_id: "e1",
    case_id: "c1",
    kind: "expense",
    sheet: "Expense Report",
    row: 4,
    values: { date: "2026-07-01", item: "Taxi", amount: "50.00", total: "50.00", currency: "MYR" },
    corrections: {},
    matched_evidence_id: "",
    verdict: "matched",
    ...over,
  };
}

export const CATALOGUE: FlagCatalogue = {
  CLAIM_AMOUNT_UNCONFIRMED: {
    code: "CLAIM_AMOUNT_UNCONFIRMED",
    title: "Claim amount unconfirmed",
    meaning: "No Reported Total was found for this case.",
    what_to_do: "Confirm the receipt totals, or leave the case out.",
    kind: "money", blocks: "open", toggle: true,
  },
  RECEIPT_MISSING: {
    code: "RECEIPT_MISSING",
    title: "Receipt missing",
    meaning: "A line has no receipt.",
    what_to_do: "Find it or leave the line out.",
    kind: "evidence", blocks: "open", toggle: true,
  },
  UNCLAIMED_RECEIPT: {
    code: "UNCLAIMED_RECEIPT",
    title: "Receipt supports no row",
    meaning: "",
    what_to_do: "",
    kind: "note", blocks: "open", toggle: true,
  },
};
