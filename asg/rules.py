"""规则表：从 discover / scan 的真实数据里自动观测并生成更新草案。

为什么不能"全自动改规则"：
  「某个事件是否代表任务结束」是语义，不是统计。把 MIDDLE 误学成 END 会在
  Agent 还在跑时误触发睡眠，直接违反「绝不打断任务」。因此分两层：

    可自动：路径/版本变化、数据源降级、事件名与状态值的观测统计、
            规则漂移（新出现 / 已消失）、生成待确认草案。
    需确认：把某个值归类为 start / middle / end / 终态状态。
            未确认前一律按最保守处理（事件 -> middle，状态 -> unknown）。

流程：
  discover / scan  ->  自动观测(observe)  ->  rules 生成草案  ->
  rules --approve 人工确认  ->  写入 agent-rules.json  ->  适配器立即生效

规则表是本工具自己的文件 `rules/agent-rules.json`，不改任何 Agent 应用。
"""
from __future__ import annotations

import fnmatch
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from . import util

RULES_FILE = "rules/agent-rules.json"

DEFAULT_RULES = {
    "schema_version": 1,
    "created_at": "",
    "updated_at": "",
    # 事件名 -> start / middle / end
    "event_kinds": {
        "autoclaw_log": {
            "event:zwork.tool.started": "start",
            "event:zwork.model.request.issued": "start",
            "event:zwork.tool.completed": "end",
            "event:zwork.tool.failed": "end",
            "event:zwork.model.token_usage.record": "end",
            "event:zwork.model.token_usage.summary": "end",
        },
        "workbuddy_local": {
            "event:message:user": "start",
            "event:message:assistant:completed": "end",
            "event:message:assistant": "middle",
            "event:function_call": "middle",
            "event:function_call_result": "middle",
            "event:reasoning": "middle",
        },
        "trae_agent": {
            "event:exec.started": "start",
            "event:exec.exited": "end",
        },
    },
    # 原始状态值 -> 归一化状态（终态集合由 config.terminal_statuses 决定）
    "status_map": {
        "workbuddy_local": {
            "working": "running", "in_progress": "running", "pending": "queued",
            "completed": "completed", "terminated": "cancelled",
            "cancelled": "cancelled", "failed": "failed",
        },
        "trae_agent": {"exited": "completed"},
        "autoclaw_log": {},
    },
    # 字段名规则（应用升级换字段名时可在草案里发现）
    "field_rules": {
        "trae_agent": {"start_field": "startedAtMs", "end_field": "finishedAtMs"},
        "workbuddy_local": {"timestamp_field": "timestamp", "role_field": "role"},
    },
    "observations": {},       # adapter -> key -> {count, first_seen, last_seen}
    "ignored": [],            # adapter:key，人工逐条判定为噪声
    # 按命名空间批量忽略背景噪声（支持 * 通配）。只覆盖明确的非任务命名空间，
    # 绝不列入 zwork.tool.* / zwork.model.request.* / zwork.model.token_usage.*。
    # 显式 event_kinds 分类优先级更高，不会被前缀吞掉；命中项仍会在报告里列出。
    "ignore_patterns": {
        "autoclaw_log": [
            "zwork.network.*", "zwork.promotion_config.*", "zwork.instrumentation.*",
            "zwork.perf.*", "zwork.browser.*", "zwork.chat.*", "zwork.input_notice.*",
            "zwork.artifact_preview.*", "zwork.live.*", "zwork.ledger.*",
            "zwork.zhipu_client_sign.*", "zwork.work.agent_connection.*",
            "zwork.runtime.deferred_tools.*", "zwork.model.image_host.*",
            "lock-screen", "unlock-screen", "suspend", "resume",
        ],
        "workbuddy_local": [],
        "trae_agent": [],
    },
    "history": [],            # 每次确认的留痕
}

_BOOK = None
_BOOK_MTIME = None
_LAST_CHECK = 0.0


def _path():
    return util.abs_path(RULES_FILE)


