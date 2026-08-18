import { useState } from "react";

// A block of copy-ready text with a Copy button and a short preview.
// Nothing is written to the reviewer's files: they paste it themselves.
export default function CopyBlock({
  title,
  hint,
  text,
  preview,
}: {
  title: string;
  hint: string;
  text: string;
  preview: string[];
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <div className="card copyblock">
      <div className="row" style={{ border: "none", padding: 0 }}>
        <div className="grow">
          <b>{title}</b>
          <span className="sub">{hint}</span>
        </div>
        <button className="btn primary" onClick={copy}>
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
      <pre>
        {preview.slice(0, 4).join("\n")}
        {preview.length > 4 ? `\n… ${preview.length - 4} more` : ""}
      </pre>
    </div>
  );
}
