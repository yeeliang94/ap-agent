// All backend calls in one place, with the shapes the API returns.

export interface RunSummary {
  id: string;
  client: string;
  status: string;
  error: string;
  progress: { done?: number; total?: number };
  documents_total: number;
  open_flags: number;
  created_at: string;
}

export interface Doc {
  id: string;
  filename: string;
  kind: string;
  fields: Record<string, unknown>;
  confidence: Record<string, string>;
  status: string;
  error: string;
}

export interface FlagItem {
  id: string;
  document_id: string;
  code: string;
  reason: string;
  basis: string;
  status: string;
  resolution: string;
}

export interface Outputs {
  listing_header: string;
  listing_rows: string[];
  bank_header: string;
  bank_rows: string[];
  filenames: string[];
  new_vendors: string[];
  totals: { listing: number; bank: number; match: boolean };
}

export interface RunDetailData extends RunSummary {
  documents: Doc[];
  flags: FlagItem[];
  outputs: Outputs | Record<string, never>;
}

export async function listRuns(): Promise<RunSummary[]> {
  const r = await fetch("/api/runs");
  if (!r.ok) throw new Error("Could not load runs");
  return r.json();
}

export async function getRun(id: string): Promise<RunDetailData> {
  const r = await fetch(`/api/runs/${id}`);
  if (!r.ok) throw new Error("Could not load run");
  return r.json();
}

export async function uploadBatch(client: string, file: File): Promise<{ run_id: string }> {
  const form = new FormData();
  form.append("client", client);
  form.append("batch", file);
  const r = await fetch("/api/runs", { method: "POST", body: form });
  if (!r.ok) throw new Error((await r.json()).detail ?? "Upload failed");
  return r.json();
}

export async function decideFlag(
  runId: string,
  flagId: string,
  decision: "accepted" | "rejected",
  note: string
): Promise<void> {
  const r = await fetch(`/api/runs/${runId}/flags/${flagId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note }),
  });
  if (!r.ok) throw new Error("Could not record decision");
}

export function documentFileUrl(runId: string, docId: string): string {
  // The preview endpoint returns the first page as PNG for every file type,
  // so the review screen never depends on the browser's PDF plugin.
  return `/api/runs/${runId}/documents/${docId}/preview`;
}
