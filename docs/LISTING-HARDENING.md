# Payment listing — what it is for, what is built, what is next

Written 2026-08-18 after walking two real ICMR monthly tabs (Apr'26, Jul'26)
through the design on paper. Plain-language, non-programmer audience.

## The one question

The payment listing is the client's record of **past** payments. The whole
reason the pipeline reads it is to answer, for every invoice about to be put
forward for payment:

> **Has this invoice been paid before? If so — which tab, which row, which
> voucher, when, how much, to whom?**

Everything about reading the workbook exists to answer that reliably and to
show the reviewer exactly where to look. Reconciling the client's books is
**not** the job. The arithmetic the code does on the sheet is only used as
evidence that the AI mapped the columns and entries correctly — never as a
verdict on the client's bookkeeping.

The design principle throughout: **the AI looks and reasons; code measures,
decides and reports.**

---

## Done (2026-08-18)

**One reader.** The old "canonical" fast path (a flat six-column sample layout,
first tab only, no AI) is removed. Every listing — the development sample
included — goes through the same reader: the AI maps each tab's structure
(which column is what, which rows form one payment entry), code pulls the
values from the cells it pointed at, and code audits the reading against the
sheet's own numbers. All tabs, always.

**Pair by row.** In a grouped entry (one voucher, several invoices, several
line amounts) an invoice number is paired with the amount on the **same
row**. An entry with a single invoice takes the payment total. Anything the
sheet does not visibly pair stays "amount unknown" — never guessed. Remark
text in the invoice column such as `(Revised invoice)` is kept as a note on
the row rather than treated as a second reference number.

**Provenance in every flag.** Every listing row remembers its tab, row,
voucher and date. Flags now read, for example:

> **ALREADY_PAID** — Invoice 245DHNQL-0015 was already paid: tab Jul'26
> row 28, voucher PV0726/07, dated 2026-07-23, RM 1,044.95, payee Lim Shea
> Fee (status 'Paid'). Paying it again would be a duplicate payment.

`LISTING_AMBIGUOUS` lists each candidate the same way. `NOT_IN_LISTING` says
what was searched ("61 invoice rows across 4 tabs: Apr'26, …"). Amount and
vendor mismatches name the row they were compared against.

**Never blocked by a bookkeeping nit — but never at the cost of an invoice.**
The audit distinguishes *structure* problems from *arithmetic* ones.
Structure = anything that could change which invoice belongs to which
payment: a required column unnamed, any money / invoice / line-amount cell
outside every entry (so an entry cut short can never hide a later invoice
row), two payees merged, overlapping entries, and line amounts that do not
sum to the entry's payment (the signature of a wrong cut). These fail the
run after three attempts. Only a *running-balance step* that is off — the
shape of a typo in the client's balance column — is **accepted with a
warning** in the Activity tab naming the rows, so it never stops the
duplicate check. The 300-row limit counts rows with content, not rows Excel
merely formatted, and a sheet holding more than 200,000 physical cells is
refused before it is walked.

