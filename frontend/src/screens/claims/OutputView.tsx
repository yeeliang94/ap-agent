import { ClaimsRunDetail } from "../../api";
import CopyBlock from "../../components/CopyBlock";
import TotalCard from "../../components/TotalCard";

// Output: the listing rows — one per confirmed Claim Case, in the client's
// own column order — behind the human gate. Locked while any flag is open,
// a claimant is unconfirmed or a file has no disposition; the server names
// what locks it (output_blockers) and is the one that enforces it.
export default function OutputView({ run, onGoReview }: { run: ClaimsRunDetail; onGoReview: () => void }) {
  const open = run.flags.filter((f) => f.status === "open").length;
  const out = run.outputs;
  const blockers = run.output_blockers ?? [];
  if (open > 0 || blockers.length > 0 || !out || !("rows" in out)) {
    return (
      <div className="card banner bad">
        <b>{open > 0 ? `Review is not complete: ${open} flag${open === 1 ? "" : "s"} open` : "The output is locked"}</b>
        {blockers.length > 0 && <ul className="muted">{blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
        <span className="sub">
          Nothing leaves this screen until every flag has a decision and every case has a confirmed
          claimant — that is the rule, enforced by the server, not just this button.
        </span>
        <div className="actions">
          <button className="btn warn" onClick={onGoReview}>Go to Review</button>
        </div>
      </div>
    );
  }
  const preview = out.rows.map((r) => r.join("  |  "));
  return (
    <div>
      <div className="totals">
        <TotalCard label="Cases included" value={String(out.included.length)} />
        <TotalCard label="Emitted total (MYR)" value={`RM ${out.totals.total_myr}`} />
        {out.totals.lines_total && <TotalCard label="Calculated Lines Total" value={`RM ${out.totals.lines_total}`} />}
        {out.totals.reported_total !== undefined && (
          <TotalCard
            label="Reported Totals"
            value={`RM ${out.totals.reported_total}${out.totals.reported_missing ? ` (${out.totals.reported_missing} case${out.totals.reported_missing === 1 ? "" : "s"} with none)` : ""}`}
          />
        )}
        <TotalCard
          label="Reconciliation"
          value={out.totals.match ? "Totals verified ✓" : `MISMATCH ✗ (off by RM ${out.totals.difference})`}
          good={out.totals.match}
        />
      </div>
      {!out.totals.match && (
        <p className="basis">
          The rows below add up to RM {out.totals.total_myr}, but the Reported Totals of the included cases
          (less excluded rows; the lines' own sum where a source states none) add up to RM {out.totals.source_total}.
          This is written to the run diary; copy is still allowed, but check before pasting.
        </p>
      )}
      {(out.totals.differences ?? []).length > 0 && (
        <ul className="basis">
          {(out.totals.differences ?? []).map((d, i) => (
            <li key={i}>
              <b>{d.name}</b>: {d.why}
            </li>
          ))}
        </ul>
      )}
      {out.header_fallback && (
        <p className="basis">⚠ {out.header_note}</p>
      )}
      <CopyBlock
        title={`Listing rows (${out.rows.length}) — received date ${out.received_date}`}
        hint={out.header_fallback ? "Fallback columns: " + out.header.join(" | ") : "Column order from the client's listing: " + out.header.join(" | ")}
        text={out.tsv}
        preview={preview}
      />
      <div className="card">
        <b>Included</b>
        <ul className="muted">
          {out.included.map((i) => (
            <li key={i.case_id || i.er_code || i.name}>
              {i.name} · {i.er_code || "no identifier"} · {i.category ? `${i.category}${i.gl ? ` (${i.gl})` : ""}` : "no category"} · RM {i.amount}
              {i.reported_total === null ? " · no Reported Total (lines only)" : i.reported_total && i.reported_total !== i.amount ? ` · Reported Total RM ${i.reported_total}` : ""}
              {i.derived ? " · lines derived from evidence" : ""}
            </li>
          ))}
        </ul>
        {out.exclusions && out.exclusions.length > 0 && (
          <>
            <span className="sub">Rows left out after review (subtracted from the case's amount):</span>
            <ul className="muted">
              {out.exclusions.map((x, i) => (
                <li key={i}>{x.name} · row {x.row} · RM {x.amount} · {x.why}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      {(out.unused_evidence ?? []).length > 0 && (
        <div className="card">
          <b>Evidence not used</b>
          <span className="sub">Receipts and map trips that support no row — nothing here is paid. Listed so nothing uploaded vanishes silently.</span>
          <ul className="muted">
            {(out.unused_evidence ?? []).map((u, i) => (
              <li key={i}>{u.name} · {u.what} · {u.where}{u.amount ? ` · ${u.amount}` : ""}{u.decision ? ` · ${u.decision}` : ""}</li>
            ))}
          </ul>
        </div>
      )}
      {out.not_included.length > 0 && (
        <div className="card">
          <b>Not included</b>
          <ul className="muted">
            {out.not_included.map((n) => (
              <li key={n.case_id || n.name}>{n.name} — {n.why}</li>
            ))}
          </ul>
        </div>
      )}
      <p className="sub">
        Nothing is written to your files — paste the rows into the month's Summary of Invoices yourself.
      </p>
    </div>
  );
}
