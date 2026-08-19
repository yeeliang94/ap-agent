# Employee Claims Verification Module — PRD

**Status:** Confirmed 2026-08-18 — all decisions and defaults signed off by the owner (see *Decisions* and *Confirmed defaults*) · **Written:** 2026-08-18 · **Owner:** William Chen
**Relationship to the rest of the app:** a *separate* run type inside the AP Agent.
The invoice pipeline (design doc: [ap-agent-design.html](../ap-agent-design.html))
is not changed by this work. Shared plumbing is reused; pipeline stages are not.

A few small operational details are still marked **[assumed]** in the flows;
they are settled on the first real batch and listed at the end. Everything
else below is confirmed.

---

## Overview

**Problem.** The managed-services team verifies employee expense claims by
hand. For each employee they open a SharePoint folder, read the expense-report
workbook row by row, hunt for each receipt inside a bundle of scans (three to a
page, in no order), check mileage against Google-Maps screenshots and a fixed
per-kilometre rate, and finally type one row per employee into the month's
payment listing ("Summary of Invoices"). A batch is up to 10 employees with
15–20 rows each — 150–200 individual checks — and every client company
submits in a slightly different shape.

**Solution.** A "Claims run": given a link to the batch's SharePoint folder,
the app maps the folder, verifies every claim row for every employee
(receipt found, arithmetic holds, mileage matches map and rate), raises
flags that cite exactly where to look, and — once a person has cleared the
flags — produces one copy-ready listing row per employee. How it looks and
what it checks is steered per client by a *profile* (settings), a *playbook*
(short notes in the team's words) and *memory* of the previous batch.

**Target user.** The managed-services AP reviewer — the same person who runs
the invoice pipeline today. Single user, no login, as today.

**Success criteria.**
1. On the synthetic sample (10 employees, planted errors), **every planted
   error is flagged**, and false flags average **≤ 1 per employee**.
2. **Every flag cites a place**: file + page (+ left/middle/right for a
   receipt) or sheet + row. Clicking the flag shows that place.
3. **Second run, same client, no corrections**: with the first run's
   confirmed map remembered, the second run's map needs no manual changes
   on the sample.
4. A 10-employee batch **finishes in under 5 minutes** with 5 parallel
   workers, and its AI cost is recorded in the run diary. *(Confirmed.)*
5. **Works for a second, differently-shaped company without code changes.**
   On a second synthetic client (flat folder, different report template,
   different category list and rates, different listing columns), the
   *discovery* step proposes that client's rules from its own files, and the
   reviewer needs to correct **at most 2 items** before the batch verifies
   correctly.

---

## Universal rules vs. company facts

Most of what was agreed while shaping this came from one company
(LinkedIn Malaysia). The design keeps the two apart on purpose:

- **Universal rules** are the same for every client and are built into the
  checks: arithmetic must hold to the cent; every claim row needs evidence
  unless a *known, confirmed* rule excuses it; one receipt supports one row;
  a mileage amount is km × rate and the km must be backed by a map; nothing
  uploaded vanishes silently; every flag cites a place; code decides
  pass/fail; a person clears flags before anything goes out.
- **Company facts** are *values* the universal rules need — the category
  list and GL codes, how a mixed report gets its listing category, the
  mileage rates, which items are receipt-optional, how tight the receipt
  date match is, the file-naming convention, which tab is what, the
  listing's columns. **These are never hard-coded.** The app *discovers*
  them from the company's own files and past practice, shows each one with
  the evidence it rests on, a reviewer confirms, and from then on code
  applies them (see *Discovery* in Flow 2 and *Steering* below). When a
  case has no precedent, the app asks — and the answer becomes precedent.

The *Decisions* table at the end marks each item as universal or as this
company's confirmed value.

---

## User Stories

| # | Story | Priority |
|---|---|---|
| 1 | As a reviewer, I want to start a claims run from a SharePoint folder link so that every employee's claims in the batch are checked without me opening each folder. | MUST HAVE |
| 2 | As a reviewer, I want to see — and correct — the agent's map of the folder **and the rules it has worked out for this client** before verification runs, so it looks in the right places and applies the right values, and I want my corrections remembered. | MUST HAVE |
| 3 | As a reviewer, I want every claim row checked against its receipt, and every mileage row against its map and the fixed rate, with flags that cite exactly where to look, so I only spend time on problems. | MUST HAVE |
| 4 | As a reviewer, I want — once the flags are cleared — one copy-ready listing row per employee with totals that reconcile, so I can paste into the month's Summary of Invoices. | MUST HAVE |
| 5 | As a reviewer, I want the decisions I make on flags offered back to me as rules for this client, so the agent improves per client without anyone editing prompts. | MUST HAVE — *promoted and confirmed 2026-08-18: this is how "no precedent" cases become precedent; built last within v1* |

---

## Detailed User Flows

Terms used below:
- **Claim map** — the app's written-down understanding of the folder: which
  subfolder is which employee, which file is the expense report, which files
  are receipt bundles, which files to ignore.
- **Worker** — one AI-assisted process that verifies one employee and sees
  *only* that employee's files. Several run at once.
- **Flag** — one thing a person must decide, with a reason and a cited place.
- **Profile / playbook** — see *Steering* under Technical Approach.

### Flow 1 — Start a claims run (Story 1)

**Trigger.** Reviewer opens the *Claims* tab and clicks *New claims run*.

**User input.**
- The SharePoint link to the batch folder (the folder that *contains* the
  per-employee subfolders, e.g. `…/Claims/Jan26/`). Pasted from the browser
  address bar, exactly as today's reference-folder link.
- A link to **this month's payment listing workbook** ("Summary of
  Invoices"). The app reads its **header row** so the batch rows come out
  in the client's own column order — nothing about the columns is
  hard-coded. *(Decided.)*
- The **received date** to write in every listing row (one date for the
  batch). *(Confirmed.)*
- The client is the one set in Settings (single client at a time, as today).
- For local development only: a `.zip` of the same folder tree instead of a
  link.

**System response.**
1. Validate the link (same parser as today's SharePoint folder setting).
   Create a *ClaimsRun* record, status `queued`. Start a background job.
2. **Survey (code, no AI).** Resolve the link through the SharePoint reader,
   list the batch folder and every subfolder (up to 3 levels deep
   *(confirmed)*), download every file into the run's own workspace (a
   private copy, like the reference files today — the run is judged against
   the files as they were when it started). Record for each file: path,
   size, type, page count (for PDFs), and the `ER(...)` code if the file
   name has one. Enforce quotas: max 30 employee folders, 60 files and 200
   pages per employee, 25 MB per file *(confirmed)*. Over quota → refuse
   with the quota named.
3. **Peek inside every file (code, cheap).** So the agent can find things
   *by itself* rather than being told where to look: for a workbook, the
   tab names and the first ~15 rows of each tab as text; for a PDF or
   image, a small rendering of page 1 plus the page count. No full reads
   yet — this costs a few small looks per employee.
4. **Map (AI proposes, with reasons).** The AI is shown the survey listing
   *and the peeks*, plus — only if they exist — the client's playbook and
   *last confirmed map* (as a worked example). It answers with a proposed
   claim map, **giving a reason for each role**: for each subfolder,
   whether it is an employee, the employee's name and `ER(...)` code; which
   file *and tab* is the expense report ("tab `Expense Report`: name header,
   Date / Expense Item / Amount columns"); which tab holds mileage ("tab
   `KM`: km and rate columns"); which files are receipt bundles ("page 1
   shows three till receipts"); which files to ignore ("`_Approval.pdf` is
   an email approval"; "`…ER(…).pdf` is a print of the report"); which
   files it could not place. If it finds no claim list for an employee, it
   says so and plans to build the rows from the receipts. A playbook line
   is needed only when a client does something the peek cannot reveal.
5. **Map audit (code, with a look-again loop).** Every subfolder is either
   an employee or explicitly "not an employee"; every file has a role or is
   listed as *unplaced*; one `ER(...)` code per employee and no code used by
   two employees; the tab called "the report" actually yields rows with
   dates and amounts that add up to a total; a file called "receipts"
   yields at least one receipt on its first page. What does not fit goes
   back to the AI with the mismatch, up to 3 rounds (the listing reader's
   loop). Remaining problems become *map warnings* shown with the map.
6. Status → `map_ready`.

**Output.** The run appears in the Claims list with status and counts
("10 employees, 47 files"). Opening it shows the map (Flow 2).

**Error states.**
- Link cannot be resolved / SharePoint session expired → run fails with the
  structured "source unavailable" message; never a sign-in popup.
- Batch folder has no subfolders and no claim-looking files → run fails:
  "Nothing that looks like a claims batch was found in `<folder>`."
- Quota exceeded → run refused before any AI call, naming the quota.
- Map AI call fails or returns an unusable answer → retried once; then the
  run fails as `could not map folder`, and the survey listing is shown so the
  reviewer can add playbook hints and start again.

### Flow 2 — Confirm or correct the map (Story 2)

**Trigger.** Run status is `map_ready`. **In v1 the run always pauses here
confirmed 2026-08-18)**; confirming is one click when nothing needs changing.
Auto-continuing on a clean map is a later option.

**What the reviewer sees.** One table row per subfolder:
folder · employee name · `ER(...)` code · report file + tab · mileage tab ·
receipt files · ignored files · unplaced files · warnings — and, on hover,
**the agent's reason** for each role ("tab `Expense Report`: name header,
Date / Expense Item / Amount columns"), so a wrong guess is obvious at a
glance.

**User input (all optional).**
- Change a file's role: *report* / *receipts* / *ignore* / *not a claim file*.
- Edit an employee's name or code; mark a folder *not an employee*.
- Mark an employee *no report — build rows from receipts*.
- Tick *remember for <client>* next to a correction: the app turns it into a
  file-role pattern (e.g. `*_Approval.pdf → ignore`) in the client profile.
- Click **Confirm & verify**.

**System response.**
- Validation before confirm: an employee must have at least one receipt file
  or a report; a report file must open. Problems shown inline; cannot
  confirm until fixed or the employee is marked *skip this employee*.
- On confirm: the map (with the reviewer's changes) is saved with the run;
  an audit event records who confirmed and what changed; the map is stored
  as the client's *last confirmed map*; status → `verifying`; workers start
  (Flow 3).

**Output.** Status changes to *Verifying — 3 of 10 employees done*.

**Error states.** Reviewer changes make the map invalid → inline message,
confirm disabled. Reviewer closes the page → nothing is lost; the run stays
at `map_ready`.

#### Flow 2b — Discovery: the app works out this client's rules (Story 2)

**Trigger.** Runs automatically with the map on a client's **first** run,
whenever the reviewer clicks **Re-learn this client**, and — in a light
form — on every run, to notice anything that no longer matches the
confirmed rules.

**What the app reads (all read-only, all already downloaded for the run).**
- *This batch's files:* the report template's tabs — an *Expense Types*
  tab (or the dropdown list) gives the category list with GL codes; a *KM*
  tab gives the mileage rate(s) in use; the header block gives the fields
  available (purpose, approver, period). How the employees actually filled
  the reports — e.g. do they list mileage per trip or as one line; do
  "receipt included = N" rows cluster on certain items.
- *The listing workbook linked at run start, all tabs:* its column headers;
  how past employee `ER(...)` rows were categorised (Category / GL per
  employee); typical amounts.
- *Previous batches, if links are given (optional):* pairs of "this
  report → that listing row" are the strongest evidence for how a mixed
  report gets its category.
- *The client's expense policy, if one is in the reference folder:* rates,
  receipt thresholds, per-item rules.

**What the app proposes — every line with its evidence.** A *proposed
client profile*, for example:
- "Categories: 23 items with GL codes — from tab *Expense Types* of
  `Aegene Ong_ER(...).xlsx`."
- "Mileage rates: RM 0.64/km (seen on the KM tab of 6 reports); RM 0.35/km
  (1 report) — car and motorcycle?"
- "Listing category for a mixed report: follows the report's *stated
  purpose* — 12 of 14 past ER rows in tabs Dec'25–Jan'26 match the purpose
  written on the report; 2 match the largest line instead."
- "Mileage: 9 of 10 employees list one report line per trip; Aegene sums it
  — treat per-trip as the norm and flag the exception?"
- "Receipt-optional: *Mobile Allowance* rows had no receipts in 4 past
  reports and were paid — propose receipt-optional."
- "File roles: `*_Approval.pdf` never referenced by anything — propose
  ignore."
Anything it cannot find is listed as *no evidence — please set*, with a
sensible default shown.

**User input.** Accept, edit or reject each proposed line; type any rule the
files cannot show; click **Confirm rules** (on the first run this is the
same screen as the map). Confirmed values go into the client profile
(structured) and, where they are prose, into the playbook.

**System response.** The confirmed profile is stored per client with the
evidence and the date; an audit event records it. Workers use only
confirmed values. On later runs the light pass compares the batch to the
confirmed rules and raises a `RULE_DRIFT` note when something has changed
("a new category *Wellness (740050)* appears in the template — add it?").

**Error states.** No template, no past listing, no policy → the proposed
profile is mostly *no evidence — please set*; the reviewer types the values
once and the run proceeds. This is expected for a brand-new client with a
bare folder.

### Flow 3 — Verify every employee, then review flags (Story 3)

**Trigger.** Map confirmed.

**System response — one worker per employee, up to 5 at once,
each within the app's per-agent request cap.** Every worker runs the same
fixed sequence of *checks* (a check = a small procedure with its own prompt,
code and test). The AI reads pictures and structure; code does every
comparison and every sum.

**3a. Read the expense report** (skipped if the employee has no report).
- Open the workbook's report tab. The AI maps the *structure* — which column
  is date / expense item / reason / "receipt included" / amount / currency /
  rate / total, where the header block is (name, department, period), which
  rows are expense lines, where the total is. It answers with coordinates
  and never retypes a number. Code pulls the values from those cells.
- Code audits the reading: each row's amount × rate = total (to the cent);
  rows sum to the report total; the name in the header is the employee in
  the map; row dates fall inside the `ER(...)` period. An audit failure goes
  back to the AI with the mismatch, up to 3 rounds — the same loop the
  listing reader uses. Still failing after 3 rounds → flag
  `REPORT_UNREADABLE`; the worker continues with receipts only.
- Mileage tab (if the map names one): same method. Rows are trips: date,
  description / from–to, km, rate, amount. Code checks each row: km × rate
  = amount (→ `MILEAGE_ARITHMETIC` when off), and the rate equals one of
  the client's fixed rates (car RM 0.64, motorcycle RM 0.35 for this
  client; from the profile) → `MILEAGE_RATE` when not. A row with no rate
  typed is judged by amount ÷ km against the same rates, so the check is
  never skipped silently.
- **Mileage lines on the report tab are one line per trip** *(decided)*.
  Code pairs each such line with the KM-tab trip of the same date and
  amount; a report line with no KM row, or a KM row with no report line →
  flag `MILEAGE_LINE_MISMATCH` naming both places. Mileage lines are
  recognised by the profile's mileage item pattern (e.g. item name contains
  "Mileage") **[assumed]**.
