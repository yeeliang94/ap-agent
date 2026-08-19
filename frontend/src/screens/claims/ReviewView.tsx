import { useMemo, useState } from "react";
import {
  ClaimEmployee,
  ClaimEvidence,
  ClaimFlag,
  ClaimRow,
  ClaimsRunDetail,
  FlagInfo,
  claimsFileUrl,
  correctClaimRow,
  decideClaimFlag,
  retryClaimEmployee,
  setEmployeeCategory,
} from "../../api";
import FieldEditor from "../../components/FieldEditor";
import FlagCard from "../../components/FlagCard";

// Review: the batch at a glance, then employee by employee — every row
// with its verdict and its receipt (the flags are annotations on that
// picture), and the flag cards with their title, what they mean, what to
// do and the amount at stake. A summary strip filters by kind or by
// employee.

const ROW_FIELDS = [
  { name: "date" }, { name: "item" }, { name: "reason" }, { name: "receipt_included", label: "receipt included (Y/N)" },
  { name: "amount" }, { name: "currency" }, { name: "rate", label: "exchange rate" }, { name: "total", label: "total (MYR)" },
];
const KM_FIELDS = [{ name: "date" }, { name: "km" }, { name: "rate", label: "rate per km" }, { name: "amount" }];

// The order and the words of the summary strip's kinds.
const KIND_LABEL: Record<string, string> = {
  money: "money at risk",
  evidence: "uncertain reads",
  mileage: "mileage",
  structure: "files & rules",
  note: "notes",
};
const KIND_ORDER = ["money", "evidence", "mileage", "structure", "note"];

// The words for a code: from the run's catalogue, with a plain fallback so
// an unknown code still reads as words, never SNAKE_CASE.
export function describeFlag(run: ClaimsRunDetail, code: string): FlagInfo {
  return (
    run.catalogue?.[code] ?? {
      code,
      title: code.replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()),
      meaning: "",
      what_to_do: "",
      kind: "structure",
      blocks: "open",
      toggle: true,
    }
  );
}

// An open flag's kind for the strip: notes are the info ones; an OPEN
// flag whose catalogue kind is "note" (an unclaimed receipt above the
// threshold) is money at risk.
function kindOf(run: ClaimsRunDetail, f: ClaimFlag): string {
  if (f.status === "info") return "note";
  const k = describeFlag(run, f.code).kind;
  return k === "note" ? "money" : k;
}

