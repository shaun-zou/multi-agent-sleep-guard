#!/usr/bin/env python
"""打包分发入口：一个 exe 顶替原来 5 个 .bat。

只做「参数分发 + 首次运行自检」，不含任何业务逻辑——业务仍在 asg/ 与各入口
文件里，因此源码运行与 exe 运行是**同一份代码**，不存在两条维护线。

  AgentSleepGuard.exe                  打开控制台（承载本机服务 + 打开网页）
  AgentSleepGuard.exe --tray           托盘常驻（隐藏控制台窗口）
  AgentSleepGuard.exe --desktop        独立窗口（Edge/Chrome --app，无地址栏）
  AgentSleepGuard.exe --install [--autostart]
  AgentSleepGuard.exe --uninstall
  AgentSleepGuard.exe --status
  AgentSleepGuard.exe --asg-cli <cmd>  内部接口：界面/托盘据此复用 CLI 子进程
  AgentSleepGuard.exe --version

为什么 --asg-cli 是内部接口：界面上的每个动作（扫描、自检、规则确认、启动监控）
都通过**子进程**调 CLI，以保证「界面操作用与命令行操作留痕完全一致」。打包后
没有 agent_sleep_guard.py 这个文件，所以用 exe 自己当解释器，见 asg/util.cli_argv。

首次运行会在 exe 旁边生成 config.real.json 与 logs/ snapshots/ state/ —— 数据永远
留在 exe 所在目录，不进 %TEMP%，也不写注册表。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _force_utf8_stdio():
    """打包后必须把 stdout/stderr 钉成 UTF-8。

    原因：源码运行时本机 Python 处于 UTF-8 模式，而冻结后的 exe 在**接管道**时
    用的是控制台代码页（简体中文机器 = GBK/cp936）。界面读子进程输出用的是
    `encoding="utf-8"`，两边不一致就会把"自检报告/规则确认结果"读成乱码。
    接真实控制台时 Python 本来走 WriteConsoleW，reconfigure 是空操作，不影响
    用户直接在 cmd 里看中文。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_force_utf8_stdio()

_HERE = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(_HERE))

from asg import util  # noqa: E402

USAGE = """夜间值守 · 多 Agent 空闲监测

  AgentSleepGuard.exe                打开控制台（推荐：双击即可）
  AgentSleepGuard.exe --tray         托盘常驻（开机自启项用的就是这个）
  AgentSleepGuard.exe --desktop      独立窗口模式
  AgentSleepGuard.exe --install      安装桌面/开始菜单快捷方式
  AgentSleepGuard.exe --install --autostart   同时设置开机自启
  AgentSleepGuard.exe --uninstall    移除快捷方式（不动任何数据）
  AgentSleepGuard.exe --status       查看安装状态
  AgentSleepGuard.exe --version      版本号

真睡授权只在网页控制台里确认一次；命令行 --asg-cli 为内部接口。
"""


def _hide_console():
    """托盘模式必须无黑窗。控制台子系统 exe 无法彻底不建控制台，

    所以在入口第一件事就把它隐藏（窗口在创建后 Hide 掉，用户只看到图标）。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:  # noqa: BLE001
        pass


def ensure_runtime() -> Path:
    """首次运行铺好可写目录与默认配置（幂等，已存在则一个字都不改）。"""
    for d in ("logs", "snapshots", "state"):
        (util.ROOT / d).mkdir(parents=True, exist_ok=True)
    cfg_path = util.ROOT / "config.real.json"
    if not cfg_path.exists():
        from asg.config import DEFAULT_CONFIG
        util.atomic_write_json(cfg_path, DEFAULT_CONFIG)
        print(f"[首次运行] 已生成默认配置: {cfg_path}")
        print("           若要接入自己的 Agent，请编辑该文件的 adapters 段；")
        print("           也可先跑 `AgentSleepGuard.exe --asg-cli discover --write`。")
    return cfg_path


def dispatch(argv) -> int:
    cmd = argv[0] if argv else ""
    rest = argv[1:]

    if cmd in ("--help", "-h", "/?"):
        print(USAGE)
        return 0
    if cmd in ("--version", "-V"):
        from asg import __version__
        print(f"agent-sleep-guard {__version__}"
              f"{'（打包版）' if util.IS_FROZEN else '（源码版）'}")
        return 0

    # 内部接口：界面/托盘/自检复用 CLI 子进程。不要隐藏控制台——
    # 父进程靠管道读它的 stdout，隐藏窗口反而会丢掉输出。
    if cmd == "--asg-cli":
        from asg.cli import main as cli_main
        return cli_main(rest)

    if cmd == "--tray":
        _hide_console()
        ensure_runtime()
        import tray
        return tray.main(rest)

    if cmd == "--desktop":
        ensure_runtime()
        import desktop_launcher
        return desktop_launcher.main()

    if cmd in ("--install", "--uninstall", "--status"):
        ensure_runtime()
        import install
        alias = {"--install": "install", "--uninstall": "uninstall",
                 "--status": "status"}[cmd]
        return install.main([alias] + rest)

    if cmd.startswith("-"):
        print(f"[ERROR] 未知参数: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    # 默认：控制台（承载 127.0.0.1 服务 + 打开网页）
    ensure_runtime()
    import ui_server
    return ui_server.main()


def main() -> int:
    try:
        return dispatch(sys.argv[1:])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