- **The employee's category for the listing row — decided by precedent.**
  The category must come from the client's own category list (discovered:
  the *Expense Types* tab). *Which* category a mixed report gets follows the
  client's confirmed rule from Discovery. For LinkedIn the confirmed rule is
  **the report's overall purpose** — the "Business Reason for the Report"
  header plus the detailed reasons on the lines (e.g. "Halloween", "Offsite
  retreat" → *Company Event*, GL 710010). The AI applies the rule and quotes
  the text it relied on; it is also shown the closest past examples from
  the listing ("Nick Goh, all-taxi report → Taxi") so it reasons the way
  this client has before. If the rule does not settle it, or there is no
  rule yet → flag `CATEGORY_UNCLEAR`; the reviewer sets the category on the
  employee's summary, the choice is audited, and it is offered as a new
  precedent (Flow 5).

**3b. Inventory the evidence pages.**
- Every receipt-bundle file is split into pages (PDF → image, as today).
  Each page is read by the AI once to say what it is: *receipts page* /
  *mileage-map page* / *copy of the report* / *other*, **and** in the same
  read: for a receipts page, the list of receipts on it (vendor, date,
  amount, currency, position left/middle/right, and anything hard to read);
  for a map page, the list of trips (date, purpose, from, to, whether the
  text says "and back", the km printed on the map).
- Receipts pages are read **twice** independently (as the pipeline does
  today, because scans are misread *confidently*); a disagreement on an
  amount or date marks that receipt low-confidence and it is shown as such
  in any flag it appears in. A row matched to a receipt whose date is
  missing, fits only after a day/month swap or the second read, or whose
  date / amount / currency is low-confidence → flag `EVIDENCE_UNCERTAIN`
  (open; a person confirms — never accepted silently).
- Map pages are re-rendered at full resolution (the km on those screenshots
  is tiny) and re-read for the km figure. A km that cannot be read → the
  trip is recorded as "km unreadable", never guessed. If the full-resolution
  re-read fails, the normal-resolution km is kept but marked low-confidence,
  and a mileage row matched on it → `EVIDENCE_UNCERTAIN`.

**3c. Match report rows to receipts — code decides.**
- Candidates for a row: same currency, same amount (to the cent), and
  **the same date** *(decided — same day only)*.
- Exactly one candidate → matched. Several → one AI tie-break call is
  shown the row's reason and the candidates' vendors; if it is unsure →
  flag `RECEIPT_AMBIGUOUS` listing every candidate with its page and
  position. None → flag `NO_RECEIPT` ("searched 9 pages in 2 files"),
  *unless* the row says "receipt included = N" **and** the profile lists
  that expense item as receipt-optional (e.g. Mobile Allowance) → an
  informational note instead *(decided: allowed for named items only)*.
- A receipt may support only one row. A second use → `DUPLICATE_RECEIPT` on
  both rows. Receipts matched to no row → `UNCLAIMED_RECEIPT` (warning, not
  a blocker).
- Foreign-currency rows *(decided: accept the typed rate, check the
  arithmetic)*: receipt currency must equal the row's; the row's rate must
  be present and not 1; amount × rate must equal the MYR total → else
  `CURRENCY_MISMATCH`. The rate itself is not second-guessed.

**3d. Match mileage rows to map trips — code decides.**
- Candidates for a mileage row: map trips on the same date **[assumed]**.
- Compare km: claimed vs printed. **Any difference flags** *(decided)*: the
  claimed km must equal the printed km, **or** exactly twice it when the
  narrative says a return trip ("and back", "to … and back to home").
  Otherwise → `MILEAGE_DISCREPANCY` with both numbers, the page, and which
  reading (one-way / return) was tried. (The profile keeps a tolerance
  field, default 0, so a future client can loosen this without code.)
- No map trip for the date → `MILEAGE_NO_MAP`. Map trip with no claimed
  row → informational.

**3e. Employee summary.** Report total (or, with no report and no playbook
rule, the receipts found become the claim list and the employee is flagged
`NO_REPORT` so a person confirms the derived list — *decided*); the
employee's category and GL (from 3a); expected listing amount in MYR; count
of rows verified / flagged. Progress counter updates.

**What the reviewer sees.** The run's *Review* view: one card per open flag,
grouped by employee, each with the reason, the basis (the rule and where it
came from — "client profile: car RM 0.64/km", "report tab row 12"), and a
preview of the cited page with the receipt's position named. Actions per
flag, as today: **accept** (it's a real problem — the row is excluded from
the batch), **exclude the flag** (with a note — the row stays), or **fix a
value** (e.g. the amount was misread): the fix is audited and the affected
checks for that employee re-run instantly. A **Re-verify employee** button
re-runs one worker.

**Error states.**
- A worker fails (model error, request cap reached) → that employee is
  marked *failed* with the reason; the other employees continue; the
  reviewer can retry that employee alone.
- Source becomes unavailable mid-run → the run fails as *source
  unavailable*; nothing partial is treated as complete.
- A file the map called a report cannot be read as a workbook → the employee
  gets `REPORT_UNREADABLE` and continues on receipts.

### Flow 4 — Batch output (Story 4)

**Trigger.** Reviewer opens the *Output* tab. The server refuses to build
outputs while **any** flag is open — the same gate as today.

**System response.**
- **Columns come from the client's listing** *(decided)*: the app opens the
  listing workbook linked at run start, the AI maps its header row (which
  column is Received Date, Category, GL Account, Name of the Vendor,
  Invoice Number, amount, …) exactly as the invoice pipeline already maps
  the bank template's headers, and code emits the rows in that order. A
  header the app has no value for is left blank for a clean paste. If the
  header row cannot be mapped → the output falls back to a fixed minimal
  set (Name, `ER(...)` code, Category, GL, Amount MYR) and says so.
- One row per employee: received date (typed at run start), employee name,
  `ER(...)` code, the employee's category and GL (by the client's confirmed
  rule — 3a), amount in MYR, remark.
