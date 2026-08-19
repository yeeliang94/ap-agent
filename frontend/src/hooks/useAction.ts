import { useCallback, useRef, useState } from "react";
import { StaleRunError } from "../api";

// ONE way to run a reviewer action against the server, shared by every
// claims screen, so the stale-run contract cannot be got wrong:
//
//   - the action is awaited, then the run is RELOADED (onChanged) and the
//     reload is awaited before `busy` clears — the screen never shows an
//     idle button over a run it has not yet re-read;
//   - a StaleRunError (409: another screen changed the run) reloads first,
//     then shows its message, so "it has been reloaded; please try again"
//     is true by the time it is read;
//   - any other failure keeps the run as it was and shows the message.
//
// `busy` is the key of the action in flight ("" when idle) so a screen with
// many buttons can mark just the one pressed; `run` resolves true on
// success, false on failure (for callers that close an editor only on
// success).

export type Reload = () => Promise<unknown> | void;

export interface Action {
  /** "" when idle, else the key the running action was started with. */
  busy: string;
  error: string;
  run: (fn: () => Promise<unknown>, opts?: { key?: string; fallback?: string }) => Promise<boolean>;
  clearError: () => void;
}

/** What the screen should say about a failed action: a stale run reloads the
 *  screen first; anything else is shown as it is. Exported for code paths
 *  that keep their own error state (FieldEditor). */
export async function explainFailure(e: unknown, fallback: string, reload?: Reload): Promise<string> {
  if (e instanceof StaleRunError) {
    try {
      await reload?.();
    } catch {
      /* the reload's own failure is reported by the screen that owns it */
    }
    return e.message;
  }
  return e instanceof Error ? e.message : fallback;
}

export function useAction(onChanged: Reload, defaultFallback = "The action failed"): Action {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // The latest reload, without making `run` a new function on each render.
  const reloadRef = useRef(onChanged);
  reloadRef.current = onChanged;

  const run = useCallback(
    async (fn: () => Promise<unknown>, opts?: { key?: string; fallback?: string }): Promise<boolean> => {
      setBusy(opts?.key ?? "action");
      setError("");
      try {
        await fn();
        try {
          await reloadRef.current();
        } catch {
          /* the reload's own failure is reported by the screen that owns it */
        }
        return true;
      } catch (e) {
        setError(await explainFailure(e, opts?.fallback ?? defaultFallback, reloadRef.current));
        return false;
      } finally {
        setBusy("");
      }
    },
    [defaultFallback]
  );

  const clearError = useCallback(() => setError(""), []);
  return { busy, error, run, clearError };
}
