import { ClaimEvidence, ClaimFlag, ClaimRow, FlagCatalogue, FlagInfo } from "../../api";

// Pure helpers behind the Review screen: a flag's words, its kind on the
// summary strip, and the money at stake — in integer cents parsed from the
// server's decimal strings, never through a float.

export const ROW_FIELDS = [
  { name: "date" }, { name: "item" }, { name: "reason" }, { name: "receipt_included", label: "receipt included (Y/N)" },
  { name: "amount" }, { name: "currency" }, { name: "rate", label: "exchange rate" }, { name: "total", label: "total (MYR)" },
];
export const KM_FIELDS = [{ name: "date" }, { name: "km" }, { name: "rate", label: "rate per km" }, { name: "amount" }];

// The order and the words of the summary strip's kinds.
export const KIND_LABEL: Record<string, string> = {
  money: "money at risk",
  evidence: "uncertain reads",
  mileage: "mileage",
  structure: "files & rules",
  note: "notes",
};
export const KIND_ORDER = ["money", "evidence", "mileage", "structure", "note"];

// The words for a code: from the run's catalogue, with a plain fallback so
// an unknown code still reads as words, never SNAKE_CASE.
export function describeFlag(catalogue: FlagCatalogue | undefined, code: string): FlagInfo {
  return (
    catalogue?.[code] ?? {
      code,
      title: code.replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()),
      meaning: "",
      what_to_do: "",
      kind: "structure",
      blocks: "open",
      toggle: true,
    }
  );
}

// An open flag's kind for the strip: notes are the info ones; an OPEN
// flag whose catalogue kind is "note" (an unclaimed receipt above the
// threshold) is money at risk.
export function kindOf(catalogue: FlagCatalogue | undefined, f: ClaimFlag): string {
  if (f.status === "info") return "note";
  const k = describeFlag(catalogue, f.code).kind;
  return k === "note" ? "money" : k;
}

/** "1,234.5" / "-12" / " 0.075 " → integer cents (123450 / -1200 / 8), by
 *  string arithmetic: the whole part and the first two decimals are read as
 *  integers, a third decimal rounds half up. Anything that is not a plain
 *  decimal number is null (no currency symbols, no exponents). */
export function centsOf(text: unknown): number | null {
  if (text === null || text === undefined) return null;
  const raw = String(text).replace(/,/g, "").trim();
  const m = /^([+-])?(\d*)(?:\.(\d*))?$/.exec(raw);
  if (!m || (m[2] === "" && (m[3] ?? "") === "")) return null;
  const whole = m[2] === "" ? 0 : parseInt(m[2], 10);
  const frac = (m[3] ?? "").padEnd(3, "0");
  let cents = whole * 100 + parseInt(frac.slice(0, 2), 10);
  if (parseInt(frac[2], 10) >= 5) cents += 1;
  return m[1] === "-" ? -cents : cents;
}

/** Cents at stake for a flag: the row's MYR total (or amount), else the
 *  cited receipt's amount. */
export function stakeCents(f: ClaimFlag, rowById: Map<string, ClaimRow>, evById: Map<string, ClaimEvidence>): number | null {
  const row = f.row_id ? rowById.get(f.row_id) : undefined;
  if (row) return centsOf(row.values.total ?? row.values.amount);
  const ev = f.evidence_id ? evById.get(f.evidence_id) : undefined;
  if (ev && ev.kind === "receipt") return centsOf(ev.values.amount);
  return null;
}

/** Integer cents → "RM 1,234.50" (the whole part grouped, two decimals),
 *  with no division through a float. */
export function rm(cents: number | null): string {
  if (cents === null) return "";
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const whole = Math.floor(abs / 100);
  const frac = String(abs % 100).padStart(2, "0");
  return `${sign}RM ${whole.toLocaleString("en-MY")}.${frac}`;
}
