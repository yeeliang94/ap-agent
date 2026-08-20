# Implementation Plan: Uploads-First Ingestion + Frontend Redesign

**Overall Progress:** `100%` — all phases implemented 2026-08-20; prototype capture (scratch branch) at commit time
**PRD Reference:** [docs/PRD.md](PRD.md) — Flows 1–4. This plan changes *how files
arrive* and *how the app looks*; it does not change what the agent pipeline does.
**Related plans:** [docs/PLAN.md](PLAN.md) (claims module — its open Step 15,
the Settings → Claims screen, is absorbed into Phase 6 here) ·
[docs/CLAIMS-AGENT-HARDENING.md](CLAIMS-AGENT-HARDENING.md) (H0–H12, untouched)
**Last Updated:** 2026-08-20 (implementation complete)

> Saved as `PLAN-UPLOADS-AND-UI.md` rather than overwriting `PLAN.md`, because
> `PLAN.md` is the live claims-module plan and still tracks open steps.

## Summary

Make **uploading files the normal way to start a claims run** (folder, zip, or
loose files of any readable type), demoting the SharePoint link to an optional
path behind a switch — sidestepping the SharePoint MCP connection problems
without touching the hardened agent pipeline. At the same time, rebuild the
frontend's *presentation layer*: a dedicated Settings screen that always shows
every flip-switch, a small design system (tokens + shared components), and a
modern-minimalist theme in the style of LlamaParse's application. A throwaway
HTML prototype (`frontend/ui-redesign-prototype.html`, kept on the scratch
branch `prototype/ui-redesign`, not on main) settled the look **before** any
real frontend code changes.

## Key Decisions

- **Promote the existing upload path, don't build a new one** — the backend
  already ingests a zip (tree preserved) and an optional listing workbook;
  quotas, grouping and verification are source-agnostic. We widen what the
  upload accepts and stop hiding it behind "local development".
- **Multi-format batch upload** — one zip, OR loose files of the readable set
  (`.pdf .png .jpg .jpeg .webp .xlsx .xlsm`), OR a whole folder picked/dropped
  in the browser (each file carries its relative path so subfolders survive).
  A single lone PDF is a valid batch. 200 MB per upload stays the cap (owner
  confirmed 2026-08-20).
- **SharePoint stays, behind a switch** — the link fields and MCP code are kept
  but shown only when the "SharePoint source" switch is on. Nothing is deleted;
  when MCP is fixed it can come back with one toggle.
- **Feature switches become runtime settings, not import-time constants** —
  the `CLAIMS_*` behavior flags and `DOC_SOURCE` move to the database-backed
  settings store (env value = default, read at call time, snapshotted per run
  so a running run never changes rules mid-flight). Secrets and machine facts
  (API keys, MCP endpoint, sandbox runner, `CLAIMS_LOCAL_ROOT`) stay in `.env`
  — IT-owned, shown read-only as "set / not set", never their values.
- **Prototype before frontend code** — three structurally different layouts in
  one double-clickable HTML file with a variant switcher; the winning variant
  (or mix) becomes the spec for Phases 4–6. Prototype is throwaway: it lands
  on a scratch branch once the decision is captured, never in main.
- **Theme: modern minimalist (LlamaParse-like)** — Inter type, 1px hairline
  borders, generous whitespace. All colors/spacing/radii become CSS design
  tokens so "inconsistent afterthought" styling can't recur.
- **Layout: variant B won (owner, 2026-08-20)** — top bar with centered tabs,
  one centered content column, numbered-step New-run form, "new run" and
  detail as full pages. Variants A and C discarded.
