@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 夜间值守 · 卸载托盘入口 ===
echo 只移除快捷方式与开机自启；程序目录、日志、快照、配置一律不动。
python install.py uninstall
if errorlevel 1 pause
