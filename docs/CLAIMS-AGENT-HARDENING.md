# Claims Agent Hardening Plan — Tool-Using Investigation and Full-Folder Dumps

**Status:** Rebaselined after delivered R1–R10 and review fixes; H1–H12
remain proposed

**Written:** 2026-08-19

**Baseline reviewed:** `330b972`

**Owner decision:** Do not build a company-recipe feature. No automatic rule
learning, recipe promotion, or cross-run recipe drift workflow.

**Extends:** [Employee Claims Verification plan](PLAN.md) and
[PRD](PRD.md)

**Invoice pipeline:** Out of scope; `backend/app/pipeline/` remains untouched.

## Outcome

A reviewer gives the Claims module a folder and a short objective such as:

> Check the expense records and all supporting evidence, group what belongs
> together, reconcile every line and total, and show me anything that does not
> agree.

The system must handle all of these without company-specific code:

1. One folder per claimant, with a report workbook and receipt bundles.
2. A flat folder containing several claimants' reports and evidence.
3. A full folder dump containing evidence but no per-claimant folders.
4. Evidence-only submissions with no claim summary.
5. A master workbook containing several claimants.
6. A familiar input whose layout changes in a later month.

The agent may decide how to investigate and may use file, workbook, document,
calculator, and isolated Python tools. It may not decide that an unsupported
payment is safe. Code enforces the universal controls, and a reviewer confirms
grouping, ownership, material assumptions, and every blocking Flag.

## Scope decision: no company recipe

This plan deliberately excludes a reusable or learned company recipe.

- The agent creates an **Investigation Plan for the current Claims Run only**.
- The plan, tool calls, calculations, and source hashes are stored for audit and
  replay, but are not applied automatically to a later run.
- The existing Explicit Client Profile remains for facts a reviewer deliberately
  maintains: mileage rates, tolerances, category values, receipt exceptions, and
  check toggles.
- Optional plain-language instructions remain. They apply to the current run;
  a saved playbook may prefill them, but it never overrides a source audit.
- The last confirmed map may remain as a non-authoritative worked example. It
  never bypasses a fresh inventory, grouping confirmation, or control check.
- Reviewer decisions do not silently update settings or create executable rules.

This is less automation than a recipe system, but it is simpler to explain and
safer when a company's process changes unpredictably month to month.

## What changes from the current module

The existing module is strong at a known claims shape but assumes the grouping
too early:

- `FolderMap` starts with one employee per subfolder, while root files can only
  be classified, not grouped into cases (`backend/app/claims/mapping.py`).
- `ClaimEmployee` owns rows, evidence, status, totals, and category
  (`backend/app/claims/models.py`).
- A no-report worker can derive rows from receipts, but only after an employee
  already exists (`backend/app/claims/worker.py`).
- Report, evidence, and listing readers adapt coordinates within fixed semantic
  forms (`report_reader.py`, `evidence.py`, and `listing.py`).
- `create_agent` provides a required output form but no investigation tools
  (`backend/app/model_layer.py`).
- Run instructions currently reach the folder mapper; they do not form the
  objective of the per-case worker.

The delivered R1–R10 work in `docs/PLAN.md`, including the review fixes through
`330b972`, is now the protected baseline. R1–R6 — the flag catalogue,
report-total handling, skipped subtotal rows, duplicate scans, cross-case
duplicate evidence, and material unclaimed evidence — becomes part of the
universal control layer. R7–R10's cards, filters, all-lines table (PLAN's
*All rows* table; relabelled to lines in H10), unused evidence, progress, and
output reconciliation are the presentation baseline.
They must be adapted from employee ids to case ids, not rebuilt as a parallel
review surface and not replaced with AI judgment.

## Design principles

1. **Claim Case, not employee folder, is the organizing concept.** A Claim Case
   may have a confirmed, proposed, or unknown Claimant.
2. **Inventory before interpretation.** Every Source Artifact is hashed,
   snapshotted, inspected, and ultimately dispositioned.
3. **Agent investigates; code controls; reviewer authorizes.** Tool freedom is
   inside the investigation. Release conditions remain deterministic.
4. **Stable result, flexible implementation.** Input layouts and investigation
   steps may vary; the normalized result does not.
5. **Missing information stays missing.** Plausible ownership, purpose, category,
   or amount is not converted into fact.
6. **No direct side effects from the agent.** Tools read the run snapshot or
   write only to the run's temporary output area. The agent never writes to
   SharePoint, the source workbook, settings, or the database directly.
7. **Replayable money.** Every payment amount is reproducible with `Decimal`
   from cited source values and recorded transformations.
8. **Graceful degradation.** If Python is unavailable, the rest of the toolset
   still works and the run names the capability it could not perform.

## Target deep module and seam

Create one deep `ClaimsInvestigator` module. Its interface is the test surface:

```python
async def investigate(request: InvestigationRequest,
                      tools: InvestigationTools) -> InvestigationResult:
    ...
```

`InvestigationRequest` contains only:

- Claims Run id and immutable workspace path.
- Source Artifact manifest with hashes.
- Current-run instructions and objective.
- Snapshot of the Explicit Client Profile.
- Optional current listing, policy, roster, or historical references supplied
  for this run.
- Resource budget: wall time, model requests, tool calls, and bytes/pages read.

`InvestigationResult` contains only normalized domain results:

- Source Artifacts and their dispositions.
- Evidence Items with values, confidence, and Citations.
- proposed Claim Cases and Claimants.
- Evidence Assignments, including basis and status.
- Claim Lines, whether reported or evidence-derived.
- Flags, assumptions, and unresolved questions.
- the run-local Investigation Plan and tool-execution record.

The module hides prompting, tool selection, retries, sandbox mechanics, file
formats, and model-provider details. Callers and end-to-end tests should not
need to know which internal adapter produced the result.

Behind the seam there are exactly two implementations plus one test double:

