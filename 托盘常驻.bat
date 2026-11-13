@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 常驻托盘：无黑窗（pythonw）；没有 pythonw 时退回 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw tray.py
) else (
  start "" python tray.py
)
