// A number the reviewer reads at a glance (a total, a reconciliation).
export default function TotalCard({
  label,
  value,
  good,
}: {
  label: string;
  value: string;
  good?: boolean;
}) {
  return (
    <div className={`card total ${good ? "good" : ""}`}>
      <span className="sub">{label}</span>
      <b>{value}</b>
    </div>
  );
}
