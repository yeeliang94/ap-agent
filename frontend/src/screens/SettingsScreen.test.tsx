import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ClaimsSettings, SwitchBoard } from "../api";
import SettingsScreen from "./SettingsScreen";
import { BrowserRouter } from "../router";

// The Settings screen: every switch is on screen, a flip is one audited
// PUT, and the row says whether the reviewer or the .env default answers.

vi.mock("../api", async (importOriginal) => {
  const real = await importOriginal<typeof api>();
  return {
    ...real,
    getSettings: vi.fn(),
    saveSettings: vi.fn(),
    getClaimsSettings: vi.fn(),
    saveClaimsSettings: vi.fn(),
    getFlagCatalogue: vi.fn(),
    getSwitches: vi.fn(),
    saveSwitches: vi.fn(),
  };
});

const getSwitches = vi.mocked(api.getSwitches);
const saveSwitches = vi.mocked(api.saveSwitches);
const renderSettings = () => render(<BrowserRouter><SettingsScreen /></BrowserRouter>);

const CLAIMS_SETTINGS: ClaimsSettings = {
  client: "Client ABC",
  local_mode: true,
  sharepoint_source: false,
  profile: {
    mileage_rates: { Car: "0.64" },
    km_tolerance: "0",
    receipt_date_window_days: 0,
    unclaimed_receipt_threshold: "100",
    receipt_optional_items: [],
    mileage_item_pattern: "",
    categories: [],
    category_rule: "",
    file_role_patterns: [],
    checks: {},
    set_by: {},
  },
  playbook: "",
  last_map: {},
};

function board(caseModelOn: boolean, saved: boolean): SwitchBoard {
  return {
    switches: [
      {
        key: "claims_case_model",
        label: "Case model",
        description: "Cases on the run detail.",
        value: caseModelOn,
        default: true,
        saved,
      },
    ],
    deployment: [{ label: "AI key", value: "set" }],
  };
}

beforeEach(() => {
  vi.mocked(api.getSettings).mockResolvedValue({
    client_name: "Client ABC",
    sharepoint_folder_url: "https://x.sharepoint.com/ref",
    draft_prepared_by: "",
    draft_reviewed_by: "",
    draft_bank_charge: "0.10",
  });
  vi.mocked(api.getClaimsSettings).mockResolvedValue(CLAIMS_SETTINGS);
  vi.mocked(api.getFlagCatalogue).mockResolvedValue({ codes: {}, kinds: [], toggleable: [] });
  getSwitches.mockResolvedValue(board(true, false));
  saveSwitches.mockReset();
});

describe("SettingsScreen — the switchboard", () => {
  it("shows each switch with its provenance, and a flip is one PUT", async () => {
    saveSwitches.mockResolvedValue(board(false, true));
    renderSettings();

    await waitFor(() => expect(screen.getByRole("switch", { name: "Case model" })).toBeTruthy());
    // Nobody saved a choice yet: the .env default answers, and says so.
    expect(screen.getByText(".env default")).toBeTruthy();

    fireEvent.click(screen.getByRole("switch", { name: "Case model" }));
    await waitFor(() => expect(saveSwitches).toHaveBeenCalledWith({ claims_case_model: false }));
    // The server's answer (now reviewer-set) replaces the board.
    await waitFor(() => expect(screen.getByText("set by reviewer")).toBeTruthy());
    expect(screen.getByRole("switch", { name: "Case model" }).getAttribute("aria-checked")).toBe("false");
  });

  it("shows the read-only deployment facts without values of secrets", async () => {
    renderSettings();
    await waitFor(() => expect(screen.getByText("AI key")).toBeTruthy());
    expect(screen.getByText("set")).toBeTruthy();
  });
});
