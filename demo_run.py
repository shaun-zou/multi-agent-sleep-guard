#!/usr/bin/env python
"""一键演示：自动跑完 扫描 -> 人工修正 -> 核对锁定 -> 监控 -> 自检。

不需要填任何编号，全部自动完成。使用 config.demo.json（秒级间隔），
约 20 秒结束，且是 dry-run 演练模式，绝不会让电脑真的睡眠。
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
DEMO = "config.demo.json"


def banner(step, text):
    print()
    print("=" * 72)
    print(f"  第 {step} 步：{text}")
    print("=" * 72)


def run(args, timeout=180):
    proc = subprocess.run(
        [PY, "agent_sleep_guard.py"] + args,
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print("[提示]", proc.stderr.strip())
    return proc.returncode, proc.stdout


def main():
    os.chdir(ROOT)

    print("=" * 72)
    print("  多 Agent 空闲监测工具 —— 一键演示")
    print("  演练模式(dry-run)：只演示流程，电脑不会真的睡眠，请放心")
    print("=" * 72)

    # 把上一轮的监控日志归档，避免旧的日志干扰"心跳连续性"判断；不删除任何文件
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    old = []
    for pattern in ("monitor-*.log", "sleep-sequence-*.log", "selfcheck-*.log"):
        old.extend(logs.glob(pattern))
    if old:
        archive = logs / f"archive-{time.strftime('%Y%m%dT%H%M%S')}"
        archive.mkdir(exist_ok=True)
        moved, skipped = 0, 0
        for p in old:
            try:
                p.rename(archive / p.name)
                moved += 1
            except OSError:
                # 文件可能被控制台服务/杀软短暂占用，跳过即可，不让演示中断
                skipped += 1
        print(f"\n[准备] 上一轮日志已归档 {moved} 个到 {archive.name}/"
              f"（{skipped} 个被占用，保留原处），没有删除。")

    # 让示例任务的日志变成"刚刚还在活动"，这样能看到监控从"等待"到"全部完成"的过程
    for p in glob.glob(str(ROOT / "data" / "logs" / "*.log")):
        os.utime(p, None)
    print("[准备] 已把 3 个示例 Agent 的任务状态设为「运行中」。")

    banner(1, "手动扫描，列出当前任务清单并生成快照版本")
    rc, out = run(["scan", "--config", DEMO])
    if rc != 0:
        return rc
    m = re.search(r"快照 ID\s*:\s*(\S+)", out)
    if not m:
        print("[错误] 没能读出快照编号，演示中止。")
        return 1
    sid_v1 = m.group(1)

    banner(2, "模拟人工核对：删掉一个不该跟踪的任务，自动生成新版本")
    rc, out = run([
        "scan", "revise", "--config", DEMO, "--snapshot", sid_v1,
        "--remove", "T-2001", "--reason", "演示：人工确认该任务已取消，从清单剔除",
        "--operator", "demo",
    ])
    if rc != 0:
        return rc
    m2 = re.search(r"-> (SNAP-\S+) \(", out)
    if not m2:
        print("[错误] 没能读出新版本编号，演示中止。")
        return 1
    sid = m2.group(1)

    banner(3, "人工确认清单 + 记录重试风险决定，锁定这个具体版本")
    rc, _ = run([
        "scan", "verify", "--config", DEMO, "--snapshot", sid,
        "--retry-reuses-id", "false", "--decision", "watch_retry_chain",
        "--watch-task", "T-1001", "--operator", "demo",
        "--note", "演示：该 Agent 重试会换新任务编号，标记需观察重试链",
    ])
    if rc != 0:
        return rc

    banner(4, "开启监控（约 20 秒）：轮询 → 任务陆续完成 → 连续 3 次稳定复核 → 收尾")
    print("[说明] 这一步会自动等待，请不要关闭窗口。演示不会真的让电脑睡眠。\n")
    rc, _ = run(["monitor", "--config", DEMO, "--snapshot", sid, "--dry-run"], timeout=180)
    if rc != 0:
        return rc

    banner(5, "模拟早上唤醒后的自检报告")
    rc, out = run(["selfcheck", "--config", DEMO])
    if rc != 0:
        return rc

    # 另外存一份排版整齐的报告，方便用记事本直接打开（控制台窗口可能会折行）
    report = [
        "多 Agent 空闲监测工具 —— 一键演示报告",
        f"运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"使用的快照：{sid}",
        "",
        "这次演示依次做了 5 件事：",
        "  1. 手动扫描，列出当前任务清单，生成快照 v1",
        "  2. 模拟人工核对，删掉一个不该跟踪的任务，自动升级为 v2 并留痕",
        "  3. 记录「已核对」标记与重试风险决定，锁定 v2 这个具体版本",
        "  4. 开启监控：轮询 → 任务陆续完成 → 连续 3 次稳定复核 → 按固定顺序收尾",
        "  5. 模拟早上唤醒，输出自检报告",
        "",
        "【重要】本次是演练模式（dry-run），电脑没有、也不会真的睡眠。",
        "【注意】当前用的是「示例数据」，不是你真实 Agent 的任务。",
        "",
        "=" * 70,
        "以下是自检报告全文：",
        "",
        out.strip(),
        "",
        "=" * 70,
        "想看更细的记录，打开 logs/ 文件夹：",
        "  events-*.log            扫描、版本变更、核对决定的完整留痕",
        "  monitor-*.log            每一轮轮询、进度、心跳、终态判定",
        "  sleep-sequence-*.log     收尾 5 个步骤的先后顺序",
        "",
    ]
    report_path = logs / "演示报告.txt"
    report_path.write_text("\n".join(report), encoding="utf-8-sig")

    print()
    print("=" * 72)
    print("  演示结束")
    print("  - 上面这份自检报告就是「早上起来看的东西」，全 PASS 表示流程正常。")
    print("  - 本次是演练模式，电脑没有、也不会真的睡眠。")
    print(f"  - 已另存一份排版整齐的报告：{report_path}")
    print("  - 所有记录在 logs/ 文件夹里；快照在 snapshots/ 文件夹里。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
