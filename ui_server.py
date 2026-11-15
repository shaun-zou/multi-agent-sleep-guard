#!/usr/bin/env python
"""本地控制台 P0：只监听 127.0.0.1，零第三方依赖，数据不出本机。

架构要点：
  - 界面只是「观察窗 + 操作面板」，所有动作都复用已验证的 CLI 入口（留痕一致）。
  - monitor 跑在**独立进程**里，关闭浏览器/页面不会影响它；状态记录在
    state/ui-monitor.json。
  - 停止监控写 state/monitor.stop 标志，由 monitor 自己走 _abort 优雅收尾，
    绝不强杀进程、绝不截断日志、绝不发出睡眠命令。
  - 真实睡眠只需「开启监控」时的一次确认：后端下发一次性确认令牌（不落盘、
    用后即焚、有 TTL），校验通过才允许非演练。不再有持久「总闸」或演练开关，
    避免状态残留与误判；命令行 --dry-run 仍保留用于演练/回归自测。
"""
from __future__ import annotations

import json
import os
import re
import secrets
from fnmatch import fnmatch
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from asg import util  # noqa: E402
from asg.config import Config  # noqa: E402

PORT = int(os.environ.get("ASG_UI_PORT", "8765"))
CONFIG_NAME = "config.real.json"
# ROOT = 可写数据根（源码=项目目录 / 冻结=exe 所在目录），
# UI_DIR = 只读资源（源码=项目目录/ui / 冻结=_MEIPASS/ui）。两者只在 exe 里分叉。
ROOT = util.ROOT
CONFIG_PATH = str(ROOT / CONFIG_NAME)
UI_DIR = util.BUNDLE / "ui"
MONITOR_STATE = util.abs_path("state") / "ui-monitor.json"
STOP_FILE = util.abs_path("state") / "monitor.stop"
STATE_LOCK = threading.Lock()

# 独立前端工程的静态资源白名单：只暴露这三个文件，不接受任意路径（防目录穿越）
STATIC_FILES = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/api-contract.json": ("api-contract.json", "application/json; charset=utf-8"),
}

# ---- 一次性确认令牌 ----------------------------------------------------
# 只存在于服务进程内存：不落盘、用后即焚、有 TTL。取代旧的持久「真实睡眠总闸」。
# 界面点「开启监控」时弹一次确认，确认后带上令牌；后端校验通过才允许非演练。
_CONFIRM_TTL = 600.0            # 10 分钟；睡前刷新一次页面即可
_CONFIRM_LOCK = threading.Lock()
_CONFIRM_TOKENS = {}            # token -> 到期时间(monotonic)


def issue_confirm_token():
    """复用当前有效令牌，避免界面每 3 秒刷新就换一枚导致用户手里的令牌失效。

    令牌直到被消费或过期才轮换；多个标签页共用同一枚，谁先启动谁消费
    （本来也只允许一个监控实例），其余页面下次刷新自动拿到新令牌。
    """
    now = time.monotonic()
    with _CONFIRM_LOCK:
        for k, exp in list(_CONFIRM_TOKENS.items()):
            if exp <= now:
                _CONFIRM_TOKENS.pop(k, None)
        for existing, exp in _CONFIRM_TOKENS.items():
            if exp > now:
                return existing
        token = secrets.token_urlsafe(18)
        _CONFIRM_TOKENS[token] = now + _CONFIRM_TTL
    return token


def consume_confirm_token(token):
    """校验并立即作废；返回 (ok, reason)。任何非法/过期/复用都拒绝。"""
    if not token:
        return False, "缺少一次性确认令牌，请刷新页面后重新操作"
    now = time.monotonic()
    with _CONFIRM_LOCK:
        exp = _CONFIRM_TOKENS.pop(token, None)
    if exp is None:
        return False, "确认令牌无效或已被使用，请刷新页面后重新操作"
    if exp <= now:
        return False, "确认令牌已过期（10 分钟），请刷新页面后重新操作"
    return True, ""


