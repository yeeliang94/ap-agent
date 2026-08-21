import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import axe from "axe-core";
import App from "./App";
import { BrowserRouter } from "./router";

const json = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), {
  status: 200, headers: { "Content-Type": "application/json" },
}));

describe("routed shell accessibility", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/invoices");
    document.documentElement.lang = "en";
    document.title = "AP Agent";
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/runs")) return json([]);
      if (url.endsWith("/api/claims-runs")) return json([]);
      return json({});
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("has no automatically detectable WCAG A/AA violations on a run list", async () => {
    render(<BrowserRouter><App /></BrowserRouter>);
    await waitFor(() => expect(document.querySelector("h1")?.textContent).toBe("Invoice runs"));
    const results = await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
      rules: { "color-contrast": { enabled: false } }, // jsdom has no layout/canvas; browser checks cover contrast.
    });
    expect(results.violations.map((violation) => ({
      id: violation.id,
      targets: violation.nodes.map((node) => node.target),
    }))).toEqual([]);
  });
});
