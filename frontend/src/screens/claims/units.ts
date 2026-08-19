import { ClaimCase, ClaimEmployee, ClaimsRunDetail, retryCase, retryClaimEmployee } from "../../api";

// The review surface is keyed by Claim Case (hardening H10). A run from
// before the case model — or one served with CLAIMS_CASE_MODEL off — has
// only employees; each employee is then its own unit. Flags, rows and
// evidence carry both ids, so `unitIdOf` picks the case id when the run
// speaks cases and the employee id otherwise.

export interface ReviewUnit {
  /** The id flags/rows/evidence are keyed by (case id, or employee id). */
  id: string;
  employee_id: string;
  case_id: string;
  label: string;
  name: string;
  identifier: string;
  claimant_state: "confirmed" | "proposed" | "unknown" | "";
  claimant_basis: string;
  grouping_basis: string;
  confidence: number;
  status: string;
  error: string;
  category: string;
  gl: string;
  category_basis: string;
  reported_total: string;
  lines_total: string;
  summary: Record<string, unknown>;
  roles: ClaimEmployee["roles"] | undefined;
  employee: ClaimEmployee | undefined;
  case: ClaimCase | undefined;
}

export function usesCases(run: ClaimsRunDetail): boolean {
  return Array.isArray(run.cases) && run.cases.length > 0;
}

export function reviewUnits(run: ClaimsRunDetail): ReviewUnit[] {
  const byEmp = new Map(run.employees.map((e) => [e.id, e]));
  if (usesCases(run)) {
    return (run.cases ?? []).map((c) => {
      const e = byEmp.get(c.employee_id);
      return {
        id: c.id, employee_id: c.employee_id, case_id: c.id, label: c.label,
        name: c.claimant.name, identifier: c.claimant.identifier, claimant_state: c.claimant.state,
        claimant_basis: c.claimant.basis, grouping_basis: c.grouping_basis, confidence: c.confidence,
        status: c.status, error: c.error, category: c.category, gl: c.gl, category_basis: c.category_basis,
        reported_total: c.reported_total, lines_total: c.lines_total, summary: c.summary,
        roles: c.roles, employee: e, case: c,
      };
    });
  }
  return run.employees.map((e) => ({
    id: e.id, employee_id: e.id, case_id: "", label: e.folder, name: e.name, identifier: e.er_code,
    claimant_state: "", claimant_basis: "", grouping_basis: "folder", confidence: 0,
    status: e.status, error: e.error, category: e.category, gl: e.gl, category_basis: e.category_basis,
    reported_total: e.report_total, lines_total: String(e.summary?.rows_total ?? ""), summary: e.summary,
    roles: e.roles, employee: e, case: undefined,
  }));
}

/** The unit a flag / row / evidence item belongs to ("" = whole batch). */
export function unitIdOf(run: ClaimsRunDetail, x: { case_id?: string; employee_id: string }): string {
  return usesCases(run) ? (x.case_id ?? "") : x.employee_id;
}

/** Re-run one unit's worker: the case route when the run speaks cases,
 *  the delivered employee route otherwise. */
export function retryUnit(run: ClaimsRunDetail, u: ReviewUnit): Promise<void> {
  return u.case_id ? retryCase(run.id, u.case_id, run.revision) : retryClaimEmployee(run.id, u.employee_id, run.revision);
}