- Totals reconciled independently from the emitted text: sum of employee
  rows = sum of the report totals of included employees; excluded rows are
  subtracted and named. Employees marked failed or skipped are listed under
  *not included*, with why.
- Copy button → tab-separated rows for a clean Excel paste, sanitised as
  today.

**Output.** Totals cards, the reconciliation line (green/red), the copy
block, the *not included* list.

**Error states.** Any open flag → "Review is not complete: 4 flags open"
and no output. Reconciliation mismatch → red line naming the difference;
copy still allowed but the mismatch is written to the run diary.

### Flow 5 — Learn from decisions (Story 5)

This is the ongoing half of Discovery: Flow 2b learns from the *files*;
this flow learns from the *reviewer*. When a run's review is complete, the
app looks at the reviewer's *exclude* notes, *fix a value* corrections,
category choices on `CATEGORY_UNCLEAR`, and repeated decisions, and
proposes profile changes and playbook lines — e.g. "6 `NO_RECEIPT` flags on
*Mobile Allowance* rows were excluded with note 'allowance, no receipt':
mark *Mobile Allowance* receipt-optional for <client>?" or "You set
*Company Event* on 3 mixed reports whose stated purpose was an offsite:
confirm rule 'category follows stated purpose'?" Each proposal is accepted
or dismissed by the reviewer. Nothing enters the playbook or profile
without a click. Accepted proposals are audited and carry the run they came
from as their evidence.

