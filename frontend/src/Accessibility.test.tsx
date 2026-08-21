import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import axe from "axe-core";
import App from "./App";
import { BrowserRouter } from "./router";
import { makeCase, makeRun } from "./test/fixtures";

const json = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), {
  status: 200, headers: { "Content-Type": "application/json" },
}));

describe("routed shell accessibility", () => {
  beforeEach(() => {
    document.documentElement.lang = "en";
    document.title = "AP Agent";
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/runs/r1")) return json({ id: "r1", client: "Acme", status: "checking", error: "", progress: { phase: "checking", step: "running_checks", done: 1, total: 2, unit: "documents", updated_at: new Date().toISOString() }, documents_total: 2, open_flags: 0, errors: 0, warnings: 0, created_at: new Date().toISOString(), documents: [], flags: [], outputs: {} });
      if (url.endsWith("/api/claims-runs/run1")) return json(makeRun({ status: "map_ready", cases: [makeCase()], artifacts: [], grouping: { problems: [], by_case: {}, actions_enabled: false, counts: { artifacts: 0, dispositioned: 0, unresolved: 0, material_unresolved: 0, cases: 1, to_verify: 1, claimants_confirmed: 0, conflicts: 0 }, ok: true } }));
      if (url.endsWith("/api/runs")) return json([]);
      if (url.endsWith("/api/claims-runs")) return json([]);
      if (url.endsWith("/api/settings")) return json({ client_name: "Acme", sharepoint_folder_url: "", draft_prepared_by: "", draft_reviewed_by: "", draft_bank_charge: "0.10" });
      if (url.endsWith("/api/sharepoint/status")) return json({ required: false, connected: false });
      return json({});
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  async function checkRoute(path: string, heading: string) {
    window.history.replaceState({}, "", path);
    render(<BrowserRouter><App /></BrowserRouter>);
    await waitFor(() => expect(document.querySelector("h1")?.textContent).toBe(heading));
    const results = await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
      rules: { "color-contrast": { enabled: false } }, // jsdom has no layout/canvas; browser checks cover contrast.
    });
    expect(results.violations.map((violation) => ({
      id: violation.id,
      targets: violation.nodes.map((node) => node.target),
    }))).toEqual([]);
  }

  it("has no automatically detectable WCAG A/AA violations on a run list", async () => {
    await checkRoute("/invoices", "Invoice runs");
  });

  it("checks the active progress screen, not only an empty list", async () => {
    await checkRoute("/invoices/r1/progress", "Acme");
  });

  it("checks the Claims organization workbench", async () => {
    await checkRoute("/claims/run1/organize", "Acme");
  });

  it("checks routed Settings section navigation", async () => {
    await checkRoute("/settings/workspace", "Settings");
  });
});
