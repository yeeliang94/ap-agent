# Implementation Plan: Wrapper-Folder Uploads & Zero-Case Guard

**Overall Progress:** `100%` — all steps plus the peer-review round done, full suite green (408 passed, 2 skipped — the skips need optional sample data)
**PRD Reference:** [docs/PRD.md](PRD.md) (Flow 1 — batch intake); bug traced in the 2026-08-21 debug session (ICMR run: wrapper folder `Emp B1 Test` produced 0 cases and a green "Ready" run)
**Master plan:** [docs/PLAN.md](PLAN.md)
**Last Updated:** 2026-08-21

## Summary

An uploaded batch whose employee folders sit inside a wrapping folder (e.g.
`Emp B1 Test/Aegene Ong_1/…`) is flattened by the survey into a single
folder, so the mapper can build no cases — and the run then sails to
"Ready" having verified nothing. Fix at the door: peel non-person wrapper
folders during upload ingestion with the same shared logic the zip path
already half-has, and refuse to confirm a map that creates zero cases while
the batch holds files.

## Key Decisions

- **Strip at ingestion, not in the survey/mapper** — the whole pipeline
  (survey, audit, reviewer validation, grouping, investigator) shares the
  "one path level = one employee folder" model consistently; unwrapping the
  upload before any of them see it fixes the bug without rewriting five
  subsystems.
