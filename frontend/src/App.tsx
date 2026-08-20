import { useState } from "react";
import RunsList from "./screens/RunsList";
import RunDetail from "./screens/RunDetail";
import ClaimsList from "./screens/ClaimsList";
import ClaimsRunDetailScreen from "./screens/ClaimsRunDetail";
import SettingsScreen from "./screens/SettingsScreen";

type Section = "runs" | "claims" | "settings";

// The shell (redesign variant B): a top bar with the brand, the two run
// types as centered tabs, and Settings always reachable on the right.
// Each run type has a list and a detail screen.
export default function App() {
  const [section, setSection] = useState<Section>("runs");
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [openClaimsRunId, setOpenClaimsRunId] = useState<string | null>(null);

  const open =
    section === "runs" ? openRunId : section === "claims" ? openClaimsRunId : null;
  const closeAll = () => {
    setOpenRunId(null);
    setOpenClaimsRunId(null);
  };
  const go = (s: Section) => {
    setSection(s);
    closeAll();
  };

  return (
    <>
      <header className="topnav">
        <div className="brand" onClick={() => go("runs")}>
          <span className="logo">AP</span>AP Agent
        </div>
        <nav className="tabs" aria-label="Sections">
          <button
            className={section === "runs" ? "tab active" : "tab"}
            onClick={() => setSection("runs")}
          >
            Invoice runs
          </button>
          <button
            className={section === "claims" ? "tab active" : "tab"}
            onClick={() => setSection("claims")}
          >
            Claims
          </button>
        </nav>
        <button
          className="btn ghost sm"
          style={section === "settings" ? { color: "var(--ink)" } : undefined}
          onClick={() => setSection("settings")}
        >
          Settings
        </button>
      </header>
      <main className="shell">
        {open && (
          <button className="btn sm" style={{ marginBottom: 12 }} onClick={closeAll}>
            ← All {section === "runs" ? "runs" : "claims runs"}
          </button>
        )}
        {section === "runs" &&
          (openRunId ? <RunDetail runId={openRunId} /> : <RunsList onOpen={setOpenRunId} />)}
        {section === "claims" &&
          (openClaimsRunId ? (
            <ClaimsRunDetailScreen runId={openClaimsRunId} />
          ) : (
            <ClaimsList onOpen={setOpenClaimsRunId} />
          ))}
        {section === "settings" && <SettingsScreen />}
      </main>
    </>
  );
}
