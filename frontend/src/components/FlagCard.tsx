import { ReactNode } from "react";

// A generic flag card: the code, the plain reason, the basis (the rule
// and where it came from), a slot for the evidence preview and a slot
// for the actions. Both run types can use it; what goes in the slots is
// theirs.
export default function FlagCard({
  code,
  title,
  subtitle,
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
        {info && <span className="chip wait">note</span>}
      </div>
      <p className="reason">{reason}</p>
      {basis && <p className="basis">Basis: {basis}</p>}
      {evidence}
      {actions}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
