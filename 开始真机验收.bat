@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo.
echo   REAL SLEEP acceptance test
echo   This is the ONLY entry that actually puts the PC into low power.
echo   Save all work first. After it sleeps, press the power button to wake.
echo   Type SLEEP (uppercase) to confirm, or just close this window to cancel.
echo   Formal configs remain dry_run=true; all artifacts go to acceptance/.
echo.

python run_acceptance.py
if errorlevel 1 (
  echo.
  echo   Cancelled or failed. No sleep command was issued.
)
pause
