#!/usr/bin/env python
"""一键安装 / 卸载（P2）：把托盘入口装进桌面、开始菜单，可选开机自启。

零第三方依赖：快捷方式用 PowerShell 的 WScript.Shell COM 创建（Windows 自带），
不引入安装器框架。同一份代码既服务「源码运行」也服务「打包后的 exe」：
  - 源码运行  -> 快捷方式指向 pythonw.exe + tray.py（无黑窗常驻）
  - 打包后exe -> 快捷方式指向 exe 本身 + `--tray` 参数

安装内容：
  - 桌面快捷方式「夜间值守」      -> 常驻托盘
  - 开始菜单快捷方式「夜间值守」  -> 同上
  - 开机自启（--autostart）       -> Startup 文件夹快捷方式（删掉即取消，无注册表残留）
  - state/tray-icon.ico           -> 若不存在则生成

卸载内容：只删上述快捷方式与自启项；**程序目录、日志、快照、配置一律不动**。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包后 __file__ 在 PyInstaller 的 _MEIPASS 里，不能当安装目录用
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from asg import util  # noqa: E402  （只为 spawn_kwargs：PowerShell 子进程不弹黑框）

IS_FROZEN = bool(getattr(sys, "frozen", False))
TRAY = ROOT / "tray.py"
ICON = ROOT / "state" / "tray-icon.ico"
STARTUP = Path.home() / "AppData" / "Roaming" / "Microsoft" / \
    "Windows" / "Start Menu" / "Programs" / "Startup"
NAME = "夜间值守"


def pythonw():
    """优先用 pythonw（无黑窗常驻）；找不到再退回当前解释器。"""
    cand = Path(sys.executable).with_name("pythonw.exe")
    return cand if cand.exists() else Path(sys.executable)


def tray_command():
    """返回 (目标可执行文件, 参数字符串)，供快捷方式使用。"""
    if IS_FROZEN:
        return Path(sys.executable), "--tray"
    return pythonw(), f'"{TRAY}"'


def _run_ps(script: str, timeout=30):
    """跑一段 PowerShell。

    注意编码：PowerShell 输出跟随控制台代码页（GBK），而本解释器默认 UTF-8，
    text=True 的读取线程会直接 UnicodeDecodeError 崩掉 —— 必须拿原始字节自己解码。
    """
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=timeout, **util.spawn_kwargs())

    def dec(b):
        if not b:
            return ""
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

    return r.returncode, dec(r.stdout), dec(r.stderr)


def make_shortcut(lnk: Path, description: str):
    """用 PowerShell 创建快捷方式（WScript.Shell COM，Windows 自带）。"""
    # 父目录不存在时 COM 会静默失败（如 开始菜单\夜间值守 子目录），必须先建
    lnk.parent.mkdir(parents=True, exist_ok=True)
    exe, args = tray_command()
    exe = str(exe)
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$sc = $s.CreateShortcut('{lnk}'); "
        f"$sc.TargetPath = '{exe}'; "
        f"$sc.Arguments = '{args}'; "
        f"$sc.WorkingDirectory = '{ROOT}'; "
        f"$sc.IconLocation = '{ICON if ICON.exists() else exe}'; "
        f"$sc.Description = '{description}'; "
        "$sc.Save()"
    )
    rc, out, err = _run_ps(ps)
    if rc != 0:
        raise RuntimeError(f"创建快捷方式失败 {lnk.name}: {err.strip()[:200]}")
    if not lnk.exists():
        raise RuntimeError(f"快捷方式未生成: {lnk}")


def shortcuts_to_create():
    """桌面路径用 COM 解析，兼容 OneDrive 重定向。"""
    rc, out, _ = _run_ps("[Environment]::GetFolderPath('Desktop')")
    desktop = Path(out.strip() or (Path.home() / "Desktop"))
    if rc != 0 or not desktop.exists():
        desktop = Path.home() / "Desktop"
    start_menu = Path.home() / "AppData" / "Roaming" / "Microsoft" / \
        "Windows" / "Start Menu" / "Programs"
    return [
        (desktop / f"{NAME}.lnk", "夜间值守控制台（托盘常驻）"),
        (start_menu / NAME / f"{NAME}.lnk", "夜间值守控制台（托盘常驻）"),
        (STARTUP / f"{NAME}.lnk", "夜间值守托盘 · 开机自启"),
    ]


def read_shortcut_targets(paths):
    """读出快捷方式实际指向的 (TargetPath, Arguments)。

    为什么需要：只看文件在不在就报「已安装」，会在两种常见情况下骗人 ——
    ① 从源码装过一次、后来换成 exe；② 整个文件夹搬到别的盘。
    文件都还在，但双击跑的是旧路径/旧解释器。状态里必须把它标出来。
    """
    exist = [Path(p) for p in paths if Path(p).exists()]
    if not exist:
        return {}
    joined = ",".join("'" + str(p) + "'" for p in exist)
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"foreach ($p in @({joined})) {{ $sc = $s.CreateShortcut($p); "
        "Write-Output ($p + '|' + $sc.TargetPath + '|' + $sc.Arguments) }"
    )
    rc, out, _ = _run_ps(ps)
    res = {}
    if rc != 0:
        return res
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            res[parts[0].strip()] = (parts[1].strip(), parts[2].strip())
    return res


def cmd_status(args):
    targets = shortcuts_to_create()
    want_exe, want_args = tray_command()
    actual = read_shortcut_targets([lnk for lnk, _ in targets])
    print(f"运行形态 : {'打包 exe' if IS_FROZEN else '源码'}")
    print(f"托盘入口 : {want_exe} {want_args}")
    if not IS_FROZEN:
        print(f"tray.py  : {TRAY} ({'存在' if TRAY.exists() else '缺失!'})")
    print(f"图标     : {ICON} ({'存在' if ICON.exists() else '首次启动托盘时生成'})")
    for lnk, desc in targets:
        kind = "开机自启" if lnk.parent == STARTUP else "快捷方式"
        if not lnk.exists():
            state = "未安装"
        elif str(lnk) not in actual:
            state = "已安装（无法读取指向，建议重装一次核对）"
        else:
            tgt, targs = actual[str(lnk)]
            same_exe = Path(tgt).resolve() == Path(want_exe).resolve()
            same_args = targs.strip().strip('"') == want_args.strip().strip('"')
            state = ("已安装（指向当前入口）" if same_exe and same_args
                     else f"已安装，但指向别处：{tgt} {targs} → 请重装")
        print(f"{kind:<6} {desc:<28} {state}")
    return 0


def cmd_install(args):
    ensure_icon()
    targets = shortcuts_to_create()
    created = []
    for lnk, desc in targets:
        is_autostart = lnk.parent == STARTUP
        if is_autostart and not args.autostart:
            continue
        make_shortcut(lnk, desc)
        created.append(lnk)
        print(f"[已创建] {lnk}")
    if args.autostart:
        print("[说明] 开机自启 = Startup 文件夹里的快捷方式，删掉该文件即取消，"
              "不写注册表。")
    print(f"[完成] 共 {len(created)} 项。双击桌面「{NAME}」即可常驻托盘；"
          "真睡授权仍在网页控制台里确认一次。")
    return 0


def cmd_uninstall(args):
    removed = 0
    for lnk, _ in shortcuts_to_create():
        if lnk.exists():
            lnk.unlink()
            print(f"[已删除] {lnk}")
            removed += 1
            parent = lnk.parent
            if parent.name == NAME and not any(parent.iterdir()):
                parent.rmdir()
                print(f"[已删除] 空目录 {parent}")
    if not removed:
        print("没有找到需要卸载的快捷方式。")
    print("[完成] 程序目录、日志、快照、配置均未改动；想彻底移除请手动删除项目文件夹。")
    return 0


def ensure_icon():
    from tray import ensure_icon
    ensure_icon()


def main(argv=None):
    p = argparse.ArgumentParser(description="夜间值守 · 一键安装/卸载托盘入口")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看安装状态")
    pi = sub.add_parser("install", help="创建桌面/开始菜单快捷方式")
    pi.add_argument("--autostart", action="store_true",
                    help="同时创建开机自启（Startup 文件夹快捷方式）")
    sub.add_parser("uninstall", help="移除快捷方式与自启项（不动数据）")
    args = p.parse_args(argv)
    if not IS_FROZEN and not TRAY.exists():
        print(f"[ERROR] 缺少 {TRAY}")
        return 1
    return {"status": cmd_status, "install": cmd_install,
            "uninstall": cmd_uninstall}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