class RuleBook:
    def __init__(self, path=None):
        self.path = Path(path) if path else _path()
        import copy
        raw = util.read_json(self.path)
        self.data = raw if isinstance(raw, dict) else copy.deepcopy(DEFAULT_RULES)
        if not self.data.get("created_at"):
            self.data = copy.deepcopy(DEFAULT_RULES)
            self.data["created_at"] = util.iso()
            self.save()
        # 兼容旧规则表：补齐新增的顶层字段，不覆盖已有的人工规则
        changed = False
        for k, v in DEFAULT_RULES.items():
            if k not in self.data:
                self.data[k] = copy.deepcopy(v)
                changed = True
        if changed:
            self.save()

    def save(self):
        self.data["updated_at"] = util.iso()
        util.atomic_write_json(self.path, self.data)

    # ---------- 查询（适配器用） ----------

    def classify_event(self, adapter, key):
        """返回 start/middle/end；未归类返回 None（调用方须按保守处理）。"""
        v = self.data["event_kinds"].get(adapter, {}).get(key)
        return v if v in ("start", "middle", "end") else None

    def classify_status(self, adapter, value):
        """返回归一化状态；未归类返回 None（调用方须按 unknown 处理）。"""
        return self.data["status_map"].get(adapter, {}).get(str(value).lower())

    def field_rule(self, adapter, name, default=None):
        return self.data["field_rules"].get(adapter, {}).get(name, default)

    def match_ignore_pattern(self, adapter, key):
        """返回命中的前缀模式（未命中返回 None）。

        key 形如 `event:zwork.network.request.failed`；模式可写 `zwork.network.*`
        或 `event:zwork.network.*`，用 fnmatch 做大小写敏感匹配。
        """
        name = key[6:] if key.startswith("event:") else key
        for pat in self.data.get("ignore_patterns", {}).get(adapter, []):
            p = pat[6:] if pat.startswith("event:") else pat
            if fnmatch.fnmatchcase(name, p):
                return pat
        return None

    def is_ignored(self, adapter, key):
        """人工逐条忽略 > 前缀忽略；显式 event_kinds 由调用方优先判断。"""
        if f"{adapter}:{key}" in self.data.get("ignored", []):
            return True
        return self.match_ignore_pattern(adapter, key) is not None

    def add_ignore_pattern(self, adapter, pattern, operator=""):
        patterns = self.data.setdefault("ignore_patterns", {}).setdefault(adapter, [])
        if pattern not in patterns:
            patterns.append(pattern)
        self._record_pattern(adapter, pattern, "add", operator)
        return list(patterns)

    def remove_ignore_pattern(self, adapter, pattern, operator=""):
        patterns = self.data.setdefault("ignore_patterns", {}).setdefault(adapter, [])
        self.data["ignore_patterns"][adapter] = [p for p in patterns if p != pattern]
        self._record_pattern(adapter, pattern, "remove", operator)
        return list(self.data["ignore_patterns"][adapter])

    def _record_pattern(self, adapter, pattern, action, operator):
        self.data.setdefault("history", []).append({
            "at": util.iso(), "adapter": adapter, "key": f"pattern:{pattern}",
            "decision": f"ignore_pattern_{action}", "operator": operator or "-",
            "applied": f"ignore_patterns -> {action}",
        })
        self.save()

    # ---------- 观测 ----------

    def observe(self, adapter, keys=None, counts=None):
        """记录本次采样到的原始 key（事件名或状态值）。"""
        obs = self.data["observations"].setdefault(adapter, {})
        now = util.iso()
        src = counts if counts is not None else Counter(keys or [])
        for key, n in src.items():
            e = obs.setdefault(str(key), {"count": 0, "first_seen": now, "last_seen": now})
            e["count"] = int(e.get("count", 0)) + int(n)
            e["last_seen"] = now

    # ---------- 草案 ----------

    def draft(self, current_seen=None):
        """生成待确认草案 + 规则漂移提示。current_seen 为本次采样集合。"""
        pending, stale, prefix_ignored = [], [], []
        ignored = set(self.data.get("ignored", []))
        for adapter, obs in self.data["observations"].items():
            kinds = self.data["event_kinds"].get(adapter, {})
            statuses = self.data["status_map"].get(adapter, {})
            for key, info in obs.items():
                if key.startswith("field:"):
                    continue          # 字段观测单独做漂移检测，不进事件草案
                known = key in kinds or (key.startswith("status:") and
                                         key[7:] in statuses)
                if known:
                    continue
                pat = self.match_ignore_pattern(adapter, key)
                if pat:
                    # 前缀已自动忽略：不进草案，但仍列出供人工复核，避免悄悄吞掉新事件
                    prefix_ignored.append({"adapter": adapter, "key": key,
                                           "pattern": pat, "count": info.get("count", 0)})
                    continue
                if f"{adapter}:{key}" in ignored:
                    continue
                kind = "status" if key.startswith("status:") else "event"
                low = key.lower()
                if any(w in low for w in ("started", "issued", "begin", "opened")):
                    suggestion = "start"
                elif any(w in low for w in ("completed", "failed", "finished", "exited")):
                    # 终态/失败类一律交人工判断，绝不自动建议 end：
                    # 例如 zwork.network.request.failed 是睡眠断网噪声，不是任务结束
                    suggestion = "review"
                elif kind == "status":
                    suggestion = "review"
                else:
                    suggestion = "ignore"
                pending.append({
                    "adapter": adapter, "key": key, "kind": kind,
                    "count": info.get("count", 0), "last_seen": info.get("last_seen"),
                    "suggestion": suggestion,
                    "note": "未确认前：事件按 middle、状态按 unknown，绝不判终态",
                })
        if current_seen:
            for adapter, kinds in self.data["event_kinds"].items():
                seen_here = current_seen.get(adapter, set())
                for key in kinds:
                    if key.startswith("event:") and key not in seen_here:
                        stale.append({"adapter": adapter, "key": key,
                                      "note": "规则表里有、本次未观测到（版本升级可能换了事件名）"})
            for adapter, frules in self.data["field_rules"].items():
                seen_here = current_seen.get(adapter, set())
                for name, value in frules.items():
                    if isinstance(value, str) and f"field:{value}" not in seen_here:
                        stale.append({"adapter": adapter, "key": f"field:{value}",
                                      "note": f"规则表字段 {name} 本次未观测到（可能改名）"})
        pending.sort(key=lambda x: -x["count"])
        prefix_ignored.sort(key=lambda x: -x["count"])
        return {"pending": pending, "stale": stale,
                "prefix_ignored": prefix_ignored,
                "ignore_patterns": dict(self.data.get("ignore_patterns", {})),
                "classified_events": sum(len(v) for v in self.data["event_kinds"].values()),
                "observed_keys": sum(len(v) for v in self.data["observations"].values())}

    # ---------- 人工确认 ----------

    def approve(self, adapter, key, decision, operator=""):
        if decision not in ("start", "middle", "end", "ignore",
                            "completed", "failed", "cancelled", "running", "queued"):
            raise ValueError(f"无效判定: {decision}")
        tag = f"{adapter}:{key}"
        if decision == "ignore":
            if tag not in self.data["ignored"]:
                self.data["ignored"].append(tag)
            target = "忽略（视为噪声）"
        elif key.startswith("status:"):
            self.data["status_map"].setdefault(adapter, {})[key[7:]] = decision
            target = f"status_map -> {decision}"
        else:
            self.data["event_kinds"].setdefault(adapter, {})[key] = decision
            target = f"event_kinds -> {decision}"
        self.data["history"].append({
            "at": util.iso(), "adapter": adapter, "key": key,
            "decision": decision, "operator": operator or "-", "applied": target,
        })
        self.save()
        return target

    def version_fingerprint(self):
        """规则表内容指纹，供 discover 判断规则是否需要复查。"""
        payload = {"event_kinds": self.data["event_kinds"],
                   "status_map": self.data["status_map"],
                   "field_rules": self.data["field_rules"],
                   "ignore_patterns": self.data.get("ignore_patterns", {})}
        import hashlib
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                              .encode("utf-8")).hexdigest()[:12]


