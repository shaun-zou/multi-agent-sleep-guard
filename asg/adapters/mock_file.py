"""示例适配器: 基于本地 JSON 状态文件 + 日志文件 mtime 的模拟 Agent 数据源。

这是可直接运行的示例 (也是端到端演示用的数据源), 演示两种取状态方式:

  status_source = "field" : 状态直接读 data/agents_state.json 的 status 字段。
                            手工把 status 改成 completed/failed/cancelled 即模拟任务终结。
  status_source = "mtime" : 任务日志文件的 mtime 静默超过 idle_seconds 即视为
                            idle_implies_status (默认 completed)。完全无需人工干预,
                            适合自动演示"任务陆续跑完 -> 系统睡眠"的全流程。
  status_source = "both"  : 字段为终态则终态; 否则再看 mtime。

agent_is_idle() 会检查该 Agent 名下所有日志文件的 mtime, 用于
"需观察重试链": 即使旧任务已终结, 只要同 Agent 又冒出新的活跃日志文件
(例如重试生成了新 task_id), 也能被 Agent 级信号挡住, 不会误判为全空闲。

data/agents_state.json 格式:
{
  "tasks": [
    {"task_id": "T-1001", "agent": "agent-alpha",
     "start_time": "2026-11-08T01:00:00+08:00", "status": "running",
     "log_file": "data/logs/agent-alpha.log"}
  ]
}
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .. import util
from .base import BaseAdapter


def _mtime_iso(path: Path):
    try:
        return util.iso(datetime.fromtimestamp(path.stat().st_mtime).astimezone())
    except OSError:
        return None


class MockFileAdapter(BaseAdapter):
    type = "mock_file"

    def __init__(self, name, options, cfg):
        super().__init__(name, options, cfg)
        self.state_file = util.abs_path(options.get("state_file", "data/agents_state.json"))
        self.status_source = options.get("status_source", "field")
        self.idle_seconds = float(options.get("idle_seconds", 0) or 0)
        self.idle_implies_status = options.get("idle_implies_status", "completed")
        self.log_dir = util.abs_path(options.get("log_dir", "data/logs"))

    # ---------- 内部 ----------

    def _read(self):
        data = util.read_json(self.state_file, {}) or {}
        return data.get("tasks", [])

    def _log_path(self, task):
        raw = task.get("log_file")
        if raw:
            return util.abs_path(raw)
        return self.log_dir / f"{task.get('agent', 'unknown')}.log"

    def _log_is_idle(self, task):
        path = self._log_path(task)
        if not path.exists() or self.idle_seconds <= 0:
            return False
        try:
            return (time.time() - path.stat().st_mtime) >= self.idle_seconds
        except OSError:
            return False

    def _derive_status(self, task):
        field_status = str(task.get("status", "running")).lower()
        if self.status_source == "field":
            return field_status
        idle = self._log_is_idle(task)
        if self.status_source == "mtime":
            return self.idle_implies_status if idle else "running"
        if self.status_source == "both":
            field_terminal = field_status in ("completed", "failed", "cancelled")
            if field_terminal:
                return field_status
            return self.idle_implies_status if idle else field_status
        return field_status

    # ---------- 接口 ----------

    def scan_tasks(self):
        out = []
        for raw in self._read():
            item = dict(raw)
            item["status"] = self._derive_status(item)
            log_path = self._log_path(item)
            item["last_active"] = _mtime_iso(log_path) or item.get("start_time")
            item["log_file"] = str(log_path) if log_path.is_absolute() else str(log_path)
            item["adapter_type"] = self.type
            out.append(item)
        return out

    def get_task_status(self, task):
        for raw in self._read():
            if str(raw.get("task_id")) == str(task.get("task_id")):
                probe = dict(raw)
                for key in ("log_file", "start_time", "status"):
                    if key in task and key not in probe:
                        probe[key] = task[key]
                return self._derive_status(probe)
        # 任务从状态文件里消失: 保守判 unknown (非终态), 宁可整夜不睡
        return "unknown"

    def get_last_active(self, task):
        return _mtime_iso(self._log_path(task)) or task.get("last_active") or task.get("start_time")

    def agent_is_idle(self, agent):
        """Agent 级活跃信号: 扫描 log_dir 中名字含 agent 的所有日志文件 mtime。

        这能捕获"重试生成新 task_id"的情况 —— 新 ID 不在快照里, 但只要它
        写了新的日志文件, 该 Agent 就不会被判定为空闲。
        """
        if self.idle_seconds <= 0 or not self.log_dir.exists():
            return None
        try:
            candidates = [p for p in self.log_dir.iterdir()
                          if p.is_file() and str(agent) in p.name]
        except OSError:
            return None
        if not candidates:
            return None
        now = time.time()
        for p in candidates:
            try:
                if (now - p.stat().st_mtime) < self.idle_seconds:
                    return False
            except OSError:
                continue
        return True

    def describe(self):
        return f"mock_file:{self.name}(source={self.status_source}, idle={self.idle_seconds}s)"
