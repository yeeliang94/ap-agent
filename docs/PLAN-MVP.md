# Implementation Plan: AP Agent MVP — thin end-to-end demo

> **Historical record (frozen 2026-08-18).** This is the plan the MVP was
> built to, plus the dated changes made after it. It is no longer edited;
> the current claims work lives in [PRD.md](PRD.md) → [PLAN.md](PLAN.md).
> Where a line below was later superseded, a *[superseded]* note says so
> — the original text is kept so the history stays honest.

**Status (three different things, kept apart):**

| What | State |
|---|---|
| Local synthetic demo — the 13 steps below, on this Mac, synthetic files, direct OpenAI key | **Complete** (2026-08-12), re-verified after each fix pass |
| Enterprise integration — real SharePoint MCP reader, delegated Entra sign-in, proxy model layer, Windows start script | **Built**, not yet exercised against the live enterprise services |
| Pilot validation — the four Windows checks in [README.md](../README.md#windows-test-checklist-maps-to-design-doc-9), plus data-governance and accuracy gates | **Pending** — 1 SharePoint read access ☐ · 2 sign-in lasts a run ☐ · 3 model tiering on the proxy ☐ · 4 template output pastes cleanly ☐ |

"100%" below refers to the first row only. Do not read it as "ready for a
client."

**Overall Progress (local demo):** `100%` — all 13 steps done, all local verifications passed (2026-08-12)
**PRD Reference:** [ap-agent-design.html](../ap-agent-design.html) (solution design doc — sections 2, 5, 6, 9)
**Last Updated:** 2026-08-18 (frozen)

## Summary
Build a working thin slice of the AP agent: a user uploads a small demo batch (invoices + claims, including scans), the pipeline sorts, extracts, and checks them, flags land on a review screen, and the app produces template-aligned draft rows for the payment listing and the Maybank upload (bank rows carry an explicit `[ACCOUNT UNKNOWN]` placeholder until a vendor master exists — they are *not* copy-ready as-is; the listing rows were later replaced by a drafted tab, see 2026-08-18 below). Every stage is real but minimal. *Note on "claims":* the MVP's claims are a simplified single-form claim with a filename-paired receipt; the real employee-claims workflow (per-employee folders, expense-report workbooks, receipt bundles, mileage) is a separate module — see [PRD.md](PRD.md). Development happens on this Mac against synthetic sample files and a direct OpenAI key; the enterprise proxy, real SharePoint MCP, and real documents are swapped in during Windows testing (see design doc section 9, "Assumptions to validate").

## Key Decisions
- **MVP scope: thin end-to-end** — every pipeline stage present but basic, so the full concept is demoable; depth comes later.
- **Standalone app** — own repo, but mirrors the enterprise repo's conventions (Python + FastAPI + PydanticAI, LiteLLM-compatible model layer, Node-built frontend) so it transplants cleanly.
- **Synthetic sample data first** — generated fake invoices/claims/listing/policy/template; real anonymized files swap in later without code changes.
- **React + TypeScript SPA (Vite), 3 screens** — upload/runs, review, copy-output. Minimal styling; nothing throwaway.
- **OpenAI direct key locally, via the swappable model layer** — all model calls go through one factory reading env config, so pointing at the enterprise proxy (`LLM_PROXY_URL` + catalogue model IDs) is a config change, not a rebuild.
- **SharePoint is read-only and stubbed locally** — a fake MCP server mimics the probed tool contract (search sites, list folder, get metadata, download URL) including the `Shared Documents`/`Documents` alias and intermittent-error retry behaviour. Real MCP is Windows-only testing. *[superseded in part: the real MCP reader and delegated Entra sign-in were built later (`docsource.RealMcpSource`, `sharepoint_auth.py`); they remain untested against the live service — see Status above.]*
- **Copy-paste output, never file writes** — the agent formats rows (tab-separated for clean Excel paste); a person pastes. No SharePoint or workbook writes anywhere in the app. *[clarified 2026-08-18: the app never writes to SharePoint or to any client working file. Since the 2026-08-18 listing work it does write a DRAFT workbook — a copy — into the run's own folder for the reviewer to download; the rule is "no writes to the client's files", not "no files at all".]*
- **AI judgments must cite their policy basis** — category and policy-clause decisions carry a quoted policy line; "unsure" always becomes a flag.

## Pre-Implementation Checklist
- [x] 🟩 Interaction model agreed (user-initiated runs, copy-paste output)
- [x] 🟩 Enterprise constraints folded into design doc (read-only MCP, delegated auth, proxy catalogue)
- [x] 🟩 Stack agreed (FastAPI + PydanticAI backend, React/TS frontend, SQLite)
- [ ] 🟥 OpenAI API key placed in local `.env` (never committed)
- [ ] 🟥 No conflicting in-progress work (fresh repo — confirm)

## Tasks

### Phase 1: Foundation
- [x] 🟩 **Step 1: Repo scaffold** — backend and frontend skeletons, so every later step has a home.
  - [x] 🟩 `backend/` — FastAPI app with `/api/health`, venv (Python 3.12 via uv), SQLite via SQLAlchemy, tables: runs/documents/flags/audit_events
  - [x] 🟩 `frontend/` — Vite + React + TS app, placeholder page proxying `/api` to the backend
  - [x] 🟩 `.gitignore`, `.env.example`; user's `.env` reformatted (key had been pasted without the `OPENAI_API_KEY=` name — apps couldn't read it)
  - **Note:** system Python was 3.9; used Homebrew Python 3.12 to match the enterprise 3.10+ rule.
  - **Verify:** `uvicorn` serves `/api/health` → `{"ok": true}`; `npm run dev` shows the placeholder page calling it.

- [x] 🟩 **Step 2: Synthetic sample data** — `samples/generate_samples.py`: 8 invoices (6 PDF, 2 photo-style incl. 1 blurry), 4 claims + 4 receipts, listing/policy/Maybank workbooks, `demo_batch.zip`, `ground_truth.json` with the 5 planted anomalies. Verified: images render legibly (fixed an em-dash glyph the default font couldn't draw).

- [x] 🟩 **Step 3: Model layer (the swap point)** — `app/model_layer.py` factory (direct OpenAI locally, proxy when `LLM_PROXY_URL` set) + `app/schemas_ai.py` answer forms. **Verified live:** `pytest tests/test_model_layer.py` passed — gpt-4o read a photo-style invoice and returned exact invoice number, amount, currency as a validated object.

### Phase 2: Pipeline core (backend, no UI yet)
- [x] 🟩 **Step 4: Intake** — `POST /api/runs` (zip + client), per-run workspace, SQLite records, background job; status endpoint with stage counts. Verified via the e2e script: 16 documents registered and processed.

- [x] 🟩 **Step 5: Sort stage** — per-document AI classification + filename-convention receipt pairing. **Verified: 16/16 correct.**

- [x] 🟩 **Step 6: Extract stage** — parallel workers (cap 5), typed fields + per-field confidence, PDF→PNG + downsizing. **Verified: 24/24 key fields correct; blurry scan read correctly AND flagged low-confidence.**
  - **Finding worth remembering:** with heavier blur, the model *confidently invented* an invoice number without admitting doubt — the listing lookup caught it as a side effect. This is the concrete argument for the "double-read on scans" dial before production. Prompt now states an empty low_confidence asserts every character was crisp.

- [x] 🟩 **Step 7: Checks** — code: listing lookup, date age, duplicates, currency/cap arithmetic; AI: category + clause with quoted policy line, unsure→flag (incl. the rule that a clause needing absent information — per-head caps without headcount — means unsure). **Verified: all 5 planted anomalies flagged; extra flags are legitimate (MX-2214 is both old-dated and not-in-listing, as the design doc's own example says).**

- [x] 🟩 **Step 8: Copy-ready output builder** *[wording superseded: "template-aligned draft rows" — bank rows carry `[ACCOUNT UNKNOWN]` until a vendor master exists (see 2026-08-12 fix pass), and the TSV listing rows were dropped on 2026-08-18 in favour of a drafted tab]* — TSV listing rows with continued running numbers, Maybank rows from the template's learned headers, filename list, totals reconciliation, new-vendor detection ("Apex Renovation Works" caught). Excel paste check deferred to the Phase 3 browser demo where copy buttons exist.

### Phase 3: The three screens
- [x] 🟩 **Step 9: Runs dashboard + upload** — upload card, 3s polling, stage chips ("Reading documents 5/12"), failed-run surfacing. Verified in browser against three live runs.

- [x] 🟩 **Step 10: Review screen** — flag cards with reason + cited basis + inline source preview; accept/exclude with optional note; resolved flags collapse; output tab locked until review complete. Verified: decision in browser (with note) + 6 via the same API; **audit trail: 7 events in SQLite with actor/action/detail/timestamp**. *[clarified 2026-08-18: the actor is the fixed word "reviewer" (single user, no login), so this is a decision log — who-did-what is not attributable until the signed-in Windows/Entra identity is captured; follow-up noted in README.]*
  - **Note:** browser PDF embeds rendered as a black box — replaced with a backend-rendered PNG preview endpoint (works for every file type, no browser plugin dependence).

- [x] 🟩 **Step 11: Copy-output screen** — totals cards, new-vendor warning, three copy blocks with TSV preview. Verified: rejecting MX-2214 rebuilt the blocks — 7 rows instead of 8, totals RM 14,930.00 both sides (hand-checked), reconciliation green, "Apex Renovation Works" flagged for Maybank registration.

### Phase 4: Enterprise-readiness seams
- [x] 🟩 **Step 12: Fake SharePoint MCP + adapter** — `fake_mcp/server.py` implements the probed contract (site search, folder-URL resolve with the Shared Documents/Documents alias, metadata with single-use temporary download URLs, deterministic every-7th-call ReadError); `backend/app/docsource.py` adapter with 3× retry; `DOC_SOURCE=local|mcp` in .env. **Verified: references loaded through the stub with a transient 500 retried transparently; stub killed → clean `SourceUnavailable`, not a crash.**

- [x] 🟩 **Step 13: Windows handoff pack** — `README.md` (local-vs-enterprise .env table, inherited enterprise rules, 4-point Windows test checklist mapped to design doc §9), `start.sh` + `start.bat` (sets `PYTHONUTF8=1`), `.env.example` extended with `DOC_SOURCE`/`MCP_URL`/`SHAREPOINT_FOLDER_URL`.

## Peer-review fix pass (2026-08-12, after MVP completion)

An external peer review found 12 issues; 10 confirmed, 2 partially valid, 0 invalid. All fixed and re-verified (two consecutive clean end-to-end runs):
- **No synthesized bank accounts** (was CRITICAL): every account cell is an explicit `[ACCOUNT UNKNOWN]` marker until a vendor master exists.
- **Server-side human gate**: the API returns no outputs while any flag is open.
- **Deeper listing match**: Paid-status, amount, and vendor of the matched row are checked; listing block emits only genuinely new rows.
- **Real reconciliation**: totals recomputed independently from the emitted text with `Decimal`; bank rows built by template-header mapping (unknown headers refuse loudly); non-MYR invoices flagged out of the bank block.
- **Double-read extraction** (promoted from follow-up after three runs showed *confident* misreads of the blurry scan): two independent reads per document; key-field disagreement ⇒ low-confidence flag. Judge runs at temperature 0.
- **Citation verification**: the AI's quoted policy line must appear in the cited clause; category must match the clause; strict field constraints (positive finite amounts, real dates, currency codes).
- **Nothing vanishes**: unknown documents and orphan receipts are flagged; unknown docs never enter output.
- Plus: zip quotas, enforced 40-request cap, fixed-client guard, redacted source errors with fresh-URL download retries, TSV/formula/filename sanitization, FastAPI serves `frontend/dist` so `start.bat` yields a working app.
- **Verifier hardened**: asserts all 56 declared fields (correct-or-excused), tests the gate, recomputes totals independently, simulates a competent reviewer (excludes hard-to-read documents that also contradict the listing), reports false positives.
- ~~**Deferred follow-up:** audited in-app field correction at review time.~~ **Done (2026-08-12):** "Fix a value" on the flag card — audited corrections (before → after + reason), per-document instant re-check (no pipeline re-run; claims re-judge for ~a tenth of a cent), flags auto-resolve as `resolved_by_correction` / new ones raise, outputs rebuild. Verified end-to-end (verifier now *corrects* the blurry invoice instead of excluding it — ALL CHECKS PASSED) and manually in the browser (9 open flags → 6 after correcting two fields; audit trail confirmed).

## Listing reframed as past-payment history (2026-08-18)

Reading two real ICMR monthly tabs (Apr'26, Jul'26) sharpened the goal: the
listing is the record of PAST payments, and the run's central question is
"has this invoice been paid before — where?" See `docs/LISTING-HARDENING.md`.
- **One reader.** The canonical fast path (flat six-column sample layout,
  first tab only) is gone; every listing, samples included, goes through the
  AI-mapped, code-audited reader across every tab.
- **Pair by row.** Grouped entries pair invoice numbers to line amounts by
  the row they share, not by list position (Lim Shea-Fee: 2 numbers among 4
  amounts now keeps its amounts). Remark text like "(Revised invoice)" in the
  invoice column is a note, not a number.
- **Provenance in every flag.** Rows carry tab / row / voucher / date; a match
  reads "already paid: tab Jul'26 row 28, voucher PV0726/07, dated
  2026-07-23, RM 1,044.95, payee Lim Shea Fee". Ambiguous matches list every
  candidate the same way; "not found" says how many rows/tabs were searched.
- **Never blocked by a bookkeeping nit.** After 3 rounds, arithmetic-only
  leftovers (a line total or balance step off) are accepted with a WARNING
  in the Activity tab naming the rows; structural problems still fail.
  Content rows, not Excel's formatted max_row, count toward the 300 limit.
- **Read first, say how.** The listing is read before any invoice is
  extracted, and each tab's outcome (payment sheet / skipped and why, column
  map, entries → rows, rounds) is written to the run's Activity tab.
- **Paste-ready listing rows dropped.** They only ever fit the sample layout.
  Writing new entries in the client's own layout is the next piece.
- **Each run keeps its own copy of the reference files** (`runs/<id>/reference/`
  + manifest), taken at run start. Flag decisions and corrections read that
  copy — no re-download per click, and a run is judged against the files it
  started with even if the folder changes or another run starts.
- **Peer review of the above (same day):** one CRITICAL confirmed and fixed —
  an entry cut short could drop later invoice rows and the arithmetic-only
  soft-accept would have let it through; invoice/line-amount cells are now
  covered like money cells and a line-sum mismatch is structural. Positional
  pairing removed; rows carry both invoice row and entry row; twice-declined
  payment-like tabs are a WARNING; physical-cell ceiling; adversarial tests.

## Listing hardening, second pass (2026-08-18)

All "Next" items of `docs/LISTING-HARDENING.md` built, one commit each:
- **N3** `NOT_IN_LISTING` dropped; one Activity line counts new / matched /
  ambiguous invoices and what was searched.
- **N4** loose reference matching (unique + vendor-supported, else
  ambiguous; raw values shown); AI `observations` printed.
- **N6/N5** input limits (20 MB, 40 tabs, 60 columns, 200-char cell texts),
  stale-formula WARNING per tab, hidden tabs labelled; tests for each plus
  duplicate references across tabs and an opt-in real-model evaluation.
- **N2** sample regenerated as an ICMR-shaped past-payments workbook; two
  ALREADY_PAID plants; golden test.
- **N1** next month's entries drafted as a new tab on a copy of the client
  workbook (deterministic writer, `ListingLayout` contract, round-trip
  through the reader's audit, formulas for balances, `.xlsm` preserved,
  Settings for signatures / bank charge, download route, run-screen card).
  Business rules taken as recommended and marked "assumed — confirm".
- Verified end to end with the real model on the regenerated sample:
  ALL CHECKS PASSED (both plants found, zero false positives, draft
  written and downloaded).

## Rollback Plan
- Every step lands as its own git commit — `git revert` any step cleanly.
- The app never writes to SharePoint or user workbooks, so there is no external state to undo; worst case is deleting the local SQLite file and per-run workspace folders.
- `.env` holds all secrets and is git-ignored; if a key is ever committed by accident, rotate it immediately and rewrite history before pushing.

## Out of scope for the MVP (agreed)
*[Stale as of 2026-08-18: real MCP integration and Entra sign-in have since been BUILT (not yet live-tested); the rest still stands. Kept as originally agreed.]*
Real MCP integration, Entra sign-in, Azure hosting, Teams/email notifications, multi-user auth, model-tier cost optimization beyond sort-vs-extract, batch scheduling.