- **Peel rule (final, after peer review):** follow the chain of sole
  common root folders, peeling only on *affirmative wrapper evidence*,
  capped at `MAX_WRAPPER_LEVELS` (10). A sole folder holding only loose
  files is never a wrapper (that is one employee's folder). A folder
  titled with document or period words ('Claims', 'batch', 'Aug 2026')
  is always a wrapper; a folder named like a person ('Aegene Ong') never
  is; an ambiguous name ('Emp B1 Test', 'EMP001') is a wrapper only when
  it holds at least one subfolder that is NOT a single employee's
  category ('Receipts', 'July' do not count — person-named or coded
  subfolders do). This unwraps the ICMR layout and double wrappers while
  preserving `Aegene Ong/July+August`, `A_1/Receipts+Reports` and
  `EMP001/July+August`. A stray file at the wrapper level simply becomes
  a batch-root file, which the map model already supports.
- **One shared helper for zip and upload** — `unpack_zip` today strips one
  common root unconditionally; both doors will use the new rule so they
  behave identically. The existing zip test (`batch/A_1/…` keeps `A_1`)
  stays green under the new rule.
- **Zero-case guard lives in `validate_confirmed_map`** — that is the gate
  `confirm_map` already runs, so a map with files but no billable case
  returns a plain-language 400 instead of silently verifying nothing.
  (Proposal-time maps may still have zero cases; the reviewer fixes them
  on screen — only *Confirm & verify* is blocked.)
- **Person-check import stays where it is** — `folder_looks_like_a_person`
  is imported from `investigator/strategies.py` inside the helper function
  (not at module top) to avoid the `source → strategies → survey → source`
  import circle.

## Pre-Implementation Checklist
- [x] 🟩 Root cause confirmed in code (survey.py:65 flat folder model; routes.py:470 no minimum-case check; source.py:209 zip-only wrapper strip)
- [x] 🟩 No conflicting in-progress work (the four locally-modified files — routes.py, sharepoint_auth.py, telemetry.py, test_sharepoint_auth.py — are untouched by this plan)
- [x] 🟩 Existing tests located (backend/tests/test_claims_source.py, test_claims_runs.py)

## Tasks

### Phase 1: Shared wrapper-strip helper

- [x] 🟩 **Step 1: Add `strip_wrapper_roots` to `backend/app/claims/source.py`** —
  given the batch's relative file paths, return them with leading
  non-person wrapper folders peeled off (looping for multi-level wrappers),
  per the peel rule above.
  - [x] 🟩 Helper + docstring explaining the two stop conditions in plain words
  - [x] 🟩 Unit tests in `test_claims_source.py`: single wrapper stripped; double wrapper stripped; `A_1/files-only` kept; person-named wrapper kept; wrapper with a stray root file stripped (stray becomes a root file); no common root → unchanged
  - **Verify:** `pytest backend/tests/test_claims_source.py -k strip` — new tests pass, nothing else runs yet.

### Phase 2: Wire both ingestion doors

- [x] 🟩 **Step 2: Use the helper in `ingest_uploaded`** — apply to the
  staged paths before quota checks and before files are copied into the
  run's snapshot, so the survey never sees the wrapper.
  - [x] 🟩 Test: `test_ingest_uploaded_strips_wrapper_folders` (files land at `dest/Aegene Ong_1/…`, folder entries show the employee folders, not the wrapper)
  - **Verify:** `pytest backend/tests/test_claims_source.py` — all pass, including the untouched existing upload tests.
- [x] 🟩 **Step 3: Use the helper in `unpack_zip`** — replace the one-shot
  `_common_root` strip with the shared rule.
  - [x] 🟩 Test: a zip with a double wrapper unwraps; the existing `batch/A_1` test still keeps `A_1`
  - **Verify:** `pytest backend/tests/test_claims_source.py` — all pass.

### Phase 3: Zero-case guard

- [x] 🟩 **Step 4: Refuse a zero-case confirm** — in
  `mapping.validate_confirmed_map`, when the survey holds files but no
  folder is a non-skipped employee, add a problem telling the reviewer
  nothing would be verified (and to cancel the run if the batch truly
  holds no claims). `confirm_map` already turns problems into a 400.
  - [x] 🟩 Unit test on `validate_confirmed_map` (zero cases → problem; one case → no problem)
  - [x] 🟩 Route-level test in `test_claims_runs.py` style: confirming an all-`ignore` map returns 400
  - **Verify:** `pytest backend/tests/test_claims_runs.py backend/tests/test_claims_grouping.py` — all pass.

### Phase 4: Whole-suite check & docs

- [x] 🟩 **Step 5: Full regression run** — the flat-folder assumption also
  lives in grouping, runner, and the investigator; prove nothing shifted.
  - **Verify:** `pytest backend/tests` — no new failures (pre-existing failures, if any, recorded before starting).
- [x] 🟩 **Step 6: Documentation touch-up** — one paragraph in
  `unpack_zip`/`ingest_uploaded` docstrings ("uploading the folder itself
  or its contents both work"), link this plan from PLAN.md's amendment
  area, mark progress.
  - **Verify:** Reading the docstrings answers "what happens to a wrapper folder?" without opening the helper.

## Peer-review round (2026-08-21, all fixed)

- **[CRITICAL] memory blow-up in wrapper detection** — the first helper
  retained every path list per nesting level; a crafted zip (one 65 KB
  deeply nested filename) could allocate gigabytes. Fixed: no retained
  levels, peeling capped at 10, and `_check_raw_paths` refuses >1500
  files or any path over `MAX_PATH_CHARS` (1000) BEFORE detection runs,
  at both doors.
- **[HIGH] zero-case guard missed the default path** — the guard sat only
  in the legacy `validate_confirmed_map`; the default case-model UI calls
  `/confirm-grouping`, whose gate said `ok` with zero cases. Fixed in
  `grouping.gate` (`artifacts and not to_verify` → problem), tested at
  gate and route level.
- **[HIGH] batch-shape scan could strip a real employee folder**
  (`Aegene Ong/July+August`, `A_1/Receipts+Reports`) — replaced by the
  affirmative-evidence rule above, with regressions for month/category
  subfolders and coded employee names.
- **[LOW] stale test count in this plan** — corrected.

## Rollback Plan

- All changes are in `backend/app/claims/source.py`, `backend/app/claims/mapping.py`, `backend/app/claims/grouping.py` and three test files (`test_claims_source.py`, `test_claims_runs.py`, `test_claims_grouping.py`) — `git checkout` those paths reverts cleanly; no schema, data or frontend changes.
- Runs created while the fix was live keep their (already-stripped) snapshots — stripping only changes how *new* uploads are laid out, so old runs need no repair.
- The four unrelated locally-modified files (routes.py, sharepoint_auth.py, telemetry.py, test_sharepoint_auth.py) are never touched; a rollback must not sweep them up.
