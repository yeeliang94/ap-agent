import { describe, expect, it } from "vitest";
import { ClaimMap, MapEmployee, Survey } from "../../api";
import { makeRun } from "../../test/fixtures";
import { mapProblems, patternFor } from "./MapView";

// The client-side mirror of the server's map validation: it decides
// whether Confirm & verify is pressable and which row is highlighted.

function employee(over: Partial<MapEmployee> = {}): MapEmployee {
  return {
    folder: "Aegene Ong",
    is_employee: true,
    name: "Aegene Ong",
    er_code: "ER001",
    report_file: "Aegene Ong/report.xlsx",
    report_tab: "Expense Report",
    mileage_tab: null,
    no_report: false,
    files: [{ path: "Aegene Ong/report.xlsx", role: "report", reason: "" }],
    reason: "",
    ...over,
  };
}

function runWith(employees: MapEmployee[], paths: string[] = ["Aegene Ong/report.xlsx"]) {
  const survey: Survey = {
    folders: [],
    files: paths.map((path) => ({
      path, name: path.split("/").pop()!, folder: path.split("/")[0],
      type: "workbook" as const, size: 10, pages: null, er_code: "", peek: null,
    })),
    root_files: [],
  };
  const map: ClaimMap = { employees, root_files: [], notes: [] };
  return { map, run: makeRun({ status: "map_ready", survey }) };
}

describe("mapProblems", () => {
  it("is empty for a complete map", () => {
    const { map, run } = runWith([employee()]);
    expect(mapProblems(map, run)).toEqual([]);
  });

  it("skips folders that are not an employee, and ones marked skip", () => {
    const { map, run } = runWith([
      employee({ is_employee: false, name: "", report_file: null }),
      employee({ folder: "Old", skip: true, name: "", report_file: null }),
    ]);
    expect(mapProblems(map, run)).toEqual([]);
  });

  it("asks for a report file, or 'no report'", () => {
    const { map, run } = runWith([employee({ report_file: null })]);
    const problems = mapProblems(map, run);
    expect(problems).toHaveLength(1);
    expect(problems[0].message).toContain("choose the report file");
  });

  it("asks for the tab once a report file is chosen", () => {
    const { map, run } = runWith([employee({ report_tab: null })]);
    expect(mapProblems(map, run)[0].message).toContain("choose the report tab");
  });

  it("insists the report is a workbook", () => {
    const { map, run } = runWith([employee({ report_file: "Aegene Ong/scan.pdf" })]);
    expect(mapProblems(map, run)[0].message).toContain("must be a workbook");
  });

  it("wants at least one receipt file where there is no report", () => {
    const { map, run } = runWith([employee({ no_report: true, report_file: null, report_tab: null, files: [] })]);
    expect(mapProblems(map, run)[0].message).toContain("no report and no receipt files");
    const ok = runWith([employee({
      no_report: true, report_file: null, report_tab: null,
      files: [{ path: "Aegene Ong/receipts.pdf", role: "receipts", reason: "" }],
    })]);
    expect(mapProblems(ok.map, ok.run)).toEqual([]);
  });

  it("wants a name, and refuses two employees sharing an ER code", () => {
    const { map, run } = runWith([
      employee({ name: "" }),
      employee({ folder: "Nick Goh", name: "Nick Goh", report_file: null, no_report: true,
        files: [{ path: "Nick Goh/receipts.pdf", role: "receipts", reason: "" }] }),
    ]);
    const messages = mapProblems(map, run).map((p) => p.message);
    expect(messages.some((m) => m.includes("needs a name"))).toBe(true);
    expect(messages.some((m) => m.includes("ER code ER001 is also used by"))).toBe(true);
  });

  // The reason problems carry a folder at all: a name that is the start
  // of another name must not steal that row's problems.
  it("keys each problem to its own folder, not to a name prefix", () => {
    const { map, run } = runWith([
      employee({ folder: "Ali", name: "Ali", er_code: "ER001", report_file: null }),
      employee({ folder: "Alicia", name: "Alicia", er_code: "ER002", report_file: null }),
    ]);
    const problems = mapProblems(map, run);
    expect(problems).toHaveLength(2);
    expect(problems.filter((p) => p.folder === "Ali")).toHaveLength(1);
    expect(problems.filter((p) => p.folder === "Alicia")).toHaveLength(1);
    // Matching on the message text would have given "Ali" both of them.
    expect(problems.filter((p) => p.message.startsWith("Ali"))).toHaveLength(2);
  });
});

describe("patternFor", () => {
  it("turns the employee-specific prefix into a wildcard", () => {
    expect(patternFor("Aegene Ong/Aegene Ong_Approval.pdf")).toBe("*_Approval.pdf");
  });

  it("leaves a name with no underscore alone", () => {
    expect(patternFor("Aegene Ong/receipts.pdf")).toBe("receipts.pdf");
  });
});
