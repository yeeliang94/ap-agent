# Claims Module — Operations (hardening H11)

## Feature switches (Settings screen; .env = default)

Since PLAN-UPLOADS-AND-UI Phase 3, the behavior switches below are
**reviewer-flippable from Settings → Feature switches** (`GET/PUT
/api/settings/switches`). The .env value is the default until the first
in-app save; every change is written to the audit trail. A change applies
to **new runs only** — a claims run snapshots the switches at creation and
keeps them (`snapshot.switches`), so a mid-flight flip never changes a
running run. What a reviewer may *do* on screen (case routes, regrouping
actions) follows the live switch. One new switch joined them:

| Switch | Default | Off means |
|---|---|---|
| SharePoint source (`claims_sharepoint_source`) | on when `DOC_SOURCE=mcp`, else off | the New-run form offers no link fields and a SharePoint link is refused with a plain message; uploads (a folder, a zip, or files) are the only way in |

Env-only (never flippable on screen; the Settings screen shows them
read-only as set / not set): the sandbox trio, `CLAIMS_LOCAL_ROOT`, model
names, keys and the MCP endpoint.

### The switches (env spelling; read at start as defaults)

| Switch | Default | Off means |
|---|---|---|
| `CLAIMS_CASE_MODEL` | on | case fields hidden from the run detail and the case routes (`/cases/*`, `/artifacts/*`, `/confirm-grouping`) answer 404; the employee fields and the delivered MapView stay authoritative; storage unchanged |
| `CLAIMS_AGENTIC_INVESTIGATION` | off | new runs use the delivered structured-folder mapper; old runs are never reinterpreted |
| `CLAIMS_SHADOW_INVESTIGATION` | off | with the agentic switch off, the investigator also runs on each new run; its result is compared and recorded (`SHADOW_RESULT`, `shadow_investigation`), never used |
| `CLAIMS_FULL_DUMP_GROUPING` | off | the regrouping actions in Organize (create / merge / split / move / role) answer 400 and are hidden; the gate, the claim-summary sheet choice, claimants and file dispositions stay — a flat folder is inventoried and, with the agentic switch on, grouped by the investigator, but the reviewer cannot regroup it on screen |
| `CLAIMS_PYTHON_SANDBOX` + `CLAIMS_SANDBOX_RUNNER` + `CLAIMS_SANDBOX_ISOLATED` | off | `run_python` absent from the allowlist (`docs/SANDBOX.md`) |
| `CLAIMS_LOCAL_ROOT` | off (blank) | a folder path is refused exactly like any other non-https link and zip upload stays the local way in; see "Local or synced-folder ingestion" below |

Two reviewer-set profile values deliberately reach the output and later
runs: `listing_columns` may pin a literal into a column (the literal is a
reviewer-set Explicit Client Profile value, shown under "pinned columns" on
the listing header record — that is the "confirmed value" control 10 asks
for), and `PUT /artifacts/{id}/role` with `remember=true` adds a file-name →
role pattern to the profile exactly as the delivered map's "remember" did
(a supplied filename rule, never learned from AI). Neither is set by the AI.

Migrations run at every start, unconditionally and additively (`claims_schema`
records what was applied). No switch skips or reverses one.

## Live progress and reviewer routes

Claim creation lives at `/claims/new`. A successful upload opens
`/claims/{run_id}/progress` immediately. The run progresses through the
standard `preparing`, `organizing`, `checking`, and `finalizing` phases; each
worker also records its current stable step in `claim_employees.progress`.
Those worker updates use separate short database sessions and never commit
the rows, evidence, or flags being staged by verification.

Organization, review, and export use `/claims/{run_id}/organize`, `/review`,
and `/export`. Activity is a drawer on the run workspace. Technical
investigator/tool details are inside Activity → Technical details and are not
part of the default reviewer view. Polling remains three seconds only while a
run is active; a resting run is refreshed on focus or after a reviewer action.

## Local or synced-folder ingestion