- **Legacy structured-folder adapter:** the current mapper + per-employee
  worker path, wrapped unchanged. It is the H1 conformance baseline and the
  rollback path while `CLAIMS_AGENTIC_INVESTIGATION` is off.
- **The investigator:** one production implementation. It inventories first
  and only then chooses an internal *strategy* — structured (folder structure
  carries ownership), full-dump (global inventory and proposed grouping), or
  evidence-only (no summary found, so Claim Lines are derived). The caller
  never selects a strategy; a structured folder is a full dump that arrives
  with strong folder-based grouping signals, and evidence-only is a fact
  discovered after inventory, not an input type.
- **In-memory `InvestigationTools` fake:** scripted artifacts and tool results
  for deterministic tests. This is the seam H1 tests through. If an
  end-to-end test needs a whole scripted investigator, name that separately;
  do not blur the two.

The current readers remain internal implementations where they fit. They no
longer define what input shapes the external interface accepts.

## Normalized domain model

### Claims Run

Add an immutable manifest containing each file's relative path, byte size,
content hash, type, page/sheet count, and snapshot location. A run never
re-reads a live source after snapshotting.

### Source Artifact

One record per submitted file, even when it contains several Evidence Items.
Required fields:

- source path, content hash, media type, and size;
- inspection state and failure reason;
- proposed role and the reason/citations for it;
- disposition: `used`, `duplicate`, `irrelevant`, or `unreadable` (terminal),
  or `unresolved` (non-terminal);
- reviewer confirmation where required.

`unresolved` always blocks output until a reviewer sets a terminal disposition.
"Material" is not a judgment the agent makes about a file it could not
understand. `ignored` is not a terminal state. A file may be irrelevant, but the
reason must be recorded.

### Evidence Item

One extracted item from a Source Artifact. Start with receipt, map trip, report
line, approval, and other, but allow an `attributes` dictionary for source facts
that do not belong in the universal fields. Core fields are type, source
Citation, extracted values, confidence by field, and extraction method.

### Claim Case

One proposed payment-listing decision. Required fields:

- optional Claimant name and identifier;
- claimant state: `confirmed`, `proposed`, or `unknown`;
- case state: `proposed`, `confirmed`, `blocked`, or `excluded`;
- grouping basis and Citations;
- Claim Lines, assigned Evidence Items, category, and GL where known;
- optional Reported Total with its Citation;
- Calculated Lines Total derived independently from Claim Lines.

A Claim Case is allowed to exist without a Claimant so that the system can show
useful work without inventing who should be paid. A missing Reported Total stays
missing; the Calculated Lines Total must never be copied into that field to make
reconciliation appear complete.

### Evidence Assignment

Replace the single `matched_row_id` assumption with an explicit relationship:

- Evidence Item → Claim Case and optionally → Claim Line;
- state: `proposed`, `confirmed`, or `rejected`;
- basis: exact identifier, explicit name, supplied filename rule, report
  reference, reviewer decision, or AI inference;
- confidence and supporting Citations.

One Evidence Item cannot support two payable Claim Lines unless a reviewer
confirms that the source legitimately covers both.

### Claim Line

Keep universal money fields stable: date, description, claimed amount,
currency, rate, home amount, category, GL, and purpose. Preserve source-specific
values under `attributes` and cite each material value. Record origin as
`reported`, `evidence_derived`, or `reviewer_entered`.

An evidence-derived line is a proposal, not an approved claim amount.

## Full-folder-dump workflow

### 1. Snapshot and inventory

Walk the complete folder without assuming top-level folders are employees.
Enforce run-wide limits first; per-case limits apply only after cases exist.
Hash every file, reject path escapes, and build previews without executing
macros, formulas, links, or embedded code.

### 2. Inspect and classify

The agent uses tools to inspect likely workbooks, PDFs, images, and supported
documents. It proposes a role for every Source Artifact with a reason. Unknown
files remain unresolved and visible.

### 3. Extract identity signals

Collect claimant names, employee codes, email addresses, report references,
cardholder names, filename prefixes, approval subjects, and supplied roster or
listing references. Keep the source and confidence of every signal.

Identity policy:

- Explicit, unambiguous identity in a source may support a strong proposal.
- A filename convention supplied by the reviewer may support a proposal.
- Similar dates, merchants, amounts, or proximity in the folder never confirm
  ownership by themselves.
- Conflicting strong signals force `Claimant = unknown` and a blocking Flag.
- The reviewer always confirms the grouping before verification proceeds.

### 4. Propose cases and assignments

Cluster Evidence Items and summaries into Claim Cases. Each proposed assignment
must say why. Leave evidence unassigned when the basis is weak; do not optimize
for assigning everything.

### 5. Map & Group review gate

Replace the employee-only map screen with a case-oriented screen. There is one
map screen for every input shape: a structured folder arrives with its grouping
pre-proposed on a folder basis at high confidence. `MapView.tsx` is migrated
into it, not kept beside it (the same rule as for the review surface).

- proposed cases with Claimant, identifiers, totals, confidence, and reasons;
- Source Artifacts and Evidence Items inside each case;
- an unassigned pool;
- merge, split, move, create case, set Claimant, mark irrelevant, and mark
  unreadable actions;
- an explicit count of artifacts with no disposition;
- **Confirm grouping & verify**, disabled while ownership conflicts or unresolved
  potentially material artifacts remain.

Every action is audited. Confirmation converts proposed assignments to confirmed
assignments; AI inference alone never does.

### 6. Build or read Claim Lines

- If a summary/report exists, read its lines and total, then match evidence.
- If only evidence exists, create one proposed line per evidence item or one
  combined line only when the source explicitly supports aggregation.
- Raise `CLAIM_AMOUNT_UNCONFIRMED` for evidence-derived lines until a reviewer
  confirms the payable amount.
