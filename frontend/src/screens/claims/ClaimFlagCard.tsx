import { useState } from "react";
import {
  ClaimEvidence,
  ClaimFlag,
  ClaimRow,
  ClaimsRunDetail,
  Disposition,
  claimsFileUrl,
  correctClaimRow,
  decideClaimFlag,
  setClaimant,
} from "../../api";
import FieldEditor from "../../components/FieldEditor";
import FlagCard from "../../components/FlagCard";
import { Reload, useAction } from "../../hooks/useAction";
import { KM_FIELDS, ROW_FIELDS, describeFlag, rm, stakeCents } from "./flags";
import { ReviewUnit, retryUnit } from "./units";

// One flag on the Review screen: its words, the cited page, and the
// decision controls. The words on each button say what the server will
// DO with the decision (the row leaves the batch; the case leaves the
// listing; the note is recorded) — never a softer word than the effect.
export default function ClaimFlagCard({ run, flag, unit, row, evidence, onChanged, defaultOpen = false }: {
  run: ClaimsRunDetail;
  flag: ClaimFlag;
  /** The case / employee the flag belongs to (undefined for a whole-batch flag). */
  unit?: ReviewUnit;
  row?: ClaimRow;
  evidence?: ClaimEvidence;
  onChanged: Reload;
  /** Open the cited page straight away (the first flag of each employee). */
  defaultOpen?: boolean;
}) {
  const [note, setNote] = useState("");
  const [showEvidence, setShowEvidence] = useState(defaultOpen && !!(flag.cite || {}).file);
  const [editing, setEditing] = useState(false);
  const [confirmingExclude, setConfirmingExclude] = useState(false);
  const action = useAction(onChanged);
  const busy = !!action.busy;
  const info = flag.status === "info";
  const cite = flag.cite || {};
  const hasPage = !!cite.file && (cite.page ?? 0) > 0;   // a workbook or text file has no page to show
  const [claimName, setClaimName] = useState(unit?.name ?? "");
  const [claimId, setClaimId] = useState(unit?.identifier ?? "");
  // Some flags are settled by an ACTION, not a note (H9): a file's
  // disposition, a case's claimant; the card offers that action instead.
  const artifactFlag = flag.code === "ARTIFACT_UNRESOLVED";
  const claimantFlag = flag.code === "CLAIMANT_UNKNOWN";
  const conflictFlag = flag.code === "OWNERSHIP_CONFLICT";
  const amountFlag = flag.code === "CLAIM_AMOUNT_UNCONFIRMED";
  const where = hasPage
    ? `${cite.file}, page ${cite.page}${cite.position ? `, ${cite.position}` : ""}`
    : cite.file
      ? cite.file
      : cite.sheet
      ? `tab ${cite.sheet}${cite.row ? `, row ${cite.row}` : ""}`
      : unit ? "" : "whole batch";
  const rowLine = row
    ? `${row.sheet || "receipts"} row ${row.row}: ${String(row.values.date ?? "")} · ${String(row.values.item_name ?? row.values.item ?? "")} · ${String(row.values.currency ?? "MYR")} ${String(row.values.amount ?? row.values.km ?? "")}${row.values.km ? " km" : ""}`
    : "";
  const fields = row?.kind === "mileage" ? KM_FIELDS : ROW_FIELDS;
  const rowLevel = !!flag.row_id && !!row;
  const words = describeFlag(run.catalogue, flag.code);
  const stake = stakeCents(flag, new Map(row ? [[row.id, row]] : []), new Map(evidence ? [[evidence.id, evidence]] : []));
  const acceptance = acceptAction({ rowLevel, amountFlag, caseId: flag.case_id, stake: rm(stake), caseName: unit?.name || unit?.label || "" });

  function decide(decision: "accepted" | "dismissed", disposition?: Disposition) {
    return action.run(() => decideClaimFlag(run.id, flag.id, decision, note, run.revision, disposition),
      { fallback: "Could not record the decision" });
  }

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
      error={action.error}
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
              await correctClaimRow(run.id, row.id, changed, reason, run.revision);
              setEditing(false);
              await onChanged();
            }}
            onStale={onChanged}
            onCancel={() => setEditing(false)}
          />
        ) : info ? null : artifactFlag ? (
          <div className="actions">
            <input aria-label="Why (required; goes in the audit trail)" placeholder="Why — required (goes in the audit trail)" value={note} onChange={(e) => setNote(e.target.value)} />
            {(["irrelevant", "duplicate", "unreadable"] as Disposition[]).map((d) => (
              <button key={d} className="btn" disabled={busy || !note.trim()} title={note.trim() ? "" : "Type why first"}
                onClick={() => decide("dismissed", d)}>
                Mark {d}
              </button>
            ))}
            <span className="sub" style={{ flexBasis: "100%" }}>
              A file nobody placed is settled by saying what it is — or by moving it into a case at the map before confirming. A note alone does not release it.
            </span>
          </div>
        ) : claimantFlag && unit?.case_id ? (
          <div className="actions">
            <input aria-label="Claimant name" placeholder="claimant name" value={claimName} onChange={(e) => setClaimName(e.target.value)} />
            <input aria-label="Claimant identifier (ER code / id)" placeholder="identifier (ER code / id)" value={claimId} onChange={(e) => setClaimId(e.target.value)} />
            <button className="btn primary" disabled={busy || !claimName.trim()}
              onClick={() => action.run(() => setClaimant(run.id, run.revision, unit.case_id, claimName, claimId), { fallback: "Could not set the claimant" })}>
              Set claimant
            </button>
            <span className="sub" style={{ flexBasis: "100%" }}>
              Nobody is paid on a guessed name. Setting the claimant here is audited and releases this case.
            </span>
          </div>
        ) : conflictFlag ? (
          <p className="sub">Settled at the map only: split the case or move the odd file out, then confirm the grouping again. A note cannot settle who owns what.</p>
        ) : confirmingExclude ? (
          <div className="actions" role="group" aria-label="Confirm leaving the case out">
            <span className="sub" style={{ flexBasis: "100%" }}>
              <b style={{ display: "inline" }}>Leave {unit?.name || unit?.label || "this case"} out of the Payment Listing?</b>{" "}
              The case is set to excluded — nothing from it is paid in this batch — and the decision is audited. Fix a line's value instead if one is merely misread.
            </span>
            <button className="btn warn" disabled={busy} onClick={async () => { if (await decide("accepted")) setConfirmingExclude(false); }}>
              Yes — leave this case out of the listing
            </button>
            <button className="btn" disabled={busy} onClick={() => setConfirmingExclude(false)}>Cancel</button>
          </div>
        ) : (
          <div className="actions">
            <input aria-label="Note (required to dismiss; optional to accept)" placeholder="Note — required to dismiss; optional to accept" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn warn" disabled={busy} title={acceptance.title}
              onClick={() => (acceptance.confirm ? setConfirmingExclude(true) : decide("accepted"))}>
              {acceptance.label}
            </button>
            {rowLevel && (
              <button className="btn" disabled={busy} onClick={() => { setEditing(true); setShowEvidence(hasPage); }}>
                Fix a value
              </button>
            )}
            <button className="btn primary" disabled={busy || !note.trim()} title={note.trim() ? "" : "Type a note first"} onClick={() => decide("dismissed")}>
              {amountFlag ? "Confirm these amounts" : "Dismiss — keep the row"}
            </button>
            {unit && (
              <button className="btn" disabled={busy}
                onClick={() => action.run(() => retryUnit(run, unit), { fallback: "Could not re-verify" })}>
                Re-verify case
              </button>
            )}
            <span className="sub" style={{ flexBasis: "100%" }}>{acceptance.help}</span>
          </div>
        )
      }
    />
  );
}

