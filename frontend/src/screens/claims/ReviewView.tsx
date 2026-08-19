import { useMemo, useState } from "react";
import {
  ClaimEvidence,
  ClaimFlag,
  ClaimRow,
  ClaimsRunDetail,
  setCaseCategory,
  setEmployeeCategory,
} from "../../api";
import { Reload, useAction } from "../../hooks/useAction";
import AllRows from "./AllRows";
import ClaimFlagCard from "./ClaimFlagCard";
import { KIND_LABEL, KIND_ORDER, describeFlag, kindOf, rm, stakeCents } from "./flags";
import { ReviewUnit, retryUnit, reviewUnits, unitIdOf, usesCases } from "./units";

// Review: the batch at a glance, then case by case — every line with its
// verdict and its receipt (the flags are annotations on that picture), and
// the flag cards with their title, what they mean, what to do and the
// amount at stake. A summary strip filters by kind or by case. Keyed by
// Claim Case (hardening H10); an older run keyed by employee renders the
// same way through `reviewUnits`. The units, the id maps and the per-case
// slices are computed ONCE here and handed down.

export { describeFlag, rm } from "./flags";

export default function ReviewView({ run, onChanged }: { run: ClaimsRunDetail; onChanged: Reload }) {
  const [kindFilter, setKindFilter] = useState<string | null>(null);
  const [empFilter, setEmpFilter] = useState<string | null>(null);
  const [hideNotes, setHideNotes] = useState(false);
  const [rowsOpen, setRowsOpen] = useState<Record<string, boolean>>({});

  const units = useMemo(() => reviewUnits(run), [run]);
  const unitById = useMemo(() => new Map(units.map((u) => [u.id, u])), [units]);
  const rowById = useMemo(() => new Map(run.rows.map((r) => [r.id, r])), [run.rows]);
  const evById = useMemo(() => new Map(run.evidence.map((e) => [e.id, e])), [run.evidence]);
  const unitOf = (x: { case_id?: string; employee_id: string }) => unitIdOf(run, x);
  const open = useMemo(() => run.flags.filter((f) => f.status === "open"), [run.flags]);
  const notes = run.flags.filter((f) => f.status === "info");
  const decided = run.flags.filter((f) => !["open", "info"].includes(f.status));
  const blockers = run.output_blockers ?? [];
  // Per case: its rows, flags and evidence, sliced once for the screen.
  const byUnit = useMemo(() => {
    const m = new Map<string, { rows: ClaimRow[]; flags: ClaimFlag[]; evidence: ClaimEvidence[] }>();
    const slot = (id: string) => {
      let s = m.get(id);
      if (!s) { s = { rows: [], flags: [], evidence: [] }; m.set(id, s); }
      return s;
    };
    for (const r of run.rows) slot(unitIdOf(run, r)).rows.push(r);
    for (const f of run.flags) slot(unitIdOf(run, f)).flags.push(f);
    for (const e of run.evidence) slot(unitIdOf(run, e)).evidence.push(e);
    return m;
  }, [run]);
  const sliceOf = (id: string) => byUnit.get(id) ?? { rows: [], flags: [], evidence: [] };
  // The client's category list, as seen on this run's cases (once, not per picker).
  const categoriesSeen = useMemo(
    () => Array.from(new Map(units.filter((e) => e.category).map((e) => [e.category, e.gl])).entries()),
    [units]
  );

  // The strip: per kind, how many open flags and how many RM at stake
  // (integer cents, summed as integers — advisory, never a figure to pay).
  const strip = useMemo(() => {
    const acc: Record<string, { n: number; cents: number }> = {};
    for (const f of open) {
      const k = kindOf(run.catalogue, f);
      acc[k] = acc[k] || { n: 0, cents: 0 };
      acc[k].n += 1;
      acc[k].cents += stakeCents(f, rowById, evById) ?? 0;
    }
    return acc;
  }, [open, run.catalogue, rowById, evById]);

  const passes = (f: ClaimFlag) =>
    (!kindFilter || kindOf(run.catalogue, f) === kindFilter) && (!empFilter || unitOf(f) === empFilter);
  const byStake = (a: ClaimFlag, b: ClaimFlag) =>
    (stakeCents(b, rowById, evById) ?? -1) - (stakeCents(a, rowById, evById) ?? -1);

  const runLevel = open.filter((f) => !unitOf(f) && passes(f));
  const groups = units
    .filter((e) => !empFilter || e.id === empFilter)
    .map((e) => ({
      emp: e,
      flags: open.filter((f) => unitOf(f) === e.id && passes(f)).sort(byStake),
      notes: hideNotes || (kindFilter && kindFilter !== "note") ? [] : notes.filter((f) => unitOf(f) === e.id && passes(f)),
    }))
    .filter((g) => g.flags.length || g.notes.length || (empFilter === g.emp.id));
  const filtering = !!kindFilter || !!empFilter;
  const shownCount = runLevel.length + groups.reduce((n, g) => n + g.flags.length, 0);

  return (
    <div>
      {open.length === 0 && blockers.length === 0 ? (
        <div className="card" style={{ borderColor: "var(--accent)" }}>
          <b>All flags resolved — Output unlocked</b>
          <span className="sub">Every decision is in the audit trail. Open the Output tab to copy the listing rows.</span>
        </div>
      ) : open.length === 0 ? (
        <div className="card banner bad">
          <b>No open flags, but the output is still locked</b>
          <ul className="muted">{blockers.map((b, i) => <li key={`${i}-${b}`}>{b}</li>)}</ul>
          <span className="sub">Set the claimant on the case named (the server decides, not this screen).</span>
        </div>
      ) : (
        <p className="summary-line">
          <b>{open.length} flag{open.length === 1 ? "" : "s"} need your decision</b>{" "}
          <span className="sub">
            Accept = it is a real problem, the line is left out of the batch. Dismiss = keep the line, with a note.
            Fix a value = the AI misread something; the case is re-checked instantly.
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

      <EmployeeTable run={run} units={units} categoriesSeen={categoriesSeen} onChanged={onChanged}
        openFor={(id) => sliceOf(id).flags.filter((f) => f.status === "open").length}
        derivedFor={(id) => sliceOf(id).rows.some((r) => r.kind === "derived")}
        selected={empFilter} onSelect={(id) => setEmpFilter(empFilter === id ? null : id)} />

      {filtering && shownCount === 0 && (
        <p className="sub" style={{ margin: "12px 0" }}>
          No open flags {kindFilter ? `of this kind (${KIND_LABEL[kindFilter]})` : ""}{empFilter && kindFilter ? " for " : ""}
          {empFilter ? `${unitById.get(empFilter)?.name || unitById.get(empFilter)?.label || "this case"}` : ""}. Clear the filters to see everything.
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
        const slice = sliceOf(emp.id);
        const isOpen = !!rowsOpen[emp.id];
        const derived = slice.rows.some((r) => r.kind === "derived");
        return (
          <div key={emp.id}>
            <p className="grouphead">
              <b>{emp.name || emp.label}</b>{" "}
              <ClaimantChip unit={emp} />
              {derived && <span className="chip review" title="the lines were built from receipts, not read from a claim summary">lines derived from evidence</span>}{" "}
              <span className="sub" style={{ display: "inline" }}>
                {emp.identifier} · {flags.length} to decide{empNotes.length ? ` · ${empNotes.length} note(s)` : ""} ·{" "}
                <button
                  className="btn"
                  style={{ padding: "1px 8px", fontSize: 12 }}
                  aria-expanded={isOpen}
                  onClick={() => setRowsOpen({ ...rowsOpen, [emp.id]: !isOpen })}
                >
                  {isOpen ? "Hide all rows" : `All rows (${slice.rows.length})`}
                </button>
              </span>
            </p>
            {isOpen && <AllRows run={run} emp={emp} rows={slice.rows} flags={slice.flags} evidence={slice.evidence} evById={evById} onChanged={onChanged} />}
            {flags.map((f, i) => (
              <ClaimFlagCard key={f.id} run={run} flag={f} unit={emp} row={rowById.get(f.row_id)} evidence={evById.get(f.evidence_id)} onChanged={onChanged} defaultOpen={i === 0} />
            ))}
            {empNotes.map((f) => (
              <ClaimFlagCard key={f.id} run={run} flag={f} unit={emp} row={rowById.get(f.row_id)} evidence={evById.get(f.evidence_id)} onChanged={onChanged} />
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
                <b>{describeFlag(run.catalogue, f.code).title}</b> <span className="sub">{unitById.get(unitOf(f))?.name ?? unitById.get(unitOf(f))?.label} — {f.reason.slice(0, 160)}</span>
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

// The claimant's state, as a chip: confirmed by a person, proposed by the
// investigation, or unknown (nobody to pay yet). Empty for an older run
// that has no case model.
function ClaimantChip({ unit }: { unit: ReviewUnit }) {
  if (!unit.claimant_state) return null;
  const cls = unit.claimant_state === "confirmed" ? "ok" : unit.claimant_state === "proposed" ? "review" : "flag";
  const title = `${unit.claimant_basis || unit.claimant_state}${unit.confidence ? ` · grouping confidence ${Math.round(unit.confidence * 100)}%` : ""}`;
  return <span className={`chip ${cls}`} title={title}>claimant {unit.claimant_state}</span>;
}

// The case summary: claimant, identifier, category + why, lines verified /
// flagged, totals (Reported and Calculated Lines, kept apart), status —
// with Re-verify and a category chooser.
function EmployeeTable({ run, units, categoriesSeen, onChanged, openFor, derivedFor, selected, onSelect }: {
  run: ClaimsRunDetail; units: ReviewUnit[]; categoriesSeen: [string, string][]; onChanged: Reload;
  openFor: (id: string) => number; derivedFor: (id: string) => boolean;
  selected: string | null; onSelect: (id: string) => void;
}) {
  const [editingCat, setEditingCat] = useState<string>("");
  const action = useAction(onChanged, "Could not re-verify");

  return (
    <div className="card" style={{ padding: 0, overflowX: "auto" }}>
      <table className="table">
        <thead>
          <tr>
            <th>{usesCases(run) ? "Case / claimant" : "Employee"}</th><th>Identifier</th><th>Category (why)</th><th>Lines</th><th>Totals (MYR)</th><th>Status</th><th></th>
          </tr>
        </thead>
        <tbody>
          {units.map((e) => (
            <tr key={e.id} className={selected === e.id ? "attention" : ""}>
              <td>
                <button
                  className="btn"
                  style={{ padding: "2px 8px", fontWeight: 600 }}
                  aria-pressed={selected === e.id}
                  title={selected === e.id ? "Show every case" : "Show only this case's flags"}
                  onClick={() => onSelect(e.id)}
                >
                  {e.name || e.label}
                </button>
                <ClaimantChip unit={e} />
                {e.name && e.label && e.name !== e.label && <span className="sub">{e.label}</span>}
                {derivedFor(e.id) && <span className="chip review" title="lines built from receipts">derived</span>}
              </td>
              <td className="mono">{e.identifier || "—"}</td>
              <td>
                {editingCat === e.id ? (
                  <CategoryPicker run={run} emp={e} categoriesSeen={categoriesSeen} onDone={() => setEditingCat("")} onCancel={() => setEditingCat("")} onChanged={onChanged} />
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
              <td>
                {e.reported_total
                  ? <>{e.reported_total}<span className="sub">Reported Total (the source's figure)</span></>
                  : <span className="sub">no Reported Total in the source</span>}
                {e.lines_total && <span className="sub">lines add up to {e.lines_total}{e.reported_total && e.lines_total !== e.reported_total ? " ≠ reported" : ""}</span>}
              </td>
              <td>
                <span className={`chip ${e.status === "verified" ? (openFor(e.id) ? "review" : "ok") : e.status === "failed" ? "flag" : "wait"}`}>{e.status}</span>
                {e.error && <span className="sub">{e.error}</span>}
              </td>
              <td>
                {e.status !== "verifying" && e.status !== "skipped" && (
                  <button className="btn" disabled={action.busy === e.id} onClick={() => action.run(() => retryUnit(run, e), { key: e.id })}>
                    {action.busy === e.id ? "Starting…" : e.status === "failed" ? "Retry" : "Re-verify"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {action.error && <p className="error" style={{ padding: 10 }}>{action.error}</p>}
    </div>
  );
}

function CategoryPicker({ run, emp, categoriesSeen, onDone, onCancel, onChanged }: {
  run: ClaimsRunDetail; emp: ReviewUnit; categoriesSeen: [string, string][]; onDone: () => void; onCancel: () => void; onChanged: Reload;
}) {
  // The client's list: the categories seen on this run's cases, else free text.
  const [category, setCategory] = useState(emp.category);
  const [gl, setGl] = useState(emp.gl);
  const [reason, setReason] = useState("");
  const action = useAction(onChanged, "Could not set the category");
  async function save() {
    const ok = await action.run(() =>
      emp.case_id
        ? setCaseCategory(run.id, emp.case_id, category, gl, reason, run.revision)
        : setEmployeeCategory(run.id, emp.employee_id, category, gl, reason, run.revision)
    );
    if (ok) onDone();
  }
  return (
    <div className="editor" style={{ borderTop: "none", paddingTop: 0, marginTop: 0 }}>
      <label className="editrow"><span>category</span>
        <input list={`cats-${emp.id}`} value={category} onChange={(e) => { setCategory(e.target.value); const g = categoriesSeen.find(([c]) => c === e.target.value); if (g) setGl(g[1]); }} />
        <datalist id={`cats-${emp.id}`}>{categoriesSeen.map(([c]) => <option key={c} value={c} />)}</datalist>
      </label>
      <label className="editrow"><span>GL</span><input value={gl} onChange={(e) => setGl(e.target.value)} /></label>
      <label className="editrow"><span>reason</span><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="why this category (audited)" /></label>
      <div className="actions">
        <button className="btn primary" disabled={!!action.busy || !category.trim()} onClick={save}>Save</button>
        <button className="btn" disabled={!!action.busy} onClick={onCancel}>Cancel</button>
      </div>
      {action.error && <p className="error">{action.error}</p>}
    </div>
  );
}
