import { useState } from "react";
import RunsList from "./screens/RunsList";
import RunDetail from "./screens/RunDetail";

// Two views: the runs dashboard, or one run opened for review/output.
export default function App() {
  const [openRunId, setOpenRunId] = useState<string | null>(null);

  return (
    <main className="shell">
      <header className="topbar">
        <h1 onClick={() => setOpenRunId(null)}>AP Assistant</h1>
        {openRunId && (
          <button className="btn" onClick={() => setOpenRunId(null)}>
            ← All runs
          </button>
        )}
      </header>
      {openRunId ? (
        <RunDetail runId={openRunId} />
      ) : (
        <RunsList onOpen={setOpenRunId} />
      )}
    </main>
  );
}
