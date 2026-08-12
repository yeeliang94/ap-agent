import { useEffect, useRef, useState } from "react";
import {
  getSettings,
  listRuns,
  saveSettings,
  uploadBatch,
  AppSettings,
  RunSummary,
} from "../api";

const STAGE_LABEL: Record<string, string> = {
  queued: "Queued",
  sorting: "Sorting documents",
  extracting: "Reading documents",
  checking: "Running checks",
  ready: "Ready for review",
  failed: "Failed",
};

// Screen A: your runs. A run starts only when you upload a batch here.
export default function RunsList({ onOpen }: { onOpen: (id: string) => void }) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  // One client at a time, set on screen (Settings below) — the backend
  // rejects uploads for any other name.
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const client = settings?.client_name ?? "";
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  // Keep retrying until settings load: a hiccup at startup must not leave
  // the upload button dead forever. (The runs poll below reports errors.)
  useEffect(() => {
    if (settings) return;
    let alive = true;
    const tick = () =>
      getSettings().then((s) => alive && setSettings(s)).catch(() => {});
    tick();
    const t = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [settings]);

  // Poll every 3s so progress chips move while a run is working.
  useEffect(() => {
    let alive = true;
    const tick = () =>
      listRuns()
        .then((r) => alive && setRuns(r))
        .catch(() => alive && setError("Backend not reachable"));
    tick();
    const t = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  async function onFile(file: File) {
    setBusy(true);
    setError("");
    try {
      await uploadBatch(client, file);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <section>
      <div className="card uploader">
        <div>
          <b>Start a new run</b>
          <span className="sub">Upload a client's batch zip — nothing runs until you do</span>
        </div>
        {/* The batch is checked against THIS client's policy and listing.
            Change it in Settings below — the backend rejects other names. */}
        <input value={client} readOnly title="Set in Settings below" />
        <input
          ref={fileInput}
          type="file"
          accept=".zip"
          style={{ display: "none" }}
          onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
        />
        <button
          className="btn primary"
          disabled={busy || !client.trim()}
          onClick={() => fileInput.current?.click()}
        >
          {busy ? "Uploading…" : "Upload batch"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}

      {runs.map((r) => (
        <div key={r.id} className="card row">
          <div className="grow">
            <b>{r.client}</b>
            <span className="sub">
              {r.documents_total} documents · {new Date(r.created_at).toLocaleString()}
            </span>
          </div>
          {r.status === "ready" ? (
            <>
              <span className={`chip ${r.open_flags ? "review" : "ok"}`}>
                {r.open_flags ? `${r.open_flags} flags to review` : "All flags resolved"}
              </span>
              <button className="btn primary" onClick={() => onOpen(r.id)}>
                Open
              </button>
            </>
          ) : r.status === "failed" ? (
            <span className="chip flag" title={r.error}>Failed — {r.error.slice(0, 60)}</span>
          ) : (
            <span className="chip wait">
              {STAGE_LABEL[r.status] ?? r.status}
              {r.progress?.total ? ` ${r.progress.done}/${r.progress.total}` : ""}
            </span>
          )}
        </div>
      ))}
      {runs.length === 0 && !error && <p className="sub">No runs yet.</p>}

      {settings && <SettingsCard current={settings} onSaved={setSettings} />}
    </section>
  );
}

// The two values a reviewer owns: whose policy applies, and where the
// reference files (payment listing, policy sheet, bank template) live.
// Secrets — API keys, the proxy, the MCP endpoint — stay in .env, set by IT.
function SettingsCard({
  current,
  onSaved,
}: {
  current: AppSettings;
  onSaved: (s: AppSettings) => void;
}) {
  const [clientName, setClientName] = useState(current.client_name);
  const [folderUrl, setFolderUrl] = useState(current.sharepoint_folder_url);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const dirty =
    clientName !== current.client_name || folderUrl !== current.sharepoint_folder_url;

  async function save() {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      onSaved(
        await saveSettings({ client_name: clientName, sharepoint_folder_url: folderUrl })
      );
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save settings");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card settings">
      <b>Settings</b>
      <span className="sub">
        Which client's policy applies, and the SharePoint folder holding the three
        reference files. Keys and connection secrets stay in .env.
      </span>
      <label className="editrow">
        <span>client name</span>
        <input value={clientName} onChange={(e) => setClientName(e.target.value)} />
      </label>
      <label className="editrow">
        <span>SharePoint folder URL</span>
        <input
          value={folderUrl}
          placeholder="https://…sharepoint.com/sites/…  (copy from the browser)"
          onChange={(e) => setFolderUrl(e.target.value)}
        />
      </label>
      <div className="actions">
        <button className="btn primary" disabled={busy || !dirty} onClick={save}>
          {busy ? "Saving…" : "Save settings"}
        </button>
        {saved && !dirty && <span className="sub">Saved — applies to the next run.</span>}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