---

## Technical Approach

**Stack (plain language).** The same as the rest of the app, so nothing new
to install or operate:
- Python backend (FastAPI) with the AI agents built on PydanticAI, and
  **every AI call going through the one model factory** the enterprise rules
  require — no direct provider clients. Existing model roles are reused:
  the "extract" role reads pages, the "judge" role maps folders and breaks
  ties.
- SQLite for storage; React/TypeScript for the screens.
- SharePoint stays read-only through the existing reader; it gains the
  ability to walk subfolders and download files under a folder.
- PDFs become images with the existing PDF renderer; workbooks are read
  with the existing spreadsheet library.

**Where the code lives.** A new package `backend/app/claims/` (survey,
mapping, discovery, report reader, page inventory, matching, mileage,
batch, runner, profile) and new API routes under `/api/claims-runs`. A
*Claims* tab in the frontend with two screens (list, run detail with Map &
Rules / Review / Output views). `backend/app/pipeline/*` is not modified. A
synthetic sample generator (`samples/generate_claims_sample.py`) builds
**two** clients, each with planted errors and a ground-truth file, so
nothing is ever tested on real employee data:
- *Client A, LinkedIn-shaped:* 10 employee folders, `ER(...)` naming, the
  report template with Expense Types and KM tabs, receipts three to a page
  in random order, map pages at the back, a "Summary of Invoices" listing
  with past tabs.
