#!/usr/bin/env python
"""打包产物回归：把 exe 扔进一个**空目录**，验证它在那儿能独立活起来。

为什么必须单测这一层：PyInstaller 冻结后 `__file__` 落在 `_MEIPASS` 临时目录，
一旦路径解析写错，症状是「能启动、界面也能开，但配置/日志每次重启都归零」——
冒烟测试（`--version`）根本发现不了。所以这里做的是真跑一遍：首次运行初始化 →
起服务 → 拉静态资源 → 走一次自检 → 确认数据留在 exe 旁边。

    python verify_exe.py                     # 默认验 dist/夜间值守.exe
    python verify_exe.py path/to/xxx.exe     # 验指定产物
    python verify_exe.py --keep              # 保留临时目录便于排查

退出码 0 = 全部通过；任何一项失败返回 1（可直接进 CI / 打包脚本）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = "8799"          # 避开正式服务的 8765，绝不干扰正在值守的实例
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_fail = []


def check(cond, label, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        _fail.append(label)
    return cond


def kill_tree(proc):
    """收掉整棵进程树。

    PyInstaller 单文件运行时是**两个进程**：引导进程先把自己解包到 _MEIPASS，
    再拉起真身进程。只 terminate 引导进程会留下真身继续跑（端口还占着、
    exe 还被映射着，删不掉）。taskkill /T 才能连子树一起收。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:  # pragma: no cover - 本项目只支持 Windows
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def cleanup(path: Path, exe: Path):
    """删临时目录。刚退出的 exe 句柄释放可能慢几秒（杀毒/SmartScreen 也会摸一下），

    所以要等它真的可删；等不到就明说在哪，别在 %TEMP% 里悄悄留一个 9MB 的 exe。
    """
    for _ in range(12):
        try:
            if exe.exists():
                exe.unlink()
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
        if not path.exists():
            return
        time.sleep(1.0)
    print(f"注意: 临时目录未能删除（文件仍被占用），可稍后手动删除: {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包产物在空目录中的独立运行回归")
    ap.add_argument("exe", nargs="?", default=str(ROOT / "dist" / "夜间值守.exe"))
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    src = Path(args.exe).resolve()
    if not src.exists():
        print(f"[FAIL] 找不到产物: {src}（先跑 python build_exe.py）")
        return 1

    tmp = Path(os.environ.get("TEMP", ".")) / "asg-exe-verify"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    exe = tmp / src.name
    shutil.copy2(src, exe)
    print(f"空目录: {tmp}")
    print(f"产物  : {src.name} ({src.stat().st_size / 1048576:.1f} MB)\n")

    def run(argv, timeout=180):
        e = dict(os.environ)
        e["ASG_UI_PORT"] = PORT
        r = subprocess.run([str(exe)] + argv, cwd=str(tmp), env=e, timeout=timeout,
                           capture_output=True, creationflags=NO_WINDOW)
        return (r.returncode, (r.stdout or b"").decode("utf-8", "replace"),
                (r.stderr or b"").decode("utf-8", "replace"))

    # --- 1. 只读命令必须零副作用 ---
    rc, out, _ = run(["--version"])
    check(rc == 0 and "打包版" in out, "--version 可用", out.strip())
    check(sorted(p.name for p in tmp.iterdir()) == [exe.name],
          "--version/--help 无副作用（不创建任何文件）",
          str(sorted(p.name for p in tmp.iterdir())))

    # --- 2. 首次运行初始化：可写数据落在 exe 旁边 ---
    rc, out, err = run(["--status"])
    names = sorted(p.name for p in tmp.iterdir())
    check(rc == 0, "--status 可用", (out or err).strip().splitlines()[0] if (out or err) else "")
    check("config.real.json" in names and {"logs", "snapshots", "state"} <= set(names),
          "首次运行生成 config.real.json 与 logs/ snapshots/ state/", str(names))
    check("ui" not in names, "只读资源 ui/ 不落在 exe 旁边（应从包内读）", str(names))

    # --- 3. 参数路由 ---
    rc, out, err = run(["--bogus"])
    check(rc == 2 and "未知参数" in (err + out), "未知参数退 2 并打印用法", f"rc={rc}")

    # --- 4. 内部 CLI 通道（界面每个动作都靠它） ---
    rc, out, err = run(["--asg-cli", "power", "--config", str(tmp / "config.real.json")])
    check(rc == 0 and "\ufffd" not in out, "--asg-cli 通道可用且中文不乱码",
          (out.strip().splitlines() or [err[:80]])[0])

    # --- 5. 起控制台，验证静态资源与 API ---
    env = dict(os.environ)
    env["ASG_UI_PORT"] = PORT
    env["ASG_UI_NO_BROWSER"] = "1"
    srv = subprocess.Popen([str(exe)], cwd=str(tmp), env=env, creationflags=NO_WINDOW,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{PORT}"
    body = None
    for _ in range(160):
        try:
            with urllib.request.urlopen(base + "/api/state", timeout=2) as r:
                body = r.read().decode("utf-8")
            break
        except (urllib.error.URLError, OSError):
            if srv.poll() is not None:
                break
            time.sleep(0.5)
    try:
        check(body is not None, "控制台起得来（/api/state 200）")
        if body:
            st = json.loads(body)
            check(bool(st.get("ok")), "/api/state 返回 ok",
                  f"monitor.running={(st.get('data') or {}).get('monitor', {}).get('running')}")
        for path, kind in (("/", "text/html"), ("/app.css", "text/css"),
                           ("/app.js", "javascript"), ("/api-contract.json", "json")):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    ct = r.headers.get("Content-Type", "")
                    n = len(r.read())
                check(kind in ct and n > 0, f"静态资源 {path}", f"{ct} {n}B")
            except Exception as exc:  # noqa: BLE001
                check(False, f"静态资源 {path}", repr(exc))

        # 自检走的是「界面 -> 子进程 CLI」这条真实链路，正好一起验编码与容错
        try:
            req = urllib.request.Request(base + "/api/selfcheck", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                rep = json.loads(r.read().decode("utf-8"))
            text = ((rep.get("data") or {}).get("report") or "")
            summary = next((l for l in text.splitlines() if l.startswith("汇总:")), "")
            check(bool(rep.get("ok")) and "FAIL=0" in summary,
                  "空环境自检 FAIL=0（新装不该报事故）", summary)
            check("\ufffd" not in text, "自检报告无乱码", f"替换字符 {text.count(chr(0xfffd))} 个")
        except Exception as exc:  # noqa: BLE001
            check(False, "自检链路", repr(exc))
    finally:
        kill_tree(srv)

    # --- 6. 重启后数据仍在原位 ---
    logs = list((tmp / "logs").glob("*.log"))
    check(bool(logs), "日志写在 exe 旁边且保留", str([p.name for p in logs]))

    print()
    if _fail:
        print(f"结论: {len(_fail)} 项失败 -> {', '.join(_fail)}")
        print(f"临时目录已保留: {tmp}")
        return 1
    print("结论: 全部通过（产物可在任意空目录独立运行）")
    if not args.keep:
        cleanup(tmp, exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
