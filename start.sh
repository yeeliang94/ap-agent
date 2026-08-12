#!/bin/sh
# Local development startup (Mac): backend on :8002, frontend dev on :5173.
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "Copy .env.example to .env and fill in your key first."; exit 1; }
[ -d backend/.venv ] || (cd backend && uv venv --python 3.12 .venv && uv pip install -r requirements.txt --python .venv/bin/python)
[ -d frontend/node_modules ] || (cd frontend && npm install)
(cd backend && ./.venv/bin/uvicorn app.main:app --port 8002 --reload &)
cd frontend && npm run dev
