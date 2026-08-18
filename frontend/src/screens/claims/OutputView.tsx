import { ClaimsRunDetail } from "../../api";

// Placeholder until Step 13: totals, reconciliation, copy block.
export default function OutputView({ run }: { run: ClaimsRunDetail; onGoReview: () => void }) {
  return <p className="sub">Output is built in Step 13. Status: {run.status}.</p>;
}