- Raise `PURPOSE_UNKNOWN` and `CATEGORY_UNCLEAR` when required output facts are
  absent.
- Preserve the current `NO_REPORT` behaviour as a compatibility description,
  but use `NO_SUMMARY` as the broader domain condition in new code.

### 7. Verify each confirmed case

Run case workers in parallel with per-run and per-case budgets. The worker sees
the case's confirmed artifacts plus read-only access to the global inventory for
cross-case duplicate checks. It receives the current-run objective and
instructions, not only a preselected report tab.

### 8. Review and output

The reviewer sees every case, line, Evidence Assignment, unused Evidence Item,
and Flag. Output remains locked until all blocking conditions are resolved.

## Investigation tools

The agent receives small, typed, allowlisted tools. Tools return data; they do
not write domain records directly.

| Tool | Capability | Required controls |
|---|---|---|
| `list_artifacts` | Search and filter the immutable manifest | Run-relative paths only |
| `inspect_workbook` | Sheets, tables, used ranges, names, formulas, hidden/merged areas | Cell/row/column limits; no macro execution |
| `read_cells` | Exact values/formulas for a bounded range | Range and text-size limits; provenance returned |
| `inspect_document` | Page count, text blocks, thumbnails, document metadata | Page/byte limits; embedded links inert |
| `render_page` / `crop_page` | Visual inspection and precise Citations | Image dimension and crop limits |
| `search_artifacts` | Search extracted text for names, IDs, totals, or references | Bounded results; source locations returned |
| `calculate` | Exact `Decimal` arithmetic and reconciliation | Expression grammar; no `eval`; operation cap |
| `compare_tables` | Join, group, diff, and sum bounded tables | Deterministic operations; input/output caps |
| `run_python` | Irregular read-only transformations in isolation | Disabled unless the sandbox exit gate is met |
| `record_proposal` | Add an in-memory proposed case, assignment, line, or assumption | Schema validation; no database write |

Tool outputs include a tool-call id, elapsed time, input artifact hashes, output
hash, truncation indicator, and error code. Large results are written to the
run's temporary output directory and returned by handle.

## Python sandbox hardening

Python expands capability and risk. It must be behind a `SandboxPort` with a
production adapter and an in-memory test adapter. Never execute model-generated
code inside the FastAPI process.

Production requirements:

- Ephemeral process/container identity for each execution.
- Source snapshot mounted read-only; one empty writable output directory.
- No network, inherited credentials, browser session, environment secrets,
  subprocess creation, or host filesystem access.
- Explicit allowlist of data libraries; macros and native extensions reviewed.
- Wall-time, CPU, memory, process, open-file, input-byte, and output-byte limits.
- Kill the entire execution tree on timeout or cancellation.
- Store code, dependency versions, stdout/stderr with redaction, output hashes,
  and exit status in the run record.
- Re-run successful scripts once when they affect money; outputs must be
  deterministic.
- Parse output through the same normalized schemas and controls as any other
  adapter.

Do not treat Python AST filtering as a security sandbox. If the Windows
enterprise environment cannot provide OS-level isolation, ship workbook,
document, calculator, and table tools first and leave `run_python` disabled.
The run should raise `TOOL_UNAVAILABLE` only when the requested investigation
genuinely cannot be completed without it.

## Deterministic universal controls

These controls are outside the agent and cannot be disabled by instructions:

1. **Snapshot integrity:** every Citation resolves to the hash captured at run
   start.
2. **Artifact completeness:** every Source Artifact must reach a terminal
   disposition before output; any `unresolved` artifact blocks output until a
   reviewer dispositions it.
3. **Ownership:** every payable Claim Case has a reviewer-confirmed Claimant and
   required identifier.
4. **Assignment exclusivity:** one Evidence Item cannot silently support two
   payable lines or cases.
5. **Line provenance:** every payable amount is reported, evidence-derived and
   confirmed, or reviewer-entered with an audit reason.
6. **Money arithmetic:** `Decimal` only; line, currency, mileage, tax, case, and
   batch totals reconcile to the cent.
7. **Summary reconciliation:** reported totals, derived line totals, evidence
   totals, and output totals are separately named; mismatches never disappear
   into a generic unreadable state.
8. **Evidence confidence:** uncertain material fields require review.
9. **Duplicate evidence:** within-case and cross-case duplicate checks run after
   every grouping or correction.
10. **Listing reconciliation:** emitted text is parsed and re-summed; every
    output cell traces to a confirmed case value or is intentionally blank.
11. **Human gate:** no Payment Listing Row while a blocking Flag, proposed
    Claimant, unconfirmed derived amount, or unresolved material artifact exists.
12. **No external write:** SharePoint and client files remain read-only.

### Baseline invariants carried into the investigator

The newer structured-path fixes are contract tests for every adapter:

- **Independent totals stay independent.** Reported Total is the figure stated
  by the source, or absent. Calculated Lines Total is stored separately. Output
  names a mismatch or the lack of an independent total; it never reconciles a
  line sum against itself.
- **Materiality keeps its exact settings semantics.** For unassigned receipts,
  a missing/unparseable profile threshold uses RM 100; `0` is a valid threshold
  that makes every receipt with a readable amount at or above zero require a
  decision. Ownership conflicts, uncertain payable identities, and evidence
  reuse block regardless of amount.
- **A value fingerprint needs identity.** The deterministic duplicate key is a
  tuple of normalized vendor, date, amount, and currency, and it requires a
  non-empty vendor. Missing vendor does not prove two receipts are duplicates;
  use another cited signal or leave the relationship unresolved.
- **Readable workbooks get read first.** Formula cells with no saved values
  explain a report only after normal reading fails; their presence alone must
  not reject an otherwise readable report. A dated row with an amount cannot be
  hidden in `skip_rows`.
- **Repair convergence is structural.** "Same proposal twice" compares
  normalized coordinates, spans, roles, and other decision fields, excluding
  free-text reasons and observations.