def cli(args, timeout=180):
    """跑一次 CLI 子进程（源码 / 打包两种形态由 util.cli_argv 统一处理）。"""
    proc = subprocess.run(
        util.cli_argv(args), cwd=str(ROOT), capture_output=True,
        text=True, encoding="utf-8", timeout=timeout,
        **util.spawn_kwargs(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def ok(data=None, message=""):
    return {"ok": True, "data": data, "message": message}


def fail(message):
    return {"ok": False, "data": None, "message": str(message)[:800]}


def open_console(url=None, quiet=False):
    """打开控制台页面，**绝不静默失败**。

    背景（真机踩坑）：打包成 exe 后 webbrowser.open 可能拉不起浏览器——服务
    正常、控制台窗口也在，但用户就是看不到界面，而且 Timer 线程里的异常
    不会显示在任何地方。所以这里：① 每一步结果都打到控制台；② 失败就换
    **机制**重试（Edge/Chrome 的 --app 独立窗口走 CreateProcess，与
    webbrowser 的 ShellExecute 完全不同通路）；③ 全失败时把地址醒目留下；
    ④ 结果永远追加到 logs/ui-open.log —— 控制台看不见（比如被重定向/吞掉）
    时也能事后查证。
    quiet=True 供回归脚本使用：不弹窗、只返回是否成功。
    """
    url = url or f"http://127.0.0.1:{PORT}/"
    trace = []

    def say(msg, also_file=True):
        if not quiet:
            print(msg)
        if also_file:
            trace.append(msg)

    try:
        if webbrowser.open(url):
            say(f"[OK] 已在默认浏览器打开: {url}")
            _log_open_trace(url, trace, "webbrowser")
            return True
        say("[WARN] webbrowser.open 返回 False（默认浏览器没被拉起），换独立窗口重试…")
    except Exception as exc:  # noqa: BLE001
        say(f"[WARN] webbrowser.open 异常: {exc!r}，换独立窗口重试…")
    try:
        import desktop_launcher
        desktop_launcher.open_window()
        say(f"[OK] 已改用独立窗口打开: {url}")
        _log_open_trace(url, trace, "app-window")
        return True
    except Exception as exc:  # noqa: BLE001
        say(f"[WARN] 独立窗口也失败: {exc!r}")
    say("=" * 60)
    say("  浏览器没有自动打开。请手动在浏览器地址栏访问：")
    say(f"      {url}")
    say("=" * 60)
    _log_open_trace(url, trace, "failed")
    return False


def _log_open_trace(url, trace, outcome):
    """打开结果留痕。失败本身不能再失败：写不了日志就忽略。"""
    try:
        p = ROOT / "logs" / "ui-open.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{util.iso()} outcome={outcome} url={url}\n")
            for line in trace:
                f.write(f"    {line}\n")
    except OSError:
        pass


def list_snapshots():
    out = []
    for f in sorted((ROOT / "snapshots").glob("SNAP-*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        d = util.read_json(f, {}) or {}
        out.append({
            "snapshot_id": d.get("snapshot_id", f.stem),
            "version": d.get("version"),
            "source": d.get("source"),
            "task_count": d.get("task_count", len(d.get("tasks", []))),
            "verified": d.get("verified", False),
            "locked": d.get("locked", False),
            "status": d.get("status"),
            "retry_decision": (d.get("retry_risk") or {}).get("decision"),
        })
    return out


def load_snapshot(sid):
    if not sid or not re.match(r"^SNAP-[\w-]+$", sid):
        return None
    return util.read_json(ROOT / "snapshots" / f"{sid}.json")


def log_tail(limit=28):
    logs = ROOT / "logs"
    lines = []
    for name in ("events", "monitor", "sleep-sequence", "discover"):
        files = sorted(logs.glob(f"{name}-*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            lines += util.read_lines(files[0])[-12:]
    lines.sort()
    return lines[-limit:]


# ---- P1：界面内规则确认 ------------------------------------------------

# 与 CLI `rules --as` 的 choices 保持一致（单一事实来源在 cli.build_parser，
# 这里只做展示用清单；真正校验仍由 CLI 子进程完成）。
RULE_DECISIONS = ["start", "middle", "end", "running", "queued",
                  "completed", "failed", "cancelled", "ignore"]
# 终态判定要给用户看的风险提示（确认成这些值会直接改变"能不能睡"）
TERMINAL_DECISIONS = {"end", "completed", "failed", "cancelled"}


def _rulebook_patterns(book):
    """把规则表里的 ignore_patterns 摊平成界面可直接渲染/删除的条目。"""
    raw = getattr(book, "data", {}).get("ignore_patterns", {})
    out = []
    for adapter, pats in raw.items():
        for pat in pats or []:
            out.append({"adapter": adapter, "pattern": pat})
    return out


# ---- P1：日志检索 ------------------------------------------------------

# 只允许直接读 logs/ 目录下的 *.log 文件（纯文件名，无斜杠无点开头），
# 从根上挡住目录穿越与读任意文件。
_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+\.log$")
LOG_MAX_BYTES = 8 * 1024 * 1024      # 超过 8MB 的文件拒绝整读，提示用关键词收窄


def _logs_index():
    """logs/ 下可检索的日志清单（不含 archive-* 子目录），新文件在前。"""
    out = []
    for p in (ROOT / "logs").glob("*.log"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime)
                    .astimezone().isoformat(timespec="seconds"),
                    "lines": None})
    out.sort(key=lambda x: x["modified"], reverse=True)
    return {"files": out[:60], "total": len(out)}


# 任务时间线只认这几类事件；POLL/PROGRESS 没有 task_id= 字段，天然不会命中。
_TIMELINE_EVENTS = {"STATUS_CHANGE", "TASK_TERMINAL", "RETRY_CHAIN_NOTE"}
_TIMELINE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_TIMELINE_MAX_FILES = 30          # 只看最近 30 份 monitor 日志
_TIMELINE_MAX_ROWS = 200          # 返回上限，防止一条高频任务刷屏


def _parse_kv(text):
    """日志第四段是 `k=v k=v`，value 可能含空格，按 key 起点切分而不是 split。"""
    marks = list(re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", text))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[m.end():end].strip()
    return out


def _task_timeline(task_id, sid="", scope="snapshot"):
    """按 task_id 聚合监控运行期的状态轨迹。纯只读，不参与任何终态判定。

    默认 scope=snapshot：只看当前快照绑定的那份 monitor 日志。很多 task_id
    （如 trae-session:ai-agent）跨多轮值守复用，全量聚合会把不同日子的终态
    记录混成一条轨迹，看起来像「同一个任务终态了好几次」。
    scope=all 才跨值守展示，并回带 other_runs 供界面提示。
    """
    tid = str(task_id or "").strip()
    if not _TIMELINE_ID_RE.match(tid):
        raise ValueError("非法的 task_id（只允许字母数字与 _ . : -，长度 <= 200）")
    if scope not in ("snapshot", "all"):
        raise ValueError("scope 只能是 snapshot 或 all")
    baseline = None
    if sid:
        snap = load_snapshot(sid) or {}
        for t in snap.get("tasks", []) or []:
            if str(t.get("task_id")) == tid:
                baseline = {
                    "snapshot_id": snap.get("snapshot_id"),
                    "created_at": snap.get("created_at"),
                    "status": t.get("status"),
                    "start_time": t.get("start_time"),
                    "evidence": t.get("evidence"),
                    "agent": t.get("agent"),
                }
                break
    all_files = sorted((ROOT / "logs").glob("monitor-*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:_TIMELINE_MAX_FILES]
    scoped = (scope == "snapshot" and bool(sid)
              and bool(re.match(r"^SNAP-[\w-]+$", sid)))
    pattern = f"monitor-{sid}-*.log" if scoped else None
    files = [p for p in all_files if fnmatch(p.name, pattern)] if pattern else all_files
    needle = re.compile(r"(?:^|\s)task_id=" + re.escape(tid) + r"(?=\s|$)")
    events = []

    def _collect(paths):
        found = []
        for p in paths:
            try:
                if p.stat().st_size > LOG_MAX_BYTES:
                    continue
                lines = util.read_lines(p)
            except (OSError, ValueError):
                continue
            for line in lines:
                parts = line.split(" | ", 3)
                if len(parts) < 4:
                    continue
                ts, event, msg, kvpart = parts
                if event.strip().upper() not in _TIMELINE_EVENTS:
                    continue
                if not needle.search(kvpart):
                    continue
                kv = _parse_kv(kvpart)
                found.append({
                    "ts": ts.strip(), "event": event.strip().upper(), "msg": msg.strip(),
                    "from_status": kv.get("from_status"), "to_status": kv.get("to_status"),
                    "status": kv.get("status"), "evidence": kv.get("evidence"),
                    "agent": kv.get("agent"), "file": p.name,
                })
        return found

    events = _collect(files)
    other_runs = len(_collect([p for p in all_files if p not in files])) if scoped else 0
    events.sort(key=lambda e: e["ts"])
    total = len(events)
    return {
        "task_id": tid, "baseline": baseline, "events": events[:_TIMELINE_MAX_ROWS],
        "total": total, "truncated": total > _TIMELINE_MAX_ROWS,
        "files_scanned": len(files), "scope": scope, "snapshot_id": sid or None,
        "other_runs": other_runs,
        "note": "只读展示：数据来自 monitor-*.log 与扫描时冻结的快照；"
                "仅监控运行期间才会产生状态迁移记录，不参与终态判定、不回写快照",
    }


def _logs_read(name, keyword="", tail=200):
    """读单个日志文件。非法文件名/超大文件一律 ValueError，由调用方转 400。"""
    raw = str(name or "")
    if not _LOG_NAME_RE.match(raw):
        raise ValueError(f"非法的日志文件名: {raw!r}（只允许 logs/ 下的 *.log）")
    p = ROOT / "logs" / raw
    if not p.exists():
        raise ValueError(f"日志不存在: {raw}")
    if p.stat().st_size > LOG_MAX_BYTES:
        raise ValueError(f"文件超过 8MB，请用关键词过滤或用文本编辑器打开: {raw}")
    lines = util.read_lines(p)
    total = len(lines)
    if keyword:
        lines = [l for l in lines if keyword in l.lower()]
    matched = len(lines)
    shown = lines[-tail:]
    return {"name": raw, "total_lines": total, "matched": matched,
            "shown": len(shown), "truncated": matched > len(shown),
            "lines": shown, "keyword": keyword}


def state_payload():
    from asg import powercap
    cfg = Config(CONFIG_PATH)
    caps = powercap.load(cfg)
    if not caps:                      # 首次打开界面时探测一次，之后每 3 秒只读取缓存
        caps = powercap.probe()
        powercap.save(cfg, caps)
    prefer = cfg.prefer
    lock = util.read_json(util.abs_path("state") / "monitor.lock")
    intent = util.read_json(util.abs_path("state") / "sleep_intent.json")
    ms = util.read_json(MONITOR_STATE, {}) or {}
    pid = ms.get("pid")
    started_at = ms.get("started_at")
    # 必须校验进程身份（创建时间），否则系统回收并复用 PID 后，
    # 会把无关程序误判成"监控还在跑"，导致开启监控按钮被禁用。
    alive = bool(pid and util.pid_alive(pid, started_at))
    reused = bool(pid and util.pid_reused(pid, started_at))
    # 每次状态刷新下发一枚新的一次性确认令牌（旧令牌自然过期）。
    # 界面点「开启监控」确认后回传，后端校验通过才允许非演练。
    confirm_token = issue_confirm_token()
    age = powercap.age_seconds(caps)
    return {
        "dry_run": cfg.dry_run,
        "config": CONFIG_NAME,
        "confirm": {
            "token": confirm_token,
            "ttl_seconds": int(_CONFIRM_TTL),
            "note": "开启监控时确认一次即可；确认令牌用后即焚，不留持久开关",
        },
        "power": {"prefer": prefer, "capabilities": caps,
                  "options": powercap.options(caps),
                  "recommend": powercap.recommend(caps, prefer),
                  "stale": powercap.is_stale(caps),
                  "age_seconds": None if age is None else int(age),
                  # 监控跑起来后不允许再改待机方式：否则"这次到底按哪个模式"无法复盘
                  "locked_by_monitor": alive,
                  "effect": powercap.effect_note(prefer),
                  "hints": powercap.hints(prefer),
                  "summary": powercap.short_summary(caps, prefer)},
        "snapshots": list_snapshots()[:12],
        "lock": lock,
        "monitor": {"running": alive, "pid": pid, "pid_reused": reused,
                    "snapshot_id": ms.get("snapshot_id"),
                    "started_at": ms.get("started_at"),
                    "dry_run": ms.get("dry_run"),
                    "requested_mode": ms.get("requested_mode"),
                    "note": (f"检测到 PID {pid} 已被其他程序复用（原监控 {started_at} 已退出），"
                             "状态已按未运行处理，可直接重新开启监控" if reused else "")},
        "sleep_intent": ({"snapshot_id": intent.get("snapshot_id"),
                          "dry_run": intent.get("dry_run"),
                          "command_issued_at": intent.get("command_issued_at")}
                         if intent else None),
        "events": log_tail(),
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        p = UI_DIR / "index.html"
        if not p.exists():
            return self._json(fail(f"缺少界面文件 {p}"), 500)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name, ctype):
        p = UI_DIR / name
        if not p.exists():
            return self._json(fail(f"缺少前端资源 {p}"), 404)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, *args):
        return

    # ---------------- GET ----------------

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._html()
            if u.path in STATIC_FILES:
                name, ctype = STATIC_FILES[u.path]
                return self._static(name, ctype)
            if u.path == "/api/state":
                return self._json(ok(state_payload()))
            if u.path == "/api/snapshots":
                return self._json(ok(list_snapshots()))
            if u.path == "/api/snapshot":
                sid = (q.get("id") or [""])[0]
                d = load_snapshot(sid)
                return self._json(ok(d) if d else fail(f"快照不存在: {sid}"))
            if u.path == "/api/discover":
                from asg import discovery
                rep = discovery.discover(persist=False)
                return self._json(ok({
                    "summary": rep["summary"], "in_use_ids": rep["in_use_ids"],
                    "changes": rep["changes"],
                    "text": discovery.format_report(rep),
                }))
            if u.path == "/api/rules":
                from asg import rules
                book = rules.get_rulebook()
                d = book.draft(None)
                return self._json(ok({
                    "fingerprint": book.version_fingerprint(),
                    "pending": d["pending"][:20],
                    "pending_total": len(d["pending"]),
                    "classified_events": d["classified_events"],
                    "observed_keys": d["observed_keys"],
                    # P1：界面内规则确认需要的数据（原来只能看 CLI）
                    "ignore_patterns": _rulebook_patterns(book),
                    "decisions": RULE_DECISIONS,
                }))
            if u.path == "/api/logs":
                return self._json(ok(_logs_index()))
            if u.path == "/api/logs/read":
                name = (q.get("file") or [""])[0]
                kw = (q.get("q") or [""])[0].strip().lower()
                try:
                    tail = max(50, min(2000, int((q.get("tail") or [200])[0])))
                except ValueError:
                    tail = 200
                try:
                    return self._json(ok(_logs_read(name, kw, tail)))
                except ValueError as exc:
                    return self._json(fail(str(exc)), 400)
            if u.path == "/api/task-timeline":
                tid = (q.get("task_id") or [""])[0]
                sid = (q.get("snapshot") or [""])[0]
                scope = (q.get("scope") or ["snapshot"])[0]
                try:
                    return self._json(ok(_task_timeline(tid, sid, scope)))
                except ValueError as exc:
                    return self._json(fail(str(exc)), 400)
            if u.path == "/api/privacy":
                from asg import privacy
                cfg = Config(CONFIG_PATH)
                rep = privacy.report(cfg)
                return self._json(ok({"report": rep, "text": privacy.format_text(rep),
                                      "purge_preview": privacy.purge_preview(cfg)}))
            if u.path == "/api/cases":
                from asg import cases as cases_mod
                d = cases_mod.load()
                return self._json(ok({"count": len(d["cases"]), "text": cases_mod.format_text(d),
                                      "cases": d["cases"]}))
            if u.path == "/api/metrics":
                from asg import metrics as metrics_mod
                cfg = Config(CONFIG_PATH)
                m = metrics_mod.collect(cfg, days=int((parse_qs(u.query).get("days") or [7])[0]))
                return self._json(ok({**m, "text": metrics_mod.format_text(m)}))
            return self._json(fail("未知接口"), 404)
        except Exception as exc:  # noqa: BLE001
            return self._json(fail(repr(exc)), 500)

    # ---------------- POST ----------------

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        with STATE_LOCK:
            try:
                return self._dispatch(u.path, body)
            except Exception as exc:  # noqa: BLE001
                return self._json(fail(repr(exc)), 500)

    def _dispatch(self, path, b):
        if path == "/api/privacy/purge":
            from asg import privacy
            from asg import events
            cfg = Config(CONFIG_PATH)
            if not str(b.get("confirm") or "") == "PURGE":
                return self._json(fail("需要显式确认词 PURGE 才会归档清空生成物"))
            try:
                result = privacy.purge(cfg, str(b.get("operator") or "ui"))
            except RuntimeError as exc:
                return self._json(fail(str(exc)))
            events.EventLog(cfg).emit("PRIVACY_PURGE", result["message"],
                                      operator=str(b.get("operator") or "ui"),
                                      archived_files=result["archived_files"])
            return self._json(ok(result), message=result["message"])

        if path == "/api/powercap":
            from asg import powercap
            from asg import events
            cfg = Config(CONFIG_PATH)
            caps = powercap.probe()
            powercap.save(cfg, caps)
            prefer = cfg.prefer
            events.EventLog(cfg).emit(
                "POWER_CAPABILITIES", "界面手动刷新电源能力",
                requested_mode=prefer,
                sleep_available=caps.get("sleep_available"),
                hibernate_available=caps.get("hibernate_available"),
                sleep_kind=caps.get("sleep_kind"))
            return self._json(ok({
                "capabilities": caps,
                "options": powercap.options(caps),
                "recommend": powercap.recommend(caps, prefer),
                "summary": powercap.short_summary(caps, prefer),
                "effect": powercap.effect_note(prefer),
                "hints": powercap.hints(prefer),
            }))

        if path == "/api/sleep/now":
            """界面手动直接睡眠：不经过监控 / 终态判定，确认后立刻发一条命令。

            安全护栏与「开启监控」同级，不因为是手动入口就降低：
              1) 必须带一次性确认令牌（进程内存、TTL 10 分钟、用后即焚）；
              2) 已有监控在运行时拒绝，避免与自动值守流程互相干扰；
              3) 只发一条、不补发、不改系统电源设置。
            意图基线带 source=manual 且不绑定快照，selfcheck 不会把这次
            手动睡眠拿去查「快照完成状态」而误报事故；效果判定仍可算挂起时长。
            dry_run=true 仅供回归演练：只校验、不写任何文件、不发命令。
            """
            from asg import sleepctl, events, powercap
            okc, why = consume_confirm_token(str(b.get("confirm_token") or ""))
            if not okc:
                return self._json(fail(why))
            ms = util.read_json(MONITOR_STATE, {}) or {}
            if ms.get("pid") and util.pid_alive(ms["pid"], ms.get("started_at")):
                return self._json(fail("已有监控在运行，请先「优雅停止」再直接睡眠"))
            cfg = Config(CONFIG_PATH)
            mode, cmd = sleepctl.choose_command(cfg)
            if bool(b.get("dry_run", False)):
                return self._json(ok({"dry_run": True, "mode": mode,
                                      "command": " ".join(str(c) for c in cmd)[:160]},
                                     "演练：令牌已校验，未写留痕、未发命令"))
            operator = str(b.get("operator") or "ui")
            at = util.iso()
            caps = powercap.load(cfg) or {}
            sleepctl.write_intent(
                cfg, "", 0, False,
                "界面手动直接睡眠（不绑定快照，未经过终态判定与稳定复核）",
                at, cmd, requested_mode=mode, power_capabilities=caps,
                effect_note=powercap.effect_note(mode), source="manual")
            events.EventLog(cfg).emit(
                "SLEEP_MANUAL_ISSUED", "界面手动直接睡眠：确认后发出一条待机命令",
                operator=operator, requested_mode=mode, source="manual")
            issued, detail = sleepctl.issue_sleep(cfg)
            if not issued:
                return self._json(fail(f"睡眠命令派发失败: {detail}"))
            return self._json(ok(
                {"pid": detail, "mode": mode, "requested_mode": mode,
                 "dry_run": False, "source": "manual"},
                f"已按 {mode} 发出一条命令（pid={detail}）；只发一条、不补发"))

        if path == "/api/sleep/preference":
            """设置监控结束后请求的待机方式（只影响「发哪一条命令」）。

            三条硬规则：
              1. 监控运行时拒绝 —— 否则"这一次到底按哪个模式睡"无法复盘；
              2. 只接受 sleep / hibernate；
              3. 目标不可用且另一种可用时拒绝并给出建议（除非显式 force），
                 因为让用户保存一个本机做不到的选择，等于让他以为今晚会睡。
            """
            from asg import powercap
            from asg import events
            cfg = Config(CONFIG_PATH)
            target = str(b.get("prefer") or "").strip().lower()
            if target not in ("sleep", "hibernate"):
                return self._json(fail("待机方式只能是 sleep（睡眠）或 hibernate（休眠）"))
            force = bool(b.get("force", False))

            ms = util.read_json(MONITOR_STATE, {}) or {}
            if ms.get("pid") and util.pid_alive(ms["pid"], ms.get("started_at")):
                return self._json(fail(
                    "监控正在运行，不能修改待机方式（否则无法复盘这一次到底按哪个模式睡）。"
                    "请先优雅停止监控，改完再重新开启。"))

            caps = powercap.load(cfg)
            if not caps:
                caps = powercap.probe()
                powercap.save(cfg, caps)
            opts = powercap.option_map(caps)
            chosen = opts.get(target) or {}
            if chosen.get("available") is False:
                others = [o for o in opts.values()
                          if o["value"] != target and o.get("available")]
                if others and not force:
                    return self._json(fail(
                        f"{chosen.get('label')} 在本机不可用（{chosen.get('reason')}）。"
                        f"建议改为「{others[0]['label']}」；"
                        f"如仍要保存，请显式传 force=true。"))
            if cfg.prefer == target:
                return self._json(ok({"prefer": target, "old": target,
                                      "recommend": powercap.recommend(caps, target),
                                      "options": powercap.options(caps)},
                                     message=f"已经是「{chosen.get('label') or target}」，"
                                             f"无需重复保存"))
            old, new = cfg.set_prefer(target)
            events.EventLog(cfg).emit(
                "SLEEP_PREFERENCE_CHANGED",
                f"待机方式 {old} -> {new}（只影响发出哪一条命令，不改变只发一条/不补发）",
                operator=str(b.get("operator") or "ui"),
                old=old, new=new, forced=force,
                target_available=chosen.get("available"),
                advice=powercap.recommend(caps, new).get("text"))
            warn = powercap.recommend(caps, new)
            msg = f"已保存：任务全部终结后将请求「{chosen.get('label') or new}」"
            if warn.get("level") in ("switch", "none"):
                msg += f"；注意：{warn['text']}"
            return self._json(ok({"prefer": new, "old": old,
                                  "recommend": warn,
                                  "options": powercap.options(caps)},
                                 message=msg))

        if path == "/api/scan":
            args = ["scan", "--config", CONFIG_PATH]
            if b.get("auto_discover"):
                args.append("--auto-discover")
            rc, out, err = cli(args)
            if rc != 0:
                return self._json(fail(err or out))
            m = re.search(r"快照 ID\s*:\s*(\S+)", out)
            return self._json(ok({"snapshot_id": m.group(1) if m else None,
                                 "output": out}))

        if path == "/api/revise":
            sid = b.get("snapshot", "")
            removed = [str(x) for x in b.get("removed", [])]
            if not removed:
                return self._json(ok({"snapshot_id": sid, "skipped": True,
                                     "message": "清单无变化，不生成新版本"}))
            args = ["scan", "revise", "--config", CONFIG_PATH, "--snapshot", sid]
            for t in removed:
                args += ["--remove", t]
            args += ["--reason", str(b.get("reason") or "界面人工核对剔除"),
                     "--operator", str(b.get("operator") or "ui")]
            rc, out, err = cli(args)
            if rc != 0:
                return self._json(fail(err or out))
            m = re.search(r"-> (SNAP-\S+) \(", out)
            return self._json(ok({"snapshot_id": m.group(1) if m else sid,
                                 "output": out}))

        if path == "/api/verify":
            sid = b.get("snapshot", "")
            # 界面主路径用隐式默认，不再要求人工选重试风险；
            # 高级/CLI 仍可通过 decision 显式指定（走 CLI 的三选一）。
            choice = b.get("decision") or "reuse"
            mapping = {
                "reuse": ("true", "no_action"),
                "accept": ("false", "accept_untracked_retry"),
                "watch": ("false", "watch_retry_chain"),
            }
            if choice not in mapping:
                return self._json(fail("无效的重试风险决定"))
            args = ["scan", "verify", "--config", CONFIG_PATH, "--snapshot", sid]
            if choice == "reuse":
                args.append("--implicit")
            else:
                rid, dec = mapping[choice]
                args += ["--retry-reuses-id", rid, "--decision", dec]
                for t in b.get("watch_tasks", []):
                    args += ["--watch-task", str(t)]
            args += ["--operator", str(b.get("operator") or "ui")]
            rc, out, err = cli(args)
            return self._json(ok({"snapshot_id": sid, "output": out})
                              if rc == 0 else fail(err or out))

        if path == "/api/monitor/start":
            sid = b.get("snapshot", "")
            if not load_snapshot(sid):
                return self._json(fail(f"快照不存在: {sid}"))
            snap = load_snapshot(sid)
            if not snap.get("verified") or not snap.get("locked"):
                return self._json(fail("该快照未经人工核对锁定，拒绝启动监控"))
            ms = util.read_json(MONITOR_STATE, {}) or {}
            if ms.get("pid") and util.pid_alive(ms["pid"], ms.get("started_at")):
                return self._json(fail(f"已有监控实例在运行 (pid={ms['pid']})"))

            # 界面主路径 = 真实值守，只要求在「开启监控」时确认一次。
            # 演练仅由自动化/回归显式请求（dry_run=true），不需要确认令牌。
            dry = bool(b.get("dry_run", False))
            operator = str(b.get("operator") or "ui")
            cfg_now = Config(CONFIG_PATH)
            requested_mode = cfg_now.prefer
            from asg import events
            # 先验令牌再复核：避免未授权请求也能触发一次 powercfg 探测。
            if dry:
                confirmed_at = None
            else:
                okc, why = consume_confirm_token(str(b.get("confirm_token") or ""))
                if not okc:
                    return self._json(fail(why))
                confirmed_at = util.iso()
            # 启动前做「睡前最后一次可用性复核」：探测结果过期（默认 6h）就重探一次。
            # 目的不是拦截，而是别让用户拿着 3 天前的结论去睡 —— 系统更新/组策略变更
            # 都可能让 S3 突然不可用。这里只提示与留痕，不改发出的命令。
            from asg import powercap
            caps = powercap.load(cfg_now)
            reprobed = False
            if not caps or powercap.is_stale(caps):
                caps = powercap.probe()
                powercap.save(cfg_now, caps)
                reprobed = True
            chosen = powercap.option_map(caps).get(requested_mode) or {}
            mode_warn = ""
            if chosen.get("available") is False:
                mode_warn = (f"；注意：{chosen.get('label')} 在本机不可用"
                             f"（{chosen.get('reason')}），今晚可能不会进入低功耗")
                events.EventLog(cfg_now).emit(
                    "SLEEP_MODE_UNAVAILABLE",
                    "开启监控时复核发现所选待机方式不可用，仍按用户确认继续",
                    snapshot_id=sid, requested_mode=requested_mode,
                    reason=str(chosen.get("reason") or "")[:120],
                    operator=operator)
            if not dry:
                events.EventLog(cfg_now).emit(
                    "MONITOR_CONFIRMED",
                    "开启监控时已完成一次确认；任务全部终结后将真的睡眠/休眠",
                    snapshot_id=sid, task_count=snap.get("task_count", 0),
                    operator=operator, mode="real",
                    requested_mode=requested_mode, reprobed=reprobed)
            # 启动前由界面清理残留标志，避免子进程启动慢时把新的停止请求误删
            try:
                STOP_FILE.unlink()
            except OSError:
                pass
            args = util.cli_argv(["monitor", "--config", CONFIG_PATH,
                                  "--snapshot", sid,
                                  "--dry-run" if dry else "--no-dry-run"])
            logf = open(ROOT / "logs" / "ui-monitor-stdout.log", "a", encoding="utf-8")
            proc = subprocess.Popen(args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT,
                                    **util.spawn_kwargs())
            util.atomic_write_json(MONITOR_STATE, {
                "pid": proc.pid, "snapshot_id": sid,
                "started_at": util.iso(), "dry_run": dry,
                "requested_dry_run": dry, "forced_dry_run": False,
                "requested_mode": requested_mode,
                "confirmed_at": confirmed_at, "operator": operator,
            })
            msg = ("演练模式：不会真睡（仅供自动化/回归使用）。" if dry else
                   f"已完成一次确认：任务全部完成并连续稳定后，电脑会真的请求"
                   f"「{'睡眠' if requested_mode == 'sleep' else '休眠'}」。") + mode_warn
            return self._json(ok({"pid": proc.pid, "snapshot_id": sid,
                                 "dry_run": dry, "forced_dry_run": False,
                                 "requested_mode": requested_mode,
                                 "reprobed": reprobed,
                                 "mode_available": chosen.get("available"),
                                 "confirmed_at": confirmed_at}, message=msg))

        if path == "/api/monitor/stop":
            ms = util.read_json(MONITOR_STATE, {}) or {}
            pid = ms.get("pid")
            started_at = ms.get("started_at")
            if not pid or not util.pid_alive(pid, started_at):
                note = ("（上次监控的 PID 已被其他程序复用，视为已退出）"
                        if pid and util.pid_reused(pid, started_at) else "")
                return self._json(ok({"stopped": True,
                                     "message": f"当前没有运行中的监控{note}"}))
            # 每轮重写标志：防止子进程启动阶段的清理逻辑把它删掉（启动竞态）
            for _ in range(60):
                if not util.pid_alive(pid, started_at):
                    break
                util.atomic_write_json(STOP_FILE, {
                    "requested_at": util.iso(), "reason": "界面请求优雅停止",
                    "target_pid": pid,
                })
                time.sleep(0.5)
            stopped = not util.pid_alive(pid, started_at)
            return self._json(ok({"stopped": stopped,
                                 "message": "已优雅停止，未发出睡眠命令" if stopped
                                 else "已发出停止请求，进程尚未退出"}))

        if path == "/api/selfcheck":
            rc, out, err = cli(["selfcheck", "--config", CONFIG_PATH])
            return self._json(ok({"report": out}) if rc == 0 else fail(err or out))

        if path == "/api/rules/sample":
            from asg import rules
            cfg = Config(CONFIG_PATH)
            rules.sample(cfg)
            return self._json(ok({"fingerprint": rules.get_rulebook()
                                  .version_fingerprint()}))

        # ---- P1：界面内规则确认（全部复用 CLI 子进程，留痕与命令行完全一致）----

        if path == "/api/rules/approve":
            """逐条确认草案。语义决定权在人：终态判定会改变"能不能睡"，必须弹窗警示。"""
            adapter = str(b.get("adapter") or "").strip()
            key = str(b.get("key") or "").strip()
            decision = str(b.get("decision") or "").strip()
            operator = str(b.get("operator") or "ui")
            # key 里本来就有冒号（status:failed / event:zwork.*），
            # 所以只挡 adapter 里的冒号与两者里的空白
            if not adapter or ":" in adapter or any(c.isspace() for c in adapter + key):
                return self._json(fail("adapter/key 格式非法（adapter 不能含冒号，两者不能含空白）"))
            if not key:
                return self._json(fail("缺少 key"))
            if decision not in RULE_DECISIONS:
                return self._json(fail(f"无效判定: {decision!r}；可选 {', '.join(RULE_DECISIONS)}"))
            rc, out, err = cli(["rules", "--config", CONFIG_PATH,
                                "--approve", f"{adapter}:{key}",
                                "--as", decision, "--operator", operator])
            if rc != 0:
                return self._json(fail((err or out).strip()[:400]))
            warn = ("；注意：该值已被判为任务结束，若实际仍在运行会提前判完成、"
                    "存在提前睡眠风险" if decision in TERMINAL_DECISIONS
                   else "；该事件已按噪声忽略，不再参与判定" if decision == "ignore" else "")
            return self._json(ok({"adapter": adapter, "key": key,
                                  "decision": decision},
                                 message=f"[已确认] {adapter}:{key} -> {decision}{warn}"))

        if path == "/api/rules/approve-noise":
            """批量确认建议为噪声(ignore)的草案；其余仍需逐条判断。"""
            operator = str(b.get("operator") or "ui")
            rc, out, err = cli(["rules", "--config", CONFIG_PATH,
                                "--approve-noise", "--operator", operator])
            if rc != 0:
                return self._json(fail((err or out).strip()[:400]))
            return self._json(ok({"output": out}, message=out.strip()))

        if path == "/api/rules/pattern":
            """前缀忽略规则增删（add/remove 二选一）。"""
            action = str(b.get("action") or "").strip()
            adapter = str(b.get("adapter") or "").strip()
            pattern = str(b.get("pattern") or "").strip()
            operator = str(b.get("operator") or "ui")
            if action not in ("add", "remove"):
                return self._json(fail("action 只能是 add 或 remove"))
            if not adapter or not pattern or " " in pattern:
                return self._json(fail("需要 adapter 与 pattern（pattern 不能含空格）"))
            flag = "--add-ignore-pattern" if action == "add" else "--remove-ignore-pattern"
            rc, out, err = cli(["rules", "--config", CONFIG_PATH, flag, pattern,
                                "--adapter", adapter, "--operator", operator])
            if rc != 0:
                return self._json(fail((err or out).strip()[:400]))
            verb = "已新增" if action == "add" else "已移除"
            return self._json(ok({"adapter": adapter, "pattern": pattern,
                                  "action": action},
                                 message=f"{verb} {adapter}: {pattern}（运行中的监控 5 秒内热重载）"))

        return self._json(fail("未知接口"), 404)


def port_open():
    import socket
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    if not (UI_DIR / "index.html").exists():
        print(f"[ERROR] 缺少界面文件: {UI_DIR / 'index.html'}")
        return 1
    url = f"http://127.0.0.1:{PORT}/"
    # 与桌面入口对称：谁先启动谁承载服务，后启动的只打开自己的网页/窗口
    if port_open():
        print("=" * 60)
        print("  检测到已有本机服务在运行，仅打开网页版")
        print(f"  地址: {url}  （Web 版与桌面版共用同一个服务与同一份前端）")
        print("=" * 60)
        if not os.environ.get("ASG_UI_NO_BROWSER"):
            open_console(url)
        return 0
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        print(f"[ERROR] 端口 {PORT} 不可用: {exc}；可设置 ASG_UI_PORT 换端口")
        return 1
    print("=" * 60)
    print("  夜间值守控制台已启动")
    print(f"  地址: {url}")
    print("  只监听本机；关闭浏览器不影响监控进程")
    print("  按 Ctrl+C 关闭控制台服务")
    print("=" * 60)
    if not os.environ.get("ASG_UI_NO_BROWSER"):
        threading.Timer(0.8, lambda: open_console(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台服务已关闭（监控进程不受影响）")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