- **Color rules (owner, 2026-08-20: "colours, but not too much and too
  striking")** — neutral gray base; ONE calm accent (soft indigo `#5b67d1`)
  strictly for interactive state (active tab, switches, focus, progress,
  current step); soft desaturated status tints in small doses (green =
  clean/ready, amber = needs review, red = flags/errors, always as pale
  chips or dots, never loud blocks); primary buttons stay ink; no gradients.
- **Verbose text hidden, not shown (owner, 2026-08-20)** — helper copy lives
  behind hover ⓘ tooltips and collapsed "What to do" sections; the default
  view shows labels and values only.
- **Migrate screens, don't rewrite them** — React structure, `useAction`, and
  the stale-run contract stay; only presentation (markup + CSS + shared
  components) changes, screen by screen, tests green after each.

*Recorded at implementation (2026-08-20):*

- **Shared "components" are mostly CSS classes, not React wrappers** — the
  token stylesheet styles `.btn`, `.chip`, `.table`, `.empty`, `.skeleton`
  directly on existing markup; only `Switch` and `Info` earned components
  (they carry behavior). Eight pass-through wrapper components would have
  been indirection without benefit (Step 8, adjusted).
- **Feature switches flip immediately, no Save button** — each toggle is one
  audited PUT; the row shows "set by reviewer / .env default" provenance.
  Save-with-confirmation stays on the Workspace and Claims-profile cards,
  where several fields save together (Step 12, adjusted).

## Pre-Implementation Checklist
- [x] 🟩 Backend upload path explored (routes.py, source.py) — exists end-to-end
- [x] 🟩 Switch inventory taken (config.py flags, settings_store, claims settings)
- [x] 🟩 Owner picked variant B (2026-08-20), then two refinement rounds:
  verbose text hidden behind ⓘ/collapsibles; soft restrained palette
  (details under Key Decisions) — Phase 4 unblocked
- [x] 🟩 Switch split implemented as planned (5 flippable; sandbox/keys/models/root read-only) — owner may adjust on the Settings screen itself
- [x] 🟩 No conflicting in-progress work (PLAN.md Step 15 absorbed into Phase 6)

## Tasks

### Phase 0: Prototype the look (no real frontend changes)
- [x] 🟩 **Step 1: Throwaway HTML prototype** — one standalone file,
  `frontend/ui-redesign-prototype.html`, double-click to open. Three
  structurally different variants (A sidebar console · B top-bar centered ·
  C icon rail + drawer), switchable with a floating bottom bar and ←/→ keys.
  Each shows: Claims list, the uploads-first New-run flow, and the Settings
  screen with switches.
  - **Verify:** open the file in a browser; flip variants with the bottom bar;
    every screen renders in all three variants.
- [x] 🟩 **Step 2: Decision** — variant B, refined twice on owner feedback
  (round 2: hide verbose text, minimum design language; round 3: soft
  restrained colors instead of monochrome). The prototype file now shows
  only the final refined B and is the visual spec for Phases 4–6.
  - [x] 🟩 Capture: the prototype lives on the scratch branch
    `prototype/ui-redesign` (the round-1 A/B/C version is in this
    conversation's history only; the final refined B is what the branch
    holds). Main carries no prototype file.
  - **Verify:** this plan updated with the choice ✓; main branch has no
    prototype file after capture ✓.

### Phase 1: Uploads-first — backend
- [x] 🟩 **Step 3: Accept loose files and folder trees, not just a zip** —
  `create_claims_run` takes a list of batch files plus a parallel list of
  relative paths (what the browser reports for a picked/dropped folder).
  One `.zip` → unpack as today; otherwise files are laid out under
  `runs/<id>/claims/files/` at their relative paths (flat if none). Unreadable
  extensions are refused up front with a plain message naming the file.
  200 MB total cap, 25 MB per file, path traversal rejected (`..`, absolute
  paths), same quotas as today.
  - [x] 🟩 Route signature + `_read_upload` loop over files, total-size cap
  - [x] 🟩 `source.py`: ingest-from-uploaded-set beside ingest-from-zip
  - [x] 🟩 Tests: lone PDF, flat multi-file, nested paths, zip unchanged,
    traversal refused, over-cap refused, unreadable type refused
  - **Verify:** `pytest backend/tests` green; a run started from
    `samples/` files reaches the map step with the tree intact.
- [x] 🟩 **Step 4: Uploads work in every mode** — uploads are accepted
  regardless of `DOC_SOURCE`; the SharePoint-link path stays valid only when
  the source switch (Phase 3) is on. Run detail reports "uploaded files"
  as the source.
  - **Verify:** with `DOC_SOURCE=mcp`, an upload-started run still works;
    run detail shows the source correctly.

### Phase 2: Uploads-first — frontend (current styling, function first)
- [x] 🟩 **Step 5: Upload as the primary way in** — the New-claims-run card
  leads with a dropzone: drag-and-drop or click to choose a folder, a zip, or
  files; selected files listed with sizes and a clear/remove control; listing
  workbook stays a separate optional picker; SharePoint link fields render
  only when the source switch is on. Validation messages name the actual
  problem ("photo.heic isn't a supported type").
  - [x] 🟩 Folder picker (`webkitdirectory`) + drag-drop folder traversal,
    relative paths sent alongside files
  - [x] 🟩 Upload progress bar (batches can be ~200 MB)
  - **Verify:** start a run by dropping a real sample folder — no zipping,
    subfolders preserved; vitest green.

### Phase 3: Settings — backend switches
- [x] 🟩 **Step 6: Runtime switch store** — `CLAIMS_CASE_MODEL`,
  `CLAIMS_AGENTIC_INVESTIGATION`, `CLAIMS_SHADOW_INVESTIGATION`,
  `CLAIMS_FULL_DUMP_GROUPING`, and document source (uploads / SharePoint)
  move to settings-store keys with the env value as default, read at call
  time, snapshotted onto each run at start. Every change writes an
  `AuditEvent` (who-facing: "set by reviewer on <date>").
  Env-only and read-only in the UI: sandbox flags & runner, model names,
  MCP endpoint, API keys (shown as "set / not set"), `CLAIMS_LOCAL_ROOT`.
  - [x] 🟩 `GET /api/settings/switches` — every switch with value, default,
    description, and whether it's editable
  - [x] 🟩 `PUT` for the editable ones; unknown/read-only keys refused
  - [x] 🟩 Tests: default falls back to env; run snapshot wins over a
    mid-run flip; audit row written
  - **Verify:** flip a switch via the API; a new run behaves accordingly,
    a mid-flight run doesn't change behavior.

### Phase 4: Design system foundation (needs Step 2's decision)
- [x] 🟩 **Step 7: Design tokens + app shell** — rewrite `styles.css` as
  tokens (neutral scale, one accent, status colors, spacing, radii, Inter
  with system fallback) plus the winning variant's shell (nav with Runs /
  Claims / Settings always visible). Old class names kept as aliases until
  Phase 5 finishes, so unmigrated screens don't break.
  - **Verify:** app runs with the new shell; all three sections reachable;
    existing screens still usable (even if half-styled); vitest green.
