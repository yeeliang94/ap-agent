import { describe, expect, it } from "vitest";
import { makeRunSummary } from "../test/fixtures";
import { claimsStatusLabel, runDestination } from "../runPresentation";
import { looksLikeLocalPath, mergePicked, Picked } from "./ClaimsList";

function picked(path: string, opts: { size?: number; mtime?: number; name?: string } = {}): Picked {
  const name = opts.name ?? path.split("/").pop()!;
  return {
    path,
    file: new File([new Uint8Array(opts.size ?? 10)], name, { lastModified: opts.mtime ?? 1000 }),
  };
}

describe("mergePicked — the upload's client-side rules", () => {
  it("keeps a folder tree and appends new files", () => {
    const first = mergePicked([], [picked("A_1/report.xlsx"), picked("A_1/receipts/grab.pdf")]);
    expect(first.error).toBe("");
    const second = mergePicked(first.picked, [picked("B_2/toll.png")]);
    expect(second.picked.map((p) => p.path)).toEqual([
      "A_1/report.xlsx", "A_1/receipts/grab.pdf", "B_2/toll.png",
    ]);
  });

  it("lets a zip travel only alone", () => {
    expect(mergePicked([picked("a.pdf")], [picked("b.zip")]).error).toContain("zip travels alone");
    expect(mergePicked([picked("b.zip")], [picked("a.pdf")]).error).toContain("zip travels alone");
    expect(mergePicked([], [picked("b.zip")]).error).toBe("");
  });

  it("names an unsupported type", () => {
    expect(mergePicked([], [picked("photo.heic")]).error).toContain("photo.heic isn't a supported type");
  });

  it("keeps an identical re-pick once, but refuses two different files at one path", () => {
    const once = mergePicked([], [picked("receipt.pdf")]);
    // The same file picked again (same size + mtime): kept once, silently.
    const again = mergePicked(once.picked, [picked("receipt.pdf")]);
    expect(again.error).toBe("");
    expect(again.picked).toHaveLength(1);
    // A DIFFERENT file that would land at the same path — even by case
    // (the staging filesystem may fold case) — is a named error, never a
    // silent overwrite or drop.
    const clash = mergePicked(once.picked, [picked("Receipt.pdf", { size: 99 })]);
    expect(clash.error).toContain('would land at "Receipt.pdf"');
    expect(clash.picked).toHaveLength(1);
  });

  it("refuses a set over the size limit, keeping the previous selection", () => {
    const result = mergePicked([picked("a.pdf", { size: 2 * 1024 * 1024 })],
      [picked("b.pdf", { size: 2 * 1024 * 1024 })], 3);
    expect(result.error).toContain("3 MB limit");
    expect(result.picked.map((p) => p.path)).toEqual(["a.pdf"]);
  });
});

describe("claimsStatusLabel", () => {
  it("names the file currently being copied", () => {
    const run = makeRunSummary({
      status: "surveying",
      progress: { done: 7, total: 311, file: "Aegene Ong/receipt-08.pdf" },
    });

    expect(claimsStatusLabel(run)).toBe(
      "Preparing files 7/311 · receipt-08.pdf",
    );
  });

  it("drops the file name once the copy has no file in hand", () => {
    const run = makeRunSummary({
      status: "surveying",
      progress: { done: 311, total: 311 },
    });

    expect(claimsStatusLabel(run)).toBe("Preparing files 311/311");
  });
});

describe("runDestination", () => {
  it("keeps all run-list and global-indicator destinations consistent", () => {
    expect(runDestination("claim", { id: "r", status: "map_ready" })).toBe("/claims/r/organize");
    expect(runDestination("claim", { id: "r", status: "ready", open_flags: 0 }, true)).toBe("/claims/r/export");
    expect(runDestination("invoice", { id: "r", status: "ready", open_flags: 2 }, true)).toBe("/invoices/r/review");
    expect(runDestination("invoice", { id: "r", status: "checking" })).toBe("/invoices/r/progress");
  });
});

describe("looksLikeLocalPath", () => {
  it("accepts absolute paths used on Mac, Windows drives, and Windows shares", () => {
    expect(looksLikeLocalPath("/Users/reviewer/Claims/July")).toBe(true);
    expect(looksLikeLocalPath("C:\\Claims\\July")).toBe(true);
    expect(looksLikeLocalPath("\\\\fileserver\\Claims\\July")).toBe(true);
  });

  it("does not mistake relative paths or SharePoint URLs for local paths", () => {
    expect(looksLikeLocalPath("Claims/July")).toBe(false);
    expect(looksLikeLocalPath("https://example.sharepoint.com/Claims")).toBe(false);
  });
});
