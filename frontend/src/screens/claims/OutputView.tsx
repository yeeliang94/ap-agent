import { ClaimsRunDetail } from "../../api";
import CopyBlock from "../../components/CopyBlock";
import TotalCard from "../../components/TotalCard";

// Output: the listing rows — one per employee, in the client's own column
// order — behind the human gate. Locked while any flag is open.
export default function OutputView({ run, onGoReview }: { run: ClaimsRunDetail; onGoReview: () => void }) {
  const open = run.flags.filter((f) => f.status === "open").length;
  const out = run.outputs;
  if (open > 0 || !out || !("rows" in out)) {
    return (
      <div className="card banner bad">
        <b>Review is not complete: {open} flag{open === 1 ? "" : "s"} open</b>
        <span className="sub">
          Nothing leaves this screen until every flag has a decision — that is the rule, enforced by
          the server, not just this button.
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
        <TotalCard label="Employees included" value={String(out.included.length)} />
        <TotalCard label="Total (MYR)" value={`RM ${out.totals.total_myr}`} />
        <TotalCard
          label="Reconciliation"
          value={out.totals.match ? "Totals verified ✓" : `MISMATCH ✗ (off by RM ${out.totals.difference})`}
          good={out.totals.match}
        />
      </div>
      {!out.totals.match && (
        <p className="basis">
          The rows below add up to RM {out.totals.total_myr}, but the included employees' report totals
          (less excluded rows) add up to RM {out.totals.source_total}. This is written to the run diary;
          copy is still allowed, but check before pasting.
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
            <li key={i.er_code || i.name}>
              {i.name} · {i.er_code || "no ER code"} · {i.category ? `${i.category}${i.gl ? ` (${i.gl})` : ""}` : "no category"} · RM {i.amount}
            </li>
          ))}
        </ul>
        {out.exclusions && out.exclusions.length > 0 && (
          <>
            <span className="sub">Rows left out after review (subtracted from the employee's amount):</span>
            <ul className="muted">
              {out.exclusions.map((x, i) => (
                <li key={i}>{x.name} · row {x.row} · RM {x.amount} · {x.why}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      {out.not_included.length > 0 && (
        <div className="card">
          <b>Not included</b>
          <ul className="muted">
            {out.not_included.map((n) => (
              <li key={n.name}>{n.name} — {n.why}</li>
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
