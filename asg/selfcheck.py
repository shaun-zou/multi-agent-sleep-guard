"""唤醒后自检。

检查项:
  1. "即将睡眠"(STEP1) 与 "已发出"(STEP5) 日志是否成对存在
  2. 终态收尾是否严格按 STEP1->2->3->4->5 顺序执行
  3. 是否有遗留锁文件 / 监控是否中途死亡
  4. 快照是否已标记完成
  5. 是否存在"命令已发出但系统未睡"的迹象
  6. 监控存活心跳是否连续 (识别"监控挂了 -> 永远不睡且无人知晓")
     注意：只判同一次监控内的间隔与"该收尾却没收到"；跨次空档不计入。
  7. 快照版本一致性 (核对的 A 版 vs 监控跑的 B 版)
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime

from . import sleepctl, util

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

POWER_EVENT_PS = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "Get-WinEvent -FilterHashtable @{LogName='System'; "
    "StartTime=(Get-Date).AddDays(-2); Id=42,107} -MaxEvents 100 | "
    "ForEach-Object { \"$($_.TimeCreated.ToString('o'))|$($_.Id)|$($_.ProviderName)\" }"
)


def _parse_dt(text):
    try:
        return datetime.fromisoformat(text.strip())
    except (ValueError, TypeError):
        return None


def _seq_records(cfg):
    records = []
    for path in sleepctl.seq_paths(cfg):
        for line in util.read_lines(path):
            parts = line.split(" | ")
            if len(parts) < 2:
                continue
            dt = _parse_dt(parts[0])
            step = parts[1]
            sid = ""
            for p in parts[2:]:
                if p.startswith("snapshot_id="):
                    sid = p.split("=", 1)[1]
                    break
            records.append({"dt": dt, "step": step, "snapshot_id": sid,
                            "line": line, "path": str(path)})
    return records


def _snapshot_of(record):
    return record["snapshot_id"]


def _naive(dt):
    """统一转为本地墙上时间的 naive datetime, 避免 aware/naive 比较抛异常。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _query_power_events(issued_dt):
    """读取 System 日志中的 Kernel-Power 42 / 107 事件。

    注意：107「从睡眠恢复」在休眠(hibernate)恢复时也会产生，
    因此这些事件只能佐证"进出低功耗"，**不能区分 sleep 与 hibernate**。
    """
    issued_naive = _naive(issued_dt)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWER_EVENT_PS],
            capture_output=True, text=True, timeout=25,
            **util.spawn_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return False, [], f"事件日志读取失败: {exc!r}"
    if proc.returncode != 0 and not proc.stdout.strip():
        return False, [], (proc.stderr or "无输出").strip()[:200]
    events = []
    for line in proc.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) < 3:
            continue
        dt = _parse_dt(fields[0])
        if dt is None:
            continue
        dt_naive = _naive(dt)
        if issued_naive is not None and dt_naive is not None and dt_naive < issued_naive:
            continue
        if "Kernel-Power" in fields[2]:
            events.append({"dt": dt, "id": fields[1], "provider": fields[2]})
    return True, events, ""


RUN_FINISH_EVENTS = ("PRE_SLEEP", "MONITOR_ABORTED")


def _monitor_runs(cfg):
    """把 monitor-*.log 归并成「一次监控」，供心跳连续性按次判定。

    旧实现把所有 monitor-*.log 的心跳揉进一条时间线，于是把
    「上一次监控结束 -> 下一次监控开始」之间的间隔也算成断档。
    但那个间隔里本来就不该有心跳：监控进程在 STEP2 已经 join 退出，
    睡眠期间到次日再开监控之前，没有任何进程会写心跳。
    该间隔只反映人的作息，不代表监控死亡 —— 这是"断档时间比睡眠时间短"
    却仍被判 WARN 的全部原因（跨次间隔 = 两次值守之间的空档）。
    """
    runs = []
    for path in sorted(cfg.dir("logs_dir").glob("monitor-*.log")):
        cur = None
        for line in util.read_lines(path):
            parts = line.split(" | ")
            if len(parts) < 2:
                continue
            dt = _parse_dt(parts[0])
            if dt is None:
                continue
            if cur is None:
                cur = {"file": path.name, "sid": None, "stamps": [],
                       "first": dt, "last": dt, "finished": False,
                       "last_event": parts[1].strip()}
            cur["last"] = max(cur["last"], dt)
            if not cur["sid"]:
                # kv 段的多个字段用空格拼在同一节里，必须用正则只取 ID 本身
                m = re.search(r"snapshot_id=(SNAP-[^\s|]+)", line)
                if m:
                    cur["sid"] = m.group(1)
            level = parts[1].strip()
            if level == "HEARTBEAT":
                cur["stamps"].append(dt)
            if level in RUN_FINISH_EVENTS:
                cur["finished"] = True
            cur["last_event"] = level
        if cur:
            cur["stamps"].sort()
            runs.append(cur)
    return runs


