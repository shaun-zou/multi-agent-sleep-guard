#!/usr/bin/env python
"""独立窗口入口：给同一份前端换一个「壳」，不改任何业务逻辑。

业界「既有 Web 版又有桌面版」最常见的做法 = 一套前端 + 两种壳：
    壳 A  浏览器标签页      -> 启动界面.bat（前台承载本机服务 + 普通网页）
    壳 B  无地址栏独立窗口  -> 本文件 + 启动桌面窗口.bat

两者共用同一个本机服务 (127.0.0.1:8765)、同一份 ui/index.html、同一套 /api/*。
关键：谁先启动谁前台承载服务；后启动的检测到端口已占用就只打开自己的窗口，
绝不起第二个服务实例，因此两个入口可以同时打开、看到同一份状态。

零第三方依赖：用 Edge / Chrome 自带的 --app 模式；找不到浏览器时回退普通网页版。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包后 __file__ 在 PyInstaller 的 _MEIPASS 里，不能当项目根用
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
PORT = int(os.environ.get("ASG_UI_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
APP_PROFILE = Path.home() / "AppData" / "Local" / "agent-sleep-guard" / "app-profile"


def port_open():
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def find_browser():
    roots = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for p in roots:
        if p.exists():
            return str(p)
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _ps_out(script: str, timeout=8) -> str:
    """跑一段 PowerShell 拿 stdout。只用于取 PID（纯 ASCII），

    编码按 utf-8 -> gbk 依次尝试，失败也不致命（调用方按查询失败处理）。
    """
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    raw = r.stdout or b""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def app_window_pids() -> list:
    """找出「我们的控制台独立窗口」对应的浏览器进程。

    判据：命令行同时含 `--app=<本服务地址>` 和**我们专用**的 `--user-data-dir`
    （专用 profile 目录是这个窗口的身份证；渲染/GPU 子进程不会带 --app）。
    PowerShell 查询失败就返回空 —— 宁可多开一个窗口，也不能把入口堵死。
    """
    if os.name != "nt":
        return []
    profile = str(APP_PROFILE).replace("'", "''")
    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='msedge.exe' OR Name='chrome.exe'\" | "
        "ForEach-Object { if ($_.CommandLine -like '*--app=" + URL + "*' "
        f"-and $_.CommandLine -like '*{profile}*') {{ $_.ProcessId }} }}"
    )
    return [int(tok) for tok in _ps_out(ps).split() if tok.isdigit()]


def _focus_window(pids) -> bool:
    """把这些进程名下**可见的**窗口置前。找不到可见窗口返回 False。

    为什么必须查可见窗口而不是只看进程存活：Edge 关掉窗口后常驻后台进程
    （后台扩展模式），只看 PID 会误判「窗口还开着」，导致再也打不开。
    """
    if os.name != "nt" or not pids:
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    # Win64 铁律：不声明 argtypes，句柄会被 c_int 截断（本项目踩过）
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]

    targets = set(pids)
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def on_window(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in targets and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value:                      # 过滤无标题的辅助窗口
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(on_window), 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)             # SW_RESTORE（从最小化恢复）
    user32.SetForegroundWindow(hwnd)
    return True


_OPEN_LOCK = threading.Lock()
_OPENING_UNTIL = 0.0        # 「正在开窗」保护期：浏览器建窗约需 1 秒，
                            # 期间连点不能每次都通过进程检查再开一个


def open_window():
    """打开（或复用）控制台独立窗口 —— **单实例**。

    托盘每点一次都会调用这里，绝不能每次都新开一个窗口（用户点 N 下就有 N 个
    相同页面）。顺序：正在开窗的保护期内直接忽略 -> 找我们的 --app 窗口 ->
    找到就置前返回；都找不到才真的新开。
    """
    global _OPENING_UNTIL
    with _OPEN_LOCK:
        if time.monotonic() < _OPENING_UNTIL:
            print("独立窗口正在打开…（连点不会开第二个）")
            return
    pids = app_window_pids()
    if pids and _focus_window(pids):
        print("控制台窗口已打开，已置前（不再新开第二个）")
        return
    browser = find_browser()
    if not browser:
        webbrowser.open(URL)
        print("未找到支持独立窗口的浏览器，已回退到普通网页版")
        return
    APP_PROFILE.mkdir(parents=True, exist_ok=True)
    with _OPEN_LOCK:
        _OPENING_UNTIL = time.monotonic() + 5.0
    subprocess.Popen([
        browser, f"--app={URL}", "--window-size=1200,900",
        f"--user-data-dir={APP_PROFILE}", "--no-first-run",
    ])
    print("独立窗口已打开（无地址栏 / 无标签页）")
    print("关闭窗口不会中断监控进程；本控制台窗口保持运行即可让服务常驻")


def serve():
    """前台承载本机服务（与 启动界面.bat 完全对等）。"""
    from ui_server import Handler
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("=" * 62)
    print("  夜间值守控制台 · 独立窗口模式")
    print(f"  地址: {URL}")
    print("  只监听本机；Web 版与桌面版共用这一个服务")
    print("  保持本窗口运行；按 Ctrl+C 关闭服务")
    print("=" * 62)
    if not os.environ.get("ASG_LAUNCHER_NO_OPEN"):
        threading.Timer(0.8, open_window).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已关闭（监控进程不受影响）")
    finally:
        server.server_close()


def main():
    check_only = "--check" in sys.argv
    if port_open():
        print(f"复用已运行的本机服务: {URL}（不会启动第二个实例）")
        if check_only:
            print(f"独立窗口浏览器: {find_browser() or '无，将回退普通网页版'}")
            return 0
        if not os.environ.get("ASG_LAUNCHER_NO_OPEN"):
            open_window()
        else:
            print("（测试模式：未打开窗口）")
        return 0
    if check_only:
        print("当前没有服务在运行；独立窗口入口会自行承载服务")
        print(f"独立窗口浏览器: {find_browser() or '无，将回退普通网页版'}")
        return 0
    print("没有检测到服务，由桌面入口前台承载服务…")
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
