import { useEffect, useRef, useState } from "react";
import { listRuns, uploadBatch, RunSummary } from "../api";

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
  const [client, setClient] = useState("Client ABC");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

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
        <input
          value={client}
          onChange={(e) => setClient(e.target.value)}
          placeholder="Client name"
        />
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
    </section>
  );
}
