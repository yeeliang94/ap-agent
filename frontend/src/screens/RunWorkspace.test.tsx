import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { RunActivityProvider } from "../activity";
import { BrowserRouter } from "../router";
import { CATALOGUE, makeCase, makeFlag, makeRow, makeRun } from "../test/fixtures";
import RunWorkspace from "./RunWorkspace";

vi.mock("../api", async (importOriginal) => {
  const real = await importOriginal<typeof api>();
  return {
    ...real,
    listRuns: vi.fn(async () => []),
    listClaimsRuns: vi.fn(async () => []),
    getClaimsRun: vi.fn(),
    getClaimsRunEvents: vi.fn(async () => []),
    setClaimant: vi.fn(),
    recheckClaimIdentity: vi.fn(async () => ({ ok: true, revision: 8 })),
    resolveOwnership: vi.fn(async () => ({ ok: true, revision: 8 })),
    decideClaimFlag: vi.fn(async () => {}),
  };
});

const getClaimsRun = vi.mocked(api.getClaimsRun);
const setClaimant = vi.mocked(api.setClaimant);

const grouping = (actionsEnabled = true): api.Grouping => ({
  problems: [], by_case: {}, actions_enabled: actionsEnabled,
  counts: { artifacts: 0, dispositioned: 0, unresolved: 0, material_unresolved: 0, cases: 2, to_verify: 2, claimants_confirmed: 0, conflicts: 0 },
  ok: true,
});

function renderOrganizer() {
  window.history.replaceState({}, "", "/claims/run1/organize");
  return render(<BrowserRouter><RunActivityProvider><RunWorkspace kind="claim" runId="run1" view="organize" /></RunActivityProvider></BrowserRouter>);
}

beforeEach(() => {
  getClaimsRun.mockReset();
  setClaimant.mockReset();
  localStorage.clear();
  window.scrollTo = vi.fn();
});