- **Settings cannot hide the accounting story.** A reviewer may explicitly
  toggle `REPORT_TOTAL_MISMATCH` through the Client Profile, but run instructions
  cannot do so, and Output still shows Reported Total, Calculated Lines Total,
  emitted total, missing comparisons, and named differences.
- **Every retry is bounded.** Initial verification, correction re-checks, and
  tie-break calls share explicit request/tool budgets; a correction cannot open
  an uncapped secondary agent loop.
- **Missing required controls stay visible.** `MISSING_REFERENCE` remains a
  non-toggleable, run-level Flag when verification lacks a required explicit
  input such as mileage rates or the current listing. The agent cannot treat an
  unavailable control as a successful check.

New catalogue entries expected:

- `CLAIMANT_UNKNOWN`
- `OWNERSHIP_CONFLICT`
- `UNASSIGNED_EVIDENCE`
- `ARTIFACT_UNRESOLVED`
- `CLAIM_AMOUNT_UNCONFIRMED`
- `PURPOSE_UNKNOWN`
- `NO_SUMMARY`
- `TOOL_UNAVAILABLE`
- `TOOL_FAILED`
- `SANDBOX_LIMIT`

Each needs title, meaning, reviewer action, kind, blocking default, Citation
rules, and an idempotency key before it can be raised.

## Storage migration

Use additive, idempotent migrations; do not destructively rename current tables
while runs may exist.

1. Add `claim_cases` with claimant/grouping states and `legacy_employee_id`.
2. Backfill one Claim Case for every existing `ClaimEmployee`.
3. Add nullable `case_id` to rows, evidence, and flags; backfill it from
   `employee_id` and write both during the compatibility period.
4. Add `claim_source_artifacts`, populated from the run manifest.
5. Add `claim_evidence_assignments` instead of overloading
   `matched_row_id` for proposed and rejected relationships.
6. Add `claim_investigations` and `claim_tool_executions` for the run-local plan
   and replay record.
7. Add a claims schema version and an idempotent migration runner; the current
   startup `ALTER TABLE` pattern is insufficient for related-table backfills.
8. Keep existing HTTP response fields (`employees`, `employee_id`) as deprecated
   aliases until the frontend and verification script consume `cases` and
   `case_id`.
9. After old runs render and the compatibility tests pass, stop writing the
   aliases. Physical column removal is a later maintenance task, not part of
   this rollout.

## State machine and orchestration

Target states:

```text
queued
  → surveying
  → investigating
  → group_ready       (reviewer resting state)
  → verifying
  → ready             (reviewer resting state)
  ↘ failed
```

During migration, `mapping` maps to `investigating` and `map_ready` maps to
`group_ready` in the HTTP representation. A server restart fails only active
states; resting states survive. One case failure is isolated and retryable.

The orchestration loop is bounded:

1. Agent proposes an Investigation Plan.
2. Tool harness executes allowlisted calls.
3. Agent returns an `InvestigationResult` proposal.
4. Code audits schemas, coverage, arithmetic, Citations, and budgets.
5. Audit problems return to the agent, at most three rounds.
6. Remaining uncertainty becomes visible Flags or unresolved assignments; it is
   never discarded to make the run pass.

## HTTP and frontend changes

### HTTP routes

- Run detail returns `artifacts`, `cases`, `evidence_items`, `assignments`,
  `investigation`, `tool_summary`, and compatibility `employees` fields.
- Replace the internals of `confirm-map` with case grouping confirmation while
  retaining the old route temporarily.
- Add actions to create/merge/split cases, move evidence, set/confirm a Claimant,
  and set a Source Artifact disposition.
- Every mutation takes an expected run revision to prevent two browser actions
  overwriting each other — including the existing correction, decision,
  category, retry, and confirm-map routes, which gain it in H9.
- Corrections re-run only the affected case plus global duplicate/ownership
  controls.

### Map & Group screen

- Cases as columns/cards; unassigned pool beside them.
- Artifact/evidence preview, extracted identity signals, and grouping reason.
- Merge, split, move, set Claimant, irrelevant, and unreadable actions.
- Coverage counter: `47/49 artifacts dispositioned; 2 need review`.
- One primary action: **Confirm grouping & verify**.

### Verification and Review

- Replace employee labels internally with case labels; show Claimant when known.
- Show whether a Claim Line is reported or evidence-derived.
- Expose tool failures and incomplete artifacts without raw stack traces.
- Keep R7–R10's plain-language flag cards, summary filters, all-lines table,
  unused evidence section, and output reconciliation, but key them by case.
- Reuse the delivered `FlagCard`, field editor, totals, filters, evidence preview,
  and empty states. Migrate selectors and mutations through the case-oriented
  HTTP contract; do not create a second case-only component tree beside the
  employee UI.

### Output

- Default to one Payment Listing Row per confirmed Claim Case.
- The reviewer may merge cases before confirmation when the listing requires one
  row per claimant.
- Map the current listing each run. Unknown columns remain blank and visible;
  required unknown values block rather than being fabricated.

## Security and misuse hardening

- Treat workbook cells, document text, filenames, QR content, and metadata as
  untrusted data, never as agent instructions.
- Put the user objective and tool policy above all document content; explicitly
  tell the model that documents may contain prompt injection.
- Tools resolve only manifest ids, not arbitrary paths supplied by the model.
- Preserve current zip/path, file-size, page, sheet, cell, request, timeout, and
  concurrency limits; add run-wide flat-dump quotas that do not use an employee
  folder as the unit.
- Do not execute workbook macros, formulas, links, embedded objects, or document
  scripts.
- Do not expose SharePoint tokens, temporary download URLs, environment values,
  or local absolute paths to model prompts or sandbox output.
