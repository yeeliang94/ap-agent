import { useEffect, useRef, useState } from "react";
import {
  ClaimsSettings,
  createClaimsRun,
  getClaimsSettings,
} from "../api";
import Info from "../components/Info";

// The reviewer's LOCAL calendar day (toISOString would give the UTC day,
// which in Malaysia is yesterday until 08:00).
const today = () => {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

const EXAMPLE_INSTRUCTIONS =
  "Example: “Each employee has a folder named after them. The expense report is " +
  "the .xlsx; the tab called ‘Expense Report’ has the lines and the ‘KM’ tab has " +
  "the mileage trips. Receipts are scanned three to a page; the map screenshots " +
  "for mileage are at the back of the receipt PDF. Ignore *_Approval.pdf.”";

/** A local absolute path the backend can check against CLAIMS_LOCAL_ROOT. */
export function looksLikeLocalPath(value: string): boolean {
  const path = value.trim();
  return (
    path.startsWith("/") ||
    /^[A-Za-z]:[\\/]/.test(path) ||
    /^\\\\[^\\]+\\[^\\]+/.test(path)
  );
}

// ---- the batch upload -------------------------------------------------------

/** What the backend can read (mirrors claims/source.READABLE) plus a zip. */
const READABLE_EXT = [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".xlsx", ".xlsm"];
const MAX_UPLOAD_MB = 200;

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
}

function ftypeClass(name: string): string {
  const ext = extOf(name);
  if (ext === ".pdf") return "ftype pdf";
  if (ext === ".zip") return "ftype zip";
  if (ext === ".xlsx" || ext === ".xlsm") return "ftype xls";
  if ([".png", ".jpg", ".jpeg", ".webp"].includes(ext)) return "ftype img";
  return "ftype";
}

