import { useEffect, useRef, useState } from "react";
import {
  ClaimsRunSummary,
  ClaimsSettings,
  createClaimsRun,
  getClaimsSettings,
  listClaimsRuns,
} from "../api";

// Plain words for each status, and how far along the run is.
export function claimsStatusLabel(r: ClaimsRunSummary): string {
  switch (r.status) {
    case "queued":
      return "Queued";
    case "surveying":
      return r.progress?.total
        ? `Copying files ${r.progress.done}/${r.progress.total}`
        : "Reading the folder";
    case "mapping":
      return "Mapping the folder";
    case "map_ready":
      return "Map ready — confirm to verify";
    case "verifying":
      return `Verifying ${r.employees_done}/${r.employee_count}`;
    case "ready":
      return r.open_flags ? `${r.open_flags} flags to review` : "Ready";
    case "failed":
      return "Failed";
    default:
      return r.status;
  }
}

function chipClass(r: ClaimsRunSummary): string {
  if (r.status === "failed") return "chip flag";
  if (r.status === "map_ready") return "chip review";
  if (r.status === "ready") return r.open_flags ? "chip review" : "chip ok";
  return "chip wait";
}

// The Claims tab: start a new claims run, and see past batches.
export default function ClaimsList({ onOpen }: { onOpen: (id: string) => void }) {
  const [runs, setRuns] = useState<ClaimsRunSummary[] | null>(null);
  const [error, setError] = useState("");

  // Poll every 3 s so status chips move while a run is working.
  useEffect(() => {
    let alive = true;
    const tick = () =>
      listClaimsRuns()
        .then((r) => alive && setRuns(r))
        .catch(() => alive && setError("Backend not reachable"));
    tick();
    const t = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  return (
    <section>
      <NewClaimsRunCard onStarted={onOpen} />
      {error && <p className="error">{error}</p>}
      {runs && runs.length === 0 && (
        <p className="sub">No claims runs yet — start one above.</p>
      )}
      {runs && runs.length > 0 && (
        <div className="card" style={{ padding: 0, overflowX: "auto" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Client</th>
                <th>Folder</th>
                <th>Started</th>
                <th>Status</th>
                <th>Employees</th>
                <th>Open flags</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className={r.status === "failed" ? "bad" : ""}>
                  <td>{r.client}</td>
                  <td className="mono" title={r.folder}>
                    {r.folder.length > 40 ? "…" + r.folder.slice(-40) : r.folder}
                  </td>
                  <td>{new Date(r.created_at).toLocaleString()}</td>
                  <td>
                    <span className={chipClass(r)}>{claimsStatusLabel(r)}</span>
                    {/* A failed run's reason belongs on the row — it is
                        the whole story of that run. */}
                    {r.status === "failed" && r.error && (
                      <span className="sub">{r.error}</span>
                    )}
                    {r.errors > 0 && r.status !== "failed" && (
                      <span className="chip flag" style={{ marginLeft: 6 }}>
                        {r.errors} error{r.errors === 1 ? "" : "s"}
                      </span>
                    )}
                  </td>
                  <td>{r.employee_count || "—"}</td>
                  <td>{r.open_flags || (r.status === "ready" ? "0" : "—")}</td>
                  <td>
                    <button
                      className={r.status === "failed" ? "btn warn" : "btn primary"}
                      onClick={() => onOpen(r.id)}
                    >
                      {r.status === "failed"
                        ? "See what failed"
                        : r.status === "map_ready"
                          ? "Confirm map"
                          : "Open"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const today = () => new Date().toISOString().slice(0, 10);

const EXAMPLE_INSTRUCTIONS =
  "Example: “Each employee has a folder named after them. The expense report is " +
  "the .xlsx; the tab called ‘Expense Report’ has the lines and the ‘KM’ tab has " +
  "the mileage trips. Receipts are scanned three to a page; the map screenshots " +
  "for mileage are at the back of the receipt PDF. Ignore *_Approval.pdf.”";

// The Copilot-simple start: folder + listing + date (+ an optional
// paragraph). In local mode a zip and a listing file stand in for links.
function NewClaimsRunCard({ onStarted }: { onStarted: (id: string) => void }) {
  const [settings, setSettings] = useState<ClaimsSettings | null>(null);
  const [folderUrl, setFolderUrl] = useState("");
  const [listingUrl, setListingUrl] = useState("");
  const [receivedDate, setReceivedDate] = useState(today());
  const [instructions, setInstructions] = useState("");
  const [zip, setZip] = useState<File | null>(null);
  const [listingFile, setListingFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const zipInput = useRef<HTMLInputElement>(null);
  const listingInput = useRef<HTMLInputElement>(null);

  // Keep retrying until settings load: a hiccup at startup must not leave
  // the form dead forever. The playbook prefills the instructions.
  useEffect(() => {
    if (settings) return;
    let alive = true;
    const tick = () =>
      getClaimsSettings()
        .then((s) => {
          if (!alive) return;
          setSettings(s);
          setInstructions((cur) => cur || s.playbook);
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [settings]);

  const local = settings?.local_mode ?? false;
  const useZip = local && !!zip;
  const folderOk = useZip || /^https:\/\/\S+/.test(folderUrl.trim()) || (local && folderUrl.trim().startsWith("/"));
  const listingOk =
    !!listingFile || listingUrl.trim() === "" || /^https:\/\/\S+/.test(listingUrl.trim()) || (local && listingUrl.trim().startsWith("/"));
  const dateOk = /^\d{4}-\d{2}-\d{2}$/.test(receivedDate);
  const valid = folderOk && listingOk && dateOk && !busy;
  const why = !dateOk
    ? "The received date must be a full date"
    : !folderOk
      ? local
        ? "Paste the batch folder link, or choose a zip of the folder"
        : "Paste the batch folder link (starts with https://)"
      : !listingOk
        ? "The listing link must start with https://"
        : "";

  async function start() {
    setBusy(true);
    setError("");
    try {
      const { run_id } = await createClaimsRun({
        received_date: receivedDate,
        folder_url: useZip ? "" : folderUrl.trim(),
        listing_url: listingFile ? "" : listingUrl.trim(),
        instructions: instructions.trim(),
        batch: useZip ? zip : null,
        listing: listingFile,
      });
      onStarted(run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the run");
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <b>New claims run</b>
      <span className="sub">
        Give the batch folder (the folder that contains one subfolder per employee) and
        this month's Summary of Invoices. The agent maps the folder itself; you confirm the
        map with one click, then it verifies every claim row.
        {settings ? ` Client: ${settings.client}.` : ""}
      </span>
      <label className="editrow">
        <span>Batch folder link</span>
        <input
          value={folderUrl}
          disabled={useZip}
          placeholder={
            local
              ? "https://…sharepoint.com/sites/…/Claims/Jul26  (or a folder path on this machine)"
              : "https://…sharepoint.com/sites/…/Claims/Jul26  (copy from the browser)"
          }
          onChange={(e) => setFolderUrl(e.target.value)}
          aria-invalid={!folderOk}
        />
      </label>
      <label className="editrow">
        <span>This month's listing link</span>
        <input
          value={listingUrl}
          disabled={!!listingFile}
          placeholder="https://…/Summary of Invoices JUL26.xlsx  (its header row sets the column order)"
          onChange={(e) => setListingUrl(e.target.value)}
        />
      </label>
      <label className="editrow">
        <span>Received date</span>
        <input
          type="date"
          value={receivedDate}
          onChange={(e) => setReceivedDate(e.target.value)}
          aria-invalid={!dateOk}
        />
      </label>
      <label className="editrow" style={{ alignItems: "flex-start" }}>
        <span>Instructions for this client (optional)</span>
        <textarea
          rows={4}
          value={instructions}
          placeholder={EXAMPLE_INSTRUCTIONS}
          onChange={(e) => setInstructions(e.target.value)}
        />
      </label>
      {local && (
        <div className="row" style={{ marginTop: 6 }}>
          <span className="sub">Local development: upload instead of linking —</span>
          <input
            ref={zipInput}
            type="file"
            accept=".zip"
            style={{ display: "none" }}
            onChange={(e) => setZip(e.target.files?.[0] ?? null)}
          />
          <button className="btn" onClick={() => zipInput.current?.click()}>
            {zip ? `Zip: ${zip.name}` : "Choose batch zip"}
          </button>
          <input
            ref={listingInput}
            type="file"
            accept=".xlsx,.xlsm"
            style={{ display: "none" }}
            onChange={(e) => setListingFile(e.target.files?.[0] ?? null)}
          />
          <button className="btn" onClick={() => listingInput.current?.click()}>
            {listingFile ? `Listing: ${listingFile.name}` : "Choose listing workbook"}
          </button>
          {(zip || listingFile) && (
            <button
              className="btn"
              onClick={() => {
                setZip(null);
                setListingFile(null);
                if (zipInput.current) zipInput.current.value = "";
                if (listingInput.current) listingInput.current.value = "";
              }}
            >
              Clear files
            </button>
          )}
        </div>
      )}
      <div className="actions">
        <button className="btn primary" disabled={!valid} title={why} onClick={start}>
          {busy ? "Starting…" : "Start"}
        </button>
        {why && <span className="sub">{why}</span>}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