def _heartbeat_report(cfg):
    """只判「同一次监控内」的心跳间隔，另判「该收尾却没收到」。

    两条判据分工：
      a) 同一次监控内相邻心跳间隔 > 允许值  -> 监控中途卡死/被挂起；
      b) 某次监控既没有收尾事件(PRE_SLEEP/MONITOR_ABORTED)，
         又已经静默超过允许值          -> 监控进程已经死了、且没人知道。
    跨次间隔（上一次结束到下一次开始）不参与判定，那是人的作息，不是故障。
    """
    runs = _monitor_runs(cfg)
    if not runs:
        return INFO, "没有监控日志记录，尚未开始值守，无法判断心跳连续性", []

    limit = int(cfg["selfcheck"]["heartbeat_max_gap_seconds"])
    now = datetime.now()
    total_stamps = 0
    evaluated = 0
    worst_gap = 0.0
    worst_sid = None
    hard_gaps = []
    stalled = []

    for run in runs:
        total_stamps += len(run["stamps"])
        gaps = [(b - a).total_seconds()
                for a, b in zip(run["stamps"], run["stamps"][1:])]
        if gaps:
            evaluated += 1
            g = max(gaps)
            if g > worst_gap:
                worst_gap, worst_sid = g, run["sid"] or run["file"]
            if g > limit:
                hard_gaps.append((run, g))
        if not run["finished"]:
            last = _naive(run["last"])
            idle = (now - last).total_seconds() if last else 0.0
            if idle > limit:
                stalled.append((run, idle))

    span = f"{len(runs)} 次监控 / 心跳 {total_stamps} 条"
    if hard_gaps:
        run, g = max(hard_gaps, key=lambda x: x[1])
        return (WARN,
                f"同一次监控内出现心跳断档: {run['sid'] or run['file']} 最大间隔 "
                f"{g/60:.1f} 分钟 > 允许 {limit/60:.1f} 分钟（{span}）; "
                f"监控可能中途卡死或进程被挂起", [g])

    if stalled:
        run, idle = max(stalled, key=lambda x: x[1])
        return (WARN,
                f"监控未收尾且已静默 {idle/60:.1f} 分钟: {run['sid'] or run['file']} "
                f"最后一条是 {run['last_event']}（{span}）; 监控可能已死亡、"
                f"永远不会触发睡眠", [idle])

    if evaluated == 0:
        return (INFO,
                f"心跳记录 {total_stamps} 条（{span}），每次监控都没超过两条，"
                f"无法判断间隔；短时监控属正常，整夜值守才需要连续心跳", [])

    return (PASS,
            f"心跳连续（{span}，可判定 {evaluated} 次），"
            f"单次最大间隔 {worst_gap/60:.1f} 分钟 ≤ 允许 {limit/60:.1f} 分钟；"
            f"跨次空档（睡眠到次日再开监控）不计入断档", [])


