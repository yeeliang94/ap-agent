// ⓘ — the redesign's home for helper text: labels stay quiet, the
// explanation appears on hover OR keyboard focus (a CSS tooltip, not the
// browser's title attribute, which keyboard and touch users never see).
// Screen readers get the text via aria-label.
export default function Info({ text }: { text: string }) {
  return (
    <i className="info" tabIndex={0} data-tip={text} aria-label={text} role="note">
      i
    </i>
  );
}
