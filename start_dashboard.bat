@echo off
REM Splunk traffic dashboard launcher (first run: open http://127.0.0.1:8091
REM and follow the setup wizard; see README.md for details).
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"

REM prefer a local venv if it exists, else whatever python is on PATH
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

REM refuse to double-bind the default port
netstat -ano | findstr :8091 | findstr LISTENING >nul
if not errorlevel 1 (
  echo [dash] port 8091 already in use - the dashboard is probably already running.
  echo [dash] open http://127.0.0.1:8091 , or change DASHBOARD_PORT in .env / config.json
  pause
  exit /b 1
)

echo [dash] starting on http://127.0.0.1:8091  (Ctrl+C to stop)
"%PY%" -u -m traffic_dashboard.server
pause
