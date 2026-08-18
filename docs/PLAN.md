# Implementation Plan: Employee Claims Verification Module

**Overall Progress:** `0%`
**PRD Reference:** [docs/PRD.md](PRD.md) (flows 1–5, check catalogue in Flow 3, steering, decisions table)
**Last Updated:** 2026-08-18
**Previous plan (invoice pipeline MVP, complete):** [docs/PLAN-MVP.md](PLAN-MVP.md)

## Summary

Build a *Claims run* — a second run type inside the AP Agent — that takes a
SharePoint folder link (plus a link to the month's listing), finds each
employee's expense report and receipts by itself, verifies every claim row
and mileage trip with code-checked arithmetic and cited evidence, and, once a
person has cleared the flags, produces one copy-ready listing row per
employee. The invoice pipeline is not touched. Delivery is in two tiers: **v1
is deliberately Copilot-simple** (folder + optional paragraph of
instructions → verified tables → listing rows), and **v2 adds discovery and
learning** so a new company needs no instructions at all.

## Key Decisions

- **Separate module, shared plumbing** — new package `backend/app/claims/`,
  new routes `/api/claims-runs`, new *Claims* tab; reuses the model factory,
  SharePoint reader, PDF→image, settings, telemetry. `backend/app/pipeline/*`
  is not modified — the working invoice pipeline cannot be broken by this.
- **Own tables** (`claims_runs`, `claim_employees`, `claim_rows`,
  `claim_evidence`, `claim_flags`) rather than adding columns to the invoice
  tables — isolation beats reuse here; rollback is "drop the claims tables".
  Frontend *components* (flag card, field editor, totals card, copy block)
  are shared by moving them into a `components/` folder.
- **AI reads and reasons; code measures and decides; a person clears flags**
  — the same principle as the listing reader. No number the AI reads is
  believed until code has checked it against something else.
- **The agent finds things itself** — the map step *peeks inside* every file
  (tab names + first rows; page‑1 thumbnail) and proposes each file's role
  with a reason, verified by code. The per-client instructions paragraph is
  optional, for oddities only.
- **One worker per employee, five at a time**, each sealed to that
  employee's files and within the per-agent request cap; one employee
  failing never fails the batch.
- **Company values are not hard-coded** — rates, receipt-optional items,
  category rule, mileage layout live in a per-client profile + playbook.
  LinkedIn's confirmed values are the *sample's* defaults, not the code's.
- **v1 before v2 (recommended sequencing, awaiting explicit confirmation)** —
  Phases 1–5 ship the simple experience the owner liked in the Copilot test;
  Phase 6 (Discovery, learn-from-decisions, second client) starts only after
  v1 passes its verifier. Nothing from the PRD is dropped, only ordered.
- **Listing columns come from the client's own listing** — the reviewer links
  the month's listing; the AI maps its header row; code emits rows in that
  order.
- **Old plan preserved** — the finished MVP plan is now `docs/PLAN-MVP.md`;
  README links to both.

## Screens & UX (what the reviewer sees)

Design rules for every screen below: same look as the existing screens
(same tabs, cards, buttons, CSS classes — no new component library); one
primary action per screen; every AI judgment shows its reason next to it;
every flag shows its evidence inline; loading, empty and error states are
written, not left blank; labels on every control, keyboard reachable,
readable contrast. Plain-language wording throughout — the reviewer is not
a programmer either.

| Screen / view | Purpose | Key elements | States |
|---|---|---|---|
| **Claims list** (new *Claims* tab beside *Runs*) | See past batches, start a new one | *New claims run* card at top; table of runs: client, folder, started, status chip ("Map ready", "Verifying 3/10", "Ready", "Failed"), employees, open flags | Empty: "No claims runs yet — start one above." Failed row shows the reason inline |
| **New claims run** form | The Copilot-simple start | Folder link · listing link · received date · *Instructions for this client* (textarea, prefilled from the client playbook, optional, placeholder shows an example paragraph) · *Start*. Local dev only: zip upload | Inline validation (link shape, date); disabled *Start* until valid; "Starting…" then redirect |
| **Run detail → Map & Rules** | Confirm the agent's map with one click; correct if needed | One row per subfolder: employee · ER code · report file+tab · mileage tab · receipt files · ignored · unplaced · warnings badge; each role is a dropdown; **reason on hover/expand** ("tab `Expense Report`: name header, Date/Item/Amount columns"); *remember for <client>* tick per correction; **Confirm & verify** (disabled until valid, tooltip says why); v2 adds a *Rules* panel beside the map | Loading skeleton while mapping; warnings listed above the table; invalid edit → inline message |
| **Run detail → Verifying** | Watch progress without refreshing | Employee chips: queued / verifying / done (n flags) / failed (retry button); overall bar; Activity link | Poll every 3 s (as today); failed employee shows reason and *Retry* |
| **Run detail → Review** | Clear flags fast | Employee summary table (name, ER code, category + why, rows verified/flagged, total, status); below it flag cards **grouped by employee**, each: code, reason, basis ("client profile: car RM 0.64/km"), **evidence preview** — page image with the receipt's position highlighted, or the sheet row — and actions *Accept* / *Exclude (note)* / *Fix a value* / *Re-verify employee* | "All flags resolved — Output unlocked" banner; per-flag saving state; instant re-check spinner on fix |
| **Run detail → Output** | Copy the listing rows | Totals cards (employees included, total MYR), reconciliation line (green/red with the difference named), copy block (TSV preview, header order from the client's listing), *Not included* list with reasons | Locked state: "Review is not complete: 4 flags open" with link to Review; header-fallback notice when the listing headers could not be read |
| **Run detail → Activity** | What the system did | Run diary as today: map rounds, per-employee timings, AI cost, warnings, RULE_DRIFT (v2) | — |
| **Settings → Claims (per client)** | The few values code needs | Mileage rates by vehicle type; km tolerance (default 0); receipt-optional items; mileage item pattern; the playbook textarea; last confirmed map (read-only, with *forget*) | Save confirmation; values carry "set by reviewer on <date>" (v2: evidence) |

## Pre-Implementation Checklist
- [x] 🟩 PRD written and updated with the owner's decisions (2026-08-18)
- [ ] 🟨 Owner confirms the two open items in the PRD: Story 5 promoted to MUST; v1→v2 sequencing
- [ ] 🟨 Owner confirms the four "still assumed" defaults (pause at map; received date typed; quotas; 5-minute target) — build proceeds with defaults meanwhile
- [x] 🟩 No conflicting in-progress work (repo clean at start)
- [ ] 🟥 Local `.env` OpenAI key still valid (`pytest backend/tests/test_model_layer.py` passes)

## Tasks

### Phase 1: Foundation — sample data, tables, reader
- [ ] 🟥 **Step 1: Synthetic claims sample (Client A, LinkedIn-shaped)** — the test bed for everything else; no real employee data is ever used.
  - [ ] 🟥 `samples/generate_claims_sample.py`: 10 employee folders `Name_n/`, each with `Name_ER(<period>).xlsx` (tabs *Instructions*, *Expense Types* with GL codes, *Expense Report*, *KM*), a PDF print of the report, `_Approval.pdf`, and 1–2 receipt bundles (receipts drawn 3-per-page in random order; map pages at the back with a narrative line + a fake route image with the km in small text)
  - [ ] 🟥 A "Summary of Invoices" listing workbook with the header row from the screenshot and two past tabs holding earlier ER rows
  - [ ] 🟥 Planted errors, one per kind: overstated row (RM 10, as in the owner's Copilot test); missing receipt; same receipt used twice; km ≠ map; wrong rate; a genuine return trip (must *not* flag); "receipt = N" on Mobile Allowance (must not flag) and on Taxi (must flag); a foreign-currency row; an employee with no report; an unplaced file; a mixed-category report whose stated purpose is an offsite
  - [ ] 🟥 `ground_truth_claims.json` (every row, every receipt with page + position, every trip, expected flags, expected listing rows) + `demo_claims_batch.zip`
  - **Verify:** run the generator; open two workbooks and one receipt PDF by eye; a small script asserts 10 folders, tab names, page counts, that receipts are legible at 150 dpi, and that the km text on map pages is readable at full resolution.

- [ ] 🟥 **Step 2: Claims tables + run skeleton** — a run can be created and watched before any AI exists.
  - [ ] 🟥 `backend/app/claims/models.py`: `claims_runs`, `claim_employees`, `claim_rows`, `claim_evidence`, `claim_flags`; created alongside existing tables (no change to existing ones)
  - [ ] 🟥 `POST /api/claims-runs` (folder link + listing link + received date + optional instructions; zip alternative for local), `GET /api/claims-runs`, `GET /api/claims-runs/{id}`; per-run workspace `runs/<id>/claims/`; background job with status transitions `queued → surveying → mapping → map_ready → verifying → ready / failed`; run diary events via telemetry
  - [ ] 🟥 Restart-safe from day one (lesson from the MVP peer review): claims runs in an in-progress status at server start are marked failed with a plain reason via the same startup reconciliation the invoice runs now use; `map_ready` is *not* in-progress (a run waiting for a click survives a restart)
  - **Verify:** `curl` creates a run from the zip; status advances to `surveying` and (for now) stops with a diary event; a run left at `surveying` in the DB is marked failed on the next startup while a `map_ready` run is untouched; the invoice pipeline's existing tests still pass unchanged.

- [ ] 🟥 **Step 3: SharePoint reader walks subfolders** — the real batches live in nested folders.
  - [ ] 🟥 `docsource.py`: list a folder *and its subfolders* (depth ≤ 3) and download any file under it, with the existing 3× retry; same for the fake MCP (`fake_mcp/`) with a nested test folder and the every‑7th‑call ReadError
  - [ ] 🟥 Quotas enforced before download: 30 employee folders; 60 files / 200 pages per employee; 25 MB per file — refusal names the quota
  - **Verify:** `pytest` against the fake MCP: nested folder → all files listed and downloaded, a transient error retried; stub down → structured "source unavailable"; a 31-folder tree → refused with the quota named.

### Phase 2: The map — the agent finds things itself
- [ ] 🟥 **Step 4: Survey + peek (code only)** — everything the map AI is allowed to see, gathered without a model call.
  - [ ] 🟥 Survey: path, type, size, page count, `ER(...)` code from the name, per subfolder
  - [ ] 🟥 Peek: workbook → tab names + first ~15 rows of each tab as text; PDF/image → page‑1 thumbnail + page count; stored with the run
  - **Verify:** survey JSON for the sample lists 10 folders / all files; every workbook peek shows the four tab names; every receipt bundle has a thumbnail; runtime under 10 s for the sample.

- [ ] 🟥 **Step 5: Map agent + audit loop** — propose roles with reasons; code checks the guess; look again if it doesn't fit.
  - [ ] 🟥 Map agent ("judge" role): input = survey + peeks (+ playbook + last confirmed map if any); output = per subfolder: employee?, name, ER code, report file+tab, mileage tab, receipt files, ignored files, unplaced files — **each with a one-line reason**
  - [ ] 🟥 Audit (code): every folder/file placed; one ER code per employee, none shared; the "report" tab yields dated rows with amounts that sum to a total; a "receipts" file yields ≥ 1 receipt on page 1; mismatches go back to the AI, ≤ 3 rounds; leftovers become map warnings; status → `map_ready`; the request cap is respected
  - **Verify:** on the sample with **no** playbook: 10/10 employees mapped correctly, the approval and report-print PDFs ignored with sensible reasons, the no-report employee marked "build rows from receipts", the planted stray file listed as unplaced; ≤ 2 rounds on every folder; a test playbook line ("maps are in folder `Maps/`") changes the map accordingly.

- [ ] 🟥 **Step 6 (UI): Claims tab, New claims run form, runs list, Map & Rules view** — the reviewer can start a run and confirm the map in the browser.
  - [ ] 🟥 App shell: *Runs* / *Claims* tabs; Claims list with status chips and empty state; New claims run card (links, date, optional instructions textarea prefilled from playbook, zip upload in local mode) with inline validation
  - [ ] 🟥 Map & Rules view: table per subfolder, role dropdowns, reason on hover/expand, warnings above, *remember for <client>* per correction, **Confirm & verify** with disabled-state tooltip; `POST /api/claims-runs/{id}/confirm-map` saves the map + audit event + last confirmed map
  - **Verify:** in the browser: start a run from the zip → land on Map & Rules → hover a role and read its reason → change one role, tick remember → Confirm; the audit trail shows who confirmed and what changed; the client's last confirmed map is stored; screenshot kept as proof.

### Phase 3: Verify — one worker per employee
- [ ] 🟥 **Step 7: Expense report reader (+ KM tab)** — the AI maps the sheet's structure, code pulls and audits the numbers.
  - [ ] 🟥 Report tab: AI returns column roles + header cells + row span + total cell (coordinates only); code extracts; audit: amount × rate = total per row, rows sum to total, header name = mapped employee, dates within the ER period; ≤ 3 rounds; `REPORT_UNREADABLE` after that
  - [ ] 🟥 KM tab: same; code checks km × rate = amount and rate ∈ profile rates; each report mileage line (per trip) pairs with a KM row by date + amount → `MILEAGE_RATE`, `MILEAGE_LINE_MISMATCH`
  - [ ] 🟥 Category by the client's rule (LinkedIn: stated purpose) from the report's *Expense Types* list, with the quoted header text; `CATEGORY_UNCLEAR` when unsettled
  - **Verify:** on the sample: every row of every report matches ground truth (dates, items, amounts, currency, totals); the mixed-category report gets *Company Event* with the header quoted; a deliberately scrambled tab ends as `REPORT_UNREADABLE` without stopping the employee.

- [ ] 🟥 **Step 8: Evidence page inventory** — every receipt and map trip found on every page, with where it is.
  - [ ] 🟥 Page classify + read ("extract" role): receipts page → list of receipts (vendor, date, amount, currency, position L/M/R, hard-to-read notes); map page → list of trips (date, purpose, from, to, "and back"?, km printed); other kinds named
  - [ ] 🟥 Receipts pages read twice; disagreement on amount/date → low-confidence; map pages re-rendered at full resolution for the km; "km unreadable" is a value, never a guess
  - **Verify:** on the sample the inventory equals ground truth: receipt count, amounts, positions per page; every map trip's km read; a receipt with a deliberately smudged amount comes out low-confidence, not wrong; cost per employee logged.

- [ ] 🟥 **Step 9: Matching + checks (code decides)** — the check catalogue from PRD Flow 3c–3e.
  - [ ] 🟥 Rows ↔ receipts: same day + same amount + same currency; AI tie-break only among candidates; flags `NO_RECEIPT` (with "searched N pages in M files"), `RECEIPT_AMBIGUOUS` (candidates listed with page + position), `DUPLICATE_RECEIPT`, `UNCLAIMED_RECEIPT`, `CURRENCY_MISMATCH`; receipt-optional items from the profile
  - [ ] 🟥 Mileage rows ↔ map trips: same date; km equal, or exactly double for a return trip; `MILEAGE_DISCREPANCY` (both numbers, page, reading tried), `MILEAGE_NO_MAP`
  - [ ] 🟥 No-report employee: receipts become the row list + `NO_REPORT`; employee summary (totals, category, counts); every flag carries file + page + position or sheet + row, and the basis it rests on
  - [ ] 🟥 A control the batch needs but cannot find is a run-level flag, never a silent skip (lesson from the MVP peer review): no listing link readable → `MISSING_REFERENCE` before output; profile has no mileage rate but the batch has mileage rows → `MISSING_REFERENCE`; the reviewer acknowledges or fixes and re-runs
  - **Verify:** every planted error in the sample is flagged with the expected code; the return trip and the Mobile-Allowance N row are **not** flagged; false flags ≤ 1 per employee; a script asserts every flag has a citation.

- [ ] 🟥 **Step 10: Worker runner** — per-employee workers, five at a time, failure isolated, retryable.
  - [ ] 🟥 Worker = steps 7–9 for one employee with only that employee's files; pool of 5; per-agent request cap; per-employee status + timing + cost in the diary; `POST /api/claims-runs/{id}/employees/{eid}/retry`; run → `ready` when all employees are done or failed
  - **Verify:** full run on the sample completes under 5 minutes; a forced model error for one employee marks only that employee failed while nine finish; retry succeeds; two consecutive clean runs.

- [ ] 🟥 **Step 11 (UI): Verifying progress + Review view** — clear flags fast, with the evidence in front of you.
  - [ ] 🟥 Verifying: employee chips with states, overall bar, 3‑second polling, per-employee *Retry*
  - [ ] 🟥 Review: employee summary table; flag cards grouped by employee reusing the shared flag card / field editor; **evidence preview endpoint** for claims files (`/preview?page=n`) with the receipt position highlighted; *Accept* / *Exclude with note* / *Fix a value* (audited; instant per-employee re-check; flags auto-resolve or raise) / *Re-verify employee*; "all flags resolved" banner
  - **Verify:** in the browser: watch chips move; open a `NO_RECEIPT` card and see the cited page; fix the RM 10 row → the mismatch flag resolves by correction; exclude a flag with a note; audit trail lists each action; screenshots kept.

### Phase 4: Output — the listing rows
- [ ] 🟥 **Step 12: Listing header map + batch rows + gate** — one row per employee in the client's own column order.
  - [ ] 🟥 Read the linked listing's header row (AI maps headers, as the bank template today; code emits); fallback minimal set with a notice; one row per included employee (received date, name, ER code, category/GL, MYR total, remark); totals recomputed independently with `Decimal`; server-side gate while any flag is open; *not included* list; TSV sanitised as today
  - **Verify:** with one flag open the API returns no output; after clearing, rows = included employees, header order equals the sample listing's; excluding an employee changes totals and reconciliation stays green (hand-checked); a listing with a scrambled header triggers the fallback notice.

- [ ] 🟥 **Step 13 (UI): Output view** — totals, reconciliation, copy block, not-included list, locked state.
  - **Verify:** in the browser: locked message with open flags → unlocked after review → copy block pasted into a spreadsheet lands in the right columns; reconciliation line green; screenshot kept.

- [ ] 🟥 **Step 14: End-to-end verifier** — proof, repeatable.
  - [ ] 🟥 `backend/scripts/verify_claims_run.py`: runs the sample end to end, simulates a competent reviewer (fixes the RM 10 row, excludes the acceptable N flag), asserts every planted error found, false positives ≤ 1/employee, every flag cited, the gate, totals, listing header order
  - **Verify:** `ALL CHECKS PASSED` on two consecutive runs; the run's AI cost printed.

### Phase 5: Steering v1 — the few things a client needs to say
- [ ] 🟥 **Step 15: Client profile + playbook + Settings UI** — rates, receipt-optional items, tolerances, the paragraph.
  - [ ] 🟥 Per-client profile stored under the client name (rates by vehicle, km tolerance default 0, receipt date window default same day, receipt-optional items, mileage item pattern, checks on/off) + playbook text + last confirmed map; snapshot taken per run; *Settings → Claims* section with save confirmation and "set by reviewer on <date>"
  - **Verify:** change the car rate → the next run flags `MILEAGE_RATE` on every trip; add *Taxi* to receipt-optional → the Taxi N flag disappears; the playbook prefills the New-run form; a run started before a settings change is still judged by its snapshot.

- [ ] 🟥 **Step 16: Docs + Windows handoff** — so the enterprise test can include claims.
  - [ ] 🟥 README: Claims section, .env notes; `docs/WINDOWS-AGENT-TASK.md`: add "nested folder read through the real MCP" and "listing header read on a real Summary of Invoices" to the checklist; PRD status line updated
  - **Verify:** `start.bat`/`start.sh` serve the Claims tab; docs read through once by the owner.

### Phase 6: v2 — Discovery & learning (start after Phase 4's verifier passes; confirm sequencing first)
- [ ] 🟥 **Step 17: Discovery (PRD Flow 2b)** — the app proposes the client's rules from its own files, with evidence.
  - [ ] 🟥 Evidence gathering: *Expense Types* tab → category list; *KM* tab → rates; how employees fill mileage (per trip vs summed); listing past tabs → how ER rows were categorised; policy doc if present; optional links to previous batches
  - [ ] 🟥 Proposals with evidence per line; *Rules* panel beside the map (accept / edit / reject / "no evidence — please set"); confirmed values → profile + playbook with evidence and date; light pass on later runs → `RULE_DRIFT` note
  - **Verify:** on Client A with an **empty** profile, discovery proposes: the 23 categories, RM 0.64 / 0.35, per-trip mileage, Mobile Allowance receipt-optional, `_Approval.pdf` ignore, "category follows stated purpose" with the count of matching past rows; adding a new category to the template raises `RULE_DRIFT` on the next run.

- [ ] 🟥 **Step 18: Learn from decisions (PRD Flow 5)** — reviewer decisions become proposals.
  - [ ] 🟥 End-of-review proposals from exclude notes, corrections and category choices; accept → profile/playbook with the run as evidence; audited
  - **Verify:** exclude three `NO_RECEIPT` flags on the same item with a note → one proposal appears → accept → the next run raises none for that item; the audit trail shows the acceptance.

- [ ] 🟥 **Step 19: Client B (deliberately different) + success criterion 5** — proof it isn't a LinkedIn tool.
  - [ ] 🟥 Generator adds Client B: flat folder, different report template and category list, one rate, one all-in-one PDF per person, different listing columns, its own ground truth
  - **Verify:** discovery on Client B needs ≤ 2 corrections; the batch verifies; the verifier passes on both clients.

- [ ] 🟥 **Step 20: Peer review + simplification pass** — as after the MVP.
  - **Verify:** review findings fixed and re-verified with two clean end-to-end runs on both clients.

## Rollback Plan
- Every step lands as its own commit — `git revert` any step cleanly.
- The module has its own tables and its own workspace folder (`runs/<id>/claims/`); dropping the `claims_*` tables and deleting those folders removes it entirely — the invoice pipeline's data is untouched.
- Nothing is ever written to SharePoint or to a client workbook, so there is no external state to undo.
- The client profile/playbook lives in app settings under the client name; *forget* on the Settings screen or deleting those keys resets a client.
- If a real-model step misbehaves, the run diary records every AI call's role, rounds and cost — read it before changing code.