function sizeLabel(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export interface Picked {
  file: File;
  /** Relative path inside the batch ("A_1/receipts/grab.pdf"). */
  path: string;
}

/** Merge newly picked files into the current set. Pure, so the rules are
 *  testable: one zip travels alone; only readable types; two DIFFERENT
 *  files landing at one path are a named error (case folded — the staging
 *  filesystem may fold case), while re-picking the identical file is
 *  simply kept once. The server enforces the same rules again. */
export function mergePicked(
  current: Picked[],
  candidates: Picked[],
  maxMb = MAX_UPLOAD_MB
): { picked: Picked[]; error: string } {
  const next = [...current];
  for (const c of candidates) {
    const ext = extOf(c.file.name);
    const haveZip = next.some((p) => extOf(p.path) === ".zip");
    if (ext === ".zip" && next.length > 0) {
      return { picked: current, error: "A zip travels alone — clear the other files first." };
    }
    if (ext !== ".zip" && haveZip) {
      return { picked: current, error: "A zip travels alone — clear it before adding loose files." };
    }
    if (ext !== ".zip" && !READABLE_EXT.includes(ext)) {
      return {
        picked: current,
        error: `${c.file.name} isn't a supported type (PDF, PNG, JPG, WEBP, Excel — or one zip).`,
      };
    }
    const existing = next.find((p) => p.path.toLowerCase() === c.path.toLowerCase());
    if (existing) {
      if (existing.file.size === c.file.size && existing.file.lastModified === c.file.lastModified) {
        continue; // the same file picked again — keep it once
      }
      return {
        picked: current,
        error: `Two different files would land at "${c.path}" — rename one, or pick the folder itself so the paths stay distinct.`,
      };
    }
    next.push(c);
  }
  if (next.reduce((n, p) => n + p.file.size, 0) > maxMb * 1024 * 1024) {
    return { picked: current, error: `The files add up to more than the ${maxMb} MB limit per upload.` };
  }
  return { picked: next, error: "" };
}

/** Walk a dropped folder (webkitGetAsEntry) and collect its files. */
async function walkEntry(entry: FileSystemEntry, prefix: string, out: Picked[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((res, rej) =>
      (entry as FileSystemFileEntry).file(res, rej)
    );
    if (file.name.startsWith(".")) return; // .DS_Store and friends
    out.push({ file, path: prefix + file.name });
    return;
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  // readEntries returns batches of ≤100; keep reading until empty.
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((res, rej) =>
      reader.readEntries(res, rej)
    );
    if (!batch.length) return;
    for (const child of batch) await walkEntry(child, prefix + entry.name + "/", out);
  }
}

// The uploads-first start (redesign): drop or pick the month's batch — a
// folder, a zip, or loose files — then the optional listing workbook, the
// received date and the optional instructions paragraph.
export function NewClaimsRunCard({ onStarted }: { onStarted: (id: string) => void }) {
  const [settings, setSettings] = useState<ClaimsSettings | null>(null);
  const [picked, setPicked] = useState<Picked[]>([]);
  const [folderUrl, setFolderUrl] = useState("");
  const [listingUrl, setListingUrl] = useState("");
  const [useLink, setUseLink] = useState(false);
  const [receivedDate, setReceivedDate] = useState(today());
  const [instructions, setInstructions] = useState("");
  const [listingFile, setListingFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [over, setOver] = useState(false);
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
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
  const linkAllowed = settings?.sharepoint_source ?? false;
  const haveZip = picked.some((p) => extOf(p.path) === ".zip");
  const totalBytes = picked.reduce((n, p) => n + p.file.size, 0);

  function add(candidates: Picked[]) {
    const merged = mergePicked(picked, candidates);
    setError(merged.error);
    if (merged.error) return;
    setPicked(merged.picked);
    if (merged.picked.length) setUseLink(false);
  }

  function addFileList(list: FileList | null, withRelPaths: boolean) {
    if (!list) return;
    add(
      Array.from(list)
        .filter((f) => !f.name.startsWith("."))
        .map((f) => ({
          file: f,
          path: withRelPaths && f.webkitRelativePath ? f.webkitRelativePath : f.name,
        }))
    );
  }

  async function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setOver(false);
    const items = Array.from(e.dataTransfer.items || []);
    const entries = items
      .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
      .filter((x): x is FileSystemEntry => !!x);
    if (entries.some((en) => en.isDirectory)) {
      const out: Picked[] = [];
      try {
        for (const entry of entries) await walkEntry(entry, "", out);
      } catch {
        setError("Could not read the dropped folder — try 'Choose folder' instead.");
        return;
      }
      add(out);
    } else {
      addFileList(e.dataTransfer.files, false);
    }
  }

  const dateOk = /^\d{4}-\d{2}-\d{2}$/.test(receivedDate);
  const folderOk =
    (!useLink && picked.length > 0) ||
    (useLink &&
      ((linkAllowed && /^https:\/\/\S+/.test(folderUrl.trim())) ||
        (local && looksLikeLocalPath(folderUrl))));
  const listingOk =
    !!listingFile ||
    !useLink ||
    listingUrl.trim() === "" ||
    /^https:\/\/\S+/.test(listingUrl.trim()) ||
    (local && looksLikeLocalPath(listingUrl));
  const valid = folderOk && listingOk && dateOk && !busy;
  const why = !dateOk
    ? "The received date must be a full date"
    : !folderOk
      ? useLink
        ? "Paste the batch folder link (starts with https://)"
        : "Add the batch first — drop a folder, a zip, or files"
      : !listingOk
        ? "The listing link must start with https://"
        : "";

  async function start() {
    setBusy(true);
    setProgress(0);
    setError("");
    try {
      const { run_id } = await createClaimsRun(
        {
          received_date: receivedDate,
          folder_url: useLink ? folderUrl.trim() : "",
          listing_url: useLink && !listingFile ? listingUrl.trim() : "",
          instructions: instructions.trim(),
          batch: useLink ? [] : picked.map((p) => p.file),
          batch_paths: useLink ? [] : picked.map((p) => p.path),
          listing: listingFile,
        },
        setProgress
      );
      onStarted(run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the run");
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="hero" style={{ marginBottom: 12 }}>
        <div>
          <h1>New claims run</h1>
          <span className="sub">One run per monthly batch{settings ? ` · ${settings.client}` : ""}</span>
        </div>
      </div>

      <div className="card wstep">
        <div className="whead">
          <span className="wnum">1</span>
          <h3>
            Upload the batch
            <Info
              text={
                "A folder (subfolders are kept and explored by the agent), one zip of the folder, " +
                "or loose files. A flat dump also works. Checks run before any AI call: " +
                "1,500 files / 6,000 pages per run, 25 MB per file."
              }
            />
          </h3>
        </div>
        {!useLink && (
          <>
            <div
              className={over ? "dropzone over" : "dropzone"}
              onClick={() => filesInput.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setOver(true);
              }}
              onDragLeave={() => setOver(false)}
              onDrop={onDrop}
            >
              <b>Drop the claims batch here</b>
              <span className="sub">
                A folder, a zip, or files · PDF, images, Excel · up to {MAX_UPLOAD_MB} MB
              </span>
              <div className="dz-actions" onClick={(e) => e.stopPropagation()}>
                <button className="btn sm" type="button" onClick={() => folderInput.current?.click()}>
                  Choose folder
                </button>
                <button className="btn sm" type="button" onClick={() => filesInput.current?.click()}>
                  Choose files
                </button>
              </div>
            </div>
            <input
              ref={filesInput}
              type="file"
              multiple
              accept={[".zip", ...READABLE_EXT].join(",")}
              style={{ display: "none" }}
              onChange={(e) => {
                addFileList(e.target.files, false);
                e.target.value = "";
              }}
            />
            <input
              ref={folderInput}
              type="file"
              // Non-standard but universal: lets the reviewer pick a folder;
              // every file arrives with webkitRelativePath.
              {...({ webkitdirectory: "" } as Record<string, string>)}
              multiple
              style={{ display: "none" }}
              onChange={(e) => {
                addFileList(e.target.files, true);
                e.target.value = "";
              }}
            />
            {picked.length > 0 && (
              <div className="filelist">
                {picked.slice(0, 8).map((p) => (
                  <div className="f" key={p.path}>
                    <span className={ftypeClass(p.path)}>{extOf(p.path).slice(1).toUpperCase() || "?"}</span>
                    <span>{p.path.split("/").pop()}</span>
                    <span className="path">{p.path.includes("/") ? p.path.slice(0, p.path.lastIndexOf("/") + 1) : ""}</span>
                    <span className="sz">{sizeLabel(p.file.size)}</span>
                    <button
                      className="btn ghost sm"
                      aria-label={`Remove ${p.path}`}
                      onClick={() => setPicked(picked.filter((q) => q.path !== p.path))}
                    >
                      ✕
                    </button>
                  </div>
                ))}
                {picked.length > 8 && (
                  <div className="f">
                    <span className="path">… and {picked.length - 8} more</span>
                  </div>
                )}
                <div className="sum">
                  <span>
                    {picked.length} file{picked.length === 1 ? "" : "s"} · {sizeLabel(totalBytes)}
                  </span>
                  <button className="btn ghost sm" onClick={() => setPicked([])}>
                    Clear all
                  </button>
                </div>
              </div>
            )}
          </>
        )}
        {useLink && (
          <>
            <label className="editrow">
              <span>Batch folder link</span>
              <input
                value={folderUrl}
                placeholder={
                  local
                    ? "https://…sharepoint.com/sites/…/Claims/Jul26  (or a folder path on this machine)"
                    : "https://…sharepoint.com/sites/…/Claims/Jul26"
                }
                onChange={(e) => setFolderUrl(e.target.value)}
                aria-invalid={!folderOk}
              />
            </label>
            <label className="editrow">
              <span>Listing link</span>
              <input
                value={listingUrl}
                disabled={!!listingFile}
                placeholder="https://…/Summary of Invoices JUL26.xlsx"
                onChange={(e) => setListingUrl(e.target.value)}
              />
            </label>
          </>
        )}
        {/* Always shown while a link mode is possible — the toggle must
            not vanish under the reviewer while useLink is on. */}
        {(linkAllowed || local) && (
          <div className="switchrow" style={{ paddingBottom: 0 }}>
            <div className="grow">
              <b style={{ color: "var(--muted)" }}>
                {useLink ? "Upload files instead" : "Link a folder instead"}
              </b>
              <Info
                text={
                  linkAllowed
                    ? "A SharePoint folder link (the folder that contains the employee subfolders) instead of an upload."
                    : "In local mode, a folder path on this machine under CLAIMS_LOCAL_ROOT also works."
                }
              />
            </div>
            <button
              className="btn sm"
              onClick={() => {
                setUseLink(!useLink);
                setError("");
              }}
            >
              {useLink ? "Use uploads" : "Use a link"}
            </button>
          </div>
        )}
      </div>

      <div className={listingFile ? "card wstep" : "card wstep opt"}>
        <div className="whead" style={{ marginBottom: 8 }}>
          <span className="wnum">2</span>
          <h3>
            Client's summary <span className="opt-tag">optional</span>
            <Info
              text={
                "The Summary of Invoices workbook (.xlsx), if the client sent one. Its header row " +
                "sets the column order of the output. Left empty, the agent builds the listing " +
                "from the files alone."
              }
            />
          </h3>
        </div>
        <div className="row">
          <input
            ref={listingInput}
            type="file"
            accept=".xlsx,.xlsm"
            style={{ display: "none" }}
            onChange={(e) => setListingFile(e.target.files?.[0] ?? null)}
          />
          <button className="btn sm" onClick={() => listingInput.current?.click()}>
            {listingFile ? "Change workbook" : "Choose workbook"}
          </button>
          {listingFile && (
            <>
              <span className="sub">
                {listingFile.name} · {sizeLabel(listingFile.size)}
              </span>
              <button className="btn ghost sm" onClick={() => setListingFile(null)}>
                Remove
              </button>
            </>
          )}
        </div>
      </div>

      <div className="card wstep">
        <div className="whead" style={{ marginBottom: 8 }}>
          <span className="wnum">3</span>
          <h3>Date &amp; instructions</h3>
        </div>
        <label className="editrow">
          <span>
            Received date
            <Info text="Stamped on every listing row this run produces." />
          </span>
          <input
            type="date"
            value={receivedDate}
            onChange={(e) => setReceivedDate(e.target.value)}
            aria-invalid={!dateOk}
          />
        </label>
        <label className="editrow" style={{ alignItems: "flex-start" }}>
          <span>
            Instructions <span className="opt-tag">optional</span>
            <Info text="A short paragraph about this client's oddities. The saved playbook is prefilled." />
          </span>
          <textarea
            rows={3}
            value={instructions}
            placeholder={EXAMPLE_INSTRUCTIONS}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </label>
      </div>

      {busy && progress > 0 && progress < 1 && (
        <div className="bar" aria-label="Upload progress">
          <div className="bar-fill" style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
      )}
      <div className="actions" style={{ marginTop: 4 }}>
        <button className="btn primary" disabled={!valid} title={why} onClick={start}>
          {busy ? (progress > 0 && progress < 1 ? `Uploading ${Math.round(progress * 100)}%…` : "Starting…") : "Start claims run"}
        </button>
        {why && <span className="sub">{why}</span>}
        {error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}
