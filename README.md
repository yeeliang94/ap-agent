# AP Agent — MVP

An AI-assisted Accounts Payable checker. A user uploads a client's monthly
batch (invoices + staff claims, including scans); the pipeline sorts, reads,
and checks every document; flags land on a review screen with cited reasons;
and the app produces **copy-ready** payment-listing and Maybank rows. The
agent never writes to SharePoint or any working file — a person pastes.

Design doc: [ap-agent-design.html](ap-agent-design.html) · Plan & status: [docs/PLAN.md](docs/PLAN.md)

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

## Windows enterprise setup

Run `start.bat` from the repo root. Differences that matter, all in `.env`:

| Setting | Local (Mac) | Windows enterprise |
|---|---|---|
| `LLM_PROXY_URL` | empty (direct OpenAI) | the shared enterprise proxy URL |
| `OPENAI_API_KEY` | your OpenAI key | the **enterprise proxy key** (same variable) |
| `SORT_MODEL` etc. | OpenAI names (`gpt-4o-mini`) | catalogue IDs from `config/models.json` (e.g. `openai.gpt-5.4`) |
| `DOC_SOURCE` | `local` or `mcp` (fake) | `mcp`, pointed at the real SharePoint MCP |
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
   the three blocks, paste into the real listing workbook and bank template.

## Honest limitations (MVP)

- Single user, no login; "reviewer" is hardcoded in the audit trail.
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
