#!/usr/bin/env python
"""把项目打包成**单文件 exe**（PyInstaller）。

这是整个项目里唯一用到第三方库的地方，而且只在**构建期**用：
运行时依旧是纯标准库，exe 里不含 PyInstaller 本身。

    python build_exe.py                # 构建 -> dist/夜间值守.exe
    python build_exe.py --keep-build   # 保留 build/ 中间产物便于排查

产物结构（exe 旁边才会出现可写数据，全部首启自动创建）：
    dist/夜间值守.exe        单文件，双击 = 打开控制台
    dist/config.real.json    首次运行自动生成（默认值，需按需改 adapters）
    dist/logs/ snapshots/ state/   首次运行自动创建

**不被打进 exe 的东西**：config*.json / logs / snapshots / state / rules。
它们是可写数据，必须留在 exe 旁边，否则每次启动都从临时目录重新开始、
用户填的配置也会在退出时被删掉。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

NAME = "AgentSleepGuard"          # 构建期内部名（ASCII，避免工具链编码问题）
DELIVER = "夜间值守"               # 交付名（中文，用户看到的就是这个）
ENTRY = "pack_entry.py"

# 打包进 exe 的只读资源：源目录 -> exe 内目录
BUNDLED = [("ui", "ui"), ("cases", "cases")]
# adapters 是 importlib + pkgutil 动态加载的，静态分析看不到，必须显式收集
COLLECT = ["asg", "asg.adapters"]

SMOKE = [
    (["--version"], "agent-sleep-guard"),
    (["--help"], "托盘常驻"),
    # 关键一条：验证「界面靠子进程调 CLI」这条内部通道在打包后依然通，
    # 且中文输出没被编码搞坏。
    (["--asg-cli", "cases", "--json"], "验收用例集"),
]


def _sep() -> str:
    return ";" if os.name == "nt" else ":"


def build_args(dist_dir: Path, work_dir: Path) -> list[str]:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--console",                     # 保留控制台：--asg-cli 要靠管道回传输出
        "--name", NAME,
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(work_dir),
        "--log-level", "WARN",
    ]
    icon = ROOT / "state" / "tray-icon.ico"
    if icon.exists():
        args += ["--icon", str(icon)]
    for src, dst in BUNDLED:
        p = ROOT / src
        if p.exists():
            args += ["--add-data", f"{p}{_sep()}{dst}"]
    for mod in COLLECT:
        args += ["--collect-submodules", mod]
    args.append(str(ROOT / ENTRY))
    return args


def _text(raw: bytes) -> str:
    """exe 的 stdout 正常是 UTF-8（pack_entry 已钉死），但仍按 GBK 兜底解码，

    免得因为编码问题把「跑通了」误判成「跑挂了」。
    """
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def smoke_test(exe: Path) -> bool:
    """产物必须能起来：版本号打得出、帮助页打得开（等于验证分发器与资源完好）。"""
    ok = True
    for argv, expect in SMOKE:
        try:
            r = subprocess.run([str(exe)] + argv, capture_output=True,
                               timeout=120, **({"creationflags": 0x08000000}
                                               if os.name == "nt" else {}))
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {exe.name} {' '.join(argv)} 起不来: {exc!r}")
            ok = False
            continue
        out = _text(r.stdout or b"")
        err = _text(r.stderr or b"")
        if r.returncode != 0 or expect not in out:
            print(f"[FAIL] {' '.join(argv)} rc={r.returncode}")
            print((err or out).strip()[:600])
            ok = False
        else:
            print(f"[OK]   {' '.join(argv):<12} -> {out.strip().splitlines()[0]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="打包夜间值守为单文件 exe")
    ap.add_argument("--keep-build", action="store_true", help="保留 build/ 中间产物")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[ERROR] 未安装 PyInstaller。构建期需要（运行期不需要）：")
        print("        python -m pip install pyinstaller")
        return 1

    # 图标是托盘运行时生成的；构建前先生成一次，好让 exe 也带同一个图标
    from tray import ensure_icon
    ensure_icon()

    dist_dir = ROOT / "dist"
    work_dir = ROOT / "build"
    if dist_dir.exists():
        shutil.rmtree(dist_dir, ignore_errors=True)

    cmd = build_args(dist_dir, work_dir)
    print("[1/4] 构建中…（首次约 1-2 分钟）")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("[FAIL] PyInstaller 构建失败，见上方输出")
        return r.returncode

    built = dist_dir / f"{NAME}.exe"
    if not built.exists():
        cands = list(dist_dir.glob("*.exe"))
        if not cands:
            print("[FAIL] 没有找到构建产物")
            return 1
        built = cands[0]
    final = dist_dir / f"{DELIVER}.exe"
    if final != built:
        if final.exists():
            final.unlink()
        built.replace(final)

    print(f"[2/4] 产物: {final}  ({final.stat().st_size / 1048576:.1f} MB)")

    print("[3/4] 冒烟测试…")
    if not smoke_test(final):
        return 1

    # 冒烟只证明"能起来"；这里把 exe 扔进空目录真跑一遍，专抓 _MEIPASS 路径类故障
    print("[4/4] 空目录独立运行回归（verify_exe.py）…")
    rc = subprocess.run([sys.executable, str(ROOT / "verify_exe.py"), str(final)],
                        cwd=str(ROOT)).returncode
    if rc != 0:
        print("[FAIL] 产物在空目录里跑不通，见上方逐项结果（--keep 可保留现场）")
        return rc

    if not args.keep_build:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n完成。把 dist/ 里的 exe 单独复制到任意目录即可运行：")
    print(f"  {final.name}                 打开控制台（双击）")
    print(f"  {final.name} --tray          托盘常驻")
    print(f"  {final.name} --install       创建桌面/开始菜单快捷方式")
    print("首次运行会在 exe 旁边生成 config.real.json 与 logs/ snapshots/ state/。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
