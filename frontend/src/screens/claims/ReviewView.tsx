import { ClaimsRunDetail } from "../../api";

// Placeholder until Step 11: the Review view (flag cards grouped by
// employee, evidence previews, accept / exclude / fix / re-verify).
export default function ReviewView({ run }: { run: ClaimsRunDetail; onChanged: () => void }) {
  return <p className="sub">Review is built in Step 11. {run.flags.length} flag(s) recorded so far.</p>;
}
