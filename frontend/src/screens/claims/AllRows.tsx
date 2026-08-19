import { useState } from "react";
import { ClaimEvidence, ClaimFlag, ClaimRow, ClaimsRunDetail, claimsFileUrl, correctClaimRow } from "../../api";
import FieldEditor from "../../components/FieldEditor";
import { Reload } from "../../hooks/useAction";
import { KM_FIELDS, ROW_FIELDS, describeFlag } from "./flags";
import { ReviewUnit } from "./units";

// Every row, not just the flagged ones — the case's whole picture: each
// report row with its verdict, its receipt (vendor · page · position,
// "show" opens the highlighted page), the flags on it, and Fix a value;
// the KM rows beside their map trip; and the evidence no row used. The
// data comes in already sliced to this case by ReviewView (computed once
// for the screen, not once per card).

const VERDICT_CHIP: Record<string, { cls: string; label: string }> = {
  matched: { cls: "ok", label: "matched ✓" },
  no_evidence: { cls: "flag", label: "no receipt" },
  ambiguous: { cls: "review", label: "ambiguous" },
  duplicate: { cls: "flag", label: "duplicate" },
  optional: { cls: "wait", label: "receipt-optional" },
  unchecked: { cls: "wait", label: "unchecked" },
};

export default function AllRows({ run, emp, rows, flags, evidence, evById, onChanged }: {
  run: ClaimsRunDetail;
  emp: ReviewUnit;
  /** This case's rows, flags and evidence items. */
  rows: ClaimRow[];
  flags: ClaimFlag[];
  evidence: ClaimEvidence[];
  /** Every evidence item of the run, by id (a row's match may sit elsewhere). */
  evById: Map<string, ClaimEvidence>;
  onChanged: Reload;
}) {
  const [showPage, setShowPage] = useState<string>("");   // row id whose page is open
  const [editing, setEditing] = useState<string>("");     // row id being fixed
  const expense = rows.filter((r) => r.kind !== "mileage").sort((a, b) => a.row - b.row);
  const km = rows.filter((r) => r.kind === "mileage").sort((a, b) => a.row - b.row);
  const flagsByRow = new Map<string, ClaimFlag[]>();
  for (const f of flags.filter((f) => f.row_id)) {
    flagsByRow.set(f.row_id, [...(flagsByRow.get(f.row_id) || []), f]);
  }
  const unused = evidence.filter((e) => !e.matched_row_id);
  const originChip = (r: ClaimRow) =>
    r.origin === "evidence_derived" || r.kind === "derived"
      ? <span className="chip review" title="built from a receipt; a proposal until confirmed">derived</span>
      : r.origin === "reviewer_entered"
        ? <span className="chip wait" title="a value the reviewer corrected">corrected</span>
        : null;

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
            {f.status === "open" ? <b>{describeFlag(run.catalogue, f.code).title}</b> : describeFlag(run.catalogue, f.code).title}
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
              await correctClaimRow(run.id, r.id, changed, reason, run.revision);
              setEditing("");
              await onChanged();
            }}
            onStale={onChanged}
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
        <table className="table inner" aria-label={`${emp.name || emp.label}: every report row`}>
          <thead>
            <tr><th>Row</th><th>Date</th><th>Item</th><th>Amount</th><th>Verdict</th><th>Receipt</th><th>Flags</th><th></th></tr>
          </thead>
          <tbody>
            {expense.flatMap((r) => [
              <tr key={r.id} className={excluded(r) ? "bad" : ""} style={excluded(r) ? { opacity: 0.6 } : undefined}>
                <td className="mono">{r.sheet ? r.row : `receipt ${r.row}`} {originChip(r)}</td>
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
        <table className="table inner" aria-label={`${emp.name || emp.label}: mileage rows`}>
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
      {rows.length === 0 && <p className="sub" style={{ padding: 10 }}>No lines were read for this case.</p>}
      {unused.length > 0 && (
        <p className="sub" style={{ padding: "8px 10px" }}>
          <b style={{ display: "inline" }}>Evidence no row uses:</b>{" "}
          {unused.map((e) => `${e.kind === "receipt" ? String(e.values.vendor ?? "?") : "map trip"} (${String(e.values.date ?? "no date")}, ${e.kind === "receipt" ? `${String(e.values.currency ?? "MYR")} ${String(e.values.amount ?? "")}` : `${String(e.values.km_printed ?? "?")} km`}) — ${e.file.split("/").pop()} p.${e.page}`).join("; ")}
        </p>
      )}
    </div>
  );
}
