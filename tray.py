#!/usr/bin/env python
"""托盘常驻入口（P2）：让工具从「记得去双击 bat」变成「一直在托盘里」。

零第三方依赖：托盘图标、右键菜单、气泡通知全部用 ctypes 调 Win32
（Shell_NotifyIconW / CreatePopupMenuW / TrackPopupMenu）。不引入
pywin32 / pystray / Tauri —— 这是本项目「双击即用、零安装」的前提。

职责边界（重要）：
  - 托盘只做「入口 + 状态展示 + 快捷动作」，**不代替人做任何判断**；
  - 托盘**不能**开启真实监控：真睡授权必须走网页里那次确认（一次性令牌），
    托盘菜单只提供「打开控制台，去确认开启」，绝不自己拼 API 绕过令牌；
  - 停止监控、跑自检这类可逆动作可以直接从托盘发起（与界面同一个 API）。

服务承载：与 desktop_launcher / ui_server 对称 —— 谁先启动谁承载 8765，
后启动的只复用，绝不起第二个实例。托盘通常就是那个「先启动」的常驻进程。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asg import util  # noqa: E402

# 可写数据根：源码运行 = 项目目录；打包后 = exe 所在目录（图标也生成在这里）
ROOT = util.ROOT

PORT = int(os.environ.get("ASG_UI_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
ICON_PATH = ROOT / "state" / "tray-icon.ico"

# ---- Win32 常量 -------------------------------------------------------
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAY = WM_APP + 1          # 托盘回调消息
WM_TRAY_BALLOON = WM_APP + 2  # 后台线程 -> UI 线程：弹气泡

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x00000010, 0x00000040
SW_HIDE = 0

# 菜单项 ID
MENU_OPEN, MENU_STATUS, MENU_STOP, MENU_SELFCHECK, MENU_EXIT = 101, 102, 103, 104, 199

if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
else:  # 只为让本文件在非 Windows 上能被 import（不提供功能）
    user32 = shell32 = kernel32 = None
    LRESULT = ctypes.c_long
    WNDPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_void_p, ctypes.c_void_p)


def _setup_prototypes():
    """Win64 下必须显式声明签名。

    不声明 argtypes 时，ctypes 会把 Python str 当成 ANSI 指针传，
    宽字符 API 会拿到乱码甚至崩；句柄/图标返回值默认 c_int 还会被截断成 32 位。
    """
    if os.name != "nt":
        return
    H, U, W, L = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        H, H, wintypes.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = H
    user32.DefWindowProcW.argtypes = [H, U, W, L]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreatePopupMenu.restype = H
    user32.AppendMenuW.argtypes = [H, U, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [H, U, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, H, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [H]
    user32.PostMessageW.argtypes = [H, U, W, L]
    user32.DestroyMenu.argtypes = [H]
    user32.DestroyWindow.argtypes = [H]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), H, U, U]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, U,
                                  ctypes.c_int, ctypes.c_int, U]
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                          ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


_setup_prototypes()


# ---- 图标：程序化生成一个新月 .ico（不引入二进制资源）-----------------
def ensure_icon():
    """生成 32x32 新月图标并缓存到 state/tray-icon.ico；失败返回 None。"""
    if ICON_PATH.exists():
        return ICON_PATH
    try:
        size = 32
        px = bytearray()
        for y in range(size - 1, -1, -1):          # BMP 自下而上
            for x in range(size):
                cx, cy = x + 0.5, y + 0.5
                inside = (cx - 15.5) ** 2 + (cy - 15.5) ** 2 <= 15.0 ** 2
                cut = (cx - 22.5) ** 2 + (cy - 8.0) ** 2 <= 12.2 ** 2
                if inside and not cut:
                    # 深蓝底 + 左上更亮的渐变，避免纯色扁平
                    t = max(0.0, min(1.0, (x + (size - y)) / (2 * size)))
                    px += bytes((190 - int(70 * t), 120 - int(30 * t), 60, 255))
                else:
                    px += b"\x00\x00\x00\x00"
        and_mask = b"\x00" * (size * size // 8)
        bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          len(px) + len(and_mask), 0, 0, 0, 0)
        data = px + and_mask
        # ICO = ICONDIR(6) + ICONDIRENTRY(16) + BITMAPINFOHEADER + XOR位图 + AND掩码
        entry_off = 6 + 16
        header = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32,
                            len(bih) + len(data), entry_off)
        ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
        ICON_PATH.write_bytes(header + entry + bih + data)
        return ICON_PATH
    except Exception:  # noqa: BLE001 - 图标是锦上添花，失败就用系统图标
        return None


# ---- 本地服务 ---------------------------------------------------------
def port_open():
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def api(path, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:  # noqa: BLE001
            return {"ok": False, "message": f"HTTP {e.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"服务不可用: {exc}"}


def ensure_server():
    """没有服务就由托盘承载（daemon 线程），不为托盘另起第二个实例。"""
    if port_open():
        return False
    from ui_server import Handler
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=server.serve_forever, name="asg-http", daemon=True)
    t.start()
    globals()["_SERVER"] = server
    for _ in range(50):
        if port_open():
            break
        time.sleep(0.1)
    return True


# ---- 托盘本体 ---------------------------------------------------------
class Tray:
    def __init__(self):
        self.hwnd = None
        self.hicon = None
        self.nid = None
        self.quit = False
        self._pending = []          # 后台线程要弹的气泡
        self._lock = threading.Lock()
        self._last = {"running": None, "intent": None}

    # --- 气泡 ---
    def balloon(self, title, text, level=NIIF_INFO):
        """线程安全：后台线程只排队 + PostMessage，真正弹窗在 UI 线程。"""
        with self._lock:
            self._pending.append((title, text, level))
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_TRAY_BALLOON, 0, 0)

    def _show_pending(self):
        with self._lock:
            items, self._pending = self._pending, []
        for title, text, level in items[-3:]:
            self._modify(title, text, level)

    def _modify(self, title="", text="", level=NIIF_INFO):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = level
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # --- 窗口 ---
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = (lparam or 0) & 0xFFFF
            if ev == WM_RBUTTONUP:
                self._menu()
            elif ev in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                self.action(MENU_OPEN)
            return 0
        if msg == WM_TRAY_BALLOON:
            self._show_pending()
            return 0
        if msg == WM_COMMAND:
            self.action((wparam or 0) & 0xFFFF)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)      # 必须持有引用，否则回调被回收
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "ASGTrayWindow"
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (1410,):          # 1410 = 类已注册（重复启动）
                raise OSError(f"RegisterClassExW 失败: {err}")
        self.hwnd = user32.CreateWindowExW(
            0, "ASGTrayWindow", "agent-sleep-guard tray", 0,
            0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW 失败: {ctypes.get_last_error()}")

    def _load_icon(self):
        ico = ensure_icon()
        if ico:
            h = user32.LoadImageW(None, str(ico), IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                self.hicon = h
        if not self.hicon:
            # MAKEINTRESOURCE：把整数 ID 当指针传
            self.hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _add_icon(self, tip="夜间值守 · 待命"):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = tip[:127]
        self.nid = nid
        return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))

    def _set_tip(self, tip):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_TIP
        nid.szTip = tip[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # --- 菜单 ---
    def _menu(self):
        hmenu = user32.CreatePopupMenu()
        st = api("/api/state", timeout=5)
        running = bool(((st.get("data") or {}).get("monitor") or {}).get("running")) \
            if st.get("ok") else False
        pref = (((st.get("data") or {}).get("power") or {}).get("prefer") or "sleep") \
            if st.get("ok") else "sleep"
        items = [
            (MENU_OPEN, "打开控制台（在网页里确认开启监控）", False),
            (MENU_STATUS, "查看当前状态", False),
            (0, None, False),
            (MENU_STOP, "优雅停止监控（不发出睡眠命令）", not running),
            (MENU_SELFCHECK, "早上自检", False),
            (0, None, False),
            (MENU_EXIT, "退出托盘（不影响正在运行的监控）", False),
        ]
        for mid, label, gray in items:
            if label is None:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(hmenu, MF_STRING | (MF_GRAYED if gray else 0), mid, label)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 必须先置前台，否则点击菜单外部不会关闭（Win32 老坑）
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0x0000, 0, 0)   # WM_NULL，收尾
        user32.DestroyMenu(hmenu)
        if cmd:
            self.action(cmd)

    # --- 动作 ---
    def action(self, cmd):
        if cmd == MENU_OPEN:
            self._open_console()
        elif cmd == MENU_STATUS:
            self._report_status()
        elif cmd == MENU_STOP:
            r = api("/api/monitor/stop", "POST", {})
            self.balloon("优雅停止", r.get("message", "已请求停止"),
                         NIIF_INFO if r.get("ok") else NIIF_WARNING)
        elif cmd == MENU_SELFCHECK:
            threading.Thread(target=self._selfcheck, daemon=True).start()
        elif cmd == MENU_EXIT:
            self.shutdown()

    def _open_console(self):
        try:
            from desktop_launcher import open_window
            open_window()
        except Exception:  # noqa: BLE001
            webbrowser.open(BASE + "/")

    def _report_status(self):
        r = api("/api/state", timeout=5)
        if not r.get("ok"):
            self.balloon("状态读取失败", r.get("message", ""), NIIF_ERROR)
            return
        d = r["data"]
        mon = d.get("monitor") or {}
        pw = d.get("power") or {}
        mode = "睡眠" if (pw.get("prefer") or "sleep") == "sleep" else "休眠"
        if mon.get("running"):
            txt = f"监控运行中（pid {mon.get('pid')}），任务全部终结后请求{mode}"
        else:
            txt = f"待命 · 待机方式：{mode}（{(pw.get('recommend') or {}).get('text', '')}）"
        self.balloon("夜间值守", txt, NIIF_INFO)

    def _selfcheck(self):
        self.balloon("自检", "正在生成报告…", NIIF_INFO)
        r = api("/api/selfcheck", "POST", {}, timeout=180)
        if not r.get("ok"):
            self.balloon("自检失败", (r.get("message") or "")[:200], NIIF_ERROR)
            return
        report = (r.get("data") or {}).get("report", "")
        summary = ""
        for line in report.splitlines():
            if line.startswith("汇总:"):
                summary = line.strip()
                break
        warn = "WARN=" in summary and "WARN=0" not in summary
        fail = "FAIL=" in summary and "FAIL=0" not in summary
        level = NIIF_ERROR if fail else (NIIF_WARNING if warn else NIIF_INFO)
        self.balloon("自检完成", (summary or "报告已生成")[:200], level)

    # --- 状态轮询：值守结束/开始时主动告知 ---
    def _poll_loop(self):
        while not self.quit:
            try:
                r = api("/api/state", timeout=5)
                if r.get("ok"):
                    d = r["data"]
                    mon = d.get("monitor") or {}
                    intent = (d.get("sleep_intent") or {}).get("command_issued_at")
                    running = bool(mon.get("running"))
                    prev = self._last["running"]
                    if prev is None:
                        if running:
                            self._set_tip("夜间值守 · 监控运行中")
                            self.balloon("监控运行中",
                                         "任务全部完成并稳定后会请求睡眠", NIIF_INFO)
                    elif running and not prev:
                        self._set_tip("夜间值守 · 监控运行中")
                        self.balloon("监控已启动", "任务全部完成并稳定后会请求睡眠", NIIF_INFO)
                    elif not running and prev:
                        self._set_tip("夜间值守 · 待命")
                        if intent and intent != self._last["intent"]:
                            self.balloon("值守完成",
                                         f"已按计划发出睡眠命令（{intent}），早上可跑自检",
                                         NIIF_INFO)
                        else:
                            self.balloon("监控已停止", "未发出睡眠命令", NIIF_WARNING)
                    self._last["running"] = running
                    self._last["intent"] = intent
            except Exception:  # noqa: BLE001
                pass
            for _ in range(50):          # 5 秒轮询，但能快速响应退出
                if self.quit:
                    return
                time.sleep(0.1)

    # --- 生命周期 ---
    def shutdown(self):
        self.quit = True
        try:
            if self.nid is not None:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
        except Exception:  # noqa: BLE001
            pass
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)

    def run(self, selftest_seconds=0):
        self._create_window()
        self._load_icon()
        if not self._add_icon():
            raise OSError("托盘图标注册失败（Shell_NotifyIconW）")
        threading.Thread(target=self._poll_loop, name="asg-poll", daemon=True).start()
        if selftest_seconds:
            threading.Timer(selftest_seconds, lambda: user32.PostMessageW(
                self.hwnd, WM_COMMAND, MENU_EXIT, 0)).start()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.shutdown()
        return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if os.name != "nt":
        print("托盘仅支持 Windows")
        return 1
    selftest = "--selftest" in argv
    created = ensure_server()
    print(f"[tray] 本机服务: {'由托盘承载' if created else '复用已有实例'} -> {BASE}")
    tray = Tray()
    if selftest:
        print("[tray] selftest：创建图标 3 秒后自动退出…")
        try:
            rc = tray.run(selftest_seconds=3)
        except Exception as exc:  # noqa: BLE001
            print(f"[tray] selftest 失败: {exc!r}")
            return 2
        print("[tray] selftest 通过：窗口/图标/消息循环/清理均正常")
        return rc
    try:
        tray.run()
    except KeyboardInterrupt:
        tray.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
