import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ClaimsRunDetail, Grouping } from "../api";
import { makeCase, makeEmployee, makeRun } from "../test/fixtures";
import ClaimsRunDetailScreen from "./ClaimsRunDetail";

// Review finding #19: the map screen used to be keyed by
// `${status}-${revision}`, so every action — each one bumps the revision —
// threw the whole screen away and rebuilt it. What the reviewer was
// typing, which rows were open and any error message went with it. The
// screen is keyed by STATUS alone; these tests hold it there.

vi.mock("../api", async (importOriginal) => {
  const real = await importOriginal<typeof api>();
  return {
    ...real,
    getClaimsRun: vi.fn(),
    getClaimsRunEvents: vi.fn(async () => []),
    updateCase: vi.fn(async () => ({ ok: true })),
    retryCase: vi.fn(async () => {}),
  };
});

const getClaimsRun = vi.mocked(api.getClaimsRun);
const updateCase = vi.mocked(api.updateCase);

const GROUPING: Grouping = {
  problems: [],
  by_case: {},
  actions_enabled: true,
  counts: {
    artifacts: 0, dispositioned: 0, unresolved: 0, material_unresolved: 0,
    cases: 1, to_verify: 1, claimants_confirmed: 0, conflicts: 0,
  },
  ok: true,
};

function mapReadyRun(revision: number): ClaimsRunDetail {
  return makeRun({ status: "map_ready", revision, cases: [makeCase()], artifacts: [], grouping: GROUPING });
}

const nameBox = () => screen.getByLabelText("Aegene Ong claimant name") as HTMLInputElement;

beforeEach(() => {
  updateCase.mockClear();
  getClaimsRun.mockReset();
});

describe("ClaimsRunDetail — the map screen across an action", () => {
  it("keeps the reviewer's typing and the open row when the revision bumps", async () => {
    let revision = 7;
    getClaimsRun.mockImplementation(async () => mapReadyRun(revision));
    render(<ClaimsRunDetailScreen runId="run1" />);
    await waitFor(() => expect(nameBox()).toBeTruthy());

    fireEvent.click(screen.getByLabelText("Show files"));
    fireEvent.change(nameBox(), { target: { value: "Aegene Ong Li" } });

    // An unrelated action lands and the run is re-read at a new revision.
    revision = 8;
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Aegene Ong has no summary"));
    });
    await waitFor(() => expect(updateCase).toHaveBeenCalledTimes(1));

    // The screen was updated in place, not rebuilt.
    expect(nameBox().value).toBe("Aegene Ong Li");
    expect(screen.getByText(/Why this case:/)).toBeTruthy();
  });

});

describe("ClaimsRunDetail — the chosen tab", () => {
  it("drops Review when a re-verify takes the run out of `ready`", async () => {
    let run = makeRun({
      status: "ready",
      cases: [makeCase()],
      employees: [makeEmployee()],
      grouping: GROUPING,
    });
    getClaimsRun.mockImplementation(async () => run);
    render(<ClaimsRunDetailScreen runId="run1" />);
    // `ready` opens on Review; choose it explicitly so it is the reviewer's.
    await waitFor(() => expect(screen.getByText(/All flags resolved/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^Review/ }));
    expect(screen.getByText(/All flags resolved/)).toBeTruthy();

    // Re-verifying one case takes the whole run back to `verifying`.
    await act(async () => {
      run = { ...run, status: "verifying", revision: 8 };
      screen.getByRole("button", { name: "Re-verify" }).click();
    });
    await waitFor(() => expect(vi.mocked(api.retryCase)).toHaveBeenCalledTimes(1));

    // Review is gone — not left active behind a disabled button — and the
    // screen falls back to the default for the new status.
    await waitFor(() => expect(screen.queryByText(/All flags resolved/)).toBeNull());
    expect(screen.getByRole("button", { name: "Verifying" }).className).toContain("active");
    expect(screen.getByRole("button", { name: /^Review/ }).className).not.toContain("active");
  });
});

describe("ClaimsRunDetail — stopping a working run", () => {
  it("offers Stop only while the run is working, and asks before sending", async () => {
    const cancel = vi.spyOn(api, "cancelClaimsRun").mockResolvedValue({ ok: true, status: "failed", tools_cancelled: 0 });
    getClaimsRun.mockImplementation(async () => makeRun({ status: "mapping" }));
    render(<ClaimsRunDetailScreen runId="run1" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop this run" })).toBeTruthy());

    // The first press only asks.
    await act(async () => screen.getByRole("button", { name: "Stop this run" }).click());
    expect(cancel).not.toHaveBeenCalled();
    expect(screen.getByText(/marked failed/)).toBeTruthy();

    await act(async () => screen.getByRole("button", { name: "Yes — stop this run" }).click());
    expect(cancel).toHaveBeenCalledWith("run1", 7);
  });

  it("has no Stop button on a run that is at rest", async () => {
    getClaimsRun.mockImplementation(async () => makeRun({ status: "ready" }));
    render(<ClaimsRunDetailScreen runId="run1" />);
    await waitFor(() => expect(screen.getByText(/Acme/)).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Stop this run" })).toBeNull();
  });
});
