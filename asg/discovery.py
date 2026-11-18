"""Agent 自动发现层（纯代码，产品化）。

四条规则（用户定义）：
  1. **只显示在用的 Agent**：已安装但未运行 / 未接适配器 / 没有可用数据源的一律不显示、
     不启用；`--all` 才列出全部。
  2. **变更可发现**：新增安装、卸载、版本升级、数据源降级/升级，都要在 discover 里报告。
     做法是把上次发现结果持久化到 `state/discovery-state.json`，每次做差异对比。
  3. **解析不了就换源**：每个 Agent 有多个候选数据源（DB / 结构化日志 / 文本日志 / 进程），
     健康检查不通过就自动降级到下一个可用源。
  4. **按精度排序**：同一 Agent 的数据源按「能否给出明确终态」分级，取精度最高的可用者。

精度等级（高 -> 低）：
  4 明确终态   能读到明确的任务/会话完成标记（SQLite status、finishedAtMs）
  3 事件位置   结构化事件日志，可按事件位置推断终态
  2 活动迹象   只能判断「最近有没有活动」
  1 仅进程     只知道进程在不在

职责边界：只发现「数据源在哪、能不能读、该用哪个」，不判断任务、不生成快照、
不决定是否睡眠。除本工具自己的 `state/discovery-state.json` 外不写任何文件。
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import socket
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import util

PRECISION_LABEL = {4: "明确终态", 3: "事件位置", 2: "活动迹象", 1: "仅进程"}
STATE_NAME = "discovery-state.json"


def _home():
    return Path.home()


def _appdata():
    return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))


def _localappdata():
    return Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local"))


def running_processes():
    """Windows 自带 tasklist 取进程名快照（零第三方依赖）。"""
    if not util.IS_WINDOWS:
        return set(), "非 Windows，跳过进程检测"
    try:
        proc = subprocess.run(["tasklist.exe", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=20,
                              **util.spawn_kwargs())
        names = {row[0].strip().lower() for row in csv.reader(io.StringIO(proc.stdout))
                 if row and row[0].strip()}
        return names, ""
    except Exception as exc:  # noqa: BLE001
        return set(), repr(exc)


# ---------------- 数据源健康检查器 ----------------

def _chk_sqlite_sessions(path):
    try:
        con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        finally:
            con.close()
        need = {"id", "status", "created_at", "updated_at"}
        return (True, "SQLite sessions 表字段校验通过") if need.issubset(cols) \
            else (False, f"sessions 表字段不齐，缺 {sorted(need - cols)}")
    except Exception as exc:  # noqa: BLE001
        return False, f"SQLite 只读打开/解析失败: {exc!r}"


def _chk_jsonl_events(path):
    files = sorted(Path(path).rglob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)[:3]
    if not files:
        return False, "无 *.jsonl 会话记录"
    for f in files:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("type") == "message" and d.get("timestamp"):
                    return True, f"会话事件可读（{f.name}，含 message/timestamp）"
    return False, "jsonl 里解析不到 message/timestamp 事件"


def _chk_any_jsonl(path):
    files = [f for f in Path(path).rglob("*.jsonl") if f.stat().st_size > 0]
    return (True, f"发现 {len(files)} 个非空会话记录文件") if files \
        else (False, "无会话记录文件")


def _chk_structured_log(path):
    files = sorted(Path(path).glob("zwork-main-*.log"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return False, "无 zwork-main-*.log"
    with open(files[-1], "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            i = line.find("[")
            if i < 0:
                continue
            try:
                arr = json.loads(line[i:])
                for item in arr:
                    if isinstance(item, str) and '"event"' in item:
                        return True, f"结构化 event 日志可读（{files[-1].name}）"
            except (json.JSONDecodeError, ValueError):
                continue
    return False, "日志存在但解析不到 event 字段"


def _chk_text_log(path):
    logs = [f for f in Path(path).glob("*.log") if f.is_file()]
    return (True, f"文本日志存在（{len(logs)} 个，仅能判断活动迹象）") if logs \
        else (False, "无文本日志")


def _chk_execution_json(path):
    files = list(Path(path).glob("command-execution/*/execution.json"))
    if not files:
        return False, "command-execution 下无 execution.json"
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8", errors="replace"))
        if "startedAtMs" in d:
            return True, f"execution.json 可读（{len(files)} 条，含 startedAtMs/finishedAtMs）"
    except (OSError, json.JSONDecodeError):
        pass
    return False, "execution.json 结构不符合预期"


def _chk_private_db(path):
    size = Path(path).stat().st_size
    return False, (f"非 SQLite 私有/加密格式（{size/1024/1024:.0f} MB），"
                  "无法解析任务级状态，按优先级自动降级到其他源")


CHECKS = {
    "sqlite_sessions": _chk_sqlite_sessions,
    "jsonl_events": _chk_jsonl_events,
    "any_jsonl": _chk_any_jsonl,
    "structured_log": _chk_structured_log,
    "text_log": _chk_text_log,
    "execution_json": _chk_execution_json,
    "private_db": _chk_private_db,
}


# ---------------- 数据结构 ----------------

@dataclass
class SourceSpec:
    key: str
    label: str
    precision: int
    candidates: list
    check: str
    options_key: str = ""
    note: str = ""
    value_is_parent: bool = False


@dataclass
class SourceAttempt:
    key: str
    label: str
    precision: int
    precision_label: str
    path: str
    ok: bool
    detail: str
    role: str = ""            # selected / available / unavailable / companion


@dataclass
class ProbeResult:
    id: str
    name: str
    adapter_type: str
    installed: bool = False
    running: bool = False
    in_use: bool = False
    downgraded: bool = False
    version: str = "unknown"
    version_from: str = ""
    adapter_available: bool = True
    selected_source: str = ""
    sources: list = field(default_factory=list)
    data_sources: dict = field(default_factory=dict)
    health_detail: str = ""
    notes: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    adapter_spec: dict = field(default_factory=dict)


# ---------------- 探针 ----------------

class AgentProbe:
    id = ""
    name = ""
    adapter_type = ""
    process_names = ()
    adapter_available = True

    def sources(self):
        return []

    def companion_paths(self):
        return {}

    def version_files(self):
        return []

    def exe_files(self):
        return []

    def version(self):
        for p in self.version_files():
            if not p.exists():
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                v = (d.get("version") or d.get("build")) if isinstance(d, dict) else None
                if v:
                    return str(v), str(p)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        for p in self.exe_files():
            if p.exists():
                st = p.stat()
                return f"fp:{st.st_size}-{int(st.st_mtime)}", f"{p} (文件指纹)"
        return "unknown", ""

    def spec(self, found, selected, attempts):
        return {}

    def _fallbacks(self, attempts):
        return [a.key for a in attempts if a.role == "available"]


class WorkBuddyProbe(AgentProbe):
    id = "workbuddy"; name = "WorkBuddy"; adapter_type = "workbuddy_local"
    process_names = ("workbuddy.exe",)

    def sources(self):
        r = _home() / ".workbuddy"
        return [
            SourceSpec("db_sessions", "workbuddy.db : sessions 表", 4,
                       [r / "workbuddy.db"], "sqlite_sessions", "db_path",
                       "明确终态 working/completed/terminated"),
            SourceSpec("projects_jsonl", "projects/**/<sid>.jsonl 会话事件", 3,
                       [r / "projects"], "jsonl_events", "projects_dir",
                       "role=user / assistant 事件位置判定"),
            SourceSpec("app_logs", "logs/*.log 应用日志", 2,
                       [r / "logs"], "text_log", "logs_dir",
                       "仅能判断最近有无活动"),
        ]

    def companion_paths(self):
        r = _home() / ".workbuddy"
        return {"tasks": [r / "tasks"], "sessions": [r / "sessions"]}

    def version_files(self):
        return [_home() / ".workbuddy" / "last-launch.json"]

    def spec(self, found, selected, attempts):
        return {
            "type": self.adapter_type, "name": self.adapter_type, "enabled": True,
            "options": {
                "db_path": found.get("db_path"), "tasks_dir": found.get("tasks"),
                "projects_dir": found.get("projects_dir"), "sessions_dir": found.get("sessions"),
                "logs_dir": found.get("logs_dir"),
                # 与 config.real.json 保持一致：默认不把「已终态」和「queued=计划清单里
                # 尚未勾选开始」的待办子任务纳入清单，否则「自动发现并扫描」会把用户
                # 手工关掉的开关又硬编码回 True，僵尸待办重新卡住值守。
                "include_interactive": True, "include_completed_todos": False,
                "include_pending_todos": False,
                "idle_confirm_seconds": 300,
                "primary_source": selected.key if selected else "",
                "source_fallbacks": self._fallbacks(attempts),
            },
        }


class AutoClawProbe(AgentProbe):
    id = "autoclaw2"; name = "AutoClaw2 (智谱)"; adapter_type = "autoclaw_log"
    process_names = ("autoclaw2.exe", "autoclaw.exe")

    def sources(self):
        roots = [_appdata() / "AutoClaw-official" / "logs", _appdata() / "AutoClaw" / "logs"]
        return [
            SourceSpec("structured_log", "logs/zwork-main-*.log 结构化事件", 3,
                       roots, "structured_log", "logs_dir",
                       "tool.started/completed 事件位置判定"),
            SourceSpec("text_log", "logs/*.log 文本日志", 2,
                       roots, "text_log", "logs_dir", "仅活动迹象"),
        ]

    def version_files(self):
        return [_localappdata() / "Programs" / "AutoClaw2" / "resources" / "app" / "package.json"]

    def exe_files(self):
        return [_localappdata() / "Programs" / "AutoClaw2" / "AutoClaw2.exe"]

    def spec(self, found, selected, attempts):
        return {
            "type": self.adapter_type, "name": self.adapter_type, "enabled": True,
            "options": {
                "logs_dir": found.get("logs_dir"), "log_glob": "zwork-main-*.log",
                "lookback_days": 1, "conservative_session": True,
                "idle_confirm_seconds": 300,
                "primary_source": selected.key if selected else "",
                "source_fallbacks": self._fallbacks(attempts),
            },
        }


class TraeProbe(AgentProbe):
    id = "trae_cn"; name = "Trae CN AI Agent"; adapter_type = "trae_agent"
    process_names = ("trae cn.exe", "trae.exe")

    def sources(self):
        roots = [_appdata() / "Trae CN" / "ModularData" / "ai-agent",
                 _appdata() / "Trae" / "ModularData" / "ai-agent"]
        return [
            SourceSpec("private_db", "database.db 任务级私有库", 4,
                       [r / "database.db" for r in roots], "private_db", "data_dir",
                       "理论精度最高，但本机实测为私有/加密格式，解析不了", True),
            SourceSpec("execution_json", "command-execution/*/execution.json", 3,
                       roots, "execution_json", "data_dir",
                       "命令级 startedAtMs/finishedAtMs，粒度较细但可读"),
            SourceSpec("app_logs", "ModularData 日志目录", 2,
                       [r / "logs" for r in roots], "text_log", "logs_dir",
                       "仅活动迹象"),
        ]

    def version_files(self):
        return [_localappdata() / "Programs" / "Trae CN" / "resources" / "app" / "package.json"]

    def exe_files(self):
        return [_localappdata() / "Programs" / "Trae CN" / "Trae CN.exe"]

    def spec(self, found, selected, attempts):
        return {
            "type": self.adapter_type, "name": self.adapter_type, "enabled": True,
            "options": {
                "data_dir": found.get("data_dir"), "lookback_minutes": 120,
                "conservative_session": True, "idle_confirm_seconds": 300,
                "primary_source": selected.key if selected else "",
                "source_fallbacks": self._fallbacks(attempts),
            },
        }


class ClaudeCodeProbe(AgentProbe):
    id = "claude_code"; name = "Claude Code"; adapter_type = "claude_code"
    process_names = ("claude.exe",)
    adapter_available = False

    def sources(self):
        return [SourceSpec("projects_jsonl", "~/.claude/projects/**/*.jsonl", 3,
                           [_home() / ".claude" / "projects"], "any_jsonl",
                           "projects_dir", "会话记录事件")]

    def version_files(self):
        return [_appdata() / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json"]

    def spec(self, found, selected, attempts):
        return {"type": self.adapter_type, "name": self.adapter_type,
                "enabled": False, "options": {"projects_dir": found.get("projects_dir")}}


PROBES = [WorkBuddyProbe(), AutoClawProbe(), TraeProbe(), ClaudeCodeProbe()]


# ---------------- 状态持久化与变更对比 ----------------

def _state_path(state_dir):
    return Path(state_dir or util.abs_path("state")) / STATE_NAME


def _load_state(state_dir):
    return util.read_json(_state_path(state_dir), {}) or {}


def _snapshot_entry(r):
    return {"installed": r.installed, "running": r.running, "in_use": r.in_use,
            "version": r.version, "selected_source": r.selected_source,
            "adapter_type": r.adapter_type}


def _diff(prev, r):
    p = prev.get(r.id)
    changes = []
    if p is None:
        return ["新增安装"] if r.installed else []
    if p.get("installed") and not r.installed:
        changes.append("已卸载")
    if p.get("version") != r.version:
        changes.append(f"版本变化 {p.get('version')} -> {r.version}")
    if p.get("selected_source") != r.selected_source:
        changes.append(f"数据源变化 {p.get('selected_source') or '无'} -> {r.selected_source or '无'}")
    if p.get("in_use") and not r.in_use:
        changes.append("已停用（退出运行 / 无可用数据源）")
    if (not p.get("in_use")) and r.in_use:
        changes.append("开始使用")
    return changes


# ---------------- 主流程 ----------------

def discover(state_dir=None, persist=True):
    """纯代码自动发现。返回报告 dict。

    默认持久化到 state/discovery-state.json 以便下次做变更对比；
    除此之外不写任何文件、不修改任何 Agent 应用。
    """
    procs, proc_err = running_processes()
    state = _load_state(state_dir)
    prev = state.get("agents", {})
    baseline = not bool(prev)
    results = []

    for probe in PROBES:
        found, attempts = {}, []
        # ① 按精度降序逐个健康检查
        for spec in sorted(probe.sources(), key=lambda s: -s.precision):
            path = next((str(p) for p in spec.candidates if Path(p).exists()), "")
            if not path:
                attempts.append(SourceAttempt(spec.key, spec.label, spec.precision,
                                              PRECISION_LABEL.get(spec.precision, "?"), "",
                                              False, "路径不存在", "unavailable"))
                continue
            ok, detail = CHECKS[spec.check](path)
            found[spec.key] = path
            if spec.options_key:
                found[spec.options_key] = (str(Path(path).parent)
                                          if spec.value_is_parent else path)
            attempts.append(SourceAttempt(spec.key, spec.label, spec.precision,
                                          PRECISION_LABEL.get(spec.precision, "?"), path,
                                          ok, detail, ""))
        # ② 辅助路径（不参与精度竞争）
        for key, paths in probe.companion_paths().items():
            p = next((str(x) for x in paths if Path(x).exists()), "")
            if p:
                found[key] = p
                attempts.append(SourceAttempt(key, f"{key} 辅助路径", 0, "辅助", p,
                                              True, "辅助数据源，不参与精度竞争", "companion"))

        # ③ 选精度最高的可用源
        competing = [a for a in attempts if a.precision > 0]
        ok_list = [a for a in competing if a.ok]
        selected = ok_list[0] if ok_list else None
        for a in competing:
            a.role = "selected" if a is selected else ("available" if a.ok else "unavailable")

        installed = bool(found)
        running = any(n in procs for n in probe.process_names)
        in_use = bool(installed and running and probe.adapter_available and selected)
        version, version_from = probe.version()
        downgraded = bool(selected and any(
            a.precision > selected.precision and not a.ok for a in competing))
        notes = []
        if installed and not running:
            notes.append("已安装但当前无进程 -> 视为未在用，不显示/不启用")
        if not probe.adapter_available:
            notes.append("适配器未实现 -> 视为未在用，不显示/不启用")
        if not selected:
            notes.append("所有数据源健康检查均未通过 -> 不启用")
        for a in competing:
            if a.role == "unavailable":
                notes.append(f"降级原因 [{a.label}]: {a.detail}")

        stub = ProbeResult(id=probe.id, name=probe.name, adapter_type=probe.adapter_type,
                           installed=installed, running=running, in_use=in_use,
                           version=version,
                           selected_source=selected.key if selected else "")
        changes = [] if baseline else _diff(prev, stub)

        results.append(ProbeResult(
            id=probe.id, name=probe.name, adapter_type=probe.adapter_type,
            installed=installed, running=running, in_use=in_use, downgraded=downgraded,
            version=version, version_from=version_from,
            adapter_available=probe.adapter_available,
            selected_source=selected.key if selected else "",
            sources=[asdict(a) for a in attempts], data_sources=found,
            health_detail=selected.detail if selected else "无可用数据源",
            notes=notes, changes=changes,
            adapter_spec=probe.spec(found, selected, attempts)
            if (in_use and probe.adapter_available) else {},
        ))

    # ④ 上次存在、本次连探针都找不到的 Agent -> 视为卸载/移除
    current_ids = {r.id for r in results}
    orphan_changes = [
        {"id": pid, "name": pid, "items": ["已卸载/移除（上次发现存在，本次未检测到）"]}
        for pid, entry in prev.items()
        if pid not in current_ids and entry.get("installed")
    ]

    enabled = [r.adapter_spec for r in results if r.in_use and r.adapter_available]
    report = {
        "generated_at": util.iso(), "host": socket.gethostname(),
        "platform": "windows" if util.IS_WINDOWS else "other",
        "process_snapshot_count": len(procs), "process_error": proc_err,
        "baseline": baseline,
        "agents": [asdict(r) for r in results],
        "recommended_adapters": enabled,
        "in_use_ids": [r.id for r in results if r.in_use],
        "changes": ([{"id": r.id, "name": r.name, "items": r.changes}
                     for r in results if r.changes] + ([] if baseline else orphan_changes)),
        "summary": {
            "installed": sum(1 for r in results if r.installed),
            "running": sum(1 for r in results if r.running),
            "in_use": sum(1 for r in results if r.in_use),
            "enabled": len(enabled),
            "downgraded": sum(1 for r in results if r.downgraded),
        },
    }
    if persist:
        util.atomic_write_json(_state_path(state_dir), {
            "updated_at": report["generated_at"],
            "agents": {r.id: _snapshot_entry(r) for r in results},
        })
    return report


def build_config(report):
    from .config import DEFAULT_CONFIG
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["adapters"] = copy.deepcopy(report["recommended_adapters"])
    cfg["_comment"] = ("由 discover 自动生成；只包含在用的 Agent，数据源按精度择优/降级。"
                       "请人工核对后再用于夜间值守，默认 dry_run=true。")
    return cfg


def format_report(report, show_all=False):
    lines = []
    lines.append("=" * 82)
    lines.append("  本机 Agent 自动发现报告（纯代码探测）")
    lines.append(f"  时间: {report['generated_at']}   主机: {report['host']}   "
                 f"平台: {report['platform']}")
    lines.append(f"  进程快照 {report['process_snapshot_count']} 个"
                 + (f"  ({report['process_error']})" if report["process_error"] else ""))
    lines.append("=" * 82)

    if report["baseline"]:
        lines.append("【首次运行】已建立基线，本次不报变更；下次起会显示新增/卸载/版本/数据源变化。")
        lines.append("-" * 82)
    elif report["changes"]:
        lines.append("【本次变更】")
        for c in report["changes"]:
            lines.append(f"  * {c['name']}: " + "; ".join(c["items"]))
        lines.append("-" * 82)
    else:
        lines.append("【本次变更】无（与上次发现一致）")
        lines.append("-" * 82)

    shown = [a for a in report["agents"] if show_all or a["in_use"]]
    lines.append(f"【在用的 Agent】{len(report['in_use_ids'])} 个（默认只显示这些）")
    if not shown:
        lines.append("  (无)")
    for a in shown:
        tag = "在用" if a["in_use"] else "未在用"
        lines.append(f"  [{tag}] {a['name']}  ({a['id']})  版本 {a['version']}")
        if a["version_from"]:
            lines.append(f"        版本来源: {a['version_from']}")
        lines.append(f"        运行中={a['running']}  已安装={a['installed']}  "
                     f"适配器可用={a['adapter_available']}")
        lines.append(f"        健康检查: {a['health_detail']}")
        lines.append("        数据源优先级（按精度降序）:")
        for s in a["sources"]:
            if s["role"] == "companion":
                lines.append(f"          [辅助] {s['label']}: {s['path']}")
                continue
            mark = {"selected": "采用", "available": "备选",
                    "unavailable": "不可用", "": "-"}[s["role"]]
            lines.append(f"          [{mark:4s}] P{s['precision']} "
                         f"{PRECISION_LABEL.get(s['precision'], '?'):6s}  {s['label']}")
            if s["role"] == "unavailable":
                lines.append(f"                 原因: {s['detail']}")
        for n in a["notes"]:
            if not n.startswith("降级原因"):
                lines.append(f"        - {n}")
        lines.append("")
    hidden = len(report["agents"]) - len(shown)
    if hidden and not show_all:
        lines.append(f"  （另有 {hidden} 个已安装但未在用的 Agent 未显示；--all 可查看）")

    lines.append("-" * 82)
    s = report["summary"]
    lines.append(f"汇总: 已安装 {s['installed']} / 运行中 {s['running']} / "
                f"在用 {s['in_use']} / 自动启用适配器 {s['enabled']} / "
                f"发生降级 {s['downgraded']}")
    lines.append("说明: 只发现数据源、不判断任务、不生成快照、不决定睡眠；结果仍须人工核对。")
    lines.append("      仅写本工具 state/discovery-state.json 用于变更对比，不改任何 Agent 应用。")
    lines.append("=" * 82)
    return "\n".join(lines)
