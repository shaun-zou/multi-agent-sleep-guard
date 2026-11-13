"""真实适配器：AutoClaw2（智谱）本机结构化日志。

日志位置: %APPDATA%/AutoClaw-official/logs/zwork-main-YYYY-MM-DD.log
实测行格式:
    <时间戳> INFO ["{\"schemaVersion\":1,\"timestamp\":\"...\",\"level\":\"info\",
                    \"event\":\"zwork.tool.started\",\"component\":\"...\",
                    \"context\":{...}}"]
即方括号里是一个 JSON 数组，数组元素是**一个字符串**，字符串内容才是真 JSON。

能拿到的**明确终态**只有工具调用级:
    zwork.tool.started / zwork.tool.completed / zwork.tool.failed
实测日志里**没有** "Agent 任务完成 / 会话结束" 这类上层事件。

因此严格遵循"绝不打断任务、宁可整夜不睡":
  - 未配对的 started 工具调用 -> running 任务（明确的"未终结"）
  - 额外生成 1 条会话级任务, 恒为 running, evidence=no_explicit_terminal
    （除非人工在核对阶段剔除，否则监控不会判全终态）
  - 绝不使用"日志静默 N 分钟就算完成"这种猜测。

只提取 event / timestamp / 脱敏 ID，不把日志里的任何用户内容写进快照。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..event_position import END, MIDDLE, START, verdict
from ..rules import get_rulebook
from .base import BaseAdapter

ID_KEYS = ("operationId", "requestId", "callId", "toolCallId", "toolId", "id")
START_EVENT = "zwork.tool.started"
END_EVENTS = ("zwork.tool.completed", "zwork.tool.failed")

# 事件分类规则已外置到 rules/agent-rules.json（由 discover/scan 观测 + 人工确认维护）。
# 背景噪声（网络请求成功、配置加载、性能打点等）不参与判定。


def classify(event):
    """规则表是唯一真相源；未归类一律 MIDDLE（进行中），已确认噪声才忽略。

    这样新版本冒出未知事件时，判定会停在「进行中」-> 不睡 -> 由 rules 提示人确认，
    绝不会因为不认识某个事件就误判成任务结束。
    """
    book = get_rulebook()
    key = f"event:{event}"
    v = book.classify_event("autoclaw_log", key)
    if v in (START, MIDDLE, END):
        return v
    if book.is_ignored("autoclaw_log", key):
        return None
    return MIDDLE


def _payload(line):
    i = line.find("[")
    if i < 0:
        return None
    try:
        arr = json.loads(line[i:])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(arr, list):
        return None
    for item in arr:
        if isinstance(item, dict) and "event" in item:
            return item
        if isinstance(item, str) and item.lstrip().startswith("{"):
            try:
                d = json.loads(item)
                if isinstance(d, dict) and "event" in d:
                    return d
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _instance_key(d):
    ctx = d.get("context") or {}
    if not isinstance(ctx, dict):
        ctx = {}
    for k in ID_KEYS:
        if ctx.get(k):
            return f"{k}:{ctx[k]}"
    # 找不到实例 ID 时用工具名 + 时间戳兜底，避免不同调用互相覆盖
    return f"ts:{d.get('timestamp')}:{ctx.get('name', 'unknown')}"


def _iso(ts):
    """把 epoch 毫秒或 ISO(含 Z) 统一转成本地时区 ISO，避免日志里 UTC 与本地时间混排。"""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts) / 1000).astimezone().isoformat(timespec="seconds")
    s = str(ts)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return s


class AutoClawLogAdapter(BaseAdapter):
    type = "autoclaw_log"

    def __init__(self, name, options, cfg):
        super().__init__(name, options, cfg)
        import os
        appdata = Path(os.environ.get("APPDATA", ""))
        self.logs_dir = Path(options.get("logs_dir") or (appdata / "AutoClaw-official" / "logs"))
        self.glob = options.get("log_glob", "zwork-main-*.log")
        self.lookback_days = int(options.get("lookback_days", 1))
        self.conservative_session = bool(options.get("conservative_session", True))
        # 停在「结束」事件后，需再静默多少秒才确认完成（用户澄清的 N 分钟）
        self.idle_confirm_seconds = int(options.get("idle_confirm_seconds", 300))
        self._cache_key = None
        self._cache = None

    # ---------- 解析（按文件 mtime 缓存，避免每轮轮询反复解析大日志） ----------

    def _files(self):
        if not self.logs_dir.is_dir():
            raise RuntimeError(f"AutoClaw 日志目录不存在: {self.logs_dir}（数据源不可用，拒绝返回空清单）")
        cutoff = (datetime.now() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        files = [p for p in sorted(self.logs_dir.glob(self.glob))
                 if p.is_file() and re.search(r"\d{4}-\d{2}-\d{2}", p.name)
                 and re.search(r"\d{4}-\d{2}-\d{2}", p.name).group(0) >= cutoff]
        if not files:
            raise RuntimeError(f"AutoClaw 日志目录里没有近 {self.lookback_days} 天的 {self.glob}")
        return files

    def _parse(self):
        files = self._files()
        key = tuple((str(p), p.stat().st_size, int(p.stat().st_mtime)) for p in files)
        if key == self._cache_key and self._cache is not None:
            return self._cache
        pending, owners, latest, task_events = {}, set(), "", []
        for p in files:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    d = _payload(line)
                    if not d:
                        continue
                    ev = str(d.get("event", ""))
                    ts = str(d.get("timestamp") or "")
                    if ts > latest:
                        latest = ts
                    ctx = d.get("context") or {}
                    if isinstance(ctx, dict) and ctx.get("ownerId"):
                        owners.add(str(ctx["ownerId"]))
                    # ① 事件位置状态机：只收任务相关事件，背景噪声忽略
                    kind = classify(ev)
                    if kind:
                        task_events.append({"ts": ts, "kind": kind, "name": ev})
                    # ② 明确未终结的工具调用（配对）
                    if ev == START_EVENT:
                        k = _instance_key(d)
                        pending[k] = {"ts": ts, "file": str(p)}
                    elif ev in END_EVENTS:
                        k = _instance_key(d)
                        if k in pending:
                            del pending[k]
        self._cache_key = key
        self._cache = {"pending": pending, "owners": owners, "latest": latest,
                       "files": files, "task_events": task_events}
        return self._cache

    def _verdict(self):
        return verdict(self._parse()["task_events"],
                        idle_confirm_seconds=self.idle_confirm_seconds)

    # ---------- 接口 ----------

    def scan_tasks(self):
        data = self._parse()   # 数据源异常会抛错
        tasks = []
        # ① 明确未终结的工具调用
        for k, info in sorted(data["pending"].items()):
            short = re.sub(r"[^A-Za-z0-9_.:-]", "_", k)[-48:]
            tasks.append({
                "task_id": f"autoclaw-tool:{short}",
                "agent": "autoclaw2/tools",
                "start_time": _iso(info["ts"]),
                "status": "running",
                "last_active": _iso(data["latest"] or info["ts"]),
                "kind": "tool_call",
                "instance_key": k,
                "evidence": "explicit:tool.started_without_terminal",
                "conservative": False,
                "log_file": info["file"],
                "adapter_type": self.type,
            })
        # ② 会话级保守项：日志里没有"任务完成"事件，不能猜
        if self.conservative_session:
            owner = sorted(data["owners"])[0][:12] if data["owners"] else "default"
            v = self._verdict()
            tasks.append({
                "task_id": f"autoclaw-session:{owner}",
                "agent": "autoclaw2/session",
                "start_time": _iso(data["latest"]),
                "status": v["status"],
                "last_active": _iso(data["latest"]),
                "kind": "session",
                "evidence": v["evidence"],
                "conservative": v["status"] == "unknown",
                "event_position": v["position"],
                "last_event": v["last_event"],
                "idle_seconds": v["idle_seconds"],
                "idle_confirm_seconds": self.idle_confirm_seconds,
                "note": "终态规则：日志最后必须停在明确的结束事件，且之后 N 分钟无新的开始/中间事件；"
                        "停在任务开始或进行中一律 running（不只看静默时长）",
                "adapter_type": self.type,
            })
        return tasks

    def get_task_status(self, task):
        if task.get("kind") == "session":
            # 每轮重新解析日志，按「事件位置 + 确认静默」动态判定
            return self._verdict()["status"]
        data = self._parse()
        key = task.get("instance_key")
        if not key:
            return "unknown"
        return "running" if key in data["pending"] else "completed"

    def get_last_active(self, task):
        try:
            return _iso(self._parse()["latest"]) or task.get("last_active") or task.get("start_time")
        except RuntimeError:
            return task.get("last_active") or task.get("start_time")

    def agent_is_idle(self, agent):
        try:
            data = self._parse()
            return self._verdict()["status"] == "completed" and not data["pending"]
        except RuntimeError:
            return None

    def describe(self):
        return (f"autoclaw_log:{self.name}(logs={self.logs_dir.name}, "
                f"event_position, idle_confirm={self.idle_confirm_seconds}s)")