def get_rulebook():
    """单例；规则表文件被外部更新时自动重载（最长 5 秒延迟）。

    monitor 是长跑进程，若规则表只在启动时读一次，人工确认的新规则
    对正在运行的监控不生效，必须重启进程才能看到 —— 已修。
    """
    global _BOOK, _BOOK_MTIME, _LAST_CHECK
    now = time.monotonic()
    if _BOOK is not None and (now - _LAST_CHECK) < 5.0:
        return _BOOK
    _LAST_CHECK = now
    try:
        p = _path()
        mtime = p.stat().st_mtime if p.exists() else None
    except OSError:
        if _BOOK is None:
            _BOOK = RuleBook()
        return _BOOK
    if _BOOK is None or mtime != _BOOK_MTIME:
        _BOOK = RuleBook()
        _BOOK_MTIME = mtime
    return _BOOK


# ---------------- 从真实数据源采样 ----------------

def _opts(cfg, type_):
    """取适配器 options，并补齐与适配器一致的默认路径（手写配置也能采样）。"""
    found = {}
    for a in cfg.get("adapters", []):
        if a.get("type") == type_ and a.get("enabled", True):
            found = dict(a.get("options") or {})
            break
    if not found and not any(a.get("type") == type_ for a in cfg.get("adapters", [])):
        return {}
    import os
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    defaults = {
        "autoclaw_log": {"logs_dir": appdata / "AutoClaw-official" / "logs"},
        "workbuddy_local": {"db_path": home / ".workbuddy" / "workbuddy.db",
                            "projects_dir": home / ".workbuddy" / "projects",
                            "tasks_dir": home / ".workbuddy" / "tasks"},
        "trae_agent": {"data_dir": appdata / "Trae CN" / "ModularData" / "ai-agent"},
    }
    for k, v in defaults.get(type_, {}).items():
        if not found.get(k):
            found[k] = str(v)
    return found


