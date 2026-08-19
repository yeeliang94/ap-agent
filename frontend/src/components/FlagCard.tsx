import { ReactNode } from "react";

// A generic flag card: a plain title (never a bare code), what it means,
// the reason found on THIS row, the basis (the rule and where it came
// from), what to do, the amount at stake, a slot for the evidence preview
// and a slot for the actions. Both run types can use it; what goes in
// the slots is theirs.
export default function FlagCard({
  code,
  title,
  subtitle,
  meaning,
  whatToDo,
  stake,
  reason,
  basis,
  info,
  evidence,
  actions,
  error,
}: {
  code: string;
  title?: string;
  subtitle?: string;
  /** One sentence: what this kind of flag means (from the catalogue). */
  meaning?: string;
  /** One sentence: the reviewer's move (from the catalogue). */
  whatToDo?: string;
  /** "RM 45.00" — the amount a decision here affects. */
  stake?: string;
  reason: string;
  basis?: string;
  /** An informational note (never blocks) rather than a decision to make. */
  info?: boolean;
  evidence?: ReactNode;
  actions?: ReactNode;
  error?: string;
}) {
  return (
    <div className={`card flagcard ${info ? "info" : ""}`}>
      <div className="row" style={{ border: "none", padding: 0 }}>
        <div className="grow">
          <b>{title ?? code.replaceAll("_", " ")}</b>
          {subtitle && <span className="sub">{subtitle}</span>}
        </div>
        {stake && <span className="chip review" title="the amount this decision affects">{stake} at stake</span>}
        {info && <span className="chip wait">note</span>}
      </div>
      {meaning && <p className="sub" style={{ marginTop: 6 }}>{meaning}</p>}
      <p className="reason">{reason}</p>
      {basis && <p className="basis">Basis: {basis}</p>}
      {evidence}
      {whatToDo && (
        <p className="sub" style={{ marginTop: 8 }}>
          <b style={{ display: "inline" }}>What to do: </b>
          {whatToDo}
        </p>
      )}
      {actions}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
