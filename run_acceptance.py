#!/usr/bin/env python
"""真机非演练闭环验收（唯一会让电脑真睡的入口）。

必须由人主动触发：运行时会要求输入大写 SLEEP 才继续。
--rehearsal 为演练模式：强制 dry_run=true，走完整链路但不睡，用于验证脚本本身。

流程：
  前置检查 -> 受控快照(任务立即全部完成) -> 核对锁定 -> 开启非演练监控
  -> STEP1..STEP5 派发一条 sleep 命令 -> 系统进入低功耗
  -> (唤醒后继续) 等待 20s -> selfcheck -> 写验收报告

安全边界：
  - 不修改任何系统电源设置（不跑 powercfg /hibernate on|off、不建唤醒任务）；
  - 正式配置 config.json / config.real.json 始终是 dry_run=true，本脚本只用独立配置；
  - 验收产物全部落在 acceptance/ 目录，不污染正式快照与日志；
  - 真睡前必须由人输入大写 SLEEP，脚本自身就是一次确认，不存在持久总闸。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from asg import powercap, util  # noqa: E402
from asg.config import Config  # noqa: E402

PY = sys.executable
ENTRY = str(ROOT / "agent_sleep_guard.py")
CFG = ROOT / "config.acceptance.json"
ACC = ROOT / "acceptance"
CFG_REHEARSAL = ACC / "config.rehearsal.json"
REPORT = ACC / "验收报告.txt"


def cli(args, timeout=180):
    p = subprocess.run([PY, ENTRY] + args, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def build_config(rehearsal):
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    path = CFG
    if rehearsal:
        cfg["sleep"]["dry_run"] = True
        cfg["_comment"] = "演练模式：强制 dry_run=true，不真睡"
        path = CFG_REHEARSAL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg, str(path)


def preflight():
    print("=== 前置检查 ===")
    caps = powercap.probe()
    print(f"  电源能力: 待机可用={caps.get('sleep_available')} "
          f"休眠可用={caps.get('hibernate_available')}")
    print(f"  请求模式: {Config(str(CFG))['sleep'].get('prefer','sleep')}（只发一条、不补发）")
    lock = util.read_json(util.abs_path("state") / "monitor.lock") or {}
    pid = lock.get("pid")
    alive = bool(pid and util.pid_alive(pid, lock.get("started_at")))
    print(f"  正式监控进程: {'运行中 pid='+str(pid) if alive else '未运行'}")
    if alive:
        print("  [中止] 有正式监控在跑，请先停止再验收。")
        return False
    print("  真睡确认方式: 输入大写 SLEEP（脚本内一次确认，无持久总闸）")
    return True


def main():
    rehearsal = "--rehearsal" in sys.argv
    cfg, cfg_path = build_config(rehearsal)
    for key in ("snapshots_dir", "logs_dir", "state_dir", "data_dir"):
        util.ensure_dir(util.abs_path(cfg["paths"][key]))
    if not preflight():
        return 1

    if not rehearsal:
        print()
        print("=" * 70)
        print("  警告：这是唯一会让电脑真正进入低功耗的入口")
        print("  请确认：已保存所有工作、已关闭正在编辑的文件、电源/电池正常")
        print("  系统睡下后，请按电源键唤醒；唤醒后本脚本会自动生成自检报告")
        print("  输入大写 SLEEP 继续，输入其他内容或 Ctrl+C 取消")
        print("=" * 70)
        try:
            answer = input("确认词 > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消，未发出睡眠命令。")
            return 1
        if answer != "SLEEP":
            print("已取消，未发出睡眠命令。")
            return 1
    else:
        print("\n[演练模式] 强制 dry_run=true，不会真睡；仅验证脚本链路。")

    # 归档历史监控日志：心跳连续性检查会汇总所有 monitor-*.log，
    # 不清理会把几小时前的演练心跳算进来，产生无关 WARN。
    old_logs = sorted((ACC / "logs").glob("monitor-*.log"))
    if old_logs:
        arc = ACC / f"archive-{time.strftime('%Y%m%dT%H%M%S')}"
        arc.mkdir(parents=True, exist_ok=True)
        for p in old_logs:
            p.rename(arc / p.name)
        print(f"已归档 {len(old_logs)} 个历史监控日志 -> {arc.name}")

    # 受控数据源：两条任务开局即终态，避免真实 Agent 状态干扰
    (ACC / "state.json").write_text(json.dumps({"tasks": [
        {"task_id": "ACC-1", "agent": "acceptance-a",
         "start_time": util.iso(), "status": "completed"},
        {"task_id": "ACC-2", "agent": "acceptance-b",
         "start_time": util.iso(), "status": "completed"},
    ]}, ensure_ascii=False), encoding="utf-8")

    rc, out, err = cli(["scan", "--config", cfg_path])
    if rc != 0:
        print("[ERROR] 扫描失败:", err or out)
        return 1
    sid = re.search(r"快照 ID\s*:\s*(\S+)", out).group(1)
    print(f"\n扫描: {sid}")

    rc, out, err = cli(["scan", "verify", "--config", cfg_path, "--snapshot", sid,
                        "--retry-reuses-id", "true", "--decision", "no_action",
                        "--operator", "acceptance"])
    if rc != 0:
        print("[ERROR] 核对锁定失败:", err or out)
        return 1
    print("核对锁定: 完成")

    print("开启监控（即将走到 STEP5）…")
    args = ["monitor", "--config", cfg_path, "--snapshot", sid]
    if rehearsal:
        args.append("--dry-run")
    rc, out, err = cli(args, timeout=300)
    monitor_out = out + ("\n" + err if err else "")
    print(f"monitor exit={rc}")
    if rc != 0:
        print(monitor_out[-800:])

    # 给系统时间真正进入低功耗；若真睡了，这段会被挂起，唤醒后继续
    print("等待 20 秒后自检（真睡时此等待会随系统挂起，唤醒后继续）…")
    time.sleep(20)

    rc2, out2, err2 = cli(["selfcheck", "--config", cfg_path])
    report = out2 if rc2 == 0 else (err2 or out2)
    # 命令进程可能刚随系统一起被唤醒，稍等再读它自写的诊断日志
    time.sleep(3)

    # 收尾：复核本次是否真的发出了非演练命令（不再有总闸需要关回）
    confirm_note = ("演练模式：未真睡" if rehearsal else
                    "真睡授权：输入大写 SLEEP（脚本内一次确认）")

    intent = util.read_json(ACC / "state" / "sleep_intent.json") or {}
    seq = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                    for p in (ACC / "logs").glob("sleep-sequence-*.log"))
    diag_files = sorted((ACC / "logs").glob("sleep-command-*.log"))
    diag_expect = ACC / "logs" / f"sleep-command-{util.today_str()}.log"
    diag_text = ("\n".join(util.read_lines(diag_files[-1])[-12:])
                 if diag_files else
                 "（无诊断日志：演练模式不派发命令；若真实模式也没有，说明命令进程在写日志前被回收）")
    diag_text = f"预期路径: {diag_expect}\n若此刻为空，命令进程可能稍晚才写，请稍后重看该文件\n" + diag_text
    lines = [
        "=" * 72,
        "  真机非演练闭环验收报告" if not rehearsal else "  验收脚本演练报告（未真睡）",
        f"  生成时间: {util.iso()}",
        "=" * 72,
        f"  快照: {sid}",
        f"  dry_run: {intent.get('dry_run')} | 请求模式: {intent.get('requested_mode')}",
        f"  fallback_enabled: {intent.get('fallback_enabled')} | runtime_fallback: {intent.get('runtime_fallback')}",
        f"  一次确认: {confirm_note}",
        f"  本次非演练 STEP5 记录数: {seq.count('dry_run=False')}",
        "",
        "  --- monitor 输出 ---",
        monitor_out.strip()[-2500:],
        "",
        "  --- selfcheck 报告 ---",
        report.strip(),
        "",
        "  --- 命令进程诊断日志（由命令自己写，工具不等待/不据此判断） ---",
        diag_text.strip(),
        "=" * 72,
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:12]))
    print(f"\n完整报告: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