def _sample_autoclaw(opts, seen):
    counts = Counter()
    d = opts.get("logs_dir")
    if not d:
        return counts
    for f in sorted(Path(d).glob("zwork-main-*.log"))[-2:]:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                i = line.find("[")
                if i < 0:
                    continue
                try:
                    arr = json.loads(line[i:])
                    for item in arr:
                        if isinstance(item, str) and item.lstrip().startswith("{"):
                            obj = json.loads(item)
                            if obj.get("event"):
                                counts[f"event:{obj['event']}"] += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    seen.update(counts.keys())
    return counts


def _sample_workbuddy(opts, seen):
    counts = Counter()
    projects = opts.get("projects_dir")
    if projects:
        for f in sorted(Path(projects).rglob("*.jsonl"))[-40:]:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(d, dict):
                        continue
                    if d.get("timestamp"):
                        counts["field:timestamp"] += 1
                    if d.get("role"):
                        counts["field:role"] += 1
                    t, role = d.get("type"), d.get("role")
                    if t == "message" and role == "assistant" and \
                            str(d.get("status", "")).lower() == "completed":
                        counts["event:message:assistant:completed"] += 1
                    elif t == "message" and role:
                        counts[f"event:message:{role}"] += 1
                    elif t:
                        counts[f"event:{t}"] += 1
    db = opts.get("db_path")
    if db and Path(db).exists():
        try:
            con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True, timeout=3)
            for (v,) in con.execute("SELECT DISTINCT status FROM sessions"):
                if v:
                    counts[f"status:{str(v).lower()}"] += 1
            con.close()
        except sqlite3.Error:
            pass
    tasks = opts.get("tasks_dir")
    if tasks:
        for f in Path(tasks).rglob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                if isinstance(d, dict) and d.get("status"):
                    counts[f"status:{str(d['status']).lower()}"] += 1
            except (OSError, json.JSONDecodeError):
                continue
    seen.update(counts.keys())
    return counts


def _sample_trae(opts, seen):
    counts = Counter()
    d = opts.get("data_dir")
    if not d:
        return counts
    for f in list(Path(d).glob("command-execution/*/execution.json"))[:300]:
        try:
            o = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if o.get("startedAtMs"):
            counts["event:exec.started"] += 1
        if o.get("finishedAtMs") or str(o.get("status", "")).lower() == "exited":
            counts["event:exec.exited"] += 1
        if o.get("status"):
            counts[f"status:{str(o['status']).lower()}"] += 1
        for k in ("startedAtMs", "finishedAtMs"):
            if k in o:
                counts[f"field:{k}"] += 1
    seen.update(counts.keys())
    return counts


