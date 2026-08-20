# AP Agent — MVP

An AI-assisted Accounts Payable checker. A user uploads a client's monthly
batch (invoices + staff claims, including scans); the pipeline sorts, reads,
and checks every document; flags land on a review screen with cited reasons;
and the app produces **copy-ready** Maybank upload rows and file names, plus
a **draft** of next month's payment-listing tab on a copy of the client's own
workbook. The agent never writes to SharePoint or any working file — a
person pastes, and finalises the draft after the bank run.

Design doc: [ap-agent-design.html](ap-agent-design.html) · MVP plan (done): [docs/PLAN-MVP.md](docs/PLAN-MVP.md) · Claims module: [docs/PRD.md](docs/PRD.md) → [docs/PLAN.md](docs/PLAN.md) → [agentic/full-dump hardening](docs/CLAIMS-AGENT-HARDENING.md)

## Layout

| Path | What it is |
|---|---|
| `backend/` | Python FastAPI app: pipeline (sort → extract → check → output), SQLite, API |
| `frontend/` | React + TypeScript app: dashboard, review screen, copy-output screen |
| `samples/` | Synthetic demo data generator (`generate_samples.py`) with planted anomalies + ground truth |
| `fake_mcp/` | Local stand-in for the enterprise SharePoint MCP (read-only contract, incl. its quirks) |
| `backend/scripts/verify_run.py` | End-to-end scoring against `ground_truth.json` |

## Quickstart (Mac / local development)

```bash
cp .env.example .env         # then put your OpenAI key in OPENAI_API_KEY
./start.sh                   # backend :8002 + frontend :5173
```

Generate demo data and verify the pipeline end to end:

```bash
backend/.venv/bin/python samples/generate_samples.py
backend/.venv/bin/python backend/scripts/verify_run.py
```

Then open http://localhost:5173, upload `samples/generated/demo_batch.zip`.

Tests: `cd backend && .venv/bin/python -m pytest` runs the whole suite with the
AI faked — it makes **no paid calls**. Two tests are opt-in because they call
the real model: `AP_LIVE_TESTS=1` for the model-layer smoke test, and
`AP_LISTING_EVAL=<workbook>` for the real-listing evaluation. Exact dependency
versions used for verification are recorded in `backend/requirements.lock`
(`requirements.txt` stays the readable list of direct dependencies).

## Windows enterprise setup

Run `start.bat` from the repo root. Differences that matter, all in `.env`:

| Setting | Local (Mac) | Windows enterprise |
|---|---|---|
| `LLM_PROXY_URL` | empty (direct OpenAI) | the shared enterprise proxy URL |
| `OPENAI_API_KEY` | your OpenAI key | the **enterprise proxy key** (same variable) |
| `SORT_MODEL` etc. | OpenAI names (`gpt-4o-mini`) | catalogue IDs from `config/models.json` (e.g. `openai.gpt-5.4`) |
| `DOC_SOURCE` | `local` or `mcp` (fake) | `mcp`, pointed at the real SharePoint MCP |
| `CLAIMS_LOCAL_ROOT` | optional allowed parent for direct folder ingestion | leave blank when using SharePoint MCP |
| `PYTHONUTF8` | not needed | **required** — `start.bat` sets it |

Rules inherited from the enterprise repo, already honored in code:
- All model calls go through one factory (`backend/app/model_layer.py`); no
  direct provider clients anywhere else. Request cap ≤ 40 per agent.
- Credentials, tokens, and temporary download URLs stay in the server layer:
  never in prompts, logs, or the browser.
- SharePoint access is read-only and delegated; a background job that loses
  its connection fails with a structured message — it never opens a browser.
- Do not disable TLS validation; the enterprise app uses `truststore` for
  the corporate certificate authority.

## Windows test checklist (maps to design doc §9)

1. **SharePoint read access suffices** — point `DOC_SOURCE=mcp` + `MCP_URL`
   at the real MCP, set the folder URL in **Settings on the main screen**
   (paste the real AP folder's browser address; `SHAREPOINT_FOLDER_URL` in
   .env is only the first-start default), and confirm the three reference
   files load. Watch for intermittent ReadError: the adapter retries 3×
   (`backend/app/docsource.py`) — confirm that's enough.
2. **Sign-in lasts a full run** — run a real batch end-to-end against a live
   delegated session; confirm mid-run expiry produces the structured
   "source unavailable" failure, not a hang or a popup.
3. **Model tiering works on the proxy** — set catalogue IDs for the three
   roles, run `pytest backend/tests/test_model_layer.py`, then the full
   verify script; compare extraction accuracy and cost with the local run.
4. **Template output pastes cleanly** — run a real (anonymized) batch, copy
   the bank block into the real bank template, and open the downloaded
   listing draft beside the real workbook (see docs/LISTING-HARDENING.md;
   the draft's grouping / voucher / fund rules are marked "assumed —
   confirm" there). Run the opt-in real-model evaluation
   (`AP_LISTING_EVAL=<anonymised listing> pytest backend/tests/test_listing_eval.py`)
   before switching models.

## Honest limitations (MVP)

- Single user, no login; "reviewer" is hardcoded as the actor, so what the
  code calls the audit trail is really a **decision log** (what was decided,
  when, with what note) — not yet *who*. Follow-up for the Windows pilot:
  record the delegated Entra sign-in's account name as the actor.
- If the server restarts mid-run, that run is marked failed at startup with
  a plain reason ("interrupted by a server restart — start a new run");
  runs do not resume.
- A batch that needs a reference file the folder lacks (staff claims with no
  expense policy; any batch with no bank upload template) gets a run-level
  `MISSING_REFERENCE` flag the reviewer must acknowledge before output —
  never a silent skip.
- One client at a time, enforced: the API rejects any client other than the
  one set in Settings on the main screen (client name + SharePoint folder;
  .env values are the first-start defaults). Per-client config files —
  several clients side by side — are designed but not built.
- Bank rows never contain account numbers — there is no vendor master yet,
  so every account cell says `[ACCOUNT UNKNOWN - fill from vendor master]`.
  A vendor master reference file is the intended next data source.
- Reviewers can accept, exclude, or **correct** a flagged document ("Fix a
  value" on the flag card): corrections are audited (before → after, who,
  why), the document alone is re-checked instantly — no pipeline re-run —
  and flags that no longer apply resolve themselves as "resolved by
  correction". Clean documents are not pre-checked by humans; flags direct
  attention.
- Every document is read twice ("double-read"): disagreement between the two
  independent reads marks the document low-confidence, because models can
  misread degraded scans *confidently*. This doubles extraction cost
  (~US$0.10 per demo batch) and is worth every cent.
- Claim receipts pair by filename convention; unmatched receipts are flagged, not guessed.
  **"Claims" here means the MVP's simplified single-form claim.** The real
  employee-claims workflow (per-employee SharePoint folders, expense-report
  workbooks, receipt bundles, mileage against maps, one listing row per
  employee) is a separate module, specified in [docs/PRD.md](docs/PRD.md) and
  planned in [docs/PLAN.md](docs/PLAN.md) — not built yet.
- The Maybank rows are template-aligned **draft** rows: every account cell is
  `[ACCOUNT UNKNOWN]` until a vendor master exists (see above), so they are
  not paste-and-upload ready. The listing draft's bookkeeping rules are
  working assumptions awaiting client sign-off — see the sign-off table at
  the end of [docs/LISTING-HARDENING.md](docs/LISTING-HARDENING.md).
