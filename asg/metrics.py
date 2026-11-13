"""产品指标口径与周报（从已有日志离线聚合，不改造运行链路、不新增埋点 SDK）。

原则：只统计 `logs/`（正式值守）；`acceptance/` 是脚本自测，单独计数但不计入产品指标。

"误睡率"无法仅凭日志自动判定（需要"用户回来输入 + 命令已发出"的组合证据），
所以只给出可证实的「差点误睡被拦住」次数，并明确标注不可自动计算的项。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import util

METRIC_DEFS = {
    "sessions_started": {
        "name": "值守启动次数", "unit": "次", "definition": "日志窗口内 MONITOR_START 的条数",
        "source": "logs/monitor-*.log", "better": "higher",
    },
    "real_sessions": {
        "name": "真实值守次数", "unit": "次", "definition": "MONITOR_START 中 dry_run=False 的条数",
        "source": "logs/monitor-*.log", "better": "higher",
    },
    "step5_real": {
        "name": "非演练睡眠触发次数", "unit": "次", "definition": "sleep-sequence 中 STEP5 且 dry_run=False 的条数",
        "source": "logs/sleep-sequence-*.log", "better": "higher",
    },
    "sleep_confirmed": {
        "name": "确认挂起次数", "unit": "次", "definition": "自检报告里「系统确实挂起过」的条数",
        "source": "logs/selfcheck-*.log", "better": "higher",
    },
    "sleep_success_rate": {
        "name": "值守成功率", "unit": "%", "definition": "确认挂起次数 / 非演练睡眠触发次数",
        "source": "派生指标", "better": "higher",
    },
    "silent_failures": {
        "name": "静默失败次数", "unit": "次", "definition": "自检里「命令已发出但系统未睡」FAIL 条数",
        "source": "logs/selfcheck-*.log", "better": "lower",
    },
    "command_ret_false": {
        "name": "命令被系统拒绝次数", "unit": "次", "definition": "命令诊断日志里 setSuspendStateReturn=False 的条数",
        "source": "logs/sleep-command-*.log", "better": "lower",
    },
    "jitter_blocks": {
        "name": "差点误睡被拦住次数", "unit": "次", "definition": "JITTER_REGRESSION / STABILITY_RESET 条数（终态回退、用户回来）",
        "source": "logs/monitor-*.log", "better": "context",
    },
    "mean_wait_minutes": {
        "name": "平均等待时长", "unit": "分钟", "definition": "MONITOR_START → PRE_SLEEP 的时间差均值",
        "source": "logs/monitor-*.log", "better": "context",
    },
    "heartbeat_gap_max_minutes": {
        "name": "心跳最大断档", "unit": "分钟", "definition": "自检「监控存活心跳连续性」报出的最大间隔",
        "source": "logs/selfcheck-*.log", "better": "lower",
    },
    "avg_tasks_per_snapshot": {
        "name": "平均快照任务数", "unit": "条", "definition": "窗口内快照 task_count 均值",
        "source": "snapshots/*.json", "better": "context",
    },
    "rule_confirmations": {
        "name": "规则人工确认次数", "unit": "次", "definition": "rules/agent-rules.json 的 history 条数",
        "source": "rules/agent-rules.json", "better": "context",
    },
}

NOT_AVAILABLE = {
    "mis_sleep_rate": {
        "name": "误睡率",
        "reason": "仅凭现有日志无法自动判定「命令已发出时用户刚好回来」。需要新增明确的「用户输入事件 + 命令发出时间」交叉证据才能计算；当前用 jitter_blocks 作为可证实的替代指标（差点误睡被拦住次数）。",
    },
    "user_retention": {
        "name": "留存/转化", "reason": "单机工具，无账号与会话体系，不适用。",
    },
}


def _in_window(line, cutoff):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", line or "")
    if not m:
        return True          # 无日期的行（如续行）不因格式问题丢弃
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date() >= cutoff
    except ValueError:
        return True


def _lines(paths, cutoff):
    out = []
    for p in paths:
        for l in util.read_lines(p):
            if _in_window(l, cutoff):
                out.append(l)
    return out


def _parse_ts(l):
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", l or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def collect(cfg, days=7):
    logs = cfg.dir("logs_dir")
    snaps = cfg.dir("snapshots_dir")
    cutoff = (datetime.now() - timedelta(days=int(days))).date()

    mon = _lines(sorted(logs.glob("monitor-*.log")), cutoff)
    seq = _lines(sorted(logs.glob("sleep-sequence-*.log")), cutoff)
    sc = _lines(sorted(logs.glob("selfcheck-*.log")), cutoff)
    diag = _lines(sorted(logs.glob("sleep-command-*.log")), cutoff)

    starts = [l for l in mon if "| MONITOR_START |" in l]
    real_starts = [l for l in starts if "dry_run=False" in l]
    step5 = [l for l in seq if "STEP5_SLEEP_COMMAND_ISSUED" in l and "dry_run=False" in l]
    confirmed = [l for l in sc if "系统确实挂起过" in l or "系统确实睡眠过" in l]
    silent = [l for l in sc if "命令已发出但系统未睡" in l]
    ret_false = [l for l in diag if "setSuspendStateReturn=False" in l]
    jitter = [l for l in mon if "JITTER_REGRESSION" in l]

    # 平均等待时长：每个 monitor 文件里 MONITOR_START → PRE_SLEEP
    waits = []
    for p in sorted(logs.glob("monitor-*.log")):
        ls = util.read_lines(p)
        t0 = t1 = None
        for l in ls:
            if _in_window(l, cutoff) and "| MONITOR_START |" in l and t0 is None:
                t0 = _parse_ts(l)
            if t1 is None and "PRE_SLEEP" in l and t0 is not None:
                t1 = _parse_ts(l)
            if t0 and t1:
                waits.append((t1 - t0).total_seconds() / 60)
                break
    gaps = []
    for l in sc:
        m = re.search(r"最大间隔\s*([\d.]+)\s*分钟", l)
        if m:
            gaps.append(float(m.group(1)))

    task_counts = []
    for p in snaps.glob("SNAP-*.json"):
        d = util.read_json(p, {}) or {}
        if isinstance(d.get("task_count"), int):
            task_counts.append(d["task_count"])

    rules = util.read_json(util.abs_path("rules/agent-rules.json"), {}) or {}
    history = rules.get("history", []) if isinstance(rules, dict) else []

    rate = (round(100.0 * len(confirmed) / len(step5), 1) if step5 else None)

    metrics = {
        "sessions_started": len(starts),
        "real_sessions": len(real_starts),
        "step5_real": len(step5),
        "sleep_confirmed": len(confirmed),
        "sleep_success_rate": rate,
        "silent_failures": len(silent),
        "command_ret_false": len(ret_false),
        "jitter_blocks": len(jitter),
        "mean_wait_minutes": round(sum(waits) / len(waits), 1) if waits else None,
        "heartbeat_gap_max_minutes": round(max(gaps), 1) if gaps else None,
        "avg_tasks_per_snapshot": round(sum(task_counts) / len(task_counts), 1) if task_counts else None,
        "rule_confirmations": len(history),
    }
    return {
        "generated_at": util.iso(),
        "window_days": int(days),
        "cutoff": str(cutoff),
        "scope": "只统计 logs/（正式值守）；acceptance/ 为脚本自测，不计入。",
        "metrics": metrics,
        "definitions": METRIC_DEFS,
        "not_available": NOT_AVAILABLE,
    }


def format_text(m):
    lines = [
        "=" * 76,
        "  产品指标周报（本地离线聚合，不上传）",
        f"  生成: {m['generated_at']} | 窗口: 近 {m['window_days']} 天（自 {m['cutoff']} 起）",
        m["scope"],
        "=" * 76,
        "",
        "【指标】",
    ]
    for k, v in m["metrics"].items():
        d = METRIC_DEFS.get(k, {})
        unit = d.get("unit", "")
        val = "N/A" if v is None else f"{v}{unit}"
        lines.append(f"  {d.get('name', k):22s} {val:>12s}   ({k})")
    lines += ["", "【口径】"]
    for k, d in METRIC_DEFS.items():
        lines.append(f"  {k}: {d['definition']}  [来源 {d['source']}]")
    lines += ["", "【暂不可自动计算】"]
    for k, d in NOT_AVAILABLE.items():
        lines.append(f"  {k}（{d['name']}）: {d['reason']}")
    lines += ["", "=" * 76]
    return "\n".join(lines)
