import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { CATALOGUE, makeCase, makeFlag, makeRow, makeRun } from "../../test/fixtures";
import ClaimFlagCard, { acceptAction } from "./ClaimFlagCard";
import { reviewUnits } from "./units";

// The words on a decision button must say what the server will DO. The
// review found the worst case of the opposite: for CLAIM_AMOUNT_UNCONFIRMED
// on a case, "Acknowledge" / "leaves the case as it is" while the server
// sets the case to `excluded` and it drops out of the Payment Listing.

vi.mock("../../api", async (importOriginal) => {
  const real = await importOriginal<typeof api>();
  return { ...real, decideClaimFlag: vi.fn(async () => ({ ok: true })) };
});

const decide = vi.mocked(api.decideClaimFlag);

function renderCard(over: Partial<Parameters<typeof makeFlag>[0]> = {}, rowLevel = false) {
  const row = makeRow();
  const run = makeRun({
    cases: [makeCase()],
    rows: rowLevel ? [row] : [],
    flags: [makeFlag(over)],
    catalogue: CATALOGUE,
  });
  const unit = reviewUnits(run)[0];
  const flag = run.flags[0];
  render(
    <ClaimFlagCard run={run} flag={flag} unit={unit} row={rowLevel ? row : undefined} onChanged={async () => {}} />
  );
  return { run, flag };
}

const button = (name: string | RegExp) => screen.getByRole("button", { name });

beforeEach(() => decide.mockClear());

describe("acceptAction — the words of the accept button", () => {
  it("names the real effect for CLAIM_AMOUNT_UNCONFIRMED on a case", () => {
    const a = acceptAction({ rowLevel: false, amountFlag: true, caseId: "c1", stake: "RM 100.00", caseName: "Aegene Ong" });
    expect(a.label).toBe("Accept — leave this case out of the listing");
    expect(a.label).not.toMatch(/acknowledge/i);
    expect(a.help).toContain("leaves this whole case out of the listing");
    // Not a word that suggests nothing happens.
    expect(a.help).not.toContain("changes nothing");
    // Dropping a whole case out of the batch is asked twice.
    expect(a.confirm).toBe(true);
    expect(a.title).toContain("Aegene Ong");
  });

  it("names the row for a row-level flag, and does not ask twice", () => {
    const a = acceptAction({ rowLevel: true, amountFlag: false, caseId: "c1", stake: "RM 45.00", caseName: "Aegene Ong" });
    expect(a.label).toBe("Accept — leave RM 45.00 out");
    expect(a.confirm).toBe(false);
  });

  it("is a plain acknowledgement only where nothing else happens", () => {
    const a = acceptAction({ rowLevel: false, amountFlag: false, caseId: undefined, stake: "", caseName: "" });
    expect(a.label).toBe("Acknowledge");
    expect(a.help).toContain("records this as seen");
    expect(a.confirm).toBe(false);
  });

  it("stays an acknowledgement for an amount flag that belongs to no case", () => {
    // With no case there is nothing for the server to exclude.
    const a = acceptAction({ rowLevel: false, amountFlag: true, caseId: undefined, stake: "", caseName: "" });
    expect(a.label).toBe("Acknowledge");
    expect(a.confirm).toBe(false);
  });
});

describe("ClaimFlagCard — CLAIM_AMOUNT_UNCONFIRMED", () => {
  it("offers the honest label and asks again before excluding the case", async () => {
    renderCard();
    const accept = button("Accept — leave this case out of the listing");
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();

    // First press only asks; nothing is sent.
    await act(async () => accept.click());
    expect(decide).not.toHaveBeenCalled();
    expect(screen.getByText(/Leave Aegene Ong out of the Payment Listing\?/)).toBeTruthy();
    expect(screen.getByText(/set to excluded/)).toBeTruthy();

    // Cancel leaves the case alone.
    await act(async () => button("Cancel").click());
    expect(decide).not.toHaveBeenCalled();

    // Confirming sends the decision, with the run's revision.
    await act(async () => button("Accept — leave this case out of the listing").click());
    await act(async () => button("Yes — leave this case out of the listing").click());
    expect(decide).toHaveBeenCalledTimes(1);
    expect(decide).toHaveBeenCalledWith("run1", "f1", "accepted", "", 7, undefined);
  });

  it("says Confirm these amounts on the dismiss side, and needs a note", async () => {
    renderCard();
    const confirmAmounts = button("Confirm these amounts");
    expect((confirmAmounts as HTMLButtonElement).disabled).toBe(true);
    await act(async () => confirmAmounts.click());
    expect(decide).not.toHaveBeenCalled();
  });
});

describe("ClaimFlagCard — a row-level flag", () => {
  it("sends the accept straight through, with no second question", async () => {
    renderCard({ code: "RECEIPT_MISSING", row_id: "r1" }, true);
    await act(async () => button(/^Accept — leave/).click());
    expect(decide).toHaveBeenCalledTimes(1);
    expect(decide).toHaveBeenCalledWith("run1", "f1", "accepted", "", 7, undefined);
  });
});
