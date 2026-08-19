import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { StaleRunError } from "../api";
import { explainFailure, Reload, useAction } from "./useAction";

// The stale-run contract, in one place. Every claims screen mutates
// through this hook, so these tests are what stands between the reviewer
// and the bug the review found: a 409 whose message SAYS the screen has
// been reloaded while nothing reloaded it, so every retry 409s again.

/** A promise a test can settle by hand, to watch what happens meanwhile. */
function deferred<T = void>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function Probe({ onChanged, fn }: { onChanged: Reload; fn: () => Promise<unknown> }) {
  const action = useAction(onChanged, "The action failed");
  return (
    <div>
      <span data-testid="busy">{action.busy}</span>
      <span data-testid="error">{action.error}</span>
      <button onClick={() => action.run(fn, { key: "save" })}>go</button>
    </div>
  );
}

const busy = () => screen.getByTestId("busy").textContent;
const error = () => screen.getByTestId("error").textContent;

async function press() {
  await act(async () => {
    screen.getByText("go").click();
  });
}

describe("explainFailure", () => {
  it("RELOADS on a stale run before returning the message that says so", async () => {
    const order: string[] = [];
    const reload = vi.fn(async () => { order.push("reload"); });
    const message = await explainFailure(new StaleRunError(), "fallback", reload);
    order.push("message");
    expect(reload).toHaveBeenCalledTimes(1);
    // The reload has to have finished FIRST: the message promises the
    // reviewer a screen that is already up to date.
    expect(order).toEqual(["reload", "message"]);
    expect(message).toContain("it has been reloaded");
  });

  it("waits for a slow reload rather than racing it", async () => {
    const gate = deferred();
    let finished = false;
    const reload = vi.fn(() => gate.promise);
    const p = explainFailure(new StaleRunError(), "fallback", reload).then(() => { finished = true; });
    await Promise.resolve();
    expect(finished).toBe(false);
    gate.resolve();
    await p;
    expect(finished).toBe(true);
  });

  it("does not reload for an ordinary failure, and shows the server's words", async () => {
    const reload = vi.fn();
    expect(await explainFailure(new Error("The row is already excluded"), "fallback", reload))
      .toBe("The row is already excluded");
    expect(reload).not.toHaveBeenCalled();
  });

  it("falls back where the failure is not an Error at all", async () => {
    expect(await explainFailure("boom", "Could not do it")).toBe("Could not do it");
  });

  it("still shows the stale message when the reload itself fails", async () => {
    const reload = vi.fn(async () => { throw new Error("offline"); });
    expect(await explainFailure(new StaleRunError(), "fallback", reload)).toContain("please try again");
  });
});

describe("useAction", () => {
  it("reloads the run after a successful action", async () => {
    const reload = vi.fn(async () => {});
    const fn = vi.fn(async () => ({ ok: true }));
    render(<Probe onChanged={reload} fn={fn} />);
    await press();
    expect(fn).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(busy()).toBe("");
    expect(error()).toBe("");
  });

  it("keeps `busy` set until the RELOAD has landed, not just the action", async () => {
    const gate = deferred();
    const reload = vi.fn(() => gate.promise);
    render(<Probe onChanged={reload} fn={async () => ({})} />);
    await press();
    // The action is done, the reload is not: the buttons must stay held,
    // or the reviewer presses again against the run's old revision.
    expect(busy()).toBe("save");
    await act(async () => { gate.resolve(); });
    expect(busy()).toBe("");
  });

  it("reloads on a stale run and then says so", async () => {
    const reload = vi.fn(async () => {});
    render(<Probe onChanged={reload} fn={async () => { throw new StaleRunError(); }} />);
    await press();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(error()).toContain("it has been reloaded");
    expect(busy()).toBe("");
  });

  it("leaves the run alone on any other failure, and shows the reason", async () => {
    const reload = vi.fn(async () => {});
    render(<Probe onChanged={reload} fn={async () => { throw new Error("Only a run that is still working can be cancelled"); }} />);
    await press();
    expect(reload).not.toHaveBeenCalled();
    expect(error()).toBe("Only a run that is still working can be cancelled");
  });

  it("clears the previous error when the next attempt starts", async () => {
    let fail = true;
    const fn = vi.fn(async () => { if (fail) throw new Error("nope"); return {}; });
    render(<Probe onChanged={async () => {}} fn={fn} />);
    await press();
    expect(error()).toBe("nope");
    fail = false;
    await press();
    expect(error()).toBe("");
  });

  it("reports success or failure to the caller, so an editor closes only on success", async () => {
    const results: boolean[] = [];
    function Caller() {
      const action = useAction(async () => {});
      return (
        <button onClick={async () => { results.push(await action.run(async () => { throw new Error("no"); })); }}>
          go
        </button>
      );
    }
    render(<Caller />);
    await press();
    expect(results).toEqual([false]);
  });

  it("survives a reload that throws — the failure belongs to the screen that owns it", async () => {
    const reload = vi.fn(async () => { throw new Error("offline"); });
    render(<Probe onChanged={reload} fn={async () => ({})} />);
    await press();
    expect(busy()).toBe("");
    expect(error()).toBe("");
  });
});