- Sanitize all TSV/spreadsheet output against formula injection.
- Redact likely account numbers, tokens, and signed URLs from telemetry.
- Cancel outstanding tool calls when the run or case is cancelled or fails.
- Record model, prompt version, tool versions, budgets, and output hashes for
  reproducibility without storing unnecessary raw prompt copies.

## Testing and evaluation matrix

### Deterministic tests

Test through the `ClaimsInvestigator` interface with in-memory tools:

- Every Source Artifact holds exactly one disposition; output is refused while
  any artifact is still `unresolved`.
- A cited cell/page outside the manifest is rejected.
- Conflicting identity signals create `OWNERSHIP_CONFLICT`.
- Weak signals never confirm a Claimant.
- Moving evidence between cases re-runs duplicate and total controls.
- Evidence-derived lines remain blocked until amount confirmation.
- One Evidence Item assigned to two payable lines is rejected or flagged.
- Decimal calculations and emitted listing totals round-trip exactly.
- Reported Total and Calculated Lines Total remain distinct when the source total
  is missing or wrong; missing never becomes a synthetic match.
- Unassigned-receipt thresholds cover missing, positive, and `0` values.
- Duplicate checks do not merge two no-vendor receipts by value alone.
- Formula-without-cache diagnostics occur only after a failed normal read.
- Repair convergence ignores prose changes but detects structural changes.
- Correction and tie-break paths stop at the same configured budget as initial
  verification.
- Tool budgets, truncation, timeout, and cancellation fail closed.
- Prompt-injection text in a workbook cannot call a forbidden tool or change the
  objective.
- Sandbox network, filesystem, subprocess, memory, and time escapes fail.
- Old employee-folder runs backfill and render through compatibility fields.

### Synthetic end-to-end clients

| Scenario | Required result |
|---|---|
| A — current employee folders | Existing planted errors and RM10 variance still found; no regression |
| B — flat folder, reports present | Correct cases proposed; reviewer needs no more than two grouping changes |
| C — full dump, names on evidence | Evidence grouped with cited identity signals; no false confirmed owner |
| D — full dump, no identity anywhere | Useful evidence ledger and totals; `CLAIMANT_UNKNOWN`; output locked |
| E — evidence only | Proposed lines created; amounts and purpose/category gaps explicitly blocked |
| F — master workbook | Several cases extracted from one Source Artifact with exact cell Citations |
| G — changed monthly layout | Investigation succeeds without code changes or produces precise unresolved items |
| H — duplicate across cases | Both cases flagged; idempotent after regroup/retry |
| I — malicious document | Prompt injection and embedded code inert; run remains contained |
| J — sandbox abuse/timeout | Execution killed, audited, no partial domain writes |

### Live-model acceptance gates

- 100% of submitted Source Artifacts dispositioned or visibly blocking.
- 100% of payable Claimants reviewer-confirmed.
- 100% of material values cited.
- 100% of internal arithmetic and emitted totals reconcile to the cent; every
  missing independent Reported Total is named rather than counted as a match.
- Zero silent evidence reuse across payable cases.
- Zero automatic owner confirmation based solely on weak/fuzzy signals.
- Every planted error found; false blocking Flags average no more than one per
  confirmed case on the synthetic suites.
- Current ten-case structured batch remains under five minutes; flat-dump timing
  and model/tool cost are reported, with an initial target set after the first
  representative run.
- Two consecutive clean runs per scenario before enabling the new path by
  default.

## Delivery phases

Each phase lands independently with its own migration, tests, documentation,
and rollback switch.

### H0 — Rebaseline and protect current correctness

- [x] Record delivered R1–R10 plus the review-fix baseline through `330b972`
  (documented 2026-08-19).
- [x] Update PRD scope: no company recipe, automatic learning, or recipe drift
  (documented 2026-08-19).
- [x] Inventory the delivered R7–R10 review surface for reuse: shared flag card,
  filters, all rows, unused evidence, progress, and reconciliation are retained;
  employee-keyed selectors/routes are the migration seam.
- [x] Verify the automated baseline: backend `254 passed, 2 skipped`; frontend
  production build green (2026-08-19).
- [x] Pin the current structured-folder end-to-end result and RM10 example
  (`backend/tests/test_claims_baseline.py`, scripted AI from the ground truth,
  2026-08-19).
- **Exit:** automated tests and frontend build green; one owner-triggered live
  structured run reproduces the RM10 example; baseline time/cost recorded.

### H1 — Deep interface and normalized contracts

- [x] Add `investigator/contracts.py` with request/result and normalized domain
  models (2026-08-19).
- [x] Put the existing structured-folder pipeline behind the new interface
  (`investigator/legacy.py`; the runner builds the hashed manifest and calls
  `investigator.investigate`; `CLAIMS_AGENTIC_INVESTIGATION` selects the adapter).
- [x] Add in-memory InvestigationTools (`tools/fake.py`, with the real Decimal
  calculator and table comparison behind it) and contract tests
  (`tests/test_investigator_contracts.py`).
- [x] Pass current-run instructions into investigation and case verification:
  the report/KM readers, the page reader and the category judge see them as
  marked steering text; the checks and audits never read them; the diary
  records that they were shown.
- [x] Freeze the baseline invariants above as adapter-neutral contract tests
  (`test_investigator_contracts.py` plus the delivered `test_claims_*` suites,
  which run unchanged through the seam).
- **Exit:** current Client A passes through the new interface with empty
  instructions without changing observable results; with instructions supplied,
  every difference is logged and reviewed, not assumed harmless.

### H2 — Additive storage migration

- [x] Add schema versioning and idempotent claims migrations
  (`claims/migrations.py`, `claims_schema` table; run from `init_db`, 2026-08-19).
- [x] Add Claim Cases, Source Artifacts, Evidence Assignments, investigations,
  and tool executions (`claim_cases`, `claim_source_artifacts`,
  `claim_evidence_assignments`, `claim_investigations`, `claim_tool_executions`;
  `case_id` on rows/evidence/flags; `manifest` and `revision` on runs).
