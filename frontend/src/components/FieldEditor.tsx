import { useState } from "react";
import { explainFailure, Reload } from "../hooks/useAction";

// A generic inline editor: a list of fields with current values, a
// required reason, and a save callback. Saving records an audited
// correction on the server, which re-checks and refreshes the flags.
export default function FieldEditor({
  fields,
  values: initial,
  notes,
  onSave,
  onCancel,
  onStale,
  hint,
}: {
  fields: { name: string; label?: string }[];
  values: Record<string, string>;
  /** Field -> a short note shown beside its label ("uncertain read", "corrected"). */
  notes?: Record<string, string>;
  onSave: (changed: Record<string, string>, reason: string) => Promise<void>;
  onCancel: () => void;
  /** Called (and awaited) when the save hits a stale run (409), BEFORE the
   *  "it has been reloaded; please try again" message is shown — the
   *  screen's reload, so the retry carries the new revision. */
  onStale?: Reload;
  hint?: string;
}) {
  const [values, setValues] = useState<Record<string, string>>({ ...initial });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const changed = fields.filter((f) => values[f.name] !== initial[f.name]);

  async function save() {
    setBusy(true);
    setError("");
    try {
      // With no changes, the current values are sent as-is: the server
      // treats that as a pure re-check (the recovery after a failed one).
      const send = changed.length > 0 ? changed : fields;
      await onSave(Object.fromEntries(send.map((f) => [f.name, values[f.name]])), reason);
    } catch (e) {
      setError(await explainFailure(e, "Correction failed", onStale));
      setBusy(false);
    }
  }

  return (
    <div className="editor">
      {hint && <p className="sub">{hint}</p>}
      {fields.map((f) => (
        <label key={f.name} className="editrow">
          <span>
            {f.label ?? f.name.replaceAll("_", " ")}
            {notes?.[f.name] && <em className="lowconf"> — {notes[f.name]}</em>}
          </span>
          <input
            value={values[f.name] ?? ""}
            onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
          />
        </label>
      ))}
      <div className="actions">
        <input
          aria-label="Reason for the correction (required)"
          placeholder="Reason (required) — e.g. 'digits misread, read from the receipt'"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <button className="btn primary" disabled={busy || !reason.trim()} onClick={save}>
          {busy ? "Saving…" : changed.length > 0 ? `Save ${changed.length} correction(s)` : "Re-check"}
        </button>
        <button className="btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