def run(cfg, store, events, lock, clear_stale=False):
    results = []

    def add(name, status, detail, reasons=None):
        results.append({"name": name, "status": status, "detail": detail,
                        "reasons": reasons or []})

    intent = util.read_json(sleepctl.intent_path(cfg))
    records = _seq_records(cfg)
    sid = intent.get("snapshot_id") if intent else None
    # 界面「直接睡眠」不经过值守流程、不绑定快照。既不能用它查快照完成状态，
    # 也**不能回退到最近一次 STEP1 的快照号**——否则会拿上一次值守的版本去比对
    # manual intent 的 version=0 占位，把一次正常手动睡眠误报成「版本冲突」。
    manual_sleep = bool(intent and intent.get("source") == "manual")
    if manual_sleep:
        sid = None
    elif not sid:
        # 没跑过睡眠流程时, 用最近一次 STEP1 的快照号, 便于诊断未完成流程
        s1 = [r for r in records if r["step"] == sleepctl.STEPS[0]]
        sid = s1[-1]["snapshot_id"] if s1 else None
    run_records = [r for r in records if r["snapshot_id"] == sid] if sid else []

    # ---- 1. 关键日志成对 ----
    has_s1 = any(r["step"] == sleepctl.STEPS[0] for r in run_records)
    has_s5 = any(r["step"] == sleepctl.STEPS[4] for r in run_records)
    if has_s1 and has_s5:
        add("关键日志成对", PASS,
            f'"即将睡眠"({sleepctl.STEPS[0]}) 与 "已发出"({sleepctl.STEPS[4]}) 均存在, snapshot={sid}')
    elif has_s1:
        add("关键日志成对", FAIL,
            f'缺失: 已发出=无（"即将睡眠"已写, snapshot={sid}）',
            ["监控在收尾前被强杀/崩溃", "写日志时进程被杀导致文件损坏",
             "残留写检查未通过导致主动放弃睡眠"])
    else:
        # 从没进入收尾流程。两种良性情形都不该判 FAIL —— 新装的机器一次都没跑过、
        # 或者任务整夜没全部终结（按设计本来就不发睡眠命令）。判 FAIL 会让人以为
        # 昨晚出了事故。真正的「收尾时崩溃」已被上面的 has_s1 分支和第 2 项兜住，
        # 「监控死在半路」由第 8 项心跳判定负责（未收尾且静默超限 -> WARN）。
        runs = _monitor_runs(cfg)
        if not runs:
            why = "尚未开始值守（没有任何监控记录），无睡眠日志可比对"
        elif runs[-1].get("finished"):
            why = (f"最近一次监控已正常收尾（{runs[-1].get('last_event')}），"
                   "未走到睡眠收尾，无睡眠日志可比对")
        else:
            why = ("最近一次监控未收尾，尚未进入睡眠收尾流程"
                   "（是否异常由第 8 项心跳连续性判定）")
        add("关键日志成对", INFO, why)
    if has_s1 and not has_s5:
        add("收尾被中断", WARN,
            "已写'即将睡眠'但未写'已发出', 说明收尾流程在 STEP2-STEP5 之间中断",
            ["进程被强杀", "残留写检查未通过导致主动放弃睡眠", "系统恰好此时断电"])

    # ---- 2. 固定顺序 ----
    last_s1 = -1
    for i, r in enumerate(run_records):
        if r["step"] == sleepctl.STEPS[0]:
            last_s1 = i
    if last_s1 >= 0:
        tail = run_records[last_s1:]
        got = [r["step"] for r in tail if r["step"] in sleepctl.STEPS]
        if got == sleepctl.STEPS:
            add("终态收尾固定顺序", PASS,
                "STEP1 -> STEP2 -> STEP3 -> STEP4 -> STEP5 顺序完整且无并发重排")
        else:
            add("终态收尾固定顺序", FAIL,
                f"实际顺序不完整: {got}",
                ["收尾流程中途失败", "多次运行日志混在一起, 请按 snapshot_id 区分"])
    else:
        add("终态收尾固定顺序", INFO, "尚未进入睡眠收尾流程, 跳过")

    # ---- 3. 遗留锁 ----
    lock_obj = lock.read()
    if lock_obj is None:
        add("遗留锁文件", PASS, "无锁文件 (监控已正常释放或未启动过)")
    else:
        status = lock_obj.get("status")
        pid = lock_obj.get("pid")
        alive = util.pid_alive(pid, lock_obj.get("started_at"))
        reused = util.pid_reused(pid, lock_obj.get("started_at"))
        if status == "completed_pre_sleep":
            add("遗留锁文件", PASS,
                f'锁状态=completed_pre_sleep (第3步预期保留, 非异常遗留), pid={pid}, alive={alive}',
                ["若仍不放心, 可用 selfcheck --clear-stale-lock 归档该锁"])
        elif status == "running" and alive:
            add("遗留锁文件", FAIL,
                f"存在 status=running 且 pid={pid} 仍存活的锁, 监控可能仍在运行或上次未退出",
                ["另一实例正在运行, 请勿重复启动"])
        else:
            reused_note = "；该 PID 已被其他程序复用（非本工具监控）" if reused else ""
            add("遗留锁文件", WARN,
                f"存在陈旧锁 (status={status}, pid={pid}, alive={alive}{reused_note})",
                ["监控中途死亡 -> 整夜不睡且无人知晓 (请结合心跳检查确认)",
                 "上次运行被强杀, 未走到第3步状态更新"])
            if clear_stale:
                lock.release("selfcheck --clear-stale-lock 清理陈旧锁")
                add("陈旧锁清理", PASS, "已归档并从 state/monitor.lock 移除")

    # ---- 4. 快照完成状态 ----
    if not sid:
        add("快照完成状态", INFO, "未发现绑定快照")
    else:
        try:
            snap = store.load(sid)
            if snap.get("status") == "completed_pre_sleep":
                add("快照完成状态", PASS,
                    f'{sid} (v{snap.get("version")}) status=completed_pre_sleep, '
                    f'completed_at={snap.get("completed_at")}')
            else:
                add("快照完成状态", WARN,
                    f'{sid} status={snap.get("status")}, 未标记完成',
                    ["监控尚未走到第3步", "绑定的是另一版本快照, 请检查版本一致性"])
            # 4b. 版本一致性
            intent_v = intent.get("version") if intent else None
            bound_v = None
            for path in cfg.dir("logs_dir").glob(f"monitor-{sid}-*.log"):
                for line in util.read_lines(path):
                    if "| BOUND_SNAPSHOT |" in line and f"snapshot_id={sid}" in line:
                        for token in line.split():
                            if token.startswith("version="):
                                bound_v = token.split("=", 1)[1]
            if intent_v is not None and bound_v is not None:
                if str(intent_v) == str(bound_v) == str(snap.get("version")):
                    add("快照版本一致性", PASS,
                        f"核对版本 / 监控绑定版本 / 睡眠记录版本 均为 v{bound_v}")
                else:
                    add("快照版本一致性", FAIL,
                        f'核对 v{snap.get("version")} / 监控绑定 v{bound_v} / 睡眠记录 v{intent_v} 不一致',
                        ["核对的是 A 版、监控跑的是 B 版"])
            else:
                add("快照版本一致性", INFO,
                    f"无法完整比对 (intent_v={intent_v}, bound_v={bound_v})")
        except Exception as exc:  # noqa: BLE001
            # 演练快照被清理是正常操作（demo/验收产物可随时归档），不该报 FAIL；
            # 但真实值守（dry_run=false）的快照丢失必须保持 FAIL，不能掩盖。
            if intent and intent.get("dry_run"):
                add("快照完成状态", INFO,
                    f"最近一次是演练，其快照 {sid} 已被清理（{exc!r}）；不影响真实值守复盘")
            else:
                add("快照完成状态", FAIL, f"读取快照失败: {exc!r}",
                    ["快照文件被移动/删除", "JSON 损坏"])

    # ---- 5. 命令已发出但系统未睡 ----
    if intent is None:
        add("效果: 系统是否进入低功耗", INFO, "未发现睡眠触发记录 (尚未触发过睡眠)")
    elif intent.get("dry_run"):
        add("效果: 系统是否进入低功耗", INFO,
            "本次为 dry-run, 未真正发出睡眠命令, 不做'未睡'判定 (避免误报)")
    else:
        issued_wall = intent.get("wall_time_epoch")
        wall_delta = util.wall_now() - float(issued_wall) if issued_wall else None
        un0 = intent.get("unbiased_interrupt_100ns")
        un1 = util.get_unbiased_interrupt_100ns()
        tick0 = intent.get("tick64_ms")
        tick1 = util.get_tick64_ms()
        wall_threshold = float(cfg["selfcheck"]["issued_but_not_slept_wall_seconds"])
        sleep_threshold = float(cfg["selfcheck"]["sleep_duration_threshold_seconds"])

        # 系统重启会让无偏中断时间归零, 必须先排除
        if tick0 and tick1 and tick1 < tick0:
            add("效果: 系统是否进入低功耗", INFO,
                "检测到系统已重启 (TickCount64 回退), 无法据此判定是否睡过",
                ["请查看 System 事件日志 Kernel-Power 107 恢复事件"])
        elif un0 and un1 and wall_delta is not None:
            awake_seconds = (un1 - un0) / 1e7
            sleep_est = wall_delta - awake_seconds
            if sleep_est >= sleep_threshold:
                add("效果: 系统是否进入低功耗", PASS,
                    f"系统确实挂起过: 墙钟经过 {wall_delta/60:.1f} 分钟, "
                    f"无偏(醒着)时间 {awake_seconds/60:.1f} 分钟, "
                    f"估算挂起 {sleep_est/60:.1f} 分钟；"
                    f"本次按 {intent.get('requested_mode','sleep')} 请求，"
                    "实际进入的可能是 sleep 或 hibernate，两者都视为成功")
            elif wall_delta >= wall_threshold:
                add("效果: 系统是否进入低功耗", FAIL,
                    f"命令已发出但系统未睡: 墙钟经过 {wall_delta/60:.1f} 分钟, "
                    f"估算睡眠仅 {max(sleep_est, 0)/60:.1f} 分钟 (< {sleep_threshold/60:.1f} 分钟阈值)",
                    ["有进程持有唤醒锁 (powercfg /requests 可查)",
                     "电源策略禁止睡眠 (组策略 / 休眠被禁用)",
                     "SetSuspendState 静默失败 (权限或驱动)",
                     "组策略 / BIOS / 驱动导致待机状态不可用",
                     "见「电源能力与客户提示」项：本工具只发一条命令、不补发，"
                     "实际进入哪种状态由系统决定"])
            else:
                add("效果: 系统是否进入低功耗", INFO,
                    f"距命令发出仅 {wall_delta/60:.1f} 分钟 (< {wall_threshold/60:.1f} 分钟), "
                    f"暂无法判定, 请稍后重跑 selfcheck")
        else:
            available, evts, err = _query_power_events(
                _parse_dt(intent.get("command_issued_at", "")))
            resume = [e for e in evts if e["id"] == "107"]
            if available and resume:
                add("效果: 系统是否进入低功耗", PASS,
                    f"时钟 API 不可用, 但事件日志发现 {len(resume)} 条 Kernel-Power 107 恢复事件, "
                    f"系统确实睡眠并唤醒")
            elif available:
                add("效果: 系统是否进入低功耗", WARN,
                    "时钟 API 不可用, 且事件日志中未发现恢复事件, 疑似命令发出但未睡",
                    ["电源请求阻止睡眠", "事件日志读取范围不足(仅查最近2天)"])
            else:
                add("效果: 系统是否进入低功耗", WARN,
                    f"无法自动判定 (无偏中断时间不可用; 事件日志: {err})",
                    ["请人工核对'即将睡眠'日志时间与系统实际时间线"])

        # 事件日志佐证 (即使时钟判定成功也给出)
        available, evts, err = _query_power_events(
            _parse_dt(intent.get("command_issued_at", "")))
        if available:
            sleep_ev = [e for e in evts if e["id"] == "42"]
            resume_ev = [e for e in evts if e["id"] == "107"]
            add("电源事件佐证", INFO,
                f"最近2天 Kernel-Power: 进入低功耗(42)={len(sleep_ev)} 条, "
                f"恢复(107)={len(resume_ev)} 条；"
                "休眠恢复也会产生 107，故仅作佐证，不能区分 sleep/hibernate")
        else:
            add("电源事件佐证", INFO,
                f"System 事件日志不可读, 跳过佐证 ({err})",
                ["读取 System 日志通常需要管理员权限, 可忽略"])

    # ---- 5a. 睡眠命令诊断（由命令进程自写，仅用于事后复盘）----
    diag_files = sorted(cfg.dir("logs_dir").glob("sleep-command-*.log"))
    if diag_files:
        last_lines = [l.strip() for l in util.read_lines(diag_files[-1])[-9:] if l.strip()]
        add("睡眠命令诊断", INFO,
            f"最近文件 {diag_files[-1].name} | " + " | ".join(last_lines))
    else:
        add("睡眠命令诊断", INFO,
            "无诊断日志：本次为 dry-run、命令进程未执行到写日志处、使用自定义命令，"
            "或是诊断功能启用前的历史记录",
            ["若本次确为真实模式且仍无该文件，说明命令进程在写日志前就被回收"])

    # ---- 5b. 电源能力与客户提示（效果层面，取消人工实测前提）----
    from . import powercap
    caps = powercap.load(cfg)
    prefer = cfg.prefer
    opts = powercap.option_map(caps)
    chosen = opts.get(prefer) or {}
    rec = powercap.recommend(caps, prefer)
    effect_detail = (
        powercap.short_summary(caps, prefer) + " | " + powercap.effect_note(prefer) +
        f" | 选择建议: {rec['text']}"
    )
    if rec["level"] in ("switch", "none"):
        effect_detail += (
            f" | 修复: {chosen.get('reason') or '见提示'}"
            + ("；" + "；".join((chosen.get("fix") or [])[:2]) if chosen.get("fix") else "")
        )
        add("电源能力与待机方式", WARN, effect_detail,
            ["本次请求的待机方式在本机不可用 -> 命令可能被系统忽略，实际没有进入低功耗",
             "请在控制台「待机方式」里改选可用项，再按提示修复电源设置"])
    else:
        add("电源能力与待机方式", INFO, effect_detail)

    # ---- 6. 心跳连续性 ----
    hb_status, hb_detail, _ = _heartbeat_report(cfg)
    add("监控存活心跳连续性", hb_status, hb_detail,
        ["同一次监控内心跳间隔超限 -> 进程卡死/被挂起，可能永远不触发睡眠",
         "监控没写收尾事件就静默 -> 进程已死，且没人知道"] if hb_status == WARN else [])

    # ---- 7. 一次确认护栏：非演练值守是否经过「开启监控」的一次确认 ----
    ms = util.read_json(util.abs_path("state") / "ui-monitor.json", {}) or {}
    last_intent = util.read_json(sleepctl.intent_path(cfg)) or {}
    confirmed = 0
    for p in cfg.dir("logs_dir").glob("events-*.log"):
        confirmed += sum(1 for l in util.read_lines(p) if "MONITOR_CONFIRMED" in l)
    real_mode = ms.get("dry_run") is False
    # 旧版本（改造前）的 ui-monitor.json 没有 confirmed_at 这个键，属于历史记录，
    # 不该按新机制判告警；只有新机制产生的记录才做严格判定。
    is_legacy = bool(ms) and "confirmed_at" not in ms
    has_confirm = bool(ms.get("confirmed_at"))
    detail = (f"上次监控: 实际演练={ms.get('dry_run')} "
              f"确认时间={ms.get('confirmed_at') or '无'}"
              f"{'（改造前历史记录，不计入新机制判定）' if is_legacy else ''} | "
              f"最近意图 dry_run={last_intent.get('dry_run')} | "
              f"一次确认事件累计 {confirmed} 次")
    reasons = []
    status = PASS
    if real_mode and not has_confirm:
        if is_legacy:
            status = INFO
            reasons.append("该记录来自旧版本的双层开关机制，无确认字段；新值守会写入 confirmed_at")
        else:
            status = WARN
            reasons.append("上次监控是非演练但未记录一次确认（可能由命令行直接启动），请人工确认是否符合预期")
    add("一次确认护栏", status, detail, reasons)

    events.emit("SELFCHECK_DONE", "唤醒后自检完成",
                echo=False,
                pass_count=sum(1 for r in results if r["status"] == PASS),
                warn_count=sum(1 for r in results if r["status"] == WARN),
                fail_count=sum(1 for r in results if r["status"] == FAIL))
    return results