- [x] Backfill existing runs and dual-write compatibility ids (`claims/cases.py`:
  every ClaimEmployee has a mirrored ClaimCase; rows/evidence/flags carry both
  ids; confirm-map stores the confirmed result and links cases to employees).
- [x] Add old-run rendering and rollback tests (`tests/test_claims_migration.py`:
  a pre-migration database migrates once and reopens unchanged; run detail
  carries `cases`/`artifacts`/`assignments`/`investigation`/`tool_summary`;
  `CLAIMS_CASE_MODEL=0` hides them with storage unchanged).
- **Exit:** a pre-migration database opens, migrates once, opens again unchanged,
  and old/new run detail is equivalent.

### H3 — Global inventory independent of folders

- [x] Replace employee-named quotas with run-wide limits plus post-group case
  budgets (`source.py`: 1500 files / 1500 MB / 6000 pages per run at ingestion;
  60 files / 200 pages per case at verification, a case over budget fails alone;
  30 cases per run at confirm; 2026-08-19).
- [x] Hash and manifest every file before mapping (`manifest.py`, H1; stored on
  `claims_runs.manifest`).
- [x] Inspect root and nested files uniformly (a flat folder with zero
  subfolders reaches investigation with every file inventoried; `FLAT_FOLDER`
  diary line; nothing is refused for lacking subfolders).
- [x] Create Source Artifact dispositions and completeness audit
  (`ARTIFACT_UNRESOLVED` at run close, one per unresolved file, keyed by the
  artifact id; released only by a reviewer disposition —
  `POST /artifacts/{id}/disposition` or the flag decision carrying one; the
  catalogue gained the H3–H8 codes, each with an identity key, `profile.flag_key`).
- **Exit:** a flat folder with zero subfolders reaches investigation with every
  file visible; nothing is silently dropped.

### H4 — Safe deterministic tool harness

- [x] Implement typed workbook, document, image, search, calculator, and bounded
  table-comparison tools (`tools/workbook.py`, `documents.py`, `files.py`,
  `calculator.py`, `tables.py`; `tools/harness.py` = the production
  `InvestigationTools`; 2026-08-19).
- [x] Use manifest ids and return Citations/provenance on every read (a typed
  path is `NOT_FOUND`; every result carries artifact ids + hashes and Citations).
- [x] Enforce tool budgets, output handles, cancellation, redaction, and logs
  (call/page/byte budgets fail closed with `BUDGET`; page renders return a
  handle under `<run>/tool_output`; `cancel()` fails every later call; absolute
  paths are redacted from errors; one `ToolExecution` per call with input/output
  hashes).
- [x] Add prompt-injection and hostile-file fixtures (`tests/test_claims_tools.py`:
  injection text in cells and PDF text is returned as data; formulas are text and
  never evaluated; macros never loaded; links/scripts counted, never followed; a
  corrupted workbook is a named, redacted failure).
- **Exit:** tools cannot read outside the snapshot or write outside temporary
  output; calculator/table results replay exactly.

### H5 — Tool-using investigation loop

- [x] Allow `create_agent` to receive an allowlisted InvestigationTools adapter
  (`model_layer.create_agent(..., tools=)`; `tools/binding.py` binds the harness
  as typed tools; `run_python` is bound only when `CLAIMS_PYTHON_SANDBOX` is on;
  2026-08-19).
- [x] Implement plan → act → normalized proposal → deterministic audit → repair,
  capped at three rounds (`investigator/investigator.py`, `proposal.py`,
  `audit.py`, `strategies.py`; the audit checks coverage, case shape, report
  plausibility, one identifier per case, and verifies every claimed name /
  identifier at a cited place or file name with the tools).