With `DOC_SOURCE=local`, a reviewer may paste a folder path instead of a
SharePoint URL when `CLAIMS_LOCAL_ROOT` names the parent tree the server is
allowed to read. A OneDrive-synced SharePoint library is the fastest option:
the run still copies an immutable snapshot, but avoids downloading each file
through the gateway. Keep the allowed root narrow; a drive root or whole user
directory would expose unrelated files to anyone who can start a run. Leaving
the value blank disables folder-path ingestion and leaves zip upload available.

## Copying a SharePoint folder: one session, serial calls

A folder copy holds ONE MCP session open for the whole copy (`docsource.batch`
over `mcp_client.SessionWorker`) and resolves the site and library once inside
it, rather than once per file. The listing workbook is fetched the same way.
The session is per FOLDER, not per run: a run that also fetches a listing
workbook from a different folder opens a second one.

Calls on that session are served **one at a time, deliberately**. Copying
files concurrently is the obvious next speed-up and is **deferred**: the
enterprise gateway's behaviour under concurrent calls (its throttling and
per-tenant limits) is not documented to us, and a parallel copy that trips it
would be slower and more visible than a serial one. Revisit once a real
gateway has been measured; until then serial is the recorded decision, not an
oversight.

## Budgets

| Budget | Where | Default |
|---|---|---|
| AI requests per agent conversation | `MAX_AGENT_REQUESTS` | 40 |
| AI requests per case worker (report + pages + category + tie-breaks + correction re-checks) | `worker.WORKER_REQUEST_CAP` | 160 |
| Investigation: model requests / tool calls / bytes read / pages read / wall time | `investigator.contracts.Budget` | 160 / 400 / 200 MB / 2000 / 600 s |
| Page reads in flight across all workers | `evidence.PAGE_READ_CONCURRENCY` | 5 |
| One AI call | `evidence.AI_CALL_TIMEOUT` | 180 s |
| Sandbox | `SandboxLimits` | 30 s wall, 20 s CPU, 512 MB, 1 process, 64 files, 50 MB in, 2 MB out |
| Ingestion | `source.py` | 1500 files / 1500 MB / 6000 pages per run, 25 MB per file, depth 3 |
| Per case | `source.py` | 60 files / 200 pages; 30 cases per run |

Every run's diary records per-case seconds, AI requests and tokens; the
investigator adds its own request/token/tool-call line. The speed target
(PRD): a ten-case structured batch under five minutes.

## Retention

A run's workspace `runs/<id>/claims/` holds `files/` (the immutable snapshot;
Citations and the replay bundle resolve to it — kept for the run's life),
`peeks/` (survey thumbnails — kept), and `tool_output/` (page renders and
sandbox output handed to the agent — scratch, pruned automatically when the
run closes: `retention.prune_tool_output`). `retention.prune_closed_runs(db)`
prunes the scratch of every closed run on demand. Nothing in the module
deletes a run, a database row, or anything in SharePoint; run deletion is an
operator decision outside the module.

## Recovery

- **Restart:** runs left in an active state (queued / surveying / mapping /
  verifying) are marked failed with a plain reason at startup; resting states
  (map_ready, ready) survive (`runner.fail_interrupted_runs`).
- **Cancellation:** `POST /api/claims-runs/{id}/cancel` stops the active
  investigation's tool calls, marks the run failed ("cancelled by the
  reviewer"), and the workers stop at their next step; a failed run is never
  closed as ready.
- **Partial tool failure:** a failed tool call is recorded with its error and
  becomes a `TOOL_FAILED` flag; the investigation continues; unresolved files
  stay visible.
- **One case failing:** isolated; retry per case (`POST /cases/{id}/retry`).
- **Stale screens:** every mutation carries `expected_revision`; a 409 reloads.

## Replay

`GET /api/claims-runs/{id}/replay` returns the bundle (manifest with hashes,
versions, plan, tool executions with hashes and the recorded calculations,
cases, lines, flags with decisions, reviewer actions, listing header map,
published output). `?verify=1` returns the verifier's report: every recorded
calculation re-evaluated, every cited file checked against the manifest,
the published output rebuilt from the stored state and compared, the TSV
re-summed, each case's lines re-summed (`replay.verify_bundle`).
