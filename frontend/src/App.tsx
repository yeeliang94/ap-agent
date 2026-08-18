import { useState } from "react";
import RunsList from "./screens/RunsList";
import RunDetail from "./screens/RunDetail";
import ClaimsList from "./screens/ClaimsList";
import ClaimsRunDetailScreen from "./screens/ClaimsRunDetail";

type Section = "runs" | "claims";

// Two run types, two tabs: the invoice pipeline (Runs) and the employee
// claims module (Claims). Each has a list and a detail screen.
export default function App() {
  const [section, setSection] = useState<Section>("runs");
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [openClaimsRunId, setOpenClaimsRunId] = useState<string | null>(null);

  const open = section === "runs" ? openRunId : openClaimsRunId;
  const closeAll = () => {
    setOpenRunId(null);
    setOpenClaimsRunId(null);
  };

  return (
    <main className="shell">
      <header className="topbar">
        <h1 onClick={closeAll}>AP Assistant</h1>
        <nav className="tabs" aria-label="Sections">
          <button
            className={section === "runs" ? "tab active" : "tab"}
            onClick={() => setSection("runs")}
          >
            Runs
          </button>
          <button
            className={section === "claims" ? "tab active" : "tab"}
            onClick={() => setSection("claims")}
          >
            Claims
          </button>
        </nav>
        {open && (
          <button className="btn" onClick={closeAll}>
            ← All {section === "runs" ? "runs" : "claims runs"}
          </button>
        )}
      </header>
      {section === "runs" &&
        (openRunId ? <RunDetail runId={openRunId} /> : <RunsList onOpen={setOpenRunId} />)}
      {section === "claims" &&
        (openClaimsRunId ? (
          <ClaimsRunDetailScreen runId={openClaimsRunId} />
        ) : (
          <ClaimsList onOpen={setOpenClaimsRunId} />
        ))}
    </main>
  );
}