describe("RunWorkspace organization contracts", () => {
  it("reloads before reporting a stale-revision conflict", async () => {
    const first = makeRun({ status: "map_ready", revision: 7, cases: [makeCase()], artifacts: [], grouping: grouping() });
    const refreshed = { ...first, revision: 8 };
    getClaimsRun.mockResolvedValueOnce(first).mockResolvedValue(refreshed);
    setClaimant.mockRejectedValueOnce(new api.StaleRunError());
    renderOrganizer();

    await waitFor(() => expect(screen.getByRole("button", { name: "Save claimant" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Save claimant" }));

    await waitFor(() => expect(getClaimsRun).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/it has been reloaded; please try again/i)).toBeTruthy();
  });

  it("shows real regrouping controls only when the server capability is enabled", async () => {
    const second = makeCase({ id: "c2", employee_id: "e2", label: "Second Claim" });
    const artifacts: api.ClaimArtifact[] = [
      { id: "a1", path: "first/report.xlsx", sha256: "1", media_type: "workbook", size: 1, pages: null, sheets: ["Claim"], inspection_state: "inspected", failure_reason: "", proposed_role: "report", role_reason: "", disposition: "used", disposition_reason: "", disposition_by: "adapter", needs_confirmation: false, case_id: "c1" },
      { id: "a2", path: "loose/receipt.pdf", sha256: "2", media_type: "pdf", size: 1, pages: 1, sheets: [], inspection_state: "inspected", failure_reason: "", proposed_role: "receipts", role_reason: "", disposition: "unresolved", disposition_reason: "", disposition_by: "", needs_confirmation: true, case_id: "" },
    ];
    getClaimsRun.mockResolvedValue(makeRun({ status: "map_ready", cases: [makeCase(), second], artifacts, grouping: grouping(true) }));
    renderOrganizer();

    fireEvent.click(await screen.findByText("Advanced actions"));
    expect(screen.getByRole("button", { name: "Merge Claim" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Split into new Claim" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create Claim" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Merge into Claim" })).toBeTruthy();
  });

  it("opens the highest-risk blocking finding and advances within its Claim", async () => {
    const claim = makeCase();
    const low = makeFlag({ id: "f-low", code: "CLAIM_AMOUNT_UNCONFIRMED", row_id: "r-low" });
    const high = makeFlag({ id: "f-high", code: "RECEIPT_MISSING", row_id: "r-high" });
    const run = makeRun({ status: "ready", cases: [claim], grouping: grouping(), flags: [low, high], catalogue: CATALOGUE,
      rows: [makeRow({ id: "r-low", values: { amount: "50.00" } }), makeRow({ id: "r-high", values: { amount: "250.00" } })] });
    getClaimsRun.mockResolvedValue(run);
    window.history.replaceState({}, "", "/claims/run1/review");
    render(<BrowserRouter><RunActivityProvider><RunWorkspace kind="claim" runId="run1" view="review" /></RunActivityProvider></BrowserRouter>);

    expect(await screen.findByRole("heading", { name: "Receipt missing" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Decision note"), { target: { value: "Receipt is in the report" } });
    fireEvent.click(screen.getByRole("button", { name: "Keep in payment" }));

    await waitFor(() => expect(window.location.search).toContain("finding=f-low"));
    expect(screen.getByRole("heading", { name: "Claim amount unconfirmed" })).toBeTruthy();
  });

  it("reads whether a finding needs a decision from the flag's status, not the catalogue's default class", async () => {
    // checks.py escalates per flag: an unclaimed receipt at or above the client's threshold is
    // "open" (a person must decide) although UNCLAIMED_RECEIPT is an "info" code in the catalogue.
    const escalated = makeFlag({ id: "f-big", code: "UNCLAIMED_RECEIPT", status: "open", evidence_id: "ev1" });
    const note = makeFlag({ id: "f-note", code: "PURPOSE_UNKNOWN", status: "info" });
    getClaimsRun.mockResolvedValue(makeRun({ status: "ready", cases: [makeCase()], grouping: grouping(), flags: [note, escalated], catalogue: CATALOGUE }));
    window.history.replaceState({}, "", "/claims/run1/review");
    render(<BrowserRouter><RunActivityProvider><RunWorkspace kind="claim" runId="run1" view="review" /></RunActivityProvider></BrowserRouter>);

    expect(await screen.findByRole("heading", { name: "Receipt supports no row" })).toBeTruthy();
    const listed = [...document.querySelectorAll(".finding-list button")].map((b) => `${b.querySelector("span")?.textContent} · ${b.querySelector("small")?.textContent}`);
    expect(listed).toEqual(["Receipt supports no row · Needs decision", "No stated purpose · Information"]);
    expect(screen.getByRole("button", { name: "Keep in payment" })).toBeTruthy();
  });

  it("offers the real audited action for identity findings instead of generic decisions", async () => {
    const conflict = makeFlag({ id: "ownership", code: "OWNERSHIP_CONFLICT", reason: "two names", cite: { file: "report.xlsx", sheet: "Expense Report", row: 1 } });
    getClaimsRun.mockResolvedValue(makeRun({ status: "ready", cases: [makeCase()], artifacts: [
      { id: "a1", path: "report.xlsx", sha256: "1", media_type: "workbook", size: 1, pages: null, sheets: ["Expense Report"], inspection_state: "inspected", failure_reason: "", proposed_role: "report", role_reason: "", disposition: "used", disposition_reason: "", disposition_by: "adapter", needs_confirmation: false, case_id: "c1" },
    ], grouping: grouping(), flags: [conflict], catalogue: {
      ...CATALOGUE, OWNERSHIP_CONFLICT: { code: "OWNERSHIP_CONFLICT", title: "Two people could own this", meaning: "", what_to_do: "Resolve ownership", kind: "structure", blocks: "open", toggle: false },
    } }));
    window.history.replaceState({}, "", "/claims/run1/review");
    render(<BrowserRouter><RunActivityProvider><RunWorkspace kind="claim" runId="run1" view="review" /></RunActivityProvider></BrowserRouter>);

    expect(await screen.findByRole("button", { name: "Re-check file identities" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Confirm ownership" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Download workbook" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Keep in payment" })).toBeNull();
    expect(screen.queryByAltText(/Cited page/)).toBeNull();
  });

  it("offers claimant entry for an unknown claimant instead of payment decisions", async () => {
    const claim = makeCase({ claimant: { name: "Audrey Ng", identifier: "", state: "unknown", basis: "conflict", citations: [] } });
    const unknown = makeFlag({ id: "unknown", code: "CLAIMANT_UNKNOWN", reason: "claimant unknown" });
    getClaimsRun.mockResolvedValue(makeRun({ status: "ready", cases: [claim], grouping: grouping(), flags: [unknown], catalogue: {
      ...CATALOGUE, CLAIMANT_UNKNOWN: { code: "CLAIMANT_UNKNOWN", title: "Nobody knows whose claim this is", meaning: "", what_to_do: "Set claimant", kind: "structure", blocks: "open", toggle: false },
    } }));
    window.history.replaceState({}, "", "/claims/run1/review");
    render(<BrowserRouter><RunActivityProvider><RunWorkspace kind="claim" runId="run1" view="review" /></RunActivityProvider></BrowserRouter>);

    expect(await screen.findByRole("button", { name: "Set claimant" })).toBeTruthy();
    expect((screen.getByLabelText("Claimant name") as HTMLInputElement).value).toBe("Audrey Ng");
    expect(screen.queryByRole("button", { name: "Keep in payment" })).toBeNull();
  });

  it("forgets a split selection when another Claim is selected", async () => {
    const second = makeCase({ id: "c2", employee_id: "e2", label: "Second Claim" });
    const file = (id: string, path: string, case_id: string): api.ClaimArtifact => ({ id, path, sha256: id, media_type: "pdf", size: 1, pages: 1, sheets: [], inspection_state: "inspected", failure_reason: "", proposed_role: "receipts", role_reason: "", disposition: "used", disposition_reason: "", disposition_by: "adapter", needs_confirmation: false, case_id });
    getClaimsRun.mockResolvedValue(makeRun({ status: "map_ready", cases: [makeCase(), second], grouping: grouping(true),
      artifacts: [file("a1", "first/report.xlsx", "c1"), file("a2", "second/receipt-2.pdf", "c2"), file("a3", "second/receipt-3.pdf", "c2")] }));
    renderOrganizer();

    fireEvent.click(await screen.findByText("Advanced actions"));
    fireEvent.change(screen.getByLabelText("New split Claim name"), { target: { value: "Part" } });
    fireEvent.click(screen.getByLabelText("report.xlsx"));
    fireEvent.click(screen.getByRole("button", { name: /Second Claim/ }));

    fireEvent.click(screen.getByText("Advanced actions"));
    expect((screen.getByLabelText("receipt-2.pdf") as HTMLInputElement).checked).toBe(false);
    expect((screen.getByLabelText("New split Claim name") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("button", { name: "Split into new Claim" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("sends a finished organization to the organize workbench through runDestination", async () => {
    getClaimsRun.mockResolvedValue(makeRun({ status: "map_ready", cases: [makeCase()], artifacts: [], grouping: grouping() }));
    window.history.replaceState({}, "", "/claims/run1/progress");
    render(<BrowserRouter><RunActivityProvider><RunWorkspace kind="claim" runId="run1" view="progress" /></RunActivityProvider></BrowserRouter>);

    const link = await screen.findByRole("link", { name: "Organize claims" });
    expect(link.getAttribute("href")).toBe("/claims/run1/organize");
  });
});
