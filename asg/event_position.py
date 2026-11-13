"""基于「事件位置」的终态判定器（用户澄清后的规则）。

规则不是「日志静默 N 分钟就算完成」——那会在任务**刚开始或进行到一半**时误判。
正确的判定是先看日志最后停在任务状态机的哪个位置：

    最后一条任务事件是 START  / MIDDLE  -> running（无论静默多久，绝不判完成）
    最后一条任务事件是 END            -> 再看之后静默了多久
         静默 >= idle_confirm_seconds -> completed（停在上一任务的结束，且确认无新任务）
         静默 <  idle_confirm_seconds -> running（还在确认窗口内）

也就是说：**必须同时满足「看到明确的结束事件」+「之后 N 分钟没有新的开始/中间事件」**。
单纯的时间静默永远不够。

此模块被 autoclaw_log / trae_agent 共用，便于统一口径、统一留痕。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import util

START = "start"
MIDDLE = "middle"
END = "end"

_EPOCH_MS = re.compile(r"^\d{13}$")


def parse_ts(value):
    """把 epoch 毫秒 / ISO(可带 Z) / datetime 统一转成 aware datetime。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and _EPOCH_MS.match(value)):
        try:
            return datetime.fromtimestamp(float(value) / 1000).astimezone()
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def verdict(events, idle_confirm_seconds=300, now=None):
    """events: [{"ts":..., "kind": start|middle|end, "name":...}]

    返回 dict:
      status      completed / running / unknown
      position    ended / started / in_progress / no_events
      last_event  最后一条任务事件名
      idle_seconds 距最后一条任务事件的静默秒数
      evidence    写进快照「状态依据」列的字符串
    """
    now = now or util.now_dt()
    parsed = []
    for e in events:
        dt = parse_ts(e.get("ts"))
        if dt is None:
            continue
        parsed.append((dt, e.get("kind", MIDDLE), e.get("name", "?")))
    if not parsed:
        return {
            "status": "unknown",
            "position": "no_events",
            "last_event": "-",
            "idle_seconds": -1,
            "evidence": "event_position:no_events(conservative)",
        }

    last_dt, last_kind, last_name = max(parsed, key=lambda x: x[0])
    idle = max(0.0, (now - last_dt).total_seconds())
    n = int(idle_confirm_seconds)

    if last_kind == END:
        if idle >= n:
            return {
                "status": "completed",
                "position": "ended",
                "last_event": last_name,
                "idle_seconds": round(idle, 1),
                "evidence": f"event_position:ended+idle>={n}s",
            }
        return {
            "status": "running",
            "position": "ended",
            "last_event": last_name,
            "idle_seconds": round(idle, 1),
            "evidence": f"event_position:ended+idle<{n}s(confirming)",
        }
    if last_kind == START:
        position, base = "started", "event_position:started"
    else:
        position, base = "in_progress", "event_position:in_progress"
    return {
        "status": "running",
        "position": position,
        "last_event": last_name,
        "idle_seconds": round(idle, 1),
        "evidence": base,
    }
