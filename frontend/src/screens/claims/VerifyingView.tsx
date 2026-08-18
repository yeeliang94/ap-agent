import { useState } from "react";
import { ClaimsRunDetail, retryClaimEmployee } from "../../api";

// Watch the workers without refreshing: one chip per employee, an overall
// bar, and a Retry for an employee whose worker failed.
export default function VerifyingView({
  run,
  onChanged,
}: {
  run: ClaimsRunDetail;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");
  const total = run.employees.length;
  const done = run.employees.filter((e) => ["verified", "failed", "skipped"].includes(e.status)).length;
  const flagsFor = (id: string) => run.flags.filter((f) => f.employee_id === id && f.status === "open").length;

  async function retry(id: string) {
    setBusy(id);
    setError("");
    try {
      await retryClaimEmployee(run.id, id);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not retry");
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <p className="summary-line">
        <b>
          {done} of {total} employees done
        </b>{" "}
        <span className="sub">
          Five workers run at once, each sealed to one employee's files. One employee failing never
          fails the batch — retry it alone.
        </span>
      </p>
      <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
        <div className="bar-fill" style={{ width: total ? `${(100 * done) / total}%` : 0 }} />
      </div>
      <div className="chips">
        {run.employees.map((e) => (
          <div key={e.id} className={`card emp ${e.status}`}>
            <b>{e.name || e.folder}</b>
            <span className="sub">{e.er_code || "no ER code"}</span>
            {e.status === "pending" && <span className="chip wait">queued</span>}
            {e.status === "verifying" && <span className="chip wait">verifying…</span>}
            {e.status === "verified" && (
              <span className={`chip ${flagsFor(e.id) ? "review" : "ok"}`}>
                done · {flagsFor(e.id)} flag{flagsFor(e.id) === 1 ? "" : "s"}
              </span>
            )}
            {e.status === "skipped" && <span className="chip wait">skipped</span>}
            {e.status === "failed" && (
              <>
                <span className="chip flag">failed</span>
                <span className="sub">{e.error}</span>
                <button className="btn warn" disabled={busy === e.id || run.status === "verifying" && false} onClick={() => retry(e.id)}>
                  {busy === e.id ? "Retrying…" : "Retry"}
                </button>
              </>
            )}
            {e.summary && typeof e.summary.seconds === "number" && (
              <span className="sub">{Math.round(e.summary.seconds as number)} s · {String(e.summary.requests ?? "?")} AI calls</span>
            )}
          </div>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
