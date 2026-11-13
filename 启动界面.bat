@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo.
echo   agent-sleep-guard : local console (P0)
echo   Browser will open at http://127.0.0.1:8765/
echo   Local only. No network. No data leaves this machine.
echo   Press Ctrl+C here to close the console service.
echo   Closing the browser does NOT stop the monitor process.
echo.

python ui_server.py
if errorlevel 1 (
  echo.
  echo   [ERROR] Failed to start. Make sure Python 3.9+ is installed.
)
pause
