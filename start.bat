@echo off
REM Windows enterprise startup. Mirrors the enterprise repo's conventions:
REM UTF-8 mode is REQUIRED (financial PDF text extraction fails without it).
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist .env (
  echo Copy .env.example to .env and fill in the enterprise proxy values first.
  exit /b 1
)
if not exist backend\.venv (
  python -m venv backend\.venv
  backend\.venv\Scripts\pip install -r backend\requirements.txt
)
if not exist frontend\node_modules (
  cd frontend && call npm install && cd ..
)
cd frontend && call npm run build && cd ..
start "ap-agent-backend" backend\.venv\Scripts\uvicorn app.main:app --port 8002 --app-dir backend
echo App starting at http://localhost:8002 (backend serves the built frontend from frontend\dist).
