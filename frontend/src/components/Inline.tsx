import { useState } from "react";

// Questions asked IN PLACE, never through window.prompt / window.confirm:
// a browser dialog cannot be styled, cannot be read by the screen around
// it, blocks the whole tab, and gives no way to require an answer. Both
// controls keep the reviewer inside the page, and the round-trip is made
// only once the answer is there.

/** A one-line inline question with a required text answer (a reason for
 *  the audit trail, a name for a case): the confirm button stays disabled
 *  until something is typed, so the server is never asked with a blank. */
export function InlineReason({ prompt, placeholder, initial, confirmLabel, busy, onConfirm, onCancel }: {
  prompt: string;
  placeholder?: string;
  initial: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const ok = value.trim().length > 0;
  return (
    <form className="actions" style={{ marginTop: 4 }}
      onSubmit={(ev) => { ev.preventDefault(); if (ok && !busy) onConfirm(value.trim()); }}>
      <span className="sub" style={{ flexBasis: "100%" }}>{prompt}</span>
      <input aria-label={prompt} value={value} placeholder={placeholder} autoFocus disabled={busy}
        onChange={(ev) => setValue(ev.target.value)} />
      <button type="submit" className="btn primary" disabled={busy || !ok} title={ok ? "" : "Type something first"}>{confirmLabel}</button>
      <button type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
    </form>
  );
}

/** A yes / no asked in place (a merge, a cancel): the action runs only on
 *  the yes. */
export function InlineConfirm({ question, confirmLabel, busy, onConfirm, onCancel }: {
  question: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="actions" role="group" aria-label={question} style={{ marginTop: 4 }}>
      <span className="sub" style={{ flexBasis: "100%" }}>{question}</span>
      <button type="button" className="btn warn" disabled={busy} onClick={onConfirm}>{confirmLabel}</button>
      <button type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
    </div>
  );
}
