@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo.
echo   agent-sleep-guard : one-click DEMO (dry-run, will NOT sleep your PC)
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo   [ERROR] Python not found. Please install Python 3.9 or newer first.
  echo.
  if not "%ASG_NO_PAUSE%"=="1" pause
  exit /b 1
)

python demo_run.py
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo   Demo finished successfully.
) else (
  echo   Demo stopped with error code %RC%. Please check the messages above.
)
if not "%ASG_NO_PAUSE%"=="1" pause
exit /b %RC%