def format_report(results, cfg, events=None):
    lines = []
    lines.append("=" * 78)
    lines.append("唤醒后自检报告 (agent-sleep-guard selfcheck)")
    lines.append(f"生成时间: {util.iso()}")
    lines.append("=" * 78)
    for i, r in enumerate(results, 1):
        lines.append(f"[{r['status']:<4}] {i}. {r['name']}")
        lines.append(f"        {r['detail']}")
        for reason in r["reasons"]:
            lines.append(f"        - 可能原因: {reason}")
    lines.append("-" * 78)
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in (PASS, WARN, FAIL, INFO)}
    lines.append(
        f"汇总: PASS={counts[PASS]}  WARN={counts[WARN]}  "
        f"FAIL={counts[FAIL]}  INFO={counts[INFO]}"
    )
    failed = [r["name"] for r in results if r["status"] == FAIL]
    warned = [r["name"] for r in results if r["status"] == WARN]
    if failed:
        lines.append(f"需要处理的失败项: {', '.join(failed)}")
    if warned:
        lines.append(f"需要人工确认的告警项: {', '.join(warned)}")
    if not failed and not warned:
        lines.append("结论: 全部检查通过, 昨晚流程按预期完成。")
    lines.append("=" * 78)
    text = "\n".join(lines)
    if events:
        events.emit("SELFCHECK_REPORT", "自检报告已生成", echo=False,
                    pass_count=counts[PASS], warn_count=counts[WARN],
                    fail_count=counts[FAIL])
        util.append_line(cfg.dir("logs_dir") / f"selfcheck-{util.today_str()}.log", text)
    return text
