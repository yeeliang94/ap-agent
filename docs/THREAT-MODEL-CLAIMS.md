# Claims Module Threat Model (hardening H11)

**Written:** 2026-08-19 · **Scope:** the claims run from file ingestion to the
Payment Listing, including the tool-using investigator (H5), the Map & Group
gate (H6), the sandbox port (H8) and the replay bundle (H11). The invoice
pipeline is out of scope.

Each finding is either **fixed** (a control in code, with the test that pins it)
or **accepted** (stated here; a warning in a log is not an accepted risk).

## Assets

- Client claim files (the immutable snapshot) and what they say about people.
- The Payment Listing rows — money goes where they say.
- Reviewer decisions and the audit trail.
- Credentials: the AI key / proxy, the SharePoint session.
- The host the app runs on.

## Trust boundaries

1. **Files are untrusted.** Workbook cells, document text, file names, QR
   content, metadata — data, never instructions.
2. **The model is untrusted.** Its proposals are audited by code; it never writes
   a domain record, never confirms a claimant, never releases output.
3. **The reviewer is trusted for decisions, not for bypassing controls.** Run
   instructions steer; they cannot switch a control off (only the Explicit
   Client Profile can, and only for toggleable checks).
4. **The sandbox runner is trusted only when the operator asserts it** — see
   `docs/SANDBOX.md`.

## 1. File ingestion

| Threat | Control | Pinned by |
|---|---|---|
| Zip bomb / oversize file | run-wide quotas before decompression; streamed write with a hard stop; per-file MB cap | `test_claims_source.py` |
| Path escape in archive or listing (`../`) | `_safe_join` refuses anything outside the workspace; manifest ids, not paths, are what tools resolve | `test_claims_source.py`, `test_claims_tools.py` |
| Too many files / pages for one run | 1500 files / 1500 MB / 6000 pages per run; 60 files / 200 pages per case after grouping (a case over budget fails alone) | `test_claims_inventory.py` |
| A file silently dropped | hashed manifest before anything looks inside; every Source Artifact must reach a disposition; `ARTIFACT_UNRESOLVED` blocks output | `test_claims_inventory.py`, `test_claims_baseline.py` |
| Macros / formulas / links / embedded objects executing | openpyxl never loads VBA; formulas are returned as text beside saved values, never computed; PDF links/scripts/embedded files are counted, never opened | `test_claims_tools.py` |
| A file changed after the run started | Citations resolve to the hash captured at run start; the replay verifier re-checks stored hashes against the manifest | `test_claims_replay.py` |

**Accepted:** a corrupted or malicious file that crashes a parser fails that
tool call (`TOOL_FAILED`, named, redacted) — the run continues; the file stays
unresolved and visible. No sandboxing of the parsers themselves (pymupdf,
openpyxl, Pillow run in-process); this is the same exposure the delivered
module has and is accepted for the pilot. Mitigation if needed later: run the
document tools in the sandbox runner.

## 2. Prompt injection

| Threat | Control | Pinned by |
|---|---|---|
| Text in a cell / page / file name instructs the model | objective and tool policy sit above the data in the prompt; the instructions say file contents are DATA; the model is asked to report such text in `injection_seen`; it surfaces as a warning | `test_claims_investigator.py` |
| Injection makes the model call a forbidden tool | the tool list IS the allowlist: `run_python` is not bound unless the sandbox switch is on; a call to a tool that does not exist is refused by the framework | `test_claims_investigator.py::test_a_real_agent_calls_the_bound_tools_and_forbidden_tools_do_not_exist` |
| Injection makes the model approve / confirm | the model cannot: claimants are proposed only (`Claimant.state` never `confirmed` from an adapter); release is a server-side gate over flags, dispositions and claimant state | `test_investigator_contracts.py`, `test_claims_output_gates.py` |
| Injection moves a report span / changes arithmetic | the readers' audits re-derive every value from the sheet; "same reading twice" convergence is structural | delivered `test_claims_checks.py`, `test_claims_robustness.py` |
| Run instructions used to disable a control | instructions reach the readers as marked steering; the checks never read them; `REPORT_TOTAL_MISMATCH` toggles only through the profile | `test_investigator_contracts.py` |

**Accepted:** a sufficiently clever injection can still make the model propose a
WRONG grouping (e.g. two people in one case). The gate catches conflicting
strong signals (`OWNERSHIP_CONFLICT`) and the reviewer confirms every grouping;
what the gate cannot catch is a plausible-but-wrong grouping with no
contradicting signal — the same exposure as a human error at the map, and
why grouping always pauses.

## 3. Tool use

| Threat | Control | Pinned by |
|---|---|---|
| Model reads outside the snapshot | tools resolve manifest ids only; a typed path is `NOT_FOUND`; the harness refuses a resolved path outside `files/` | `test_claims_tools.py` |
| Model writes outside the temp area | tools never write domain records; renders go to `<run>/tool_output` by handle; the snapshot's hashes are unchanged after a run | `test_claims_tools.py` |
| Runaway cost / time | tool-call, page and byte budgets fail closed (`BUDGET`); model request caps per investigation and per worker; correction and tie-break share the worker cap; wall-time budget on the loop | `test_claims_tools.py`, `test_investigator_contracts.py` |
| Absolute paths / secrets leaking into prompts or records | harness redacts absolute paths from every error; the sandbox environment is cleared; the sandbox record is redacted of numbers/tokens/signed URLs | `test_claims_tools.py`, `test_claims_sandbox.py` |
| A cancelled run keeps calling tools | `cancel()` fails every later call; `POST /cancel` cancels the active harness and marks the run failed; workers do not start on a failed run; `_finish_run` never turns a failed run ready | `test_claims_replay.py` |

## 4. Sandboxing

See `docs/SANDBOX.md`. Summary: disabled by default; requires an
operator-declared OS-level runner AND an explicit isolation assertion; the
adapter adds cleared env, read-only inputs, empty output dir, tree kill,
limits, double execution and a redacted record. **Accepted:** no production
runner exists yet; the feature ships off.

## 5. Telemetry and audit

| Threat | Control |
|---|---|
| Secrets / account numbers in the diary | redaction in the sandbox record; tool errors carry no absolute paths; telemetry describes failures, not stack traces to the screen |
| An action without a trace | every reviewer mutation writes an `AuditEvent`; every tool call writes a `ClaimToolExecution` with input/output hashes; the replay bundle assembles them |
| Two reviewers overwriting each other | every mutation takes `expected_revision`; a stale one is a 409 |

## 6. Output injection

| Threat | Control | Pinned by |
|---|---|---|
| A cell value starting with `=`, `+`, `-`, `@` executing on paste | `_cell` prefixes a quote; tabs/newlines stripped | delivered `test_listing_draft.py` / claims outputs |
| A fabricated value in a required column | unknown columns stay blank; required roles (vendor, amount) fall back visibly when missing; pinned literals are reviewer-set profile values | `test_claims_output_gates.py` |
| Output released early | server-side `output_blockers`: open flags, unconfirmed claimant, unresolved file — the screen cannot bypass it | `test_claims_output_gates.py` |

## Residual risks accepted for the pilot

1. In-process document parsers (see §1).
2. Plausible-but-wrong grouping with no contradicting signal (see §2) —
   mitigated by the mandatory Map & Group pause.
3. The live model's behaviour is evaluated on scripted stand-ins in CI; the
   H12 shadow runs are the live evidence, and default-on waits for them.
