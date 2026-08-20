import { describe, expect, it } from "vitest";
import { makeRunSummary } from "../test/fixtures";
import { claimsStatusLabel, looksLikeLocalPath } from "./ClaimsList";

describe("claimsStatusLabel", () => {
  it("names the file currently being copied", () => {
    const run = makeRunSummary({
      status: "surveying",
      progress: { done: 7, total: 311, file: "Aegene Ong/receipt-08.pdf" },
    });

    expect(claimsStatusLabel(run)).toBe(
      "Copying files 7/311 · receipt-08.pdf",
    );
  });

  it("drops the file name once the copy has no file in hand", () => {
    const run = makeRunSummary({
      status: "surveying",
      progress: { done: 311, total: 311 },
    });

    expect(claimsStatusLabel(run)).toBe("Copying files 311/311");
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
