import { useCallback, useEffect, useState } from "react";
import {
  correctFields,
  decideFlag,
  documentFileUrl,
  getRun,
  getRunEvents,
  CORRECTABLE,
  Doc,
  FlagItem,
  RunDetailData,
  Outputs,
  listingDraftUrl,
  RunEvent,
} from "../api";

// Screens B + C: review the flags, then copy the output blocks.
export default function RunDetail({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunDetailData | null>(null);
  // null = "not chosen yet", so the first sensible tab can depend on how
  // the run ended. A failed run has no flags and no output; opening it on
  // Review would show an empty screen and hide the only thing worth
  // reading.
  const [chosenTab, setTab] = useState<"review" | "output" | "activity" | null>(null);
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

  const failed = run.status === "failed";
  const tab = chosenTab ?? (failed ? "activity" : "review");
  const openFlags = run.flags.filter((f) => f.status === "open");
  const cleanCount = run.documents.filter(
    (d) => d.kind !== "receipt" && !run.flags.some((f) => f.document_id === d.id)
  ).length;

  return (
    <section>
      {/* A run can finish "ready" with errors recorded against it — that
          pairing is exactly what used to slip past, so it is stated at
          the top of the screen rather than left inside a panel. */}
      {failed ? (
        <div className="card banner bad">
          <b>This run stopped before it finished</b>
          <span className="sub">
            {run.error || "No reason was recorded."}
          </span>
          <span className="sub">
            Nothing from this run may be used. Fix the cause below, then upload
            the batch again.
          </span>
        </div>
      ) : run.errors > 0 ? (
        <div className="card banner bad">
          <b>
            {run.errors} error{run.errors === 1 ? "" : "s"} happened while this run
            was processed
          </b>
          <span className="sub">
            Some documents may be incomplete or wrongly classified. Open Activity
            below to see what failed before you trust this run's output.
          </span>
          <div className="actions">
            <button className="btn warn" onClick={() => setTab("activity")}>
              Show me what failed
            </button>
          </div>
        </div>
      ) : null}

      <div className="tabs">
        <button
          className={tab === "review" ? "tab active" : "tab"}
          onClick={() => setTab("review")}
          disabled={failed}
          title={failed ? "This run did not finish, so there is nothing to review" : ""}
        >
          Review {openFlags.length > 0 && <em>{openFlags.length}</em>}
        </button>
        <button
          className={tab === "output" ? "tab active" : "tab"}
          onClick={() => setTab("output")}
          disabled={failed || openFlags.length > 0}
          title={
            failed
              ? "This run did not finish, so it produced no output"
              : openFlags.length > 0
                ? "Resolve all flags first"
                : ""
          }
        >
          Copy-ready output
        </button>
        <button
          className={tab === "activity" ? "tab active" : "tab"}
          onClick={() => setTab("activity")}
        >
          Activity{" "}
          {run.errors + run.warnings > 0 && (
            <em className={run.errors > 0 ? "bad" : ""}>{run.errors + run.warnings}</em>
          )}
        </button>
      </div>

      {tab === "review" && (
        <Review run={run} onDecided={reload} cleanCount={cleanCount} />
      )}
      {tab === "output" && <Output run={run} />}
      {tab === "activity" && <Activity runId={run.id} />}
    </section>
  );
}

const STAGE_LABEL: Record<string, string> = {
  run: "Run",
  reference: "Reading SharePoint",
  sort: "Sorting",
  extract: "Reading documents",
  check: "Checking",
  output: "Building output",
};

// The run diary. Everything the pipeline recorded about itself: which
// reference files it used, how long each stage took, and — the reason
// this exists — every failure it absorbed and carried on from.
function Activity({ runId }: { runId: string }) {
  const [events, setEvents] = useState<RunEvent[] | null>(null);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    getRunEvents(runId, onlyProblems)
      .then((e) => alive && setEvents(e))
      .catch(() => alive && setError("Could not load the activity log"));
    return () => {
      alive = false;
    };
  }, [runId, onlyProblems]);

  if (error) return <p className="error">{error}</p>;
  if (!events) return <p className="sub">Loading…</p>;

  return (
    <div>
      <p className="summary-line">
        <b>What the system did with this batch</b>
        <label className="filter">
          <input
            type="checkbox"
            checked={onlyProblems}
            onChange={(e) => setOnlyProblems(e.target.checked)}
          />
          Only show problems
        </label>
      </p>
      {events.length === 0 && (
        <p className="sub">
          {onlyProblems
            ? "Nothing went wrong — no warnings or errors were recorded."
            : "Nothing recorded for this run."}
        </p>
      )}
      {events.map((e) => (
        <div key={e.id} className={`card event ${e.level}`}>
          <div className="row" style={{ border: "none", padding: 0 }}>
            <div className="grow">
              <b>{STAGE_LABEL[e.stage] ?? e.stage}</b>
              <span className="sub">{new Date(e.at).toLocaleTimeString()}</span>
            </div>
            <span className={`chip ${e.level === "error" ? "flag" : e.level === "warning" ? "review" : "ok"}`}>
              {e.level}
            </span>
          </div>
          <p className="reason">{e.message}</p>
          {/* The engineer's version, folded away: a reviewer never needs
              it, and whoever is debugging always asks for it. */}
          {e.detail && (
            <details>
              <summary className="sub">Technical detail</summary>
              <pre>{e.detail}</pre>
            </details>
          )}
        </div>
      ))}
    </div>
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
  doc?: Doc;
  onDecided: () => void;
}) {
  const [note, setNote] = useState("");
  const [showDoc, setShowDoc] = useState(false);
  const [editing, setEditing] = useState(false);
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

  const correctable = doc && CORRECTABLE[doc.kind]?.length > 0;
  // A flag with no document is about the whole batch (e.g. a reference
  // file the batch needed was missing). There is nothing to exclude or
  // correct — the reviewer acknowledges it and the run proceeds without.
  const runLevel = !flag.document_id;
  return (
    <div className="card flagcard">
      <div className="row" style={{ border: "none", padding: 0 }}>
        <div className="grow">
          <b>{flag.code.replaceAll("_", " ")}</b>
          {doc && <span className="sub">{doc.filename}</span>}
          {runLevel && <span className="sub">whole batch</span>}
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
      {editing && doc ? (
        <FieldEditor
          runId={runId}
          doc={doc}
          // Close the editor before refreshing: if this flag stays open
          // (value corrected but still wrong), a still-mounted editor
          // would keep its busy=true state and lock its buttons forever.
          onDone={() => {
            setEditing(false);
            onDecided();
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <div className="actions">
          <input
            placeholder="Note (optional) — recorded in the audit trail"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button className="btn primary" disabled={busy} onClick={() => decide("accepted")}>
            {runLevel ? "Acknowledge — proceed without it" : "Accept — include in output"}
          </button>
          {correctable && (
            <button
              className="btn"
              disabled={busy}
              onClick={() => {
                setEditing(true);
                setShowDoc(true); // correcting means reading the source
              }}
            >
              Fix a value
            </button>
          )}
          {!runLevel && (
            <button className="btn warn" disabled={busy} onClick={() => decide("rejected")}>
              Exclude &amp; query
            </button>
          )}
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// Inline editor for the document's extracted values. Saving a change
// records an audited correction, re-checks just this document on the
// server, and refreshes the flags — flags that no longer apply resolve
// themselves.
function FieldEditor({
  runId,
  doc,
  onDone,
  onCancel,
}: {
  runId: string;
  doc: Doc;
  onDone: () => void;
  onCancel: () => void;
}) {
  const fields = CORRECTABLE[doc.kind] ?? [];
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(fields.map((f) => [f, String(doc.fields[f] ?? "")]))
  );
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const changed = fields.filter((f) => values[f] !== String(doc.fields[f] ?? ""));

  async function save() {
    setBusy(true);
    setError("");
    try {
      // One request for all changed fields — the server validates them all
      // before applying any, and re-checks the document once. With no
      // changes, the current values are sent as-is: the server treats
      // that as a pure re-check (the recovery after a failed one).
      const send = changed.length > 0 ? changed : fields;
      await correctFields(
        runId,
        doc.id,
        Object.fromEntries(send.map((f) => [f, values[f]])),
        reason
      );
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Correction failed");
      setBusy(false);
    }
  }

  return (
    <div className="editor">
      <p className="sub">
        Compare with the document above, then correct any misread value. The
        change is recorded in the audit trail and this document is re-checked.
      </p>
      {fields.map((f) => (
        <label key={f} className="editrow">
          <span>
            {f.replaceAll("_", " ")}
            {doc.confidence?.[f] && <em className="lowconf"> — uncertain read</em>}
            {doc.corrections?.[f] && <em className="corrected"> — corrected</em>}
          </span>
          <input value={values[f]} onChange={(e) => setValues({ ...values, [f]: e.target.value })} />
        </label>
      ))}
      <div className="actions">
        <input
          placeholder="Reason (required) — e.g. 'digits blurred, read from source'"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <button
          className="btn primary"
          disabled={busy || !reason.trim()}
          onClick={save}
        >
          {busy
            ? "Saving…"
            : changed.length > 0
              ? `Save ${changed.length} correction(s)`
              : "Re-check document"}
        </button>
        <button className="btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Output({ run }: { run: RunDetailData }) {
  const out = run.outputs;
  if (!out || !("bank_rows" in out)) return <p className="sub">No output yet.</p>;

  return (
    <div>
      <div className="totals">
        <TotalCard label="Bank entries total" value={`RM ${out.totals.bank.toFixed(2)}`} />
        <TotalCard
          label="Reconciliation"
          value={out.totals.match ? "Totals verified ✓" : "MISMATCH ✗"}
          good={out.totals.match}
        />
      </div>
      {out.new_vendors.length > 0 && (
        <p className="basis">
          ⚠ No past payment to these vendors in the listing — verify they are
          registered Maybank beneficiaries before uploading: {out.new_vendors.join(", ")}
        </p>
      )}
      <ListingDraftCard runId={run.id} draft={out.listing_draft} />
      {/* No template in the reference folder means no column layout to
          follow, so the block is omitted — and the reviewer is told why,
          rather than left to wonder where the bank rows went. */}
      {out.bank_skipped ? (
        <div className="card">
          <b>Maybank entry rows</b>
          <p className="basis">
            No bank rows were built: the reference folder has no bank upload template,
            so there is no column layout to follow. Add the template to the folder and
            start a new run to get them.
          </p>
        </div>
      ) : (
        <CopyBlock
          title={`Maybank entry rows (${out.bank_rows.length})`}
          hint="Format learned from the uploaded Maybank template — account numbers are template-aligned placeholders until a vendor master exists"
          text={[out.bank_header, ...out.bank_rows].join("\n")}
          preview={out.bank_rows}
        />
      )}
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

// Next month's payment-listing entries, drafted as ONE new tab on a copy
// of the client's workbook. A draft: it is downloaded, checked and pasted
// by a person after the bank run — never written into the live listing,
// because the listing is the record of payments that have happened.
function ListingDraftCard({
  runId,
  draft,
}: {
  runId: string;
  draft: Outputs["listing_draft"] | undefined;
}) {
  if (!draft || "skipped" in draft) {
    return (
      <div className="card">
        <b>Payment-listing draft</b>
        <p className="muted">
          No draft was written for this run: {draft?.skipped ?? "not built"}. Add the
          new entries in the listing workbook by hand.
        </p>
      </div>
    );
  }
  return (
    <div className="card">
      <b>Payment-listing draft — tab “{draft.tab}”</b>
      <span className="sub">
        {draft.entries.length} payment {draft.entries.length === 1 ? "entry" : "entries"} ·{" "}
        {draft.invoice_count} invoice{draft.invoice_count === 1 ? "" : "s"}, laid out like
        tab “{draft.source_tab}”; voucher numbers {draft.entries[0]?.voucher || "blank"}
        {draft.entries.length > 1 ? ` … ${draft.entries[draft.entries.length - 1].voucher}` : ""}
        {draft.excluded_non_myr > 0 ? ` · ${draft.excluded_non_myr} non-MYR invoice(s) left out` : ""}
      </span>
      {draft.has_bank_block && (
        <p className="muted">
          Balance b/f RM {draft.opening_balance} · net payment RM {draft.net_payment} · estimated
          bank charges RM {draft.bank_charges} · total fund to request RM {draft.fund_to_request}{" "}
          (balance returns to RM {draft.closing_balance}).
        </p>
      )}
      <ul className="muted">
        {draft.entries.map((e) => (
          <li key={`${e.voucher}|${e.invoices.map((i) => i.number).join(",")}`}>
            {e.voucher || "—"} · {e.payee} · RM {e.total}
            {e.invoices.length > 1 ? ` (${e.invoices.map((i) => i.number).join(", ")})` : ` (${e.invoices[0].number})`}
          </li>
        ))}
      </ul>
      <div className="actions">
        <a className="btn primary" href={listingDraftUrl(runId)} download={draft.file}>
          Download draft workbook
        </a>
      </div>
      <p className="basis">
        This is a DRAFT on a copy of the client's workbook. Payment dates are left blank
        and the voucher numbers and fund figures are to be confirmed after the bank run —
        a person finalises it; nothing is written into the live listing. The rules used
        to group entries, continue voucher numbers and work out the fund request are
        working assumptions taken from two past tabs and are not yet confirmed by the
        client — check them before relying on the figures.
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