// Cents at stake for a flag: the row's MYR total (or amount), else the
// cited receipt's amount. Integer cents so sums stay exact.
function centsOf(text: unknown): number | null {
  if (text === null || text === undefined || text === "") return null;
  const n = Number(String(text).replace(/,/g, ""));
  return Number.isFinite(n) ? Math.round(n * 100) : null;
}
function stakeCents(f: ClaimFlag, rowById: Map<string, ClaimRow>, evById: Map<string, ClaimEvidence>): number | null {
  const row = f.row_id ? rowById.get(f.row_id) : undefined;
  if (row) return centsOf(row.values.total ?? row.values.amount);
  const ev = f.evidence_id ? evById.get(f.evidence_id) : undefined;
  if (ev && ev.kind === "receipt") return centsOf(ev.values.amount);
  return null;
}
export function rm(cents: number | null): string {
  if (cents === null) return "";
  return `RM ${(cents / 100).toLocaleString("en-MY", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export default function ReviewView({ run, onChanged }: { run: ClaimsRunDetail; onChanged: () => void }) {
  const [kindFilter, setKindFilter] = useState<string | null>(null);
  const [empFilter, setEmpFilter] = useState<string | null>(null);
  const [hideNotes, setHideNotes] = useState(false);
  const [rowsOpen, setRowsOpen] = useState<Record<string, boolean>>({});

  const open = run.flags.filter((f) => f.status === "open");
  const notes = run.flags.filter((f) => f.status === "info");
  const decided = run.flags.filter((f) => !["open", "info"].includes(f.status));
  const empById = new Map(run.employees.map((e) => [e.id, e]));
  const rowById = new Map(run.rows.map((r) => [r.id, r]));
  const evById = new Map(run.evidence.map((e) => [e.id, e]));

  // The strip: per kind, how many open flags and how many RM at stake.
  const strip = useMemo(() => {
    const acc: Record<string, { n: number; cents: number }> = {};
    for (const f of open) {
      const k = kindOf(run, f);
      acc[k] = acc[k] || { n: 0, cents: 0 };
      acc[k].n += 1;
      acc[k].cents += stakeCents(f, rowById, evById) ?? 0;
    }
    return acc;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.flags, run.rows, run.evidence]);

  const passes = (f: ClaimFlag) =>
    (!kindFilter || kindOf(run, f) === kindFilter) && (!empFilter || f.employee_id === empFilter);
  const byStake = (a: ClaimFlag, b: ClaimFlag) =>
    (stakeCents(b, rowById, evById) ?? -1) - (stakeCents(a, rowById, evById) ?? -1);

  const runLevel = open.filter((f) => !f.employee_id && passes(f));
  const groups = run.employees
    .filter((e) => !empFilter || e.id === empFilter)
    .map((e) => ({
      emp: e,
      flags: open.filter((f) => f.employee_id === e.id && passes(f)).sort(byStake),
      notes: hideNotes || (kindFilter && kindFilter !== "note") ? [] : notes.filter((f) => f.employee_id === e.id && passes(f)),
    }))
    .filter((g) => g.flags.length || g.notes.length || (empFilter === g.emp.id));
  const filtering = !!kindFilter || !!empFilter;
  const shownCount = runLevel.length + groups.reduce((n, g) => n + g.flags.length, 0);

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

      {/* The strip: click a kind to see only those; click again to clear. */}
      <div className="chips" role="group" aria-label="Filter flags by kind" style={{ margin: "10px 0" }}>
        {KIND_ORDER.filter((k) => k !== "note" && strip[k]).map((k) => (
          <button
            key={k}
            className={`btn ${kindFilter === k ? "primary" : ""}`}
            aria-pressed={kindFilter === k}
            onClick={() => setKindFilter(kindFilter === k ? null : k)}
            title={`Show only: ${KIND_LABEL[k]}`}
          >
            {strip[k].n} {KIND_LABEL[k]}
            {strip[k].cents > 0 ? ` · ${rm(strip[k].cents)}` : ""}
          </button>
        ))}
        {notes.length > 0 && (
          <button
            className={`btn ${kindFilter === "note" ? "primary" : ""}`}
            aria-pressed={kindFilter === "note"}
            onClick={() => { setKindFilter(kindFilter === "note" ? null : "note"); setHideNotes(false); }}
            title="Show only the notes (they never block)"
          >
            {notes.length} note{notes.length === 1 ? "" : "s"}
          </button>
        )}
        {(filtering || hideNotes) && (
          <button className="btn" onClick={() => { setKindFilter(null); setEmpFilter(null); setHideNotes(false); }}>
            Clear filters
          </button>
        )}
        {notes.length > 0 && kindFilter !== "note" && (
          <label className="filter" style={{ float: "none", alignSelf: "center" }}>
            <input type="checkbox" checked={hideNotes} onChange={(e) => setHideNotes(e.target.checked)} />
            hide notes
          </label>
        )}
      </div>

      <EmployeeTable run={run} onChanged={onChanged} selected={empFilter} onSelect={(id) => setEmpFilter(empFilter === id ? null : id)} />

      {filtering && shownCount === 0 && (
        <p className="sub" style={{ margin: "12px 0" }}>
          No open flags {kindFilter ? `of this kind (${KIND_LABEL[kindFilter]})` : ""}{empFilter && kindFilter ? " for " : ""}
          {empFilter ? `${empById.get(empFilter)?.name || "this employee"}` : ""}. Clear the filters to see everything.
        </p>
      )}

      {runLevel.length > 0 && (
        <>
          <p className="grouphead"><b>Whole batch</b></p>
          {runLevel.map((f) => (
            <ClaimFlagCard key={f.id} run={run} flag={f} onChanged={onChanged} />
          ))}
        </>
      )}
      {groups.map(({ emp, flags, notes: empNotes }) => {
        const empRows = run.rows.filter((r) => r.employee_id === emp.id);
        const isOpen = !!rowsOpen[emp.id];
        return (
          <div key={emp.id}>
            <p className="grouphead">
              <b>{emp.name || emp.folder}</b>{" "}
              <span className="sub" style={{ display: "inline" }}>
                {emp.er_code} · {flags.length} to decide{empNotes.length ? ` · ${empNotes.length} note(s)` : ""} ·{" "}
                <button
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 12 }}
                  aria-expanded={isOpen}
                  onClick={() => setRowsOpen({ ...rowsOpen, [emp.id]: !isOpen })}
                >
                  {isOpen ? "Hide all rows" : `All rows (${empRows.length})`}
                </button>
              </span>
            </p>
            {isOpen && <AllRows run={run} emp={emp} onChanged={onChanged} />}
            {flags.map((f, i) => (
              <ClaimFlagCard key={f.id} run={run} flag={f} row={rowById.get(f.row_id)} evidence={evById.get(f.evidence_id)} onChanged={onChanged} defaultOpen={i === 0} />
            ))}
            {empNotes.map((f) => (
              <ClaimFlagCard key={f.id} run={run} flag={f} row={rowById.get(f.row_id)} evidence={evById.get(f.evidence_id)} onChanged={onChanged} />
            ))}
          </div>
        );
      })}
      {decided.length > 0 && !filtering && (
        <details>
          <summary className="sub">{decided.length} decided / resolved flag(s)</summary>
          {decided.map((f) => (
            <div key={f.id} className="card row">
              <div className="grow">
                <b>{describeFlag(run, f.code).title}</b> <span className="sub">{empById.get(f.employee_id)?.name} — {f.reason.slice(0, 160)}</span>
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
function EmployeeTable({ run, onChanged, selected, onSelect }: {
  run: ClaimsRunDetail; onChanged: () => void; selected: string | null; onSelect: (id: string) => void;
}) {
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
            <tr key={e.id} className={selected === e.id ? "attention" : ""}>
              <td>
                <button
                  className="btn"
                  style={{ padding: "2px 8px", fontWeight: 600 }}
                  aria-pressed={selected === e.id}
                  title={selected === e.id ? "Show every employee" : "Show only this employee's flags"}
                  onClick={() => onSelect(e.id)}
                >
                  {e.name || e.folder}
                </button>
              </td>
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

function ClaimFlagCard({ run, flag, row, evidence, onChanged, defaultOpen = false }: {
  run: ClaimsRunDetail; flag: ClaimFlag; row?: ClaimRow; evidence?: ClaimEvidence; onChanged: () => void;
  /** Open the cited page straight away (the first flag of each employee). */
  defaultOpen?: boolean;
}) {
  const [note, setNote] = useState("");
  const [showEvidence, setShowEvidence] = useState(defaultOpen && !!(flag.cite || {}).file);
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
  const words = describeFlag(run, flag.code);
  const stake = row
    ? centsOf(row.values.total ?? row.values.amount)
    : evidence && evidence.kind === "receipt"
      ? centsOf(evidence.values.amount)
      : null;

  return (
    <FlagCard
      code={flag.code}
      title={words.title}
      meaning={words.meaning}
      whatToDo={words.what_to_do}
      stake={rm(stake) || undefined}
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
            <button className="btn warn" disabled={busy} onClick={() => decide("accepted")}
              title={rowLevel ? "The row is left out of the batch; the employee stays with the rest" : "Recorded as seen; the run proceeds"}>
              {rowLevel ? `Accept — leave ${rm(stake) || "this row"} out` : "Acknowledge"}
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

// ---- Every row, not just the flagged ones -----------------------------------------
// The employee's whole picture: each report row with its verdict, its
// receipt (vendor · page · position, "show" opens the highlighted page),
// the flags on it, and Fix a value; the KM rows beside their map trip;
// and the evidence no row used. Data the run detail already carries.

const VERDICT_CHIP: Record<string, { cls: string; label: string }> = {
  matched: { cls: "ok", label: "matched ✓" },
  no_evidence: { cls: "flag", label: "no receipt" },
  ambiguous: { cls: "review", label: "ambiguous" },
  duplicate: { cls: "flag", label: "duplicate" },
  optional: { cls: "wait", label: "receipt-optional" },
  unchecked: { cls: "wait", label: "unchecked" },
};

function AllRows({ run, emp, onChanged }: { run: ClaimsRunDetail; emp: ClaimEmployee; onChanged: () => void }) {
  const [showPage, setShowPage] = useState<string>("");   // row id whose page is open
  const [editing, setEditing] = useState<string>("");     // row id being fixed
  const evById = new Map(run.evidence.map((e) => [e.id, e]));
  const rows = run.rows.filter((r) => r.employee_id === emp.id);
  const expense = rows.filter((r) => r.kind !== "mileage").sort((a, b) => a.row - b.row);
  const km = rows.filter((r) => r.kind === "mileage").sort((a, b) => a.row - b.row);
  const flagsByRow = new Map<string, ClaimFlag[]>();
  for (const f of run.flags.filter((f) => f.employee_id === emp.id && f.row_id)) {
    flagsByRow.set(f.row_id, [...(flagsByRow.get(f.row_id) || []), f]);
  }
  const unused = run.evidence.filter((e) => e.employee_id === emp.id && !e.matched_row_id);

  function verdictChip(r: ClaimRow) {
    const fl = flagsByRow.get(r.id) || [];
    if (fl.some((f) => f.status === "accepted")) return <span className="chip flag">excluded</span>;
    const v = VERDICT_CHIP[r.verdict] || { cls: "wait", label: r.verdict || "—" };
    const uncertain = fl.some((f) => f.status === "open" && f.code === "EVIDENCE_UNCERTAIN");
    return (
      <>
        <span className={`chip ${v.cls}`}>{v.label}</span>
        {uncertain && <span className="chip review" style={{ marginLeft: 4 }}>uncertain</span>}
      </>
    );
  }

  function flagWords(r: ClaimRow) {
    const fl = flagsByRow.get(r.id) || [];
    if (!fl.length) return null;
    return (
      <span className="sub">
        {fl.map((f) => (
          <span key={f.id} style={{ display: "block" }}>
            {f.status === "open" ? <b>{describeFlag(run, f.code).title}</b> : describeFlag(run, f.code).title}
            {f.status !== "open" && f.status !== "info" ? ` (${f.status.replaceAll("_", " ")})` : ""}
          </span>
        ))}
      </span>
    );
  }

  function evidenceCell(r: ClaimRow, isMileage: boolean) {
    const ev = r.matched_evidence_id ? evById.get(r.matched_evidence_id) : undefined;
    if (!ev) return <span className="sub">—</span>;
    const v = ev.values;
    const open = showPage === r.id;
    return (
      <>
        <span>{isMileage ? `map: ${String(v.km_printed ?? "?")} km` : String(v.vendor ?? "?")}</span>
        <span className="sub">
          {ev.file.split("/").pop()} p.{ev.page}{ev.position ? `, ${ev.position}` : ""}{" "}
          <button className="btn" style={{ padding: "0 6px", fontSize: 12 }} aria-expanded={open}
            onClick={() => setShowPage(open ? "" : r.id)}>
            {open ? "hide" : "show"}
          </button>
        </span>
        {ev.confidence && Object.keys(ev.confidence).length > 0 && (
          <span className="lowconf">low-confidence read: {Object.values(ev.confidence).join("; ")}</span>
        )}
      </>
    );
  }

  function pageRow(r: ClaimRow, cols: number, isMileage: boolean) {
    const ev = r.matched_evidence_id ? evById.get(r.matched_evidence_id) : undefined;
    if (showPage !== r.id || !ev) return null;
    return (
      <tr key={`${r.id}-page`} className="detail">
        <td colSpan={cols}>
          <div className="evidence">
            <img
              src={claimsFileUrl(run.id, ev.file, ev.page || 1, ev.position || "", isMileage)}
              alt={`${ev.file} page ${ev.page}${ev.position ? `, ${ev.position} receipt highlighted` : ""}`}
            />
            <a className="sub" style={{ display: "block", padding: 6 }} href={claimsFileUrl(run.id, ev.file, ev.page || 1, "", true)} target="_blank" rel="noreferrer">
              open at full resolution
            </a>
          </div>
        </td>
      </tr>
    );
  }

  function editorRow(r: ClaimRow, cols: number, isMileage: boolean) {
    if (editing !== r.id) return null;
    const fields = isMileage ? KM_FIELDS : ROW_FIELDS;
    return (
      <tr key={`${r.id}-edit`} className="detail">
        <td colSpan={cols}>
          <FieldEditor
            fields={fields}
            values={Object.fromEntries(fields.map((f) => [f.name, String(r.values[f.name] ?? "")]))}
            notes={Object.fromEntries(Object.entries(r.corrections || {}).map(([k]) => [k, "corrected"]))}
            hint="Correct the misread value. The change is audited and this employee is re-checked at once."
            onSave={async (changed, reason) => {
              await correctClaimRow(run.id, r.id, changed, reason);
              setEditing("");
              onChanged();
            }}
            onCancel={() => setEditing("")}
          />
        </td>
      </tr>
    );
  }

  const corrected = (r: ClaimRow) => {
    const keys = Object.keys(r.corrections || {});
    return keys.length ? <span className="sub">corrected: {keys.join(", ")}</span> : null;
  };
  const excluded = (r: ClaimRow) => (flagsByRow.get(r.id) || []).some((f) => f.status === "accepted");

  return (
    <div className="card" style={{ padding: 0, overflowX: "auto" }}>
      {expense.length > 0 && (
        <table className="table inner" aria-label={`${emp.name || emp.folder}: every report row`}>
          <thead>
            <tr><th>Row</th><th>Date</th><th>Item</th><th>Amount</th><th>Verdict</th><th>Receipt</th><th>Flags</th><th></th></tr>
          </thead>
          <tbody>
            {expense.flatMap((r) => [
              <tr key={r.id} className={excluded(r) ? "bad" : ""} style={excluded(r) ? { opacity: 0.6 } : undefined}>
                <td className="mono">{r.sheet ? r.row : `receipt ${r.row}`}</td>
                <td>{String(r.values.date ?? "")}</td>
                <td>{String(r.values.item_name ?? r.values.item ?? r.values.reason ?? "")}{corrected(r)}</td>
                <td>{String(r.values.currency ?? "MYR")} {String(r.values.amount ?? "")}{r.values.total && r.values.currency && r.values.currency !== "MYR" ? <span className="sub">= MYR {String(r.values.total)}</span> : null}</td>
                <td>{verdictChip(r)}</td>
                <td>{evidenceCell(r, false)}</td>
                <td>{flagWords(r)}</td>
                <td>{r.kind !== "derived" && <button className="btn" style={{ padding: "2px 8px", fontSize: 12 }} onClick={() => setEditing(editing === r.id ? "" : r.id)}>Fix a value</button>}</td>
              </tr>,
              pageRow(r, 8, false),
              editorRow(r, 8, false),
            ])}
          </tbody>
        </table>
      )}
      {km.length > 0 && (
        <table className="table inner" aria-label={`${emp.name || emp.folder}: mileage rows`}>
          <thead>
            <tr><th>KM row</th><th>Date</th><th>Trip</th><th>km × rate</th><th>Amount</th><th>Verdict</th><th>Map</th><th>Flags</th><th></th></tr>
          </thead>
          <tbody>
            {km.flatMap((r) => [
              <tr key={r.id} className={excluded(r) ? "bad" : ""} style={excluded(r) ? { opacity: 0.6 } : undefined}>
                <td className="mono">{r.row}</td>
                <td>{String(r.values.date ?? "")}</td>
                <td>{String(r.values.from ?? "")} → {String(r.values.to ?? "")}{corrected(r)}</td>
                <td>{String(r.values.km ?? "?")} km{r.values.rate ? ` × ${String(r.values.rate)}` : ""}</td>
                <td>MYR {String(r.values.amount ?? "")}</td>
                <td>{verdictChip(r)}</td>
                <td>{evidenceCell(r, true)}</td>
                <td>{flagWords(r)}</td>
                <td><button className="btn" style={{ padding: "2px 8px", fontSize: 12 }} onClick={() => setEditing(editing === r.id ? "" : r.id)}>Fix a value</button></td>
              </tr>,
              pageRow(r, 9, true),
              editorRow(r, 9, true),
            ])}
          </tbody>
        </table>
      )}
      {rows.length === 0 && <p className="sub" style={{ padding: 10 }}>No rows were read for this employee.</p>}
      {unused.length > 0 && (
        <p className="sub" style={{ padding: "8px 10px" }}>
          <b style={{ display: "inline" }}>Evidence no row uses:</b>{" "}
          {unused.map((e) => `${e.kind === "receipt" ? String(e.values.vendor ?? "?") : "map trip"} (${String(e.values.date ?? "no date")}, ${e.kind === "receipt" ? `${String(e.values.currency ?? "MYR")} ${String(e.values.amount ?? "")}` : `${String(e.values.km_printed ?? "?")} km`}) — ${e.file.split("/").pop()} p.${e.page}`).join("; ")}
        </p>
      )}
    </div>
  );
}
