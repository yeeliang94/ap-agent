import { useCallback, useEffect, useState } from "react";
import {
  decideFlag,
  documentFileUrl,
  getRun,
  FlagItem,
  RunDetailData,
} from "../api";

// Screens B + C: review the flags, then copy the output blocks.
export default function RunDetail({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunDetailData | null>(null);
  const [tab, setTab] = useState<"review" | "output">("review");
  const [error, setError] = useState("");

  const reload = useCallback(
    () => getRun(runId).then(setRun).catch(() => setError("Could not load run")),
    [runId]
  );
  useEffect(() => {
    reload();
  }, [reload]);

  if (error) return <p className="error">{error}</p>;
  if (!run) return <p className="sub">Loading…</p>;

  const openFlags = run.flags.filter((f) => f.status === "open");
  const cleanCount = run.documents.filter(
    (d) => d.kind !== "receipt" && !run.flags.some((f) => f.document_id === d.id)
  ).length;

  return (
    <section>
      <div className="tabs">
        <button
          className={tab === "review" ? "tab active" : "tab"}
          onClick={() => setTab("review")}
        >
          Review {openFlags.length > 0 && <em>{openFlags.length}</em>}
        </button>
        <button
          className={tab === "output" ? "tab active" : "tab"}
          onClick={() => setTab("output")}
          disabled={openFlags.length > 0}
          title={openFlags.length > 0 ? "Resolve all flags first" : ""}
        >
          Copy-ready output
        </button>
      </div>

      {tab === "review" ? (
        <Review run={run} onDecided={reload} cleanCount={cleanCount} />
      ) : (
        <Output run={run} />
      )}
    </section>
  );
}

function Review({
  run,
  onDecided,
  cleanCount,
}: {
  run: RunDetailData;
  onDecided: () => void;
  cleanCount: number;
}) {
  const open = run.flags.filter((f) => f.status === "open");
  const decided = run.flags.filter((f) => f.status !== "open");
  const docById = new Map(run.documents.map((d) => [d.id, d]));

  return (
    <div>
      <p className="summary-line">
        <b>
          {cleanCount} documents clean — {open.length} need your decision
        </b>
      </p>
      {open.map((f) => (
        <FlagCard key={f.id} runId={run.id} flag={f} doc={docById.get(f.document_id)} onDecided={onDecided} />
      ))}
      {open.length === 0 && (
        <p className="sub">All flags resolved. The copy-ready output tab is unlocked.</p>
      )}
      {decided.length > 0 && (
        <details>
          <summary className="sub">{decided.length} resolved flag(s)</summary>
          {decided.map((f) => (
            <div key={f.id} className="card row">
              <div className="grow">
                <b>[{f.code}]</b> <span className="sub">{f.reason}</span>
              </div>
              <span className={`chip ${f.status === "accepted" ? "ok" : "flag"}`}>{f.status}</span>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

function FlagCard({
  runId,
  flag,
  doc,
  onDecided,
}: {
  runId: string;
  flag: FlagItem;
  doc?: { id: string; filename: string };
  onDecided: () => void;
}) {
  const [note, setNote] = useState("");
  const [showDoc, setShowDoc] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function decide(decision: "accepted" | "rejected") {
    setBusy(true);
    setError("");
    try {
      await decideFlag(runId, flag.id, decision, note);
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to record decision");
      setBusy(false);
    }
  }

  return (
    <div className="card flagcard">
      <div className="row" style={{ border: "none", padding: 0 }}>
        <div className="grow">
          <b>{flag.code.replaceAll("_", " ")}</b>
          {doc && <span className="sub">{doc.filename}</span>}
        </div>
        {doc && (
          <button className="btn" onClick={() => setShowDoc(!showDoc)}>
            {showDoc ? "Hide document" : "View document"}
          </button>
        )}
      </div>
      <p className="reason">{flag.reason}</p>
      {flag.basis && <p className="basis">Basis: {flag.basis}</p>}
      {showDoc && doc && (
        <div className="docview">
          <img src={documentFileUrl(runId, doc.id)} alt={doc.filename} />
        </div>
      )}
      <div className="actions">
        <input
          placeholder="Note (optional) — recorded in the audit trail"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <button className="btn primary" disabled={busy} onClick={() => decide("accepted")}>
          Accept — include in output
        </button>
        <button className="btn warn" disabled={busy} onClick={() => decide("rejected")}>
          Exclude &amp; query
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Output({ run }: { run: RunDetailData }) {
  const out = run.outputs;
  if (!out || !("listing_rows" in out)) return <p className="sub">No output yet.</p>;

  return (
    <div>
      <div className="totals">
        <TotalCard label="Payment listing total" value={`RM ${out.totals.listing.toFixed(2)}`} />
        <TotalCard label="Bank entries total" value={`RM ${out.totals.bank.toFixed(2)}`} />
        <TotalCard
          label="Reconciliation"
          value={out.totals.match ? "Totals match ✓" : "MISMATCH ✗"}
          good={out.totals.match}
        />
      </div>
      {out.new_vendors.length > 0 && (
        <p className="basis">
          ⚠ Register in Maybank before uploading: {out.new_vendors.join(", ")}
        </p>
      )}
      <CopyBlock
        title={`Payment listing rows (${out.listing_rows.length})`}
        hint="Paste into the payment listing workbook — one value per cell"
        text={[out.listing_header, ...out.listing_rows].join("\n")}
        preview={out.listing_rows}
      />
      <CopyBlock
        title={`Maybank entry rows (${out.bank_rows.length})`}
        hint="Format learned from the uploaded Maybank template"
        text={[out.bank_header, ...out.bank_rows].join("\n")}
        preview={out.bank_rows}
      />
      <CopyBlock
        title={`Proposed file names (${out.filenames.length})`}
        hint="Apply when filing the invoices"
        text={out.filenames.join("\n")}
        preview={out.filenames}
      />
      <p className="sub">
        Nothing is written to your files — you paste each block into the real working
        document yourself. Uploading to the bank remains a human action, always.
      </p>
    </div>
  );
}

function TotalCard({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className={`card total ${good ? "good" : ""}`}>
      <span className="sub">{label}</span>
      <b>{value}</b>
    </div>
  );
}

function CopyBlock({
  title,
  hint,
  text,
  preview,
}: {
  title: string;
  hint: string;
  text: string;
  preview: string[];
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <div className="card copyblock">
      <div className="row" style={{ border: "none", padding: 0 }}>
        <div className="grow">
          <b>{title}</b>
          <span className="sub">{hint}</span>
        </div>
        <button className="btn primary" onClick={copy}>
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
      <pre>{preview.slice(0, 4).join("\n")}{preview.length > 4 ? `\n… ${preview.length - 4} more` : ""}</pre>
    </div>
  );
}
