import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { ClaimsRunDetail, Grouping } from "../../api";
import { makeCase, makeRun } from "../../test/fixtures";
import GroupView from "./GroupView";

// Map & Group survives a reload IN PLACE (review finding #19): the screen
// is keyed by the run's status, not its revision, so every action — which
// bumps the revision — must leave what the reviewer is typing, which rows
// are open and the error message alone. What the server has since changed
// still wins.

vi.mock("../../api", async (importOriginal) => {
  const real = await importOriginal<typeof api>();
  return {
    ...real,
    setClaimant: vi.fn(async () => ({ ok: true })),
    confirmClaimant: vi.fn(async () => ({ ok: true })),
  };
});

const setClaimant = vi.mocked(api.setClaimant);

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

function runWith(over: Partial<ClaimsRunDetail> = {}, caseName = "Aegene Ong") {
  return makeRun({
    status: "map_ready",
    cases: [makeCase({ claimant: { name: caseName, identifier: "ER001", state: "proposed", basis: "header", citations: [] } })],
    artifacts: [],
    grouping: GROUPING,
    ...over,
  });
}

const nameBox = () => screen.getByLabelText("Aegene Ong claimant name") as HTMLInputElement;

beforeEach(() => setClaimant.mockClear());

describe("GroupView across a reload", () => {
  it("keeps what the reviewer is typing when only the revision moves", () => {
    const run = runWith();
    const { rerender } = render(<GroupView run={run} onChanged={async () => {}} onConfirmed={() => {}} />);
    fireEvent.change(nameBox(), { target: { value: "Aegene Ong Li" } });
    expect(nameBox().value).toBe("Aegene Ong Li");

    // Another action landed: same values from the server, new revision.
    rerender(<GroupView run={runWith({ revision: 8 })} onChanged={async () => {}} onConfirmed={() => {}} />);
    expect(nameBox().value).toBe("Aegene Ong Li");
  });

  it("keeps an expanded row open across the same reload", () => {
    const run = runWith();
    const { rerender } = render(<GroupView run={run} onChanged={async () => {}} onConfirmed={() => {}} />);
    fireEvent.click(screen.getByLabelText("Show files"));
    expect(screen.getByText(/Why this case:/)).toBeTruthy();
    rerender(<GroupView run={runWith({ revision: 8 })} onChanged={async () => {}} onConfirmed={() => {}} />);
    expect(screen.getByText(/Why this case:/)).toBeTruthy();
  });

  it("drops the draft once the server's own value has moved on", () => {
    const run = runWith();
    const { rerender } = render(<GroupView run={run} onChanged={async () => {}} onConfirmed={() => {}} />);
    fireEvent.change(nameBox(), { target: { value: "typed over the old value" } });

    // Another screen set the claimant to something else: the server wins,
    // so the reviewer is never editing a value that no longer exists.
    rerender(
      <GroupView run={runWith({ revision: 9 }, "Nick Goh")} onChanged={async () => {}} onConfirmed={() => {}} />
    );
    expect((screen.getByLabelText("Aegene Ong claimant name") as HTMLInputElement).value).toBe("Nick Goh");
  });

  it("saves name and identifier together, once, when focus leaves the claimant cell", async () => {
    const run = runWith();
    render(<GroupView run={run} onChanged={async () => {}} onConfirmed={() => {}} />);
    fireEvent.change(nameBox(), { target: { value: "Aegene Ong Li" } });
    fireEvent.change(screen.getByLabelText("Aegene Ong identifier"), { target: { value: "ER009" } });
    // Moving between the two inputs must NOT save half of the change.
    await act(async () => {
      fireEvent.blur(nameBox(), { relatedTarget: screen.getByLabelText("Aegene Ong identifier") });
    });
    expect(setClaimant).not.toHaveBeenCalled();
    // Leaving the cell altogether saves both, with the run's revision.
    await act(async () => {
      fireEvent.blur(screen.getByLabelText("Aegene Ong identifier"), { relatedTarget: null });
    });
    expect(setClaimant).toHaveBeenCalledTimes(1);
    expect(setClaimant).toHaveBeenCalledWith("run1", 7, "c1", "Aegene Ong Li", "ER009");
  });

  it("shows a stale-run message after reloading, and keeps it on screen", async () => {
    const reload = vi.fn(async () => {});
    setClaimant.mockRejectedValueOnce(new api.StaleRunError());
    render(<GroupView run={runWith()} onChanged={reload} onConfirmed={() => {}} />);
    fireEvent.change(nameBox(), { target: { value: "Aegene Ong Li" } });
    await act(async () => {
      fireEvent.blur(nameBox(), { relatedTarget: null });
    });
    expect(reload).toHaveBeenCalledTimes(1);
    // The message is only true because the reload above already happened.
    expect(screen.getByText(/it has been reloaded; please try again/)).toBeTruthy();
  });
});
