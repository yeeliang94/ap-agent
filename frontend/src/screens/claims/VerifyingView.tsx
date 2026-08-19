import { useState } from "react";
import { ClaimsRunDetail, StaleRunError } from "../../api";
import { ReviewUnit, retryUnit, reviewUnits, unitIdOf } from "./units";

// Watch the workers without refreshing: one chip per case, an overall
// bar, and a Retry for a case whose worker failed. Keyed by Claim Case
// (H10); an older run's employees render the same way.
export default function VerifyingView({
  run,
  onChanged,
}: {
  run: ClaimsRunDetail;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");
  const units = reviewUnits(run);
  const total = units.length;
  const done = units.filter((e) => ["verified", "failed", "skipped"].includes(e.status)).length;
  const flagsFor = (id: string) => run.flags.filter((f) => unitIdOf(run, f) === id && f.status === "open").length;
  const notesFor = (id: string) => run.flags.filter((f) => unitIdOf(run, f) === id && f.status === "info").length;

  async function retry(u: ReviewUnit) {
    setBusy(u.id);
    setError("");
    try {
      await retryUnit(run, u);
      onChanged();
    } catch (e) {
      if (e instanceof StaleRunError) onChanged();
      setError(e instanceof Error ? e.message : "Could not retry");
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <p className="summary-line">
        <b>
          {done} of {total} cases done
        </b>{" "}
        <span className="sub">
          Five workers run at once, each sealed to one case's files. One case failing never
          fails the batch — retry it alone.
        </span>
      </p>
      <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
        <div className="bar-fill" style={{ width: total ? `${(100 * done) / total}%` : 0 }} />
      </div>
      <div className="chips">
        {units.map((e) => (
          <div key={e.id} className={`card emp ${e.status}`}>
            <b>{e.name || e.label}</b>
            <span className="sub">{e.identifier || "no identifier"}{e.claimant_state && e.claimant_state !== "confirmed" ? ` · claimant ${e.claimant_state}` : ""}</span>
            {e.status === "pending" && <span className="chip wait">queued</span>}
            {e.status === "verifying" && <span className="chip wait">verifying…</span>}
            {e.status === "verified" && (
              <>
                <span className={`chip ${flagsFor(e.id) ? "review" : "ok"}`}>
                  done · {flagsFor(e.id)} flag{flagsFor(e.id) === 1 ? "" : "s"}
                </span>
                <span className="sub">
                  {String(e.summary?.rows ?? 0)} row{Number(e.summary?.rows ?? 0) === 1 ? "" : "s"}
                  {Number(e.summary?.km_rows ?? 0) ? ` · ${String(e.summary?.km_rows)} km row(s)` : ""}
                  {notesFor(e.id) ? ` · ${notesFor(e.id)} note${notesFor(e.id) === 1 ? "" : "s"}` : ""}
                </span>
              </>
            )}
            {e.status === "skipped" && <span className="chip wait">skipped</span>}
            {e.status === "failed" && (
              <>
                <span className="chip flag">failed</span>
                <span className="sub">{e.error}</span>
                <button className="btn warn" disabled={busy === e.id} onClick={() => retry(e)}>
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