- [x] Persist the Investigation Plan and execution summary for this run only
  (`claim_investigations` + `claim_tool_executions` via `cases.store_result`;
  confirmation keeps the proposal's plan).
- [x] Surface incomplete investigations as Flags, not generic run failures
  (`TOOL_FAILED` per failed tool call and on budget exhaustion,
  `TOOL_UNAVAILABLE` when `run_python` was asked for and the investigation did
  not converge; unresolved artifacts and unknown claimants stay visible;
  `tests/test_claims_investigator.py`, incl. Client A through the agentic path).
- **Exit:** the agent can find an unfamiliar report and evidence without a fixed
  file role map, while all current audit failures still fail closed.

### H6 — Full-dump grouping and Map & Group gate

- [x] Extract identity signals with Citations (`claims/grouping.py`: ER codes and
  names from file names, header cells beside a Name label, folder names — each
  cited and graded strong/weak; 2026-08-19).
- [x] Propose Claim Cases and Evidence Assignments, including unknown/unassigned
  (investigator normalize + `refresh`: `OWNERSHIP_CONFLICT`, `CLAIMANT_UNKNOWN`,
  the unassigned pool; a proposed claimant is confirmed only by the reviewer's
  Confirm, never by AI inference).
- [x] Implement grouping validation and revision control (`grouping.gate`,
  `expected_revision` on every mutation → 409 when stale; `confirm-grouping` is
  the one gate; `confirm-map` runs through the same core).
- [x] Build Map & Group UI and audited merge/split/move/claimant actions
  (`frontend/src/screens/claims/GroupView.tsx` — the one map screen when the
  server sends cases; the delivered `MapView.tsx` remains only as the fallback
  while `CLAIMS_CASE_MODEL` is off; coverage counter, pool, dispositions,
  role + remember, Confirm grouping & verify; verified in the browser on a
  seeded flat dump).
- **Exit:** scenarios B–D behave as specified; no output is possible before
  grouping confirmation.

### H7 — Evidence-only and no-summary verification

- [x] Derive proposed Claim Lines from Evidence Items (the worker's derived rows,
  now with `origin: evidence_derived` on the HTTP row; 2026-08-19).
- [x] Add amount, purpose, category, and summary Flags (`CLAIM_AMOUNT_UNCONFIRMED`
  one per case listing every derived line; `NO_SUMMARY` for any non-folder
  grouping, `NO_REPORT` kept for folder-based cases; `PURPOSE_UNKNOWN` as a note —
  `CATEGORY_UNCLEAR` is what blocks when the category cannot be settled).
- [x] Generalize workers from employee to confirmed case (`worker.verify_case` /
  `retry_case`, `POST /cases/{id}/retry`; the employee record is the 1:1 unit
  underneath during the compatibility period).
- [x] Re-run global duplicate controls after case changes or retries
  (`worker.rerun_global_controls` at run close, after every correction and retry;
  `SHARED_RECEIPT` now resolves itself when a receipt stops being shared;
  `tests/test_claims_evidence_only.py`).
- **Exit:** scenario E produces useful lines and totals but blocks every
  unsupported payment fact.

### H8 — Isolated Python sandbox

- [ ] Complete a Windows enterprise feasibility spike against the production
  isolation requirements — **owner action**, checklist in `docs/SANDBOX.md`;
  until it passes the feature ships disabled (the accepted outcome if no runner
  is permitted).
- [x] Define `SandboxPort`, production adapter, and in-memory fake
  (`tools/contracts.SandboxPort`; `tools/sandbox.py`: `RunnerSandbox` handing
  execution to an operator-declared OS-level runner, `UnavailableSandbox`,
  `InMemorySandbox`; 2026-08-19).
- [x] Add limits, double execution for material successful output, audit record,
  and normalized result parsing (wall/CPU/memory/file/process/input/output
  limits, tree kill, second-run hash comparison, redacted record, `SANDBOX_LIMIT`
  Flag through the harness and the investigator).
- [x] Keep feature disabled by default until security tests pass
  (`CLAIMS_PYTHON_SANDBOX` + `CLAIMS_SANDBOX_RUNNER` + `CLAIMS_SANDBOX_ISOLATED`
  all required; `tests/test_claims_sandbox.py`).
- **Exit:** scenario J passes. If OS isolation is unavailable, formally ship
  `run_python` disabled without blocking H0–H7 or H9–H12.

### H9 — Generic listing output and gates

- [x] Produce one row per confirmed Claim Case in the current listing's column
  order (`listing.build_outputs` iterates cases; a case with no confirmed
  claimant is not paid and is named; 2026-08-19).
- [x] Generalize listing field mappings beyond the current fixed roles while
  keeping required universal roles explicit (profile `listing_columns`: pin a
  header to a role, blank it, or write a literal; losing vendor/amount falls
  back and says so; unknown headers stay blank and visible).
- [x] Recompute Calculated Lines Total, case, batch, and emitted totals
  independently while preserving an optional, source-cited Reported Total
  (`totals.lines_total`, `reported_total`, `reported_missing`, `total_myr`;
  per-case `reported_total` / `lines_total` on `included`).
- [x] Update corrections/exclusions to case ids and rerun affected controls
  (rows/flags/exclusions/unused evidence carry `case_id`; run-wide controls
  re-run after corrections, H7).
- [x] Add expected-revision checks to the existing correction, decision,
  category, retry, and confirm-map routes (optional `expected_revision` → 409
  when stale; the server-side output gate `output_blockers` names what locks
  the listing; `CLAIMANT_UNKNOWN` / `OWNERSHIP_CONFLICT` are settled by actions,
  not notes; the claimant can be set at review time;
  `tests/test_claims_output_gates.py`).
- **Exit:** structured and full-dump outputs round-trip; every blocking state is
  enforced server-side.

### H10 — Case-oriented review surface

- [x] Adapt the delivered R7–R10 surface from employee ids to case ids
  (`frontend/src/screens/claims/units.ts`: the review surface is keyed by Claim
  Case, an older run's employees render through the same `reviewUnits`;
  ReviewView / VerifyingView / OutputView migrated, not duplicated; 2026-08-19).
- [x] Add claimant/assignment confidence and evidence-derived badges (claimant
  chip with basis and grouping confidence; `derived` / `corrected` origin chips
  on lines and cases; Reported Total and Calculated Lines Total shown apart;
  Output names the blockers and the three totals).
- [x] Ensure keyboard, empty, loading, stale-revision, and failure states (every
  mutation sends `expected_revision`; a 409 reloads the run and says so;
  action-settled flags — file disposition, set claimant — offer the action on
  the card instead of accept/dismiss; verified in the browser on a seeded
  flat-dump run).
- **Exit:** a reviewer can complete scenarios A–E without inspecting server logs
  or editing JSON.

### H11 — Security, audit, and operational hardening

- [ ] Threat-model file ingestion, prompt injection, tool use, sandboxing,
  telemetry, and output injection.
- [ ] Add replay bundles: manifest, versions, plan, tool hashes, calculations,
  reviewer decisions, and final output.
- [ ] Add recovery for restart, cancellation, partial tool failure, and retry.
- [ ] Document cost/time budgets and retention controls.
- **Exit:** threat-model findings fixed or explicitly accepted; replay reproduces
  all material totals.

### H12 — Evaluation, shadow rollout, and default-on decision

- [ ] Build scenarios B–J with ground truth.
- [ ] Run old and new structured paths in shadow mode on Client A.
- [ ] Pilot representative anonymized full dumps with output locked.
- [ ] Compare accuracy, false Flags, grouping corrections, time, cost, and tool
  failures.
- [ ] Enable by default only after acceptance gates pass twice.
- **Exit:** owner signs off on default-on; old adapter remains a rollback path
  for one release cycle.

## Critical path and parallel work

```text
H0 → H1 → H2 → H3 → H4 → H5 → H6 → H7 → H9 → H10 → H11 → H12
                         └──────── H8 sandbox ────────────┘
```

- H1 contracts must settle before storage or tool implementations expose new
  interfaces.
- H2 and the latter half of H3 may run in parallel after the manifest and case
  identifiers are fixed.
- Tool implementations inside H4 may run in parallel behind
  `InvestigationTools`.
- H8 is optional for default-on full-dump support and can run beside H6–H10; it
  must not delay deterministic file/workbook/document/calculator tools.
- H9 cannot complete before H6/H7 define confirmed case and derived-line gates.
- H10 should consume stable case-oriented HTTP fields from H2/H6, not temporary
  frontend-only grouping state.
- H11 threat modelling begins during H1 and closes after H8/H10; security is not
  deferred to one final review.

## Risk register

| Risk | Impact | Mitigation and exit evidence |
|---|---|---|
| AI assigns evidence to the wrong person | Wrong employee could be paid | Claimant never confirmed by AI; Map & Group always pauses; weak-signal tests and ownership gate |
| Full dump contains no ownership information | Work cannot become payable | Preserve unknown cases and evidence ledger; `CLAIMANT_UNKNOWN`; request roster/manual assignment |
| Agent ignores an inconvenient file | Missed claim or control evidence | Immutable manifest plus artifact-completeness control; every artifact disposition visible |
| Python escapes or reads credentials | Host or client-data compromise | OS isolation, no network/secrets, disabled-by-default feature; H8 escape tests |
| Enterprise Windows cannot host a sandbox | Python feature cannot ship | `SandboxPort`; ship all deterministic tools without Python; explicit `TOOL_UNAVAILABLE` |
| Tool freedom increases cost and latency | Runs become uneconomic or time out | Plan/tool/request/byte budgets, handles for large output, concurrency caps, cost acceptance gates |
| Generic attributes weaken validation | Important values bypass typed controls | Stable typed core money/identity/Citation fields; extensions cannot drive payment until normalized |
| Additive migration splits old/new identities | Flags or corrections attach to wrong case | Backfill, dual-write period, idempotent migration and old-run equivalence tests |
| Prompt injection inside client files | Objective or tool policy is altered | Untrusted-data framing, manifest-id tools, no side effects, hostile-document fixtures |
| Delivered employee UI is replaced instead of migrated | Duplicate work and inconsistent review | Reuse R7–R10 components and behavior; move selectors/routes to case ids behind compatibility aliases |
| Evidence-derived receipt total is not the amount claimed | Overpayment | `CLAIM_AMOUNT_UNCONFIRMED`; visible per-line origin; reviewer confirmation and reconciliation |
| Agent result looks plausible but is incomplete | Reviewer over-trusts presentation | Coverage, Citations, unresolved questions, replay record, and server-side output gate |

Risk acceptance must be explicit in the plan or PRD. A warning in a log is not
an accepted risk.

## Feature switches and rollback

Schema migrations run unconditionally and are additive; no switch skips or
reverses one. Once H2 lands, cases are always written (dual-write with the
employee ids) regardless of switch state — the switches gate what is *read,
routed, and shown*, never what is stored.

- `CLAIMS_CASE_MODEL` (H2): off = case fields and case routes are hidden from
  HTTP and the employee fields stay authoritative for the UI; storage is
  unchanged.
- `CLAIMS_AGENTIC_INVESTIGATION` (H5): off = new runs go through the legacy
  structured-folder adapter; old runs are never reinterpreted.
- `CLAIMS_FULL_DUMP_GROUPING` (H6): off = Map & Group actions are disabled and a
  flat folder behaves as it does today.
- `CLAIMS_PYTHON_SANDBOX` (H8): off = `run_python` is absent from the tool
  allowlist; `TOOL_UNAVAILABLE` where an investigation genuinely needs it.

No rollback deletes user data or writes to SharePoint.

## Implementation file map

Expected new modules:

```text
backend/app/claims/
├── investigator/
│   ├── contracts.py       # deep module interface and normalized results
│   ├── investigator.py    # bounded plan/act/audit loop
│   ├── audit.py           # universal result controls
│   ├── strategies.py      # post-inventory branches: structured, full-dump, evidence-only
│   └── legacy.py          # current mapper + worker path behind the interface (rollback)
├── tools/
│   ├── contracts.py       # InvestigationTools and SandboxPort
│   ├── files.py
│   ├── workbook.py
│   ├── documents.py
│   ├── calculator.py
│   ├── tables.py
│   └── sandbox.py
├── grouping.py            # identity signals and case proposals
├── migrations.py          # idempotent claims schema migrations
└── models.py              # additive case/artifact/assignment records
```

Expected frontend changes remain under `frontend/src/screens/claims/`, with a
new `GroupView.tsx` replacing the employee-only assumptions in `MapView.tsx`.

## Decisions fixed by this plan

- Claim Case is primary; Claimant may be unknown.
- Flat folders and evidence-only submissions are valid inputs.
- Grouping always pauses for reviewer confirmation.
- Evidence-derived amounts require confirmation.
- The agent receives typed tools and a run-local objective.
- Deterministic controls and the server-side human gate remain authoritative.
- Python requires OS-level isolation and may ship disabled independently.
- No company recipe, automatic learning, recipe promotion, or recipe drift.
- Explicit Client Profile values change only through deliberate reviewer action.

## Defaults to confirm before H6/H8/H9

Recommended defaults are shown first:

1. **Unresolved artifacts:** an `unresolved` artifact always blocks; only a
   reviewer's `irrelevant` or `unreadable` disposition releases it.
2. **Grouping:** always pause at Map & Group, even when identity signals are
   strong.
3. **Output granularity:** one Payment Listing Row per confirmed Claim Case;
   merge cases before confirmation when one row per claimant is required.
4. **Python:** disabled in production until the enterprise environment meets all
   sandbox requirements.
5. **Evidence-only amount:** receipt total is a proposed amount and requires one
   reviewer confirmation per case, with a bulk confirm only when every line is
   visible.
