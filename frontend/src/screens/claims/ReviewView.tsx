import { useState } from "react";
import {
  ClaimEmployee,
  ClaimEvidence,
  ClaimFlag,
  ClaimRow,
  ClaimsRunDetail,
  claimsFileUrl,
  correctClaimRow,
  decideClaimFlag,
  retryClaimEmployee,
  setEmployeeCategory,
} from "../../api";
import FieldEditor from "../../components/FieldEditor";
import FlagCard from "../../components/FlagCard";

// Review: clear flags fast, with the evidence in front of you. An
// employee summary table, then flag cards grouped by employee, each with
// its reason, its basis, and a preview of the cited page (the receipt's
// third highlighted) or the cited sheet row.

const ROW_FIELDS = [
  { name: "date" }, { name: "item" }, { name: "reason" }, { name: "receipt_included", label: "receipt included (Y/N)" },
  { name: "amount" }, { name: "currency" }, { name: "rate", label: "exchange rate" }, { name: "total", label: "total (MYR)" },
];
const KM_FIELDS = [{ name: "date" }, { name: "km" }, { name: "rate", label: "rate per km" }, { name: "amount" }];

export default function ReviewView({ run, onChanged }: { run: ClaimsRunDetail; onChanged: () => void }) {
  const open = run.flags.filter((f) => f.status === "open");
  const notes = run.flags.filter((f) => f.status === "info");
  const decided = run.flags.filter((f) => !["open", "info"].includes(f.status));
  const empById = new Map(run.employees.map((e) => [e.id, e]));
  const rowById = new Map(run.rows.map((r) => [r.id, r]));
  const evById = new Map(run.evidence.map((e) => [e.id, e]));
  const runLevel = open.filter((f) => !f.employee_id);
  const groups = run.employees
    .map((e) => ({ emp: e, flags: open.filter((f) => f.employee_id === e.id), notes: notes.filter((f) => f.employee_id === e.id) }))
    .filter((g) => g.flags.length || g.notes.length);

  return (
    <div>
      {open.length === 0 ? (
        <div className="card" style={{ borderColor: "var(--accent)" }}>
          <b>All flags resolved — Output unlocked</b>
          <span className="sub">Every decision is in the audit trail. Open the Output tab to copy the listing rows.</span>
        </div>
      ) : (
        <p className="summary-line">
          <b>{open.length} flag{open.length === 1 ? "" : "s"} need your decision</b>{" "}
          <span className="sub">
            Accept = it is a real problem, the row is left out of the batch. Dismiss = keep the row, with a note.
            Fix a value = the AI misread something; the employee is re-checked instantly.
          </span>
        </p>
      )}
      <EmployeeTable run={run} onChanged={onChanged} />
      {runLevel.length > 0 && (
        <>
          <p className="grouphead"><b>Whole batch</b></p>
          {runLevel.map((f) => (
            <ClaimFlagCard key={f.id} run={run} flag={f} onChanged={onChanged} />
          ))}
        </>
      )}
      {groups.map(({ emp, flags, notes: empNotes }) => (
        <div key={emp.id}>
          <p className="grouphead">
            <b>{emp.name || emp.folder}</b>{" "}
            <span className="sub">
              {emp.er_code} · {flags.length} to decide{empNotes.length ? ` · ${empNotes.length} note(s)` : ""}
            </span>
          </p>
          {flags.map((f) => (
            <ClaimFlagCard key={f.id} run={run} flag={f} row={rowById.get(f.row_id)} evidence={evById.get(f.evidence_id)} onChanged={onChanged} />
          ))}
          {empNotes.map((f) => (
            <ClaimFlagCard key={f.id} run={run} flag={f} row={rowById.get(f.row_id)} evidence={evById.get(f.evidence_id)} onChanged={onChanged} />
          ))}
        </div>
      ))}
      {decided.length > 0 && (
        <details>
          <summary className="sub">{decided.length} decided / resolved flag(s)</summary>
          {decided.map((f) => (
            <div key={f.id} className="card row">
              <div className="grow">
                <b>[{f.code}]</b> <span className="sub">{empById.get(f.employee_id)?.name} — {f.reason.slice(0, 160)}</span>
                {f.resolution && <span className="sub">→ {f.resolution}</span>}
              </div>
              <span className={`chip ${f.status === "accepted" ? "flag" : "ok"}`}>{f.status.replaceAll("_", " ")}</span>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

// The employee summary: name, ER code, category + why, rows verified /
// flagged, total, status — with Re-verify and a category chooser.
function EmployeeTable({ run, onChanged }: { run: ClaimsRunDetail; onChanged: () => void }) {
  const [busy, setBusy] = useState("");
  const [editingCat, setEditingCat] = useState<string>("");
  const [error, setError] = useState("");
  const openFor = (id: string) => run.flags.filter((f) => f.employee_id === id && f.status === "open").length;

  async function reverify(id: string) {
    setBusy(id);
    setError("");
    try {
      await retryClaimEmployee(run.id, id);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not re-verify");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="card" style={{ padding: 0, overflowX: "auto" }}>
      <table className="table">
        <thead>
          <tr>
            <th>Employee</th><th>ER code</th><th>Category (why)</th><th>Rows</th><th>Total (MYR)</th><th>Status</th><th></th>
          </tr>
        </thead>
        <tbody>
          {run.employees.map((e) => (
            <tr key={e.id}>
              <td><b>{e.name || e.folder}</b></td>
              <td className="mono">{e.er_code || "—"}</td>
              <td>
                {editingCat === e.id ? (
                  <CategoryPicker run={run} emp={e} onDone={() => { setEditingCat(""); onChanged(); }} onCancel={() => setEditingCat("")} />
                ) : (
                  <>
                    {e.category ? <b>{e.category}{e.gl ? ` (${e.gl})` : ""}</b> : <span className="sub">not set</span>}
                    {e.category_basis && <span className="sub" title={e.category_basis}>{e.category_basis.slice(0, 120)}{e.category_basis.length > 120 ? "…" : ""}</span>}
                    {e.status === "verified" && (
                      <button className="btn" style={{ marginTop: 4 }} onClick={() => setEditingCat(e.id)}>
                        {e.category ? "Change" : "Choose category"}
                      </button>
                    )}
                  </>
                )}
              </td>
              <td>
                {String(e.summary?.rows ?? "—")} verified
                {openFor(e.id) ? <span className="sub">{openFor(e.id)} flagged</span> : null}
              </td>
              <td>{e.report_total || "—"}</td>
              <td>
                <span className={`chip ${e.status === "verified" ? (openFor(e.id) ? "review" : "ok") : e.status === "failed" ? "flag" : "wait"}`}>{e.status}</span>
                {e.error && <span className="sub">{e.error}</span>}
              </td>
              <td>
                {e.status !== "verifying" && e.status !== "skipped" && (
                  <button className="btn" disabled={busy === e.id} onClick={() => reverify(e.id)}>
                    {busy === e.id ? "Starting…" : e.status === "failed" ? "Retry" : "Re-verify"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <p className="error" style={{ padding: 10 }}>{error}</p>}
    </div>
  );
}

function CategoryPicker({ run, emp, onDone, onCancel }: { run: ClaimsRunDetail; emp: ClaimEmployee; onDone: () => void; onCancel: () => void }) {
  // The client's list: from the profile snapshot if it has one, else the
  // categories seen on this run's employees, else free text.
  const seen = Array.from(new Map(run.employees.filter((e) => e.category).map((e) => [e.category, e.gl])).entries());
  const [category, setCategory] = useState(emp.category);
  const [gl, setGl] = useState(emp.gl);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true);
    setError("");
    try {
      await setEmployeeCategory(run.id, emp.id, category, gl, reason);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not set the category");
      setBusy(false);
    }
  }
  return (
    <div className="editor" style={{ borderTop: "none", paddingTop: 0, marginTop: 0 }}>
      <label className="editrow"><span>category</span>
        <input list={`cats-${emp.id}`} value={category} onChange={(e) => { setCategory(e.target.value); const g = seen.find(([c]) => c === e.target.value); if (g) setGl(g[1]); }} />
        <datalist id={`cats-${emp.id}`}>{seen.map(([c]) => <option key={c} value={c} />)}</datalist>
      </label>
      <label className="editrow"><span>GL</span><input value={gl} onChange={(e) => setGl(e.target.value)} /></label>
      <label className="editrow"><span>reason</span><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="why this category (audited)" /></label>
      <div className="actions">
        <button className="btn primary" disabled={busy || !category.trim()} onClick={save}>Save</button>
        <button className="btn" onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function ClaimFlagCard({ run, flag, row, evidence, onChanged }: {
  run: ClaimsRunDetail; flag: ClaimFlag; row?: ClaimRow; evidence?: ClaimEvidence; onChanged: () => void;
}) {
  const [note, setNote] = useState("");
  const [showEvidence, setShowEvidence] = useState(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const info = flag.status === "info";
  const cite = flag.cite || {};
  const hasPage = !!cite.file;
  const emp = run.employees.find((e) => e.id === flag.employee_id);

  async function decide(decision: "accepted" | "dismissed") {
    setBusy(true);
    setError("");
    try {
      await decideClaimFlag(run.id, flag.id, decision, note);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not record the decision");
      setBusy(false);
    }
  }

  const where = hasPage
    ? `${cite.file}, page ${cite.page}${cite.position ? `, ${cite.position}` : ""}`
    : cite.sheet
      ? `tab ${cite.sheet}${cite.row ? `, row ${cite.row}` : ""}`
      : flag.employee_id ? "" : "whole batch";
  const rowLine = row
    ? `${row.sheet || "receipts"} row ${row.row}: ${String(row.values.date ?? "")} · ${String(row.values.item_name ?? row.values.item ?? "")} · ${String(row.values.currency ?? "MYR")} ${String(row.values.amount ?? row.values.km ?? "")}${row.values.km ? " km" : ""}`
    : "";
  const fields = row?.kind === "mileage" ? KM_FIELDS : ROW_FIELDS;
  const rowLevel = !!flag.row_id && !!row;

  return (
    <FlagCard
      code={flag.code}
      subtitle={[where, rowLine].filter(Boolean).join(" · ")}
      reason={flag.reason}
      basis={flag.basis}
      info={info}
      error={error}
      evidence={
        <>
          {(hasPage || row) && (
            <div className="actions">
              {hasPage && (
                <button className="btn" onClick={() => setShowEvidence(!showEvidence)}>
                  {showEvidence ? "Hide page" : `Show ${cite.file?.split("/").pop()} p.${cite.page}`}
                </button>
              )}
              {evidence && evidence.confidence && Object.keys(evidence.confidence).length > 0 && (
                <span className="lowconf">low-confidence read: {Object.values(evidence.confidence).join("; ")}</span>
              )}
            </div>
          )}
          {showEvidence && hasPage && (
            <div className="evidence">
              <img
                src={claimsFileUrl(run.id, cite.file!, cite.page || 1, cite.position || "", flag.code.startsWith("MILEAGE"))}
                alt={`${cite.file} page ${cite.page}${cite.position ? `, ${cite.position} receipt highlighted` : ""}`}
              />
              <a className="sub" style={{ display: "block", padding: 6 }} href={claimsFileUrl(run.id, cite.file!, cite.page || 1, "", true)} target="_blank" rel="noreferrer">
                open at full resolution
              </a>
            </div>
          )}
        </>
      }
      actions={
        editing && row ? (
          <FieldEditor
            fields={fields}
            values={Object.fromEntries(fields.map((f) => [f.name, String(row.values[f.name] ?? "")]))}
            notes={Object.fromEntries(Object.entries(row.corrections || {}).map(([k]) => [k, "corrected"]))}
            hint="Compare with the page above, then correct the misread value. The change is audited and this employee is re-checked at once."
            onSave={async (changed, reason) => {
              await correctClaimRow(run.id, row.id, changed, reason);
              setEditing(false);
              onChanged();
            }}
            onCancel={() => setEditing(false)}
          />
        ) : info ? null : (
          <div className="actions">
            <input placeholder="Note — required to dismiss; optional to accept" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn warn" disabled={busy} onClick={() => decide("accepted")}>
              {rowLevel ? "Accept — leave this row out" : "Acknowledge"}
            </button>
            {rowLevel && (
              <button className="btn" disabled={busy} onClick={() => { setEditing(true); setShowEvidence(hasPage); }}>
                Fix a value
              </button>
            )}
            <button className="btn primary" disabled={busy || !note.trim()} title={note.trim() ? "" : "Type a note first"} onClick={() => decide("dismissed")}>
              Dismiss — keep the row
            </button>
            {emp && (
              <button className="btn" disabled={busy} onClick={async () => { setBusy(true); try { await retryClaimEmployee(run.id, emp.id); onChanged(); } catch (e) { setError(e instanceof Error ? e.message : "Could not re-verify"); } finally { setBusy(false); } }}>
                Re-verify employee
              </button>
            )}
          </div>
        )
      }
    />
  );
}