/** The words of the "accept" decision, by what the server does with it:
 *  a row-level flag leaves THAT ROW out of the batch; CLAIM_AMOUNT_UNCONFIRMED
 *  on a case leaves THE WHOLE CASE out of the listing (the server sets it to
 *  excluded — so it asks for a confirm step); any other case- or run-level
 *  flag is only acknowledged (recorded as seen; nothing else changes). */
export function acceptAction({ rowLevel, amountFlag, caseId, stake, caseName }: {
  rowLevel: boolean; amountFlag: boolean; caseId: string | undefined; stake: string; caseName: string;
}): { label: string; title: string; help: string; confirm: boolean } {
  if (amountFlag && caseId) {
    return {
      label: "Accept — leave this case out of the listing",
      title: `${caseName || "This case"} is set to excluded and drops out of the Payment Listing; nothing from it is paid in this batch`,
      help: "Confirm records, with your note, that the receipt totals listed are what should be paid · Accept leaves this whole case out of the listing (it is set to excluded, not paid) · fix a line's value first if one is wrong",
      confirm: true,
    };
  }
  if (rowLevel) {
    return {
      label: `Accept — leave ${stake || "this row"} out`,
      title: "The row is left out of the batch; the employee stays with the rest",
      help: `Accept leaves ${stake || "this row"} out of the batch · Dismiss keeps it (a note is required) · Fix a value corrects a misread and re-checks this case at once`,
      confirm: false,
    };
  }
  return {
    label: "Acknowledge",
    title: "Recorded as seen; nothing in the batch changes and the run proceeds",
    help: amountFlag
      ? "Confirm records, with your note, that the receipt totals listed are what should be paid · Acknowledge records this as seen and changes nothing · fix a line's value first if one is wrong"
      : "Acknowledge records this as seen and the run proceeds · Dismiss sets it aside with a note",
    confirm: false,
  };
}
