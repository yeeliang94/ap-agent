import { useEffect, useRef, useState } from "react";
import {
  connectSharePoint,
  disconnectSharePoint,
  getSettings,
  getSharePointStatus,
  listRuns,
  uploadBatch,
  AppSettings,
  RunSummary,
  SharePointStatus,
} from "../api";
import Info from "../components/Info";

const STAGE_LABEL: Record<string, string> = {
  queued: "Queued",
  sorting: "Sorting documents",
  extracting: "Reading documents",
  checking: "Running checks",
  ready: "Ready for review",
  failed: "Failed",
};

// Screen A: your invoice runs. A run starts only when you upload a batch
// here. The client name and other settings live in the Settings section.
export default function RunsList({ onOpen }: { onOpen: (id: string) => void }) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  // One client at a time, set in Settings — the backend rejects uploads
  // for any other name.
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
      <div className="hero">
        <div>
          <h1>Invoice runs</h1>
          <span className="sub">
            One run per uploaded invoice batch{client ? ` · ${client}` : ""}
            <Info text="The batch is checked against this client's policy and listing. Change the client in Settings — the backend rejects other names." />
          </span>
        </div>
        <div className="row">
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
      </div>
      <SharePointCard />
      {error && <p className="error">{error}</p>}

      {runs.map((r) => (
        <div key={r.id} className={`card runcard ${r.status === "failed" ? "banner bad" : ""}`}>
          <div className="grow">
            <b>{r.client}</b>
            <span className="sub">
              {r.documents_total} documents · {new Date(r.created_at).toLocaleString()}
            </span>
            {/* A failed run's reason belongs on the row, not hidden in a
                tooltip — it is the whole story of that run. */}
            {r.status === "failed" && r.error && <span className="sub error">{r.error}</span>}
          </div>

          {/* Shown for EVERY status. A run can finish "ready" with errors
              recorded against it, and that is exactly the combination
              that used to read as a clean run. */}
          {r.errors > 0 && (
            <span className="chip flag">
              {r.errors} error{r.errors === 1 ? "" : "s"}
            </span>
          )}
          {r.errors === 0 && r.warnings > 0 && (
            <span className="chip review">
              {r.warnings} warning{r.warnings === 1 ? "" : "s"}
            </span>
          )}

          {r.status === "ready" && (
            <span className={`chip ${r.open_flags ? "review" : "ok"}`}>
              {r.open_flags ? `${r.open_flags} flags to review` : "All flags resolved"}
            </span>
          )}
          {r.status !== "ready" && r.status !== "failed" && (
            <span className="chip wait">
              {STAGE_LABEL[r.status] ?? r.status}
              {r.progress?.total ? ` ${r.progress.done}/${r.progress.total}` : ""}
            </span>
          )}

          {/* Failed runs open too. They are the ones whose activity log
              matters most, and it used to be unreachable: no button, so
              no way to see why the run died. */}
          {(r.status === "ready" || r.status === "failed") && (
            <button
              className={r.status === "failed" ? "btn warn" : "btn primary"}
              onClick={() => onOpen(r.id)}
            >
              {r.status === "failed" ? "See what failed" : "Open"}
            </button>
          )}
        </div>
      ))}
      {runs.length === 0 && !error && (
        <div className="card">
          <div className="empty">
            <b>No invoice runs yet</b>
            Upload a batch zip above to start one.
          </div>
        </div>
      )}
    </section>
  );
}

// Signing in to SharePoint. Deliberately a button and not something that
// happens on its own: the sign-in opens a real browser window, and a
// background run must never do that — nobody would be watching. Connect
// once here, and every later run reuses it silently.
function SharePointCard() {
  const [status, setStatus] = useState<SharePointStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [who, setWho] = useState("");

  useEffect(() => {
    let alive = true;
    getSharePointStatus()
      .then((s) => alive && setStatus(s))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // Nothing to show when this deployment does not use delegated sign-in
  // (local development, and any gateway that only wants its API key).
  if (!status?.required) return null;

  async function connect() {
    setBusy(true);
    setError("");
    try {
      const { signed_in_as } = await connectSharePoint();
      setWho(signed_in_as);
      setStatus(await getSharePointStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not connect");
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    setError("");
    try {
      await disconnectSharePoint();
      setWho("");
      setStatus(await getSharePointStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not disconnect");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`card row ${status.connected ? "" : "banner bad"}`}>
      <div className="grow">
        <b>SharePoint {status.connected ? "connected" : "not connected"}</b>
        <span className="sub">
          {status.connected
            ? `Signed in${who ? ` as ${who}` : ""}. Runs will read the reference folder on your behalf.`
            : "Runs cannot read the reference folder until you sign in. A browser window will open."}
        </span>
      </div>
      {status.connected ? (
        <button className="btn" disabled={busy} onClick={disconnect}>
          {busy ? "Working…" : "Disconnect"}
        </button>
      ) : (
        <button className="btn primary" disabled={busy} onClick={connect}>
          {busy ? "Waiting for sign-in…" : "Connect SharePoint"}
        </button>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