SAMPLERS = {"autoclaw_log": _sample_autoclaw,
            "workbuddy_local": _sample_workbuddy,
            "trae_agent": _sample_trae}


def sample(cfg, book=None):
    """从当前启用的真实数据源采样，更新累积观测，返回本次采样集合。"""
    book = book or get_rulebook()
    current = {}
    for type_, fn in SAMPLERS.items():
        opts = _opts(cfg, type_)
        if not opts:
            continue
        seen = set()
        try:
            counts = fn(opts, seen)
        except (OSError, ValueError):
            continue
        current[type_] = seen
        if counts:
            book.observe(type_, None, counts)
    book.save()
    return current


def format_rules(book, draft, current_seen):
    lines = []
    lines.append("=" * 80)
    lines.append("  规则表状态（由 discover / scan 的真实数据驱动）")
    lines.append(f"  文件: {book.path}   指纹: {book.version_fingerprint()}")
    lines.append("=" * 80)
    lines.append(f"  已归类事件 {draft['classified_events']} 条 / "
                 f"累计观测 {draft['observed_keys']} 种 key / "
                 f"待确认 {len(draft['pending'])} 条 / 规则漂移 {len(draft['stale'])} 条 / "
                 f"前缀自动忽略 {len(draft.get('prefix_ignored', []))} 种")
    lines.append("-" * 80)
    patterns = draft.get("ignore_patterns", {})
    if patterns:
        lines.append("【前缀忽略规则】（显式 event_kinds 优先，不会被前缀吞掉）")
        for adapter, pats in patterns.items():
            lines.append(f"  {adapter:16s} {len(pats)} 条模式: "
                         + ", ".join(pats[:5]) + (" ..." if len(pats) > 5 else ""))
        pi = draft.get("prefix_ignored", [])
        if pi:
            lines.append("  本次被前缀自动忽略的观测值（列出来便于复核，防止误吞新任务事件）：")
            for p in pi[:10]:
                lines.append(f"    [{p['pattern']:28s}] {p['key']:52s} ×{p['count']}")
        lines.append("-" * 80)
    noise = sum(1 for p in draft["pending"] if p["suggestion"] == "ignore")
    if draft["pending"]:
        lines.append("【待确认草案】（代码自动生成，需人工确认后才生效）")
        lines.append("  建议列: ignore=背景噪声 / start|end=疑似任务边界 / review=新状态值")
        lines.append("  未确认前一律：事件按 middle、状态按 unknown，绝不判终态")
        for p in draft["pending"][:25]:
            lines.append(f"  [{p['suggestion']:6s}] {p['adapter']:16s} "
                         f"{p['key']:54s} 出现 {p['count']:5d} 次")
        if len(draft["pending"]) > 25:
            lines.append(f"  ... 另有 {len(draft['pending'])-25} 条")
        if noise:
            lines.append(f"\n  其中 {noise} 条建议为噪声，可一次性确认：")
            lines.append("    python agent_sleep_guard.py rules --approve-noise")
    else:
        lines.append("【待确认草案】无（所有观测值都已归类或已忽略）")
    if draft["stale"]:
        lines.append("\n【规则漂移】以下规则本次未观测到，应用升级可能换了事件名：")
        for s in draft["stale"][:15]:
            lines.append(f"  {s['adapter']:16s} {s['key']}")
    lines.append("-" * 80)
    lines.append("确认命令（人工判断后执行，未确认一律按保守处理）：")
    lines.append('  python agent_sleep_guard.py rules --approve "autoclaw_log:event:xxx" --as end')
    lines.append('  --as 可选: start / middle / end / completed / failed / cancelled / ignore')
    lines.append('  python agent_sleep_guard.py rules --add-ignore-pattern "zwork.foo.*" --adapter autoclaw_log')
    lines.append('  python agent_sleep_guard.py rules --remove-ignore-pattern "zwork.foo.*" --adapter autoclaw_log')
    lines.append("=" * 80)
    return "\n".join(lines)
