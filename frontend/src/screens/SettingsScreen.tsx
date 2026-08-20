import { useEffect, useState } from "react";
import {
  AppSettings,
  ClaimsSettings,
  FlagCatalogue,
  SwitchBoard,
  getClaimsSettings,
  getFlagCatalogue,
  getSettings,
  getSwitches,
  saveClaimsSettings,
  saveSettings,
  saveSwitches,
} from "../api";
import Info from "../components/Info";
import Switch from "../components/Switch";

// The Settings section: everything a reviewer may change, in one place —
// workspace values, the claims profile & playbook, every feature switch —
// plus the read-only .env deployment facts. Secrets stay in .env; this
// screen only ever shows whether they are set.
export default function SettingsScreen() {
  return (
    <section>
      <div className="hero">
        <div>
          <h1>Settings</h1>
          <span className="sub">
            Reviewer-changeable values and every feature switch. IT-managed values are read-only.
          </span>
        </div>
      </div>
      <WorkspaceCard />
      <ClaimsProfileCard />
      <SwitchboardCard />
    </section>
  );
}

// ---- workspace (whose documents, who signs the draft) -----------------------

function WorkspaceCard() {
  const [current, setCurrent] = useState<AppSettings | null>(null);
  const [form, setForm] = useState<AppSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let alive = true;
    getSettings()
      .then((s) => {
        if (!alive) return;
        setCurrent(s);
        setForm(s);
      })
      .catch(() => alive && setError("Could not load the workspace settings"));
    return () => {
      alive = false;
    };
  }, []);

  if (!form || !current) {
    return (
      <div className="card">
        <h3 className="section-title">Workspace</h3>
        {error ? <p className="error">{error}</p> : <div className="skeleton" aria-hidden />}
      </div>
    );
  }
  const dirty = JSON.stringify(form) !== JSON.stringify(current);
  const set = (patch: Partial<AppSettings>) => {
    setForm({ ...form, ...patch });
    setSaved(false);
  };

  async function save() {
    if (!form) return;
    setBusy(true);
    setError("");
    try {
      const next = await saveSettings(form);
      setCurrent(next);
      setForm(next);
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save settings");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3 className="section-title">
        Workspace
        <Info text="Whose documents this install processes, where the invoice pipeline's reference files live, and the names that sign the payment-listing draft." />
      </h3>
      <label className="editrow">
        <span>Client name</span>
        <input value={form.client_name} onChange={(e) => set({ client_name: e.target.value })} />
      </label>
      <label className="editrow">
        <span>
          Reference folder link
          <Info text="The SharePoint folder holding the invoice pipeline's three reference files (payment listing, policy sheet, bank template)." />
        </span>
        <input
          value={form.sharepoint_folder_url}
          placeholder="https://…sharepoint.com/sites/…"
          onChange={(e) => set({ sharepoint_folder_url: e.target.value })}
        />
      </label>
      <label className="editrow">
        <span>Prepared by (draft)</span>
        <input value={form.draft_prepared_by ?? ""} onChange={(e) => set({ draft_prepared_by: e.target.value })} />
      </label>
      <label className="editrow">
        <span>Reviewed by (draft)</span>
        <input value={form.draft_reviewed_by ?? ""} onChange={(e) => set({ draft_reviewed_by: e.target.value })} />
      </label>
      <label className="editrow">
        <span>
          Bank charge per payment (RM)
          <Info text="The per-payment bank fee used in the drafted listing tab's fund figures." />
        </span>
        <input
          value={form.draft_bank_charge ?? "0.10"}
          inputMode="decimal"
          onChange={(e) => set({ draft_bank_charge: e.target.value })}
        />
      </label>
      <div className="actions">
        <button className="btn primary" disabled={busy || !dirty} onClick={save}>
          {busy ? "Saving…" : "Save workspace"}
        </button>
        {saved && !dirty && <span className="sub">Saved — applies to the next run.</span>}
        {error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}

// ---- claims profile & playbook ----------------------------------------------

type Profile = ClaimsSettings["profile"];

function ClaimsProfileCard() {
  const [settings, setSettings] = useState<ClaimsSettings | null>(null);
  const [catalogue, setCatalogue] = useState<FlagCatalogue>({});
  const [toggleable, setToggleable] = useState<string[]>([]);
  const [rates, setRates] = useState<[string, string][]>([]);
  const [kmTolerance, setKmTolerance] = useState("0");
  const [windowDays, setWindowDays] = useState("0");
  const [threshold, setThreshold] = useState("100");
  const [optionalItems, setOptionalItems] = useState("");
  const [playbook, setPlaybook] = useState("");
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  function load(s: ClaimsSettings) {
    setSettings(s);
    setRates(Object.entries(s.profile.mileage_rates ?? {}));
    setKmTolerance(s.profile.km_tolerance ?? "0");
    setWindowDays(String(s.profile.receipt_date_window_days ?? 0));
    setThreshold(s.profile.unclaimed_receipt_threshold ?? "100");
    setOptionalItems((s.profile.receipt_optional_items ?? []).join(", "));
    setPlaybook(s.playbook ?? "");
    setChecks(s.profile.checks ?? {});
  }

  useEffect(() => {
    let alive = true;
    getClaimsSettings()
      .then((s) => alive && load(s))
      .catch(() => alive && setError("Could not load the claims profile"));
    getFlagCatalogue()
      .then((c) => {
        if (!alive) return;
        setCatalogue(c.codes);
        setToggleable(c.toggleable);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  if (!settings) {
    return (
      <div className="card settings-claims">
        <h3 className="section-title">Claims profile</h3>
        {error ? <p className="error">{error}</p> : <div className="skeleton" aria-hidden />}
      </div>
    );
  }

  const setBy = settings.profile.set_by ?? {};
  const stamp = (field: string): string => {
    const s = setBy[field];
    return s?.at ? ` Set by ${s.by || "reviewer"} on ${new Date(s.at).toLocaleDateString()}.` : "";
  };

  async function save() {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const profile: Partial<Profile> = {
        mileage_rates: Object.fromEntries(
          rates.filter(([vehicle, rate]) => vehicle.trim() && rate.trim())
        ),
        km_tolerance: kmTolerance.trim(),
        receipt_date_window_days: Number(windowDays) || 0,
        unclaimed_receipt_threshold: threshold.trim(),
        receipt_optional_items: optionalItems
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        checks,
      };
      load(await saveClaimsSettings({ profile, playbook }));
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the claims profile");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card settings-claims">
      <h3 className="section-title">
        Claims profile
        <Info text="Per-client verification values. The agent never hard-codes these; a run snapshots them at start, so changing them never affects a run already going." />
      </h3>
      {rates.map(([vehicle, rate], i) => (
        <div className="editrow" key={i}>
          <span>{i === 0 ? "Mileage rates (RM/km)" : ""}</span>
          <input
            value={vehicle}
            placeholder="vehicle (e.g. car)"
            aria-label={`Mileage vehicle ${i + 1}`}
            onChange={(e) => setRates(rates.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
          />
          <input
            value={rate}
            placeholder="rate, e.g. 0.60"
            inputMode="decimal"
            aria-label={`Mileage rate ${i + 1}`}
            onChange={(e) => setRates(rates.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
          />
          <button className="btn ghost sm" onClick={() => setRates(rates.filter((_, j) => j !== i))}>
            Remove
          </button>
        </div>
      ))}
      <div className="editrow">
        <span>{rates.length === 0 ? "Mileage rates (RM/km)" : ""}</span>
        <button className="btn sm" onClick={() => setRates([...rates, ["", ""]])}>
          + Add a rate
        </button>
      </div>
      <label className="editrow">
        <span>
          Km tolerance
          <Info text={"How many km a claimed trip may exceed the measured route before it is flagged." + stamp("km_tolerance")} />
        </span>
        <input value={kmTolerance} inputMode="decimal" onChange={(e) => setKmTolerance(e.target.value)} />
      </label>
      <label className="editrow">
        <span>
          Receipt date window (days)
          <Info text={"How many days a receipt's date may differ from the claim line's date." + stamp("receipt_date_window_days")} />
        </span>
        <input value={windowDays} inputMode="numeric" onChange={(e) => setWindowDays(e.target.value)} />
      </label>
      <label className="editrow">
        <span>
          Unclaimed receipt threshold (RM)
          <Info text={"Receipts nobody claimed at or above this amount raise a flag." + stamp("unclaimed_receipt_threshold")} />
        </span>
        <input value={threshold} inputMode="decimal" onChange={(e) => setThreshold(e.target.value)} />
      </label>
      <label className="editrow">
        <span>
          Receipt-optional items
          <Info text={"Claim items that need no receipt, separated by commas (e.g. parking, toll)." + stamp("receipt_optional_items")} />
        </span>
        <input value={optionalItems} onChange={(e) => setOptionalItems(e.target.value)} />
      </label>
      <label className="editrow" style={{ alignItems: "flex-start" }}>
        <span>
          Playbook
          <Info text={"Prefilled into every new run's instructions." + stamp("playbook")} />
        </span>
        <textarea rows={3} value={playbook} onChange={(e) => setPlaybook(e.target.value)} />
      </label>
      {toggleable.length > 0 && (
        <>
          <span className="sub" style={{ margin: "10px 0 0" }}>
            Checks — a switched-off check raises no flags on new runs.
          </span>
          {toggleable.map((code) => (
            <div className="switchrow" key={code}>
              <div className="grow">
                <b>{catalogue[code]?.title ?? code}</b>
                <Info text={catalogue[code]?.meaning ?? code} />
              </div>
              <Switch
                on={checks[code] ?? true}
                label={catalogue[code]?.title ?? code}
                onChange={(next) => {
                  setChecks({ ...checks, [code]: next });
                  setSaved(false);
                }}
              />
            </div>
          ))}
        </>
      )}
      <div className="actions">
        <button className="btn primary" disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save claims profile"}
        </button>
        {saved && <span className="sub">Saved — applies to the next run.</span>}
        {error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}

// ---- feature switches + deployment facts ------------------------------------

function SwitchboardCard() {
  const [board, setBoard] = useState<SwitchBoard | null>(null);
  const [busyKey, setBusyKey] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    getSwitches()
      .then((b) => alive && setBoard(b))
      .catch(() => alive && setError("Could not load the feature switches"));
    return () => {
      alive = false;
    };
  }, []);

  if (!board) {
    return (
      <div className="card">
        <h3 className="section-title">Feature switches</h3>
        {error ? <p className="error">{error}</p> : <div className="skeleton" aria-hidden />}
      </div>
    );
  }

  async function flip(key: string, next: boolean) {
    setBusyKey(key);
    setError("");
    try {
      setBoard(await saveSwitches({ [key]: next }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the switch");
    } finally {
      setBusyKey("");
    }
  }

  return (
    <>
      <div className="card">
        <h3 className="section-title">
          Feature switches
          <Info text="Pipeline behavior (how a run investigates, groups and checks) applies to NEW runs only — a run keeps the switches it started with. What is visible and allowed on screen (case fields, regrouping actions) follows the current switch. Every change is recorded in the audit log." />
        </h3>
        {board.switches.map((s) => (
          <div className="switchrow" key={s.key}>
            <div className="grow">
              <b>{s.label}</b>
              <Info text={s.description} />
            </div>
            <span className="val">{s.saved ? "set by reviewer" : ".env default"}</span>
            <Switch
              on={s.value}
              label={s.label}
              disabled={busyKey === s.key}
              onChange={(next) => flip(s.key, next)}
            />
          </div>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
      <div className="card">
        <h3 className="section-title">
          Deployment
          <Info text="Set by IT in the server's .env file — read-only here. Values of keys and endpoints never reach the browser; this only says whether they are set." />
        </h3>
        {board.deployment.map((d) => (
          <div className="switchrow" key={d.label}>
            <div className="grow">
              <b>{d.label}</b>
            </div>
            <span className="val">{d.value}</span>
          </div>
        ))}
      </div>
    </>
  );
}
