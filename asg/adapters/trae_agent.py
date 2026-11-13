"""真实适配器：Trae CN 的 AI Agent（本机）。

实测数据源: %APPDATA%/Trae CN/ModularData/ai-agent/
  - database.db            295 MB, **不是 SQLite**（私有/加密格式），读不到任务级状态
  - command-execution/<id>/execution.json
        executionId / status(=exited) / startedAtMs / finishedAtMs /
        exitCode / pid / target / terminalReason
        这是**单条命令**的执行记录，粒度比"Agent 任务"细，但状态明确。
  - snapshot/<id>/v2/     会话快照，格式私有

严格遵循"绝不打断任务":
  - 只把「有 startedAtMs 但没有 finishedAtMs」的执行记录视为明确的未终结任务；
    有 finishedAtMs（实测全部为 exited）才判 completed。
  - 因为拿不到 Agent 任务级终态, 额外生成 1 条会话级任务, 按事件位置动态判定。
  - 绝不用"日志静默 N 分钟就算完成"来猜。
  - 字段名与状态映射从 rules/agent-rules.json 读取，应用升级换字段可被规则草案发现。

目录不存在 / 读不到时抛异常，不静默返回空清单。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from ..event_position import END, START, verdict
from ..rules import get_rulebook
from .base import BaseAdapter


def _terminal(d, book):
    """按规则表判断一条命令是否已结束（字段名和状态名都不写死）。"""
    end_field = book.field_rule("trae_agent", "end_field", "finishedAtMs")
    if d.get(end_field):
        return True
    return book.classify_status("trae_agent", d.get("status", "")) in (
        "completed", "failed", "cancelled")


def _iso_ms(v):
    if not v:
        return ""
    try:
        return datetime.fromtimestamp(float(v) / 1000).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError, TypeError, OverflowError):
        return str(v)


class TraeAgentAdapter(BaseAdapter):
    type = "trae_agent"

    def __init__(self, name, options, cfg):
        super().__init__(name, options, cfg)
        appdata = Path(os.environ.get("APPDATA", ""))
        self.data_dir = Path(options.get("data_dir") or (
            appdata / "Trae CN" / "ModularData" / "ai-agent"
        ))
        self.exec_dir = self.data_dir / "command-execution"
        self.lookback_minutes = int(options.get("lookback_minutes", 120))
        self.conservative_session = bool(options.get("conservative_session", True))
        self.idle_confirm_seconds = int(options.get("idle_confirm_seconds", 300))

    def _check(self):
        if not self.data_dir.is_dir() or not self.exec_dir.is_dir():
            raise RuntimeError(
                f"Trae 数据目录不存在: {self.data_dir}（数据源不可用，拒绝返回空清单）"
            )

    def _executions(self):
        """返回 (未终结项, 最近活动时间戳, 任务事件流)。

        事件流必须在时间窗过滤之前收集 —— 「日志停在上一任务的结束」可能发生在几小时前，
        正是用户规则里允许判定完成的情形。
        """
        self._check()
        book = get_rulebook()
        start_field = book.field_rule("trae_agent", "start_field", "startedAtMs")
        end_field = book.field_rule("trae_agent", "end_field", "finishedAtMs")
        cutoff_ms = (time.time() - self.lookback_minutes * 60) * 1000
        pending, latest, events = [], "", []
        for f in self.exec_dir.glob("*/execution.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            start = d.get(start_field) or 0
            finish = d.get(end_field) or 0
            iso = _iso_ms(finish or start)
            if iso > latest:
                latest = iso
            eid = str(d.get("executionId") or f.parent.name)[:16]
            if start:
                events.append({"ts": start, "kind": START, "name": f"exec.started:{eid}"})
            if _terminal(d, book):
                events.append({"ts": finish or start, "kind": END, "name": f"exec.exited:{eid}"})
            # 只看回看窗口内的记录, 避免把几百条历史命令全塞进快照
            try:
                if float(start) < cutoff_ms:
                    continue
            except (TypeError, ValueError):
                continue
            if not _terminal(d, book):
                pending.append({
                    "execution_id": str(d.get("executionId") or f.parent.name),
                    "start": _iso_ms(start),
                    "file": str(f),
                    "mtime": f.stat().st_mtime,
                })
        return pending, latest, events

    def _verdict(self):
        return verdict(self._executions()[2],
                        idle_confirm_seconds=self.idle_confirm_seconds)

    # ---------- 接口 ----------

    def scan_tasks(self):
        pending, latest, events = self._executions()
        tasks = []
        for p in pending:
            tasks.append({
                "task_id": f"trae-exec:{p['execution_id']}",
                "agent": "trae-cn/commands",
                "start_time": p["start"],
                "status": "running",
                "last_active": _iso_ms(p["mtime"] * 1000),
                "kind": "command",
                "evidence": "explicit:started_without_finishedAtMs",
                "conservative": False,
                "state_file": p["file"],
                "adapter_type": self.type,
            })
        if self.conservative_session:
            v = verdict(events, idle_confirm_seconds=self.idle_confirm_seconds)
            tasks.append({
                "task_id": "trae-session:ai-agent",
                "agent": "trae-cn/session",
                "start_time": latest,
                "status": v["status"],
                "last_active": latest,
                "kind": "session",
                "evidence": v["evidence"],
                "conservative": v["status"] == "unknown",
                "event_position": v["position"],
                "last_event": v["last_event"],
                "idle_seconds": v["idle_seconds"],
                "idle_confirm_seconds": self.idle_confirm_seconds,
                "note": "终态规则：最后一条命令必须是明确的结束(finishedAtMs/exited)，"
                        "且之后 N 分钟无新命令开始；停在命令开始或进行中一律 running",
                "adapter_type": self.type,
            })
        return tasks

    def get_task_status(self, task):
        if task.get("kind") == "session":
            return self._verdict()["status"]
        f = task.get("state_file")
        if not f or not Path(f).exists():
            return "unknown"
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return "unknown"
        book = get_rulebook()
        end_field = book.field_rule("trae_agent", "end_field", "finishedAtMs")
        if d.get(end_field):
            return "completed"
        st = book.classify_status("trae_agent", d.get("status", ""))
        return st if st in ("completed", "failed", "cancelled") else "running"

    def get_last_active(self, task):
        f = task.get("state_file")
        if f and Path(f).exists():
            return _iso_ms(Path(f).stat().st_mtime * 1000)
        return task.get("last_active") or task.get("start_time")

    def agent_is_idle(self, agent):
        try:
            pending, _, _ = self._executions()
            return self._verdict()["status"] == "completed" and not pending
        except RuntimeError:
            return None

    def describe(self):
        return (f"trae_agent:{self.name}(lookback={self.lookback_minutes}min, "
                f"event_position, idle_confirm={self.idle_confirm_seconds}s)")
