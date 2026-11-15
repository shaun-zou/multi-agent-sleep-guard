"""真实适配器：扫描本机 WorkBuddy 正在运行的会话及其任务。

数据来源（全部只读，绝不修改被监控应用的文件）：
  1. ~/.workbuddy/workbuddy.db  (SQLite)
       sessions: id / cwd / status / created_at / updated_at /
                 is_background_automation / deleted_at / expert_id
       status 取值实测: working / completed / terminated
  2. ~/.workbuddy/tasks/<session-id>/<n>.json
       id / status / createdAt / updatedAt
       status 取值实测: pending / in_progress / completed
  3. ~/.workbuddy/sessions/<pid>.json
       pid / lastHeartbeat / sessionId / cwd / startedAt   (活跃心跳)

设计取舍：
  - 只把 status='working' 的会话视为「当前任务」，历史 completed/terminated 不入清单。
  - 每个活跃会话产出 1 条「会话级任务」+ 若干条「待办任务」，两者都在快照里被跟踪。
  - 交互式会话（用户正在对话的窗口）默认也扫出来，但打上 is_self_candidate 标记，
    由人工核对阶段决定是否剔除 —— 符合「人负责判断」的原则，脚本不替人做决定。
  - 数据源不可用（db 不存在 / 表结构变了 / 读取失败）时**抛异常**，绝不静默返回空清单，
    否则会把「读不到」误当成「全都完成了」进而误触发睡眠。
  - 读不到某个任务时一律返回 unknown（非终态），宁可整夜不睡。

注意：时间戳是 epoch 毫秒；这里统一转成带时区的 ISO 字符串。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .. import util
from ..event_position import END, MIDDLE, START, verdict
from ..rules import get_rulebook
from .base import BaseAdapter

EPOCH_MS = re.compile(r"^\d{13}$")

# 会话事件分类已外置到 rules/agent-rules.json；未归类的新事件一律按 MIDDLE 保守处理。


def _classify_event(d):
    """规则表是唯一真相源；未归类一律 MIDDLE，已确认噪声才忽略，绝不误判 END。

    key 形如 event:message:user / event:message:assistant:completed / event:function_call
    """
    t = str(d.get("type", ""))
    if not t:
        return None
    role = str(d.get("role", ""))
    if t == "message" and role == "assistant" and \
            str(d.get("status", "")).lower() == "completed":
        key = "event:message:assistant:completed"
    elif t == "message" and role:
        key = f"event:message:{role}"
    else:
        key = f"event:{t}"
    book = get_rulebook()
    v = book.classify_event("workbuddy_local", key)
    if v in (START, MIDDLE, END):
        return v
    if book.is_ignored("workbuddy_local", key):
        return None
    return MIDDLE

def _status(adapter, value, default="unknown"):
    """状态映射外置到规则表；未归类的状态一律 unknown（非终态）。"""
    return get_rulebook().classify_status(adapter, value) or default


def _iso(v):
    """epoch 毫秒 -> ISO 字符串；已经是字符串就原样返回。"""
    if v is None:
        return ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and EPOCH_MS.match(v)):
        try:
            return datetime.fromtimestamp(float(v) / 1000).astimezone().isoformat(timespec="seconds")
        except (OSError, ValueError, OverflowError):
            return str(v)
    return str(v)


class WorkBuddyLocalAdapter(BaseAdapter):
    type = "workbuddy_local"

    def __init__(self, name, options, cfg):
        super().__init__(name, options, cfg)
        home = Path.home()
        self.db_path = Path(options.get("db_path") or (home / ".workbuddy" / "workbuddy.db"))
        self.tasks_dir = Path(options.get("tasks_dir") or (home / ".workbuddy" / "tasks"))
        self.sessions_dir = Path(options.get("sessions_dir") or (home / ".workbuddy" / "sessions"))
        self.projects_dir = Path(options.get("projects_dir") or (home / ".workbuddy" / "projects"))
        self.include_interactive = bool(options.get("include_interactive", True))
        # 是否把「已终态」的待办子任务纳入清单。代码默认 True 是保守行为（保留更多任务、
        # 更不容易误睡）；本机真实配置 config.real.json 已设为 false，扫描结果因此只含
        # 会话级主任务 + 尚未完成的子任务。
        self.include_completed_todos = bool(options.get("include_completed_todos", True))
        # 「已终态」判定与监控同源：默认取 config.terminal_statuses（completed/failed/
        # cancelled），可用 options.todo_terminal_statuses 显式覆盖。
        # unknown 被硬排除 —— 状态读不到时绝不把子任务从清单里悄悄删掉（宁可整夜不睡）。
        default_terminal = getattr(cfg, "terminal_statuses", None) or \
            ("completed", "failed", "cancelled")
        configured = options.get("todo_terminal_statuses")
        self.todo_terminal_statuses = {
            str(s).lower() for s in (configured if configured else default_terminal)
        }
        self.todo_terminal_statuses.discard("unknown")
        # 最近一次 scan_tasks 被过滤掉的终态子任务（逐条留痕，防止悄悄吞掉）
        self.filtered_todos = []
        self.exclude_cwds = [str(x).lower() for x in options.get("exclude_cwds", [])]
        # 夜间值守：交互窗口已无用户输入，最后事件停在结束位置后需静默多久才判完成
        self.idle_confirm_seconds = int(options.get("idle_confirm_seconds", 300))
        # discover 写入的数据源优先级结果：主源失效时自动换备用源（日志/会话事件）
        self.primary_source = str(options.get("primary_source") or "db_sessions")
        self.source_fallbacks = list(options.get("source_fallbacks") or [])
        self.fallback_lookback_hours = int(options.get("fallback_lookback_hours", 24))
        self._degraded = False
        self._log_cache = {}

    # ---------- 内部 ----------

    def _connect(self):
        if not self.db_path.exists():
            raise RuntimeError(f"WorkBuddy 数据库不存在: {self.db_path}（数据源不可用，拒绝返回空清单）")
        try:
            return sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=3
            )
        except sqlite3.Error as exc:
            raise RuntimeError(f"WorkBuddy 数据库只读打开失败: {exc}") from exc

    def _active_sessions_from_db(self):
        """P4 主源：SQLite sessions 表（明确终态）。"""
        con = self._connect()
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            needed = {"id", "cwd", "status", "created_at", "updated_at"}
            if not needed.issubset(cols):
                raise RuntimeError(f"WorkBuddy sessions 表结构变化，缺少字段: {needed - cols}")
            extra = "is_background_automation" if "is_background_automation" in cols else "NULL"
            deleted = "deleted_at IS NULL" if "deleted_at" in cols else "1=1"
            rows = con.execute(
                f"SELECT id, cwd, status, created_at, updated_at, {extra} "
                f"FROM sessions WHERE {deleted} AND status='working' "
                f"ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            con.close()
        return [
            {"id": r[0], "cwd": r[1] or "", "status": r[2],
             "created_at": r[3], "updated_at": r[4], "background": bool(r[5])}
            for r in rows
        ]

    def _sessions_from_events(self):
        """P3 备用源：主源失效时，从 projects/**/<sid>.jsonl 找近期有活动的会话。"""
        if not self.projects_dir.is_dir():
            return []
        cutoff = time.time() - self.fallback_lookback_hours * 3600
        out = []
        for f in self.projects_dir.rglob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt < cutoff:
                continue
            ms = int(mt * 1000)
            out.append({"id": f.stem, "cwd": "", "status": "working",
                        "created_at": ms, "updated_at": ms, "background": False,
                        "_source": "projects_jsonl"})
        return out

    def _active_sessions(self):
        try:
            sessions = self._active_sessions_from_db()
            self._degraded = False
            return sessions
        except RuntimeError as exc:
            # 解析不了主源就换备用源；两个源都不可用时绝不静默返回空清单
            sessions = self._sessions_from_events()
            if not sessions:
                raise RuntimeError(
                    f"主源 {self.primary_source} 不可用({exc})，备用源 projects_jsonl "
                    "也无近期会话；拒绝返回空清单") from exc
            self._degraded = True
            return sessions

    def _session_status(self, sid):
        try:
            con = self._connect()
        except RuntimeError:
            return "working"      # 主源不可用，交给事件位置判定
        try:
            row = con.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        finally:
            con.close()
        return str(row[0]).lower() if row else None

    def _todos(self, sid):
        d = self.tasks_dir / sid
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict) or obj.get("id") is None:
                continue
            out.append({
                "todo_id": obj["id"],
                "status": _status("workbuddy_local", obj.get("status", "unknown")),
                "raw_status": obj.get("status"),
                "start_time": _iso(obj.get("createdAt")),
                "updated_at": _iso(obj.get("updatedAt")),
                "file": str(f),
                # 不读取 subject/description：避免把用户工作内容落到本工具的快照里
            })
        return out

    def _heartbeat(self):
        """返回所有会话心跳文件里最新的 lastHeartbeat 时间。"""
        latest = None
        if not self.sessions_dir.is_dir():
            return None
        for f in self.sessions_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                hb = d.get("lastHeartbeat")
                if hb:
                    t = _iso(hb)
                    if latest is None or t > latest:
                        latest = t
            except (OSError, json.JSONDecodeError):
                continue
        return latest

    def _session_events(self, sid):
        """读取会话记录 <sid>.jsonl 的事件流（按 size+mtime 缓存，避免每轮重解析）。"""
        cached = self._log_cache.get(sid)
        path = cached.get("path") if cached else None
        if path is None:
            try:
                path = next(self.projects_dir.rglob(f"{sid}.jsonl"))
            except (StopIteration, OSError):
                path = None
        if path is None or not path.exists():
            return []
        key = (path.stat().st_size, int(path.stat().st_mtime))
        if cached and cached.get("key") == key:
            return cached["events"]
        events = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue
                kind = _classify_event(d)
                if kind:
                    events.append({
                        "ts": d.get("timestamp"),
                        "kind": kind,
                        "name": f"{d.get('type')}:{d.get('role') or ''}",
                    })
        self._log_cache[sid] = {"path": path, "key": key, "events": events}
        return events

    def _session_verdict(self, sid, db_status):
        """会话级终态：数据库终态优先；仍为 working 时按「事件位置」判定。

        夜间值守场景下交互窗口无用户输入：最后一条事件是 END 且静默 >= N 分钟
        -> completed；一旦出现新的 role=user 输入 -> 最后事件变 START -> running。
        """
        st = str(db_status or "").lower()
        if st in ("completed", "terminated"):
            return {"status": _status("workbuddy_local", st),
                    "evidence": "explicit:sessions.status",
                    "position": "db_terminal", "last_event": st, "idle_seconds": -1}
        events = self._session_events(sid)
        if not events:
            return {"status": "unknown",
                    "evidence": "event_position:no_session_log(conservative)",
                    "position": "no_events", "last_event": "-", "idle_seconds": -1}
        v = verdict(events, idle_confirm_seconds=self.idle_confirm_seconds)
        if getattr(self, "_degraded", False):
            v["evidence"] += "+fallback:projects_jsonl"
        return v

    # ---------- 接口 ----------

    def scan_tasks(self):
        self.filtered_todos = []             # 每轮重新统计，留痕只反映本次扫描
        sessions = self._active_sessions()   # 数据源异常会抛错，不会静默给空清单
        heartbeat = self._heartbeat()
        result = []
        for s in sessions:
            sid = str(s["id"])
            if not self.include_interactive and not s["background"]:
                continue
            if any(x in str(s["cwd"]).lower() for x in self.exclude_cwds):
                continue
            short = sid[:8]
            interactive = not s["background"]
            v = self._session_verdict(sid, s["status"])
            common = {
                "agent": f"workbuddy/{short}",
                "session_id": sid,
                "cwd": s["cwd"],
                "background": s["background"],
                "interactive_session": interactive,
                "last_active": heartbeat or _iso(s["updated_at"]),
                "adapter_type": self.type,
                "source_hint": "workbuddy.db:sessions + projects/**/<sid>.jsonl + tasks/<sid>/*.json",
            }
            # ① 会话级任务：数据库终态优先；working 时按事件位置判定（含用户输入兜底）
            result.append({
                **common,
                "task_id": f"wb-session:{sid}",
                "start_time": _iso(s["created_at"]),
                "status": v["status"],
                "kind": "session",
                "raw_status": s["status"],
                "evidence": v["evidence"],
                "conservative": v["status"] == "unknown",
                "event_position": v["position"],
                "last_event": v["last_event"],
                "idle_seconds": v["idle_seconds"],
                "idle_confirm_seconds": self.idle_confirm_seconds,
                "note": "夜间值守时交互窗口无用户输入：最后事件停在结束位置且静默>=N分钟判完成；"
                        "出现新用户输入(role=user)立即回到 running，不会睡",
            })
            # ② 该会话下的待办任务
            #    会话级主任务（wb-session）始终保留；这里只过滤「已终态」的子任务。
            #    被过滤项进 self.filtered_todos 留痕，由 scan 输出/事件日志逐条列出。
            for t in self._todos(sid):
                tid = f"wb-todo:{sid}:{t['todo_id']}"
                if (not self.include_completed_todos
                        and t["status"] in self.todo_terminal_statuses):
                    self.filtered_todos.append({
                        "task_id": tid, "status": t["status"],
                        "raw_status": t["raw_status"],
                    })
                    continue
                result.append({
                    **common,
                    "task_id": tid,
                    "start_time": t["start_time"],
                    "status": t["status"],
                    "kind": "todo",
                    "raw_status": t["raw_status"],
                    "last_active": t["updated_at"] or common["last_active"],
                    "state_file": t["file"],
                    "evidence": "explicit:todo.status",
                    "conservative": False,
                })
        return result

    def get_task_status(self, task):
        sid = str(task.get("session_id") or "")
        if task.get("kind") == "session":
            st = self._session_status(sid) if sid else None
            return self._session_verdict(sid, st)["status"]
        f = task.get("state_file")
        if not f or not Path(f).exists():
            return "unknown"
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8", errors="replace"))
            return _status("workbuddy_local", d.get("status", "unknown"))
        except (OSError, json.JSONDecodeError):
            return "unknown"

    def get_last_active(self, task):
        # todo 是静态计划项：优先显示它自己文件里的 updatedAt。
        # 旧实现统一返回会话心跳，会让一条从未变过的 pending 待办在 PROGRESS 日志里
        # 看起来"每分钟都在活动"，严重误导排查（11-14 卡 11 小时那次的直接教训）。
        if task.get("kind") == "todo":
            f = task.get("state_file")
            if f and Path(f).exists():
                try:
                    d = json.loads(Path(f).read_text(encoding="utf-8", errors="replace"))
                    own = _iso(d.get("updatedAt"))
                    if own:
                        return own
                except (OSError, ValueError, TypeError):
                    pass
        return self._heartbeat() or task.get("last_active") or task.get("start_time")

    def agent_is_idle(self, agent):
        """Agent 级活跃信号：该会话按事件位置规则是否已判完成。"""
        short = str(agent).split("/")[-1]
        for s in self._active_sessions():
            if str(s["id"]).startswith(short):
                return self._session_verdict(str(s["id"]), s["status"])["status"] == "completed"
        return True

    def describe(self):
        return (f"workbuddy_local:{self.name}(db={self.db_path.name}, "
                f"event_position, idle_confirm={self.idle_confirm_seconds}s)")
