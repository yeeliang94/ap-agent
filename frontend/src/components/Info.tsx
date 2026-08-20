// ⓘ — the redesign's home for helper text: labels stay quiet, the
// explanation appears on hover (and for screen readers via aria-label).
export default function Info({ text }: { text: string }) {
  return (
    <i className="info" title={text} aria-label={text} role="img">
      i
    </i>
  );
}
