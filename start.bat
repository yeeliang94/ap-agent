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

REM Install packages when requirements.txt CHANGES, not merely when the venv
REM is new. The old check was "does uvicorn.exe exist", which is true forever
REM after the first run — so a git pull that added or changed a dependency
REM installed nothing, and the new code ran against the OLD libraries. That
REM failure is silent and looks like a bug in the app, not a stale setup.
REM
REM The stamp is the hash of requirements.txt, so any edit triggers a
REM re-install and an unchanged file does not.
set "REQ=backend\requirements.txt"
set "STAMP=%VENV%\.requirements-sha256"

set "REQHASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%REQ%" SHA256') do (
  if not defined REQHASH set "REQHASH=%%H"
)
set "REQHASH=%REQHASH: =%"

set "OLDHASH="
if exist "%STAMP%" set /p OLDHASH=<"%STAMP%"
REM No uvicorn means nothing was ever installed, whatever the stamp says.
if not exist "%VENV%\Scripts\uvicorn.exe" set "OLDHASH=missing"
REM If hashing itself failed, install rather than skip: being slow is a
REM nuisance, running against the wrong libraries is a lost afternoon.
if "%REQHASH%"=="" set "OLDHASH=unknown"

if not "%REQHASH%"=="%OLDHASH%" (
  if "%OLDHASH%"=="" (
    echo [setup] Installing Python packages...
  ) else (
    echo [setup] The package list changed since last time. Updating...
  )
  "%VENV%\Scripts\python.exe" -m pip install -r "%REQ%"
  if errorlevel 1 (
    echo [setup] Installing Python packages failed. If this is a proxy or
    echo         certificate error, you need the enterprise pip index configured.
    exit /b 1
  )
  REM Only stamp a hash we actually computed, so an unknown one retries.
  if not "%REQHASH%"=="" > "%STAMP%" echo %REQHASH%
)

REM --- Frontend -----------------------------------------------------------
REM Run every time, for the same reason as the Python packages above: only
REM checking whether node_modules exists means a pull that changed
REM package.json is never installed. npm install does nothing when nothing
REM changed, so the cost of always running it is a second or two.
echo [setup] Checking frontend packages...
pushd frontend
call npm install
if errorlevel 1 (
  popd
  echo [setup] npm install failed. See the message above.
  exit /b 1
)
popd

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
