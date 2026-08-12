@echo off
setlocal
REM Windows enterprise startup. Mirrors the enterprise repo's conventions:
REM UTF-8 mode is REQUIRED (financial PDF text extraction fails without it).
set PYTHONUTF8=1
cd /d "%~dp0"

REM Every step below is checked. A step that fails stops the script with a
REM message, instead of leaving a half-built state that later runs skip over.

if not exist .env (
  echo [setup] Copy .env.example to .env and fill in the enterprise proxy values first.
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo [setup] Python is not on PATH. Open a new terminal, or install Python 3.12+.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [setup] npm is not on PATH. Install Node.js 20+ and reopen the terminal.
  exit /b 1
)

REM --- Python environment -------------------------------------------------
REM The completion marker is Scripts\python.exe, NOT the .venv folder. A venv
REM that was interrupted leaves the folder behind with nothing usable in it;
REM testing the folder would silently skip setup forever.
set "VENV=backend\.venv"

if exist "%VENV%" if not exist "%VENV%\Scripts\python.exe" (
  echo [setup] Found an incomplete %VENV% from an earlier interrupted run. Removing it.
  rmdir /s /q "%VENV%"
  if exist "%VENV%" (
    echo [setup] Could not remove %VENV%. Close any terminal or editor using it and retry.
    exit /b 1
  )
)

if not exist "%VENV%\Scripts\python.exe" (
  echo [setup] Creating the Python environment. This writes a few thousand small
  echo         files, so on a corporate laptop it can take several minutes the
  echo         first time. Let it finish; do not press Ctrl+C.
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo [setup] Creating the environment failed. See the message above.
    exit /b 1
  )
  if not exist "%VENV%\Scripts\python.exe" (
    echo [setup] The environment was created but is incomplete. Re-run this script.
    exit /b 1
  )
)

REM Installed-packages marker: uvicorn is the last thing we launch, so if its
REM launcher is present the install got far enough to be usable.
if not exist "%VENV%\Scripts\uvicorn.exe" (
  echo [setup] Installing Python packages...
  "%VENV%\Scripts\python.exe" -m pip install -r backend\requirements.txt
  if errorlevel 1 (
    echo [setup] Installing Python packages failed. If this is a proxy or
    echo         certificate error, you need the enterprise pip index configured.
    exit /b 1
  )
)

REM --- Frontend -----------------------------------------------------------
if not exist frontend\node_modules (
  echo [setup] Installing frontend packages...
  pushd frontend
  call npm install
  if errorlevel 1 (
    popd
    echo [setup] npm install failed. See the message above.
    exit /b 1
  )
  popd
)

echo [build] Building the frontend...
pushd frontend
call npm run build
if errorlevel 1 (
  popd
  echo [build] Frontend build failed. See the message above.
  exit /b 1
)
popd

REM --- Run ----------------------------------------------------------------
REM The server runs in THIS window, not a separate one. A separate window
REM closes the instant the process dies, taking the crash message with it.
REM Tee-Object shows every line here and copies it to server-log.txt, and
REM 2>&1 merges the error channel in so tracebacks are not lost.
REM
REM ForEach-Object { $_.ToString() } matters: uvicorn writes ALL its logs
REM to stderr, including routine ones. PowerShell wraps anything a native
REM program sends to stderr in a red NativeCommandError record, so a
REM healthy startup looks like a crash and a real crash looks the same as
REM everything else. Converting each record to plain text first keeps the
REM output readable — and keeps server-log.txt free of PowerShell noise.
echo.
echo App starting at http://localhost:8002
echo The backend serves the built frontend from frontend\dist.
echo Logs appear below and are saved to server-log.txt
echo Press Ctrl+C to stop the server.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Continue'; & '%VENV%\Scripts\uvicorn.exe' app.main:app --port 8002 --app-dir backend 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath 'server-log.txt'"
endlocal
