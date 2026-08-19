import { describe, expect, it } from "vitest";
import { makeCase, makeEmployee, makeRun } from "../../test/fixtures";
import { reviewUnits, unitIdOf, usesCases } from "./units";

// The review surface is keyed by Claim Case where the run has cases, and
// by employee where it does not (an older run, or CLAIMS_CASE_MODEL off).
// Getting that wrong sends a flag to the wrong case, so it is pinned here.

describe("usesCases", () => {
  it("is false when the run carries no cases", () => {
    expect(usesCases(makeRun())).toBe(false);
    expect(usesCases(makeRun({ cases: [] }))).toBe(false);
  });

  it("is true as soon as one case is present", () => {
    expect(usesCases(makeRun({ cases: [makeCase()] }))).toBe(true);
  });
});

describe("reviewUnits", () => {
  it("builds one unit per case, carrying the claimant and both totals", () => {
    const run = makeRun({
      employees: [makeEmployee()],
      cases: [makeCase({ reported_total: "120.00", lines_total: "118.50" })],
    });
    const [u] = reviewUnits(run);
    expect(reviewUnits(run)).toHaveLength(1);
    expect(u.id).toBe("c1");
    expect(u.case_id).toBe("c1");
    expect(u.employee_id).toBe("e1");
    expect(u.claimant_state).toBe("proposed");
    expect(u.name).toBe("Aegene Ong");
    // The Reported Total and the Calculated Lines Total stay apart.
    expect(u.reported_total).toBe("120.00");
    expect(u.lines_total).toBe("118.50");
    // The employee record is attached for the screens that still need it.
    expect(u.employee?.id).toBe("e1");
  });

  it("falls back to one unit per employee when the run has no cases", () => {
    const run = makeRun({ employees: [makeEmployee({ summary: { rows_total: "99.00" } })] });
    const [u] = reviewUnits(run);
    expect(u.id).toBe("e1");
    expect(u.case_id).toBe("");
    // With no case model there is no claimant state to show at all.
    expect(u.claimant_state).toBe("");
    expect(u.label).toBe("Aegene Ong");
    expect(u.lines_total).toBe("99.00");
    expect(u.case).toBeUndefined();
  });

  it("keeps a case whose employee record is missing", () => {
    const run = makeRun({ employees: [], cases: [makeCase({ employee_id: "gone" })] });
    const [u] = reviewUnits(run);
    expect(u.id).toBe("c1");
    expect(u.employee).toBeUndefined();
  });
});

describe("unitIdOf", () => {
  const flagLike = { case_id: "c1", employee_id: "e1" };

  it("keys by case id when the run speaks cases", () => {
    expect(unitIdOf(makeRun({ cases: [makeCase()] }), flagLike)).toBe("c1");
  });

  it("keys by employee id otherwise", () => {
    expect(unitIdOf(makeRun(), flagLike)).toBe("e1");
  });

  it("is the empty string for a whole-batch item on a case run", () => {
    const run = makeRun({ cases: [makeCase()] });
    expect(unitIdOf(run, { case_id: "", employee_id: "e1" })).toBe("");
    expect(unitIdOf(run, { employee_id: "e1" })).toBe("");
  });
});