**Read first, say how.** The listing is read at the start of the run, before
any invoice is sent to the AI (it is the point of the run, and it can fail).
Each tab's outcome is written to the Activity tab: payment sheet or skipped
(with the AI's one-line reason), the column map, entries → invoice rows,
which round confirmed it and what round 1 got wrong. Cached readings replay
the same lines. The audit also now questions a multi-invoice entry that has
no line-amount column named — the way an unlabelled amount column (ICMR
column F) would otherwise be missed silently.

**Skipped tabs are visible.** A tab the AI declines as "not a payment sheet"
gets one push-back if it carries payment-style headers; a second "no" is
accepted but recorded as a **WARNING** ("SKIPPED although it carries
payment-style headers … open it and confirm") — the exact shape of a
silently skipped month, so it is never a quiet note.

**Each run keeps its own copy of the reference files.** Every flag decision
and every correction rebuilds the outputs and re-runs that document's
checks; each of those used to re-list the SharePoint folder and re-download
the listing, the policy sheet and the bank template — a round trip per click
for files that had not changed. Now, when a run starts, it copies its
reference files into its own folder on disk (`runs/<run id>/reference/`,
with a manifest saying which file plays which role) and every later touch
reads that copy. Consequences: reviewing a run never goes back to the
network; and a run is always judged against the files as they were when it
started — a client editing the workbook, or a second run starting on the
same folder, cannot change what an earlier run's review sees. Runs created
before this change have no copy; the first review action takes one, once,
from the folder the run recorded at start. The AI reading of a listing is
still shared across runs by file content, so an unchanged file is read by
the AI once, ever.

**Paste-ready listing rows dropped.** They only ever fit the sample layout.
See "Next" for the replacement.

---

## Next

### N1. Write new entries in the client's own layout
The client's format (from the ICMR tabs): a title block (Name, A/C No), a
header row, `Balance b/f`, `Fund received for <month> payment`, one block per
payment (date, PV number, invoice number(s), payee, description line(s),
line amount(s), payment total, running balance), bank charges, a totals row,
then a summary block (Opening balance to utilise, Net payment, Estimated bank
charges, Total fund to request) and Prepared by / Reviewed by.

Recommended approach — **deterministic writer, AI-drafted text only**:
- The AI reading of the existing tabs already yields the column map and
  where the last month's entries end. Code writes new cells into the same
  columns, so the output is in the client's exact layout, not a template of
  ours.
- Grouping is rule-based: approved invoices for the same vendor become one
  payment block; PV numbers continue the sequence found on the latest tab;
  Balance b/f = the latest tab's closing balance; Fund received = total of
  the new payments (so the balance returns to the same small residual);
  running balance is computed.
- The AI is asked only for what needs judgment or prose: a one-line
  description per invoice (from the extracted fields — the invoice reader
  would gain a short `description` field), and nothing numeric.
- Delivered as a new tab appended to a *copy* of the client workbook, or as
  cell-addressed rows ("append after row 34 of tab Jul'26") — to confirm with
  the stakeholder. Prepared-by / Reviewed-by names and the estimated bank
  charge come from Settings.

Three things must be settled **before** this is built (raised in peer
review, agreed):

- **Lifecycle.** The listing is the record of *executed* payments; the
  reader treats any entry with a payment total as paid. A drafted-but-not-
  yet-executed month must therefore never be written into the live listing
  by this tool — it produces a draft (a copy, or a preview of cells) that a
  person finalises after the bank run. Otherwise a later run would report an
  approved-but-never-paid invoice as ALREADY_PAID. If the client keeps
  planned and executed entries in the same workbook, the reader needs a
  status column (already supported) or another explicit signal, and
  ALREADY_PAID should require execution evidence.
- **A layout contract.** `SheetReading` knows columns and entry spans; the
  writer also needs the header row, where the last entry ends and the
  summary block begins, merged cells, formulas (balance cells are usually
  formulas — write formulas, not cached values), styles, and the signature
  block. Define this as a typed `ListingLayout` the reader fills, verify
  it, and **round-trip** every generated tab back through the reader before
  showing it.
- **Business rules, in writing.** How entries are grouped (one PV per
  vendor? per invoice?), what "vendor" identity means (canonical names, not
  raw strings), how PV numbers continue across months and years and what
  happens on a collision or a re-run (idempotency), and the exact fund /
  bank-charge / residual equations. All money in `Decimal` with stated
  rounding. These are the stakeholder's rules, not ours to infer.

### N2. Regenerate the development sample as an ICMR-shaped past-payments workbook
The current sample listing is a flat "planned payments for July" table —
the old framing. A sample with two monthly tabs of *past* payments (one of
which contains a couple of the batch's invoices) exercises the real reader,
the ALREADY_PAID path with provenance, and N1's writer, and gives CI a golden
file to guard against regressions.

### N3. Decide the fate of NOT_IN_LISTING
Under the reframed goal, "not found in any past listing" is the **normal,
healthy** case for a new invoice — yet today it raises a flag on every one.
Recommendation: stop flagging it; record a one-line count in the Activity tab
("9 of 11 invoices are new — not in any past listing tab") and let the
matched ones (ALREADY_PAID with provenance) be the signal. This inverts a
rule the sample and end-to-end verifier currently encode, so it goes
together with N2. **Needs the stakeholder's confirmation.**

### N4. Small, deterministic robustness items (do when convenient)
- Tolerant reference-number lookup: match on a normalised key (case, spaces,
  dashes) but display raw values and say "matched loosely: 'INV 1023' ↔
  'INV-1023'". Directly serves finding re-uploaded invoices. Normalisation
  can also collapse genuinely different references, so the key maps to a
  *list*: a unique, vendor-supported candidate matches; anything else is
  `LISTING_AMBIGUOUS`, never a pick.
- Ask the AI for `observations` alongside its structural answer ("column F
  has no header", "rows 26/28 are recurring payments with no reference")
  and print them in the Activity tab. Cheap, and turns silent quirks into
  visible ones.

---

### N5. Test depth
Deterministic, scripted-AI tests now cover: truncated entries (structural
and end-to-end rejection after all rounds), positional-pairing removal,
parenthesised references, summary figures in the invoice column, formatted-
but-empty rows, status columns, unlabelled line-amount columns, the
twice-declined tab warning, and the arithmetic-only acceptance path.
Still to add: formula cells with stale caches, hidden tabs, duplicate
references across tabs, and an **opt-in real-model evaluation** run against
an anonymised copy of the client workbook (N2), so a model change is caught
before a client sees it.

### N6. Input boundaries the reader does not yet enforce
File size, sheet count, column count and per-cell text length limits (grid
text truncates cells to 80 characters, nothing else is capped); stale
formula caches (a workbook saved without recalculation gives the audit
`None` where the balance should be — today that silently weakens the balance
walk); macro-preserving copies for `.xlsm` when N1 writes. Cell text is sent
to the model as data; the model answers only coordinates, schema-checked,
so instructions hidden in cells cannot reach the pipeline — worth stating,
not worth more machinery.

## Deliberately not doing

These came up while brainstorming and are set aside because they audit the
client's bookkeeping rather than answer the one question:

- Cross-tab balance checks (closing balance of one month = opening of the
  next), discrepancy flags for the client's own arithmetic, a separate
  `LISTING_DISCREPANCY` flag type.
- A second AI opinion on vendor mismatches; a double structural read; giving
  the AI spreadsheet-digging tools. Revisit only if the Activity tab shows
  first-round readings are often wrong on real files.

---

## Peer-review response (2026-08-18)

A review of this plan raised eleven points. Outcome:

| # | Finding | Verdict | Action |
|---|---------|---------|--------|
| 1 | Truncated entry can drop invoice rows without a structural error; soft-accept could wave it through | **Confirmed — real hole, introduced by the soft-accept** | Invoice and line-amount cells are now covered like money cells; line-sum mismatch is STRUCTURE; end-to-end rejection test added |
| 2 | AI can skip a whole payment tab | Partly agree | Second "no" on a payment-looking tab is a WARNING the reviewer sees, not a quiet note. Not fail-closed: a summary tab that happens to carry "balance"/"receipt" words would otherwise stop every run with no in-app override |
| 3 | "Never guessed" / exact-row provenance not yet true | Confirmed | Positional pairing removed; rows carry both the invoice's own row and the entry's start row; remark detection is keyword-only (`(INV-123)` is a reference) |
| 4 | N1 conflicts with "past payments" (approved ≠ executed) | Agree | Lifecycle section added to N1: drafts only, never the live listing; execution evidence before ALREADY_PAID if planned/executed share a workbook |
| 5 | Reader lacks layout info for N1 | Agree | `ListingLayout` contract + round-trip requirement added to N1 |
| 6 | N1's money/sequencing rules need business decisions | Agree | Listed explicitly as stakeholder decisions in N1 |
| 7 | Production boundaries absent | Partly agree | Physical-cell ceiling (200k) before walking; content rows computed from stored cells, not by materialising the rectangle. Remaining limits listed in N6 |
| 8 | Tests miss the acceptance path | Agree | Adversarial tests added (see N5) |
| 9 | Loose reference matching needs collision semantics | Agree (N4 not built yet) | Semantics written into N4 |
| 10 | Absence from history ≠ unregistered vendor; exact string compare | Agree | Wording changed to "verify beneficiary registration"; tolerant vendor matching reused |
| 11 | README / UI wording stale | Agree | Fixed |
