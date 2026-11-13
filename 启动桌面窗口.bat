@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo.
echo   agent-sleep-guard : desktop window (same local service)
echo   Opens a standalone window without address bar or tabs.
echo   The existing browser-tab version stays available and unchanged.
echo   Both share ONE local service and ONE set of /api endpoints.
echo.

python desktop_launcher.py
if errorlevel 1 (
  echo.
  echo   [ERROR] Failed to start. Check logs/ui-server.log
)
pause
