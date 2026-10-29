@echo off
setlocal EnableExtensions
title API Sentinel
rem ===========================================================================
rem  API Sentinel - one-click start for Windows
rem  Double-click this file (or run "start.bat" in a terminal). It will:
rem    1. create backend\.venv and install Python packages (first run, or when
rem       requirements.txt changes)
rem    2. install frontend packages (first run, or when package-lock.json changes)
rem    3. rebuild the web UI when its source files changed
rem    4. start the app on http://127.0.0.1:8000 and the demo deployment on :8100
rem    5. open your browser when the app is ready
rem  Stop everything with Ctrl+C in this window.
rem  Options:  start.bat --rebuild   force a fresh UI build
rem            set SENTINEL_PORT=8001 before running to use another port
rem ===========================================================================

cd /d "%~dp0"
set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"
set "VENV=%BACKEND%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PORT=8000"
if defined SENTINEL_PORT set "PORT=%SENTINEL_PORT%"
set "DEMO_PORT=8100"
set "URL=http://127.0.0.1:%PORT%"
set "FORCE_BUILD="
if /i "%~1"=="--rebuild" set "FORCE_BUILD=1"

echo.
echo  ==================  API Sentinel  ==================
echo.

rem ---------------------------------------------------------------- 1. Python
if exist "%PY%" (
  "%PY%" -c "import sys" >nul 2>nul && goto :have_venv
  echo [setup] The existing backend\.venv is broken. Recreating it...
  rmdir /s /q "%VENV%"
)
set "SYSPY="
py -3 -c "import sys" >nul 2>nul && set "SYSPY=py -3"
if not defined SYSPY python -c "import sys" >nul 2>nul && set "SYSPY=python"
if not defined SYSPY (
  echo [error] Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH" during setup.
  goto :fail
)
%SYSPY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || (
  echo [error] Python 3.10 or newer is required. Found:
  %SYSPY% --version
  goto :fail
)
echo [setup] Creating the Python environment in backend\.venv ...
%SYSPY% -m venv "%VENV%" || goto :fail

:have_venv
fc /b "%BACKEND%\requirements.txt" "%VENV%\.sentinel-requirements" >nul 2>nul && goto :have_packages
echo [setup] Installing Python packages (first run takes a few minutes)...
"%PY%" -m pip install --disable-pip-version-check -r "%BACKEND%\requirements.txt" || (
  echo [error] pip install failed. Check your internet connection and the messages above.
  goto :fail
)
copy /y "%BACKEND%\requirements.txt" "%VENV%\.sentinel-requirements" >nul
:have_packages
echo [ok]    Python environment ready.

rem ---------------------------------------------------------------- 2. Frontend
where npm >nul 2>nul || (
  echo [error] Node.js was not found. Install Node.js 18 or newer from https://nodejs.org/
  goto :fail
)
cd /d "%FRONTEND%"
if not exist "node_modules" goto :npm_install
fc /b "package-lock.json" "node_modules\.sentinel-lock" >nul 2>nul && goto :have_node_modules
:npm_install
echo [setup] Installing frontend packages...
call npm ci --no-audit --no-fund || (
  echo [error] npm ci failed. See the messages above.
  goto :fail
)
copy /y "package-lock.json" "node_modules\.sentinel-lock" >nul
:have_node_modules

if defined FORCE_BUILD goto :build
if not exist "dist\index.html" goto :build
rem Rebuild only if any UI source file is newer than the last build.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$built = (Get-Item 'dist\index.html').LastWriteTime; $newest = Get-ChildItem -Path 'src','index.html','package.json','vite.config.ts' -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($newest.LastWriteTime -gt $built) { exit 1 } else { exit 0 }" >nul 2>nul && goto :have_build
:build
echo [setup] Building the web interface...
call npm run build || (
  echo [error] The frontend build failed. See the messages above.
  goto :fail
)
:have_build
echo [ok]    Web interface ready.
cd /d "%BACKEND%"

rem ---------------------------------------------------------------- 3. Port check
call :port_pid %PORT%
if not defined PORT_PID goto :port_free
echo.
echo [!] Port %PORT% is already in use (process %PORT_PID%).
echo     This is usually an older API Sentinel window that is still running.
choice /c SOQ /n /m "    [S]top it and start fresh, [O]pen the browser only, or [Q]uit? "
if errorlevel 3 goto :end
if errorlevel 2 (
  start "" "%URL%"
  goto :end
)
taskkill /pid %PORT_PID% /f >nul 2>nul
timeout /t 2 /nobreak >nul
call :port_pid %PORT%
if defined PORT_PID (
  echo [error] Could not free port %PORT%. Close the program using it, or run:
  echo         set SENTINEL_PORT=8001 ^& start.bat
  goto :fail
)
:port_free

rem ---------------------------------------------------------------- 4. Start
call :port_pid %DEMO_PORT%
if defined PORT_PID (
  echo [info]  Port %DEMO_PORT% is busy, so the demo deployment is not started.
) else (
  start "" /b "%PY%" -m uvicorn demo_target.petstore_v2:app --host 127.0.0.1 --port %DEMO_PORT% --log-level warning
  echo [ok]    Demo deployment for live tests: http://127.0.0.1:%DEMO_PORT%/v1
)

if exist "%BACKEND%\.env" (
  echo [info]  LLM settings are read from backend\.env. Check the local model in the app's sidebar ^(Test connection^).
) else (
  echo [info]  No backend\.env found: the agent runs in rules mode. See backend\.env.example to use Ollama.
)

rem Open the browser in the background as soon as the server answers (up to 60 s).
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "for ($i = 0; $i -lt 60; $i++) { try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri '%URL%/api/health'; if ($r.StatusCode -eq 200) { Start-Process '%URL%'; exit } } catch {}; Start-Sleep -Seconds 1 }"

echo.
echo  API Sentinel is starting at %URL%
echo  Your browser opens automatically when it is ready.
echo  Press Ctrl+C in this window to stop everything.
echo.
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
set "SERVER_EXIT=%ERRORLEVEL%"

rem uvicorn has exited (Ctrl+C or an error): stop the demo deployment if it is still running.
call :port_pid %DEMO_PORT%
if defined PORT_PID taskkill /pid %PORT_PID% /f >nul 2>nul
if not "%SERVER_EXIT%"=="0" (
  echo.
  echo [error] The server stopped with exit code %SERVER_EXIT%. See the messages above.
  pause
)
goto :end

rem ---------------------------------------------------------------- helpers
:port_pid
rem Sets PORT_PID to the process listening on port %1, or clears it.
set "PORT_PID="
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /r /c:":%1 .*LISTENING"') do set "PORT_PID=%%p"
exit /b 0

:fail
echo.
echo  API Sentinel did not start. Fix the problem above and run start.bat again.
pause
exit /b 1

:end
endlocal
exit /b 0
