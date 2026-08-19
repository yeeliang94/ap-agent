import { describe, expect, it } from "vitest";
import { ClaimEvidence, ClaimRow } from "../../api";
import { CATALOGUE, makeFlag, makeRow } from "../../test/fixtures";
import { centsOf, describeFlag, kindOf, rm, stakeCents } from "./flags";

// The money on the Review screen's summary strip. It is advisory, but it
// is still money on a screen, so it never goes through a float: cents are
// parsed out of the server's decimal STRINGS and added as integers.

describe("describeFlag", () => {
  it("uses the run's catalogue", () => {
    expect(describeFlag(CATALOGUE, "RECEIPT_MISSING").title).toBe("Receipt missing");
  });

  it("turns an unknown code into words, never SNAKE_CASE", () => {
    const info = describeFlag(CATALOGUE, "SOME_NEW_CODE");
    expect(info.title).toBe("Some new code");
    expect(info.kind).toBe("structure");
  });

  it("survives a run served with no catalogue at all", () => {
    expect(describeFlag(undefined, "RECEIPT_MISSING").title).toBe("Receipt missing");
  });
});

describe("kindOf", () => {
  it("puts every info flag under notes, whatever its code says", () => {
    expect(kindOf(CATALOGUE, makeFlag({ code: "RECEIPT_MISSING", status: "info" }))).toBe("note");
  });

  it("takes the kind from the catalogue for an open flag", () => {
    expect(kindOf(CATALOGUE, makeFlag({ code: "RECEIPT_MISSING" }))).toBe("evidence");
  });

  it("counts an OPEN note-kind flag as money at risk", () => {
    // An unclaimed receipt above the threshold: the catalogue calls it a
    // note, but an open one is money nobody has accounted for.
    expect(kindOf(CATALOGUE, makeFlag({ code: "UNCLAIMED_RECEIPT", status: "open" }))).toBe("money");
  });
});

describe("centsOf", () => {
  it("reads plain decimals as integer cents", () => {
    expect(centsOf("50.00")).toBe(5000);
    expect(centsOf("1,234.5")).toBe(123450);
    expect(centsOf("12")).toBe(1200);
    expect(centsOf(".75")).toBe(75);
    expect(centsOf(" 0.075 ")).toBe(8);        // a third decimal rounds half up
    expect(centsOf("-12")).toBe(-1200);
  });

  it("adds without float error", () => {
    // 0.1 + 0.2 as floats is 0.30000000000000004; as cents it is 30.
    expect(centsOf("0.1")! + centsOf("0.2")!).toBe(30);
    let total = 0;
    for (let i = 0; i < 10; i++) total += centsOf("0.07")!;
    expect(total).toBe(70);
  });

  it("refuses anything that is not a plain decimal number", () => {
    expect(centsOf("RM 50.00")).toBeNull();
    expect(centsOf("1e3")).toBeNull();
    expect(centsOf("")).toBeNull();
    expect(centsOf("abc")).toBeNull();
    expect(centsOf(null)).toBeNull();
    expect(centsOf(undefined)).toBeNull();
  });
});

describe("stakeCents", () => {
  const row = makeRow({ id: "r1", values: { amount: "50.00", total: "220.75" } });
  const evidence: ClaimEvidence = {
    id: "ev1", employee_id: "e1", case_id: "c1", kind: "receipt",
    file: "receipts.pdf", page: 2, position: "left",
    values: { amount: "31.40" }, confidence: {}, matched_row_id: "",
  };
  const rows = new Map<string, ClaimRow>([[row.id, row]]);
  const evs = new Map<string, ClaimEvidence>([[evidence.id, evidence]]);

  it("prefers the row's MYR total over its face amount", () => {
    expect(stakeCents(makeFlag({ row_id: "r1" }), rows, evs)).toBe(22075);
  });

  it("falls back to the row's amount where there is no total", () => {
    const bare = makeRow({ id: "r2", values: { amount: "50.00" } });
    expect(stakeCents(makeFlag({ row_id: "r2" }), new Map([[bare.id, bare]]), evs)).toBe(5000);
  });

  it("uses the cited receipt when the flag is about evidence", () => {
    expect(stakeCents(makeFlag({ evidence_id: "ev1" }), rows, evs)).toBe(3140);
  });

  it("is null when nothing is at stake (a case- or run-level flag)", () => {
    expect(stakeCents(makeFlag(), rows, evs)).toBeNull();
  });
});

describe("rm", () => {
  it("formats integer cents with no division through a float", () => {
    expect(rm(123450)).toBe("RM 1,234.50");
    expect(rm(5)).toBe("RM 0.05");
    expect(rm(0)).toBe("RM 0.00");
    expect(rm(-1200)).toBe("-RM 12.00");
  });

  it("is empty for nothing at stake, so the strip shows no figure", () => {
    expect(rm(null)).toBe("");
  });
});