- [x] 🟩 **Step 8: Shared components** — `Button`, `Field` (label +
  description + inline error, one pattern everywhere), `Switch`, `Badge`,
  `Alert` (error/warning/info banners), `EmptyState`, `Skeleton`, `Table`
  wrapper. Each replaces an existing ad-hoc pattern; no new behavior.
  - **Verify:** components used on at least one screen each; visual check in
    the browser; vitest green.

### Phase 5: Screen migration (one screen per step, tests after each)
- [x] 🟩 **Step 9: Claims list + New-run card** (the front door first)
  - **Verify:** start-a-run flow works end to end in the new skin.
- [x] 🟩 **Step 10: Claims run detail** (Map / Group / Verify / Review /
  Output views, flag cards, tables, banners)
  - **Verify:** a full sample run walked through in the browser; all states
    (progress, failure banner, flags, empty) rendered consistently.
- [x] 🟩 **Step 11: Invoice Runs list + detail** — same components, so the
  older module stops looking like a different app.
  - **Verify:** invoice run flow unchanged functionally; vitest green.

### Phase 6: The Settings screen (absorbs PLAN.md Step 15)
- [x] 🟩 **Step 12: Settings as a first-class section** — one screen,
  grouped: Workspace (client name, draft names, bank charge), Claims profile
  & playbook (rates, tolerances, receipt window, check toggles,
  `unclaimed_receipt_threshold`, "set by reviewer on <date>"), Feature
  switches (Phase 3, with plain-language descriptions of what each changes),
  Deployment (read-only env facts). Save with confirmation; errors inline.
  - **Verify:** every switch in the backend inventory appears exactly once;
    a flipped switch shows its audit line; PLAN.md Step 15 marked done-here.

### Phase 7: Polish + consistency pass
- [x] 🟩 **Step 13: Sweep for stragglers** — every error through `Alert`/field
  errors, every list has an empty state, every async wait a skeleton, focus
  states and labels on all inputs (keyboard walk), copy reviewed for
  plain language.
  - **Verify:** click through every screen; screenshot set captured;
    no raw browser-default styling anywhere.
- [x] 🟩 **Step 14: Docs** — README (uploads-first quickstart, switch table),
  OPERATIONS-CLAIMS.md (how a reviewer flips switches), PRD status line.
  - **Verify:** a fresh reader can start an upload run from the README alone.

## Rollback Plan
- Each phase lands as its own commit(s); `git revert` any phase without
  touching the others. Phases 1–3 are additive (new params, new keys) — the
  zip path and env flags keep working, so reverting the frontend never
  strands the backend.
- Switch store: deleting the settings rows returns every flag to its `.env`
  default (the store treats env as default).
- Design system: until Phase 5 completes, old CSS classes remain as aliases —
  a bad screen migration reverts alone.
- Prototype and losing variants live on a scratch branch, never in main.