- *Client B, deliberately different:* a flat folder (no subfolders), a
  simpler report template with a different category list and one mileage
  rate, one all-in-one PDF per person, a listing with different columns.
  Success criterion 5 is measured on this client.

**Steering — how the agent is told what a client does.** The AI starts
every run with no memory; the app decides what it is shown. The values in
1 and 2 are **discovered, not typed**: Discovery (Flow 2b) proposes them
from the client's files with evidence, and Flow 5 keeps them current from
reviewer decisions; a person only confirms or corrects.
1. **Client profile** (structured values; code reads them, applied
   exactly): the category list with GL codes; the category rule for mixed
   reports; mileage rates by vehicle type; km tolerance (default 0 =
   exact); receipt date window (default same day); receipt-optional expense
   items; file-role patterns; mileage item pattern; mileage layout (per
   trip / summed); which checks are on. Each value carries its evidence
   ("from tab *Expense Types*", "12 of 14 past listing rows", "set by
   reviewer on run 3"). Editable on the Settings screen. (Listing columns
   are *not* here — they are read from the client's listing each run.)
2. **Client playbook** (half a page of plain-language notes; the AI is
   shown it at the map step and in every worker): where things are and what
   matters for this client. Discovery drafts it; the team edits it. It
   steers *where to look*; it never decides pass/fail.
3. **Check catalogue** (fixed, tested): the checks in Flow 3. The playbook
   and profile can turn them on/off and tune them; a *new* kind of check is
   a developer change.
4. **Memory**: the client's last confirmed map is shown to the AI as a
   worked example; the closest past listing rows are shown when a category
   is being decided; reviewer decisions become proposals (Flow 5).
Every flag names the rule it applied and where the rule came from, so the
steering is always visible on the card.

**Key dependencies.** Existing: PydanticAI + the model factory (OpenAI
locally, the enterprise proxy on Windows), the SharePoint MCP reader,
pymupdf, openpyxl, Pillow. No new external services.

**Data model (plain terms).**
- **Claims run** — one batch: client, folder link, listing link and the
  header map read from it, received date, status
  (`queued → surveying → mapping → map_ready → verifying → ready / failed`),
  progress counters, the confirmed map, the settings snapshot it ran under,
  outputs.
- **Employee** (per run) — folder, name, `ER(...)` code, roles of its files,
  report totals, category + GL (with the header text the AI relied on),
  status (pending / verified / failed / skipped).
- **Claim row** — one expense or mileage line: which sheet and row it came
  from, its values, what evidence it matched, its verdict.
- **Evidence** — one receipt or one map trip found on a page: file, page,
  position, values read, confidence.
- **Flags, audit events, run diary** — reused from the existing tables, with
  a reference to the employee and row a flag is about.
- **Client profile, playbook, last confirmed map** — stored per client name,
  so several clients can coexist later even though the app runs one at a
  time today.
- Every run keeps its own copy of the files it read.

---

## Scope Boundaries

**In scope (v1).** Flows 1–5 as written, including Discovery (2b); the
check catalogue in Flow 3; profile + playbook + last-map memory; the two
synthetic clients with ground truth and an end-to-end verifier script; the
Claims tab; per-employee retry; audited corrections.

**Out of scope (for now).**
- Writing the batch rows into a draft tab of the client's listing workbook
  (the listing writer exists, but this client's layout differs; wire in
  later once the column question is answered).
- Policy category / cap checks on claim rows (the existing invoice-pipeline
  skill could be switched on per client later).
- Reading the approval PDF (explicitly not needed).
- Inferring vehicle type; per-employee vehicle records.
- Auto-continuing past the map when it is clean (v1 always pauses).
- Several clients side by side in the UI; login; notifications.
- Any write to SharePoint or to the client's workbook.

**Known limitations (v1).**
- The km on map screenshots is small text; a blurry screenshot yields "km
  unreadable" and a flag, not a guess.
- Two receipts with the same amount and nearby dates cannot be told apart
  by code; they surface as `RECEIPT_AMBIGUOUS` for a person.
- Faded thermal receipts may be low-confidence after the double read; the
  flag says so.
- The first run for a new client will need map corrections; that is the
  design, not a defect.
- Row-date-within-period checks depend on the `ER(dd MMM yy – dd MMM yy)`
  file-name convention; clients without it simply skip that check.

---

## Decisions (confirmed by the owner, 2026-08-18)

*Scope* says whether a decision is a **universal** rule built into the
checks, or **this client's** value — a starting profile for LinkedIn
Malaysia that Discovery must be able to find (or the reviewer confirm) for
any other client. Client values are never hard-coded.

| # | Question | Decision | Scope |
|---|---|---|---|
| 1 | Category / GL on the employee's listing row when the report mixes categories | Category comes from the client's own list; *which* one follows the client's confirmed rule, learned from precedent. LinkedIn's rule: **the overall purpose of the report** ("Business Reason" header + line reasons; an offsite → *Company Event*, GL 710010). AI applies, cites, sees past examples; `CATEGORY_UNCLEAR` when unsettled; reviewer's choice becomes precedent. | Mechanism universal; **rule is this client's** |
| 2 | Listing columns | **Not hard-coded.** A link to the month's listing is given per run; the AI reads its header row; rows are emitted in that order. | Universal |
| 3 | Km tolerance | **Any difference flags** — exact, or exactly double for a return trip. Profile field, default 0. | Universal default; client may loosen |
| 4 | Mileage on the report | **One report line per trip**; KM tab is the working; lines pair with KM rows by date + amount. Discovery detects per-trip vs summed from how employees fill it. | **This client's** |
| 5 | "Receipt included = N" | **Allowed only for items the profile names** (LinkedIn: e.g. Mobile Allowance); otherwise `NO_RECEIPT`. Discovery proposes the list from past paid rows without receipts. | Mechanism universal; **item list is this client's** |
| 6 | Receipt date window | **Same day only.** Profile field. | Universal default; client may loosen |
| 7 | Foreign-currency rows | **Accept the typed rate; check the arithmetic** and that the receipt currency matches. | Universal |
| 8 | Receipts but no report, no playbook rule | **Build the rows from the receipts and flag `NO_REPORT`.** | Universal |
| 9 | Category list, GL codes, mileage rates (0.64 / 0.35), `ER(...)` naming, which tab is what | Discovered from the client's files (Expense Types tab, KM tab, file names, past listing tabs) and confirmed by the reviewer. | **This client's** |

## Confirmed defaults (owner: "default", 2026-08-18)

| Item | Confirmed value |
|---|---|
| Story 5 (learn from reviewer decisions) | MUST HAVE; built last within v1 |
| Delivery order | **v1 first** — Flows 1–4, Story 5, profile + playbook + last-map memory (plan Phases 1–5); **v2 after** v1 passes its verifier — Discovery (Flow 2b), `RULE_DRIFT`, the second synthetic client (plan Phase 6). Nothing dropped, only ordered. |
| Pause at the map | Always, in v1 (one click); auto-continue on a clean map is a later option |
| Received date on listing rows | Typed by the reviewer when starting the run |
| Quotas | 30 employee folders per batch; 60 files / 200 pages per employee; 25 MB per file; over a limit the run refuses and names it |
| Speed target | Under 5 minutes for a 10-employee batch |

Smaller details still marked **[assumed]** in the flows: map-trip date must
equal the KM row's date; mileage lines are recognised by an item-name
pattern ("Mileage") in the profile; which items are receipt-optional for a
given client is profile content to be filled in on the first real batch.
