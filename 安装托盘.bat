@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 夜间值守 · 托盘常驻安装 ===
echo 将创建桌面与开始菜单快捷方式，并设置开机自启（Startup 文件夹，无注册表残留）。
python install.py install --autostart
if errorlevel 1 pause
echo.
echo 完成。双击桌面「夜间值守」即可常驻托盘。
