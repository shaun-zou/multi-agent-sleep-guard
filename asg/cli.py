"""命令行入口: scan / monitor / selfcheck 三个可独立调用的入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata

from . import __version__, events, lockfile, monitor as monitor_mod
from . import selfcheck as selfcheck_mod
from . import snapshot, util
from .adapters import AdapterPool
from .config import Config


def _w(s):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def table(headers, rows):
    if not rows:
        return "(空)"
    widths = [_w(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _w(cell))
    def fmt(cells):
        return " | ".join(
            str(c) + " " * max(0, widths[i] - _w(c)) for i, c in enumerate(cells)
        )
    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt(headers), sep] + [fmt(r) for r in rows])


def build_parser():
    p = argparse.ArgumentParser(
        prog="agent-sleep-guard",
        description="多 Agent 空闲监测与安全睡眠工具 (夜间无人值守)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型流程:\n"
            "  python agent_sleep_guard.py scan\n"
            "  python agent_sleep_guard.py scan revise --snapshot <ID> --remove <TID>\n"
            "  python agent_sleep_guard.py scan verify --snapshot <ID> "
            "--retry-reuses-id false --decision accept_untracked_retry\n"
            "  python agent_sleep_guard.py monitor --snapshot <ID>\n"
            "  python agent_sleep_guard.py selfcheck\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"agent-sleep-guard {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan", help="手动扫描 / 快照人工修正 / 人工核对")
    ps.add_argument("action", nargs="?", default="run",
                    choices=["run", "list", "show", "revise", "verify"])
    ps.add_argument("--config", default=None)
    ps.add_argument("--auto-discover", action="store_true",
                    help="扫描前由代码自动发现本机 Agent 与状态数据源（不写配置文件）")
    ps.add_argument("--snapshot", default=None, help="快照 ID (show/revise/verify 必填)")
    ps.add_argument("--add", action="append", default=[],
                    help="新增任务, 传 JSON 字符串或 @文件路径; 可重复")
    ps.add_argument("--remove", action="append", default=[],
                    help="删除任务 task_id; 可重复")
    ps.add_argument("--tasks-file", default=None,
                    help="用整份任务清单 JSON 替换 (文件内为 tasks 数组或含 tasks 键)")
    ps.add_argument("--reason", default="", help="修正原因 (留痕)")
    ps.add_argument("--operator", default="", help="核对/修正操作人")
    ps.add_argument("--note", default="", help="备注")
    ps.add_argument("--retry-reuses-id", choices=["true", "false"], default=None,
                    help="Agent 失败重试时是否沿用原任务 ID")
    ps.add_argument("--decision", default=None,
                    choices=list(snapshot.RETRY_DECISIONS),
                    help="重试风险决定 (不沿用原 ID 时必须显式二选一)")
    ps.add_argument("--watch-task", action="append", default=[],
                    help="标记'需观察重试链'的任务 ID; 可重复; 不指定则标记全部")
    ps.add_argument("--implicit", action="store_true",
                    help="界面默认路径：不要求人工逐条选择重试风险，采用 no_action 并在留痕里标 explicit=false")

    pm = sub.add_parser("monitor", help="绑定指定快照版本启动监控 (含终态复核与睡眠触发)")
    pm.add_argument("--config", default=None)
    pm.add_argument("--snapshot", required=True)
    dry = pm.add_mutually_exclusive_group()
    dry.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    dry.add_argument("--no-dry-run", dest="dry_run", action="store_false")

    pc = sub.add_parser("selfcheck", help="唤醒后自检")
    pc.add_argument("--config", default=None)
    pc.add_argument("--clear-stale-lock", action="store_true",
                    help="归档并清理陈旧锁 (仅在判定为陈旧时生效)")

    pd = sub.add_parser("discover", help="由代码自动发现本机 Agent 与任务状态数据源（只读）")
    pd.add_argument("--config", default=None)
    pd.add_argument("--write-config", default=None, metavar="PATH",
                    help="把推荐配置写到指定文件（默认不写）")
    pd.add_argument("--json", action="store_true", help="输出原始 JSON 而非可读报告")
    pd.add_argument("--all", action="store_true",
                    help="连「已安装但未在用」的 Agent 也一并列出（默认只显示在用的）")

    pr = sub.add_parser("rules", help="从真实数据观测规则漂移、生成草案、人工确认后更新规则表")
    pr.add_argument("--config", default=None)
    pr.add_argument("--json", action="store_true", help="输出原始 JSON")
    pr.add_argument("--no-sample", action="store_true", help="不重新采样，只看现有草案")
    pr.add_argument("--approve", default=None, metavar="ADAPTER:KEY",
                    help="确认一条草案，如 autoclaw_log:event:zwork.foo.bar")
    pr.add_argument("--as", dest="decision", default=None,
                    choices=["start", "middle", "end", "running", "queued",
                             "completed", "failed", "cancelled", "ignore"])
    pr.add_argument("--approve-noise", action="store_true",
                    help="把草案里建议为噪声(ignore)的条目批量标为忽略（需人工执行这条命令确认）")
    pr.add_argument("--adapter", default="autoclaw_log", help="前缀规则作用的适配器（默认 autoclaw_log）")
    pr.add_argument("--add-ignore-pattern", default=None, metavar="PATTERN",
                    help="新增按命名空间忽略噪声的前缀规则，支持 * 通配，如 zwork.network.*")
    pr.add_argument("--remove-ignore-pattern", default=None, metavar="PATTERN",
                    help="移除一条前缀忽略规则（若该命名空间下出现真实任务事件时使用）")
    pr.add_argument("--operator", default="")

    pcases = sub.add_parser("cases", help="查看/导出异常与恢复验收用例集")
    pcases.add_argument("--config", default=None)
    pcases.add_argument("--json", action="store_true", help="导出原始 JSON")
    pmetrics = sub.add_parser("metrics", help="输出产品指标口径与近 N 天周报")
    pmetrics.add_argument("--config", default=None)
    pmetrics.add_argument("--days", type=int, default=7)
    pmetrics.add_argument("--json", action="store_true")

    ppow = sub.add_parser("power", help="探测睡眠/休眠可用性, 或设置监控后请求的待机方式")
    ppow.add_argument("action", nargs="?", default="show",
                      choices=["show", "probe", "prefer"])
    ppow.add_argument("--config", default=None)
    ppow.add_argument("--prefer", default=None, choices=["sleep", "hibernate"],
                      help="action=prefer 时必填：监控结束后请求哪一种")
    ppow.add_argument("--force", action="store_true",
                      help="即使该方式在本机不可用也照样保存（不推荐）")
    ppow.add_argument("--operator", default="")
    ppow.add_argument("--json", action="store_true")
    return p


def _ctx(config_path):
    cfg = Config(config_path)
    ev = events.EventLog(cfg)
    return cfg, ev, snapshot.SnapshotStore(cfg, ev)


def _require_snapshot(args):
    if not args.snapshot:
        raise SystemExit("错误: 该操作需要 --snapshot <快照ID>")


def _load_add(raw):
    if raw.startswith("@"):
        return util.read_json(util.abs_path(raw[1:]))
    return json.loads(raw)


def cmd_scan(args):
    cfg, ev, store = _ctx(args.config)

    if args.action == "list":
        rows = store.list()
        print(table(
            ["snapshot_id", "ver", "source", "tasks", "verified", "locked", "status", "retry_decision"],
            [[r["snapshot_id"], r["version"], r["source"], r["task_count"],
              r["verified"], r["locked"], r["status"], r["retry_decision"] or "-"]
             for r in rows],
        ))
        if not rows:
            print("\n尚无快照, 请先运行: python agent_sleep_guard.py scan")
        return 0

    if args.action == "show":
        _require_snapshot(args)
        s = store.show(args.snapshot)
        print(f"快照 ID      : {s['snapshot_id']}")
        print(f"版本         : v{s['version']} (family={s['family_id']}, parent={s['parent_id']})")
        print(f"来源         : {s['source']}   创建时间: {s['created_at']}")
        print(f"已核对       : {s['verified']}  at={s['verified_at']}  by={s['verified_by']}")
        print(f"已锁定       : {s['locked']}  at={s['locked_at']}")
        print(f"状态         : {s['status']}")
        print(f"重试风险决定 : {json.dumps(s['retry_risk'], ensure_ascii=False)}")
        print(f"版本变更记录 : {json.dumps(s['change_log'], ensure_ascii=False)}")
        print()
        headers = ["task_id", "agent", "start_time", "status", "last_active", "source", "watch_retry"]
        rows = [[t.get("task_id"), t.get("agent"), t.get("start_time"), t.get("status"),
                 t.get("last_active") or "-", t.get("source") or "-",
                 "YES" if t.get("need_watch_retry_chain") else "-"]
                for t in s["tasks"]]
        if any(t.get("evidence") for t in s["tasks"]):
            headers.append("状态依据")
            for row, t in zip(rows, s["tasks"]):
                row.append(t.get("evidence") or "-")
        print(table(headers, rows))
        return 0

    if args.action == "revise":
        _require_snapshot(args)
        old_task_count = store.load(args.snapshot)["task_count"]
        adds = [_load_add(x) for x in args.add]
        obj = store.revise(
            args.snapshot, adds=adds, removes=args.remove,
            tasks_file=args.tasks_file, reason=args.reason, operator=args.operator,
        )
        print(f"\n原版本 -> 新版本: {args.snapshot} -> {obj['snapshot_id']} "
              f"(v{obj['parent_version']} -> v{obj['version']})")
        print(f"任务数: {old_task_count} -> {obj['task_count']}")
        print(f"变更明细: {json.dumps(obj['change_log'][-1], ensure_ascii=False)}")
        print("变更已写入 change_log 与全局事件日志。请重新人工核对新版本后再启动监控。")
        return 0

    if args.action == "verify":
        _require_snapshot(args)
        if getattr(args, "implicit", False):
            # 界面主路径：无人值守时重试需人工触发，默认沿用原任务 ID、无额外处理。
            # 仍写 retry_risk 留痕（monitor 要求该字段存在），但 explicit=false。
            rid, dec = "true", "no_action"
            explicit = False
            note = args.note or "界面默认：无人值守场景重试需人工触发，沿用原任务 ID，无额外处理"
        else:
            if args.retry_reuses_id is None or args.decision is None:
                raise SystemExit(
                    "错误: 核对必须显式给出 --retry-reuses-id 与 --decision，"
                    "或使用 --implicit 采用界面默认\n"
                    "  (重试沿用原 ID -> --decision no_action;\n"
                    "   重试生成新 ID -> --decision watch_retry_chain 或 accept_untracked_retry)"
                )
            rid, dec, explicit, note = args.retry_reuses_id, args.decision, True, args.note
        obj = store.verify(
            args.snapshot,
            retry_reuses_id=(rid == "true"),
            decision=dec,
            watch_tasks=args.watch_task,
            note=note,
            operator=args.operator,
            explicit=explicit,
        )
        print(f"\n已核对并锁定: {obj['snapshot_id']} (v{obj['version']})")
        print(f"重试风险决定: {json.dumps(obj['retry_risk'], ensure_ascii=False)}")
        print("现在可用该**具体版本**启动监控: "
              f"python agent_sleep_guard.py monitor --snapshot {obj['snapshot_id']}")
        return 0

    # ---- run: 手动扫描 ----
    if getattr(args, "auto_discover", False):
        from . import discovery
        rep = discovery.discover()
        specs = rep["recommended_adapters"]
        if not specs:
            raise SystemExit("自动发现未找到任何「健康且已实现适配器」的 Agent 数据源")
        cfg.raw["adapters"] = specs
        ev.emit("AUTO_DISCOVER", "scan 前由代码自动发现并应用 Agent 数据源",
                installed=rep["summary"]["installed"], in_use=rep["summary"]["in_use"],
                enabled=len(specs), downgraded=rep["summary"]["downgraded"])
        print(f"[auto-discover] 代码自动发现: 已安装 {rep['summary']['installed']} 个 Agent, "
              f"在用 {rep['summary']['in_use']} 个, 自动启用 {len(specs)} 个数据源\n")
    pool = AdapterPool(cfg)
    tasks = pool.scan_tasks()
    print(f"适配器: {pool.describe()}")
    print(f"扫描时间: {util.iso()}\n")
    if getattr(pool, "skipped_not_running", []):
        print(f"[已跳过] 以下 Agent 进程未运行，按“只显示在用的 Agent”规则不纳入清单: "
              f"{', '.join(pool.skipped_not_running)}")
    filtered = pool.filtered_todos()
    if filtered:
        by_status = {}
        for item in filtered:
            by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
        print(f"[已过滤] {len(filtered)} 条已终态子任务未纳入清单（{detail}）:"
              " 主任务与未完成子任务全部保留")
        for item in filtered[:8]:
            print(f"       - {item['task_id']}  status={item['status']} "
                  f"raw={item['raw_status']}")
        if len(filtered) > 8:
            print(f"       ... 另有 {len(filtered) - 8} 条，未逐条列出")
        print("       这些任务已是终态，过滤不影响睡眠判定；如需保留请把 config 里 "
              "adapters[workbuddy_local].options.include_completed_todos 设为 true。")
        ev.emit("TODO_FILTERED", f"扫描过滤 {len(filtered)} 条已终态子任务",
                echo=False, count=len(filtered), by_status=detail)
    headers = ["task_id", "agent", "start_time", "status", "last_active", "source"]
    rows = [[t.get("task_id"), t.get("agent"), t.get("start_time"), t.get("status"),
             t.get("last_active") or "-", t.get("source")] for t in tasks]
    if any(t.get("evidence") for t in tasks):
        headers.append("状态依据")
        for row, t in zip(rows, tasks):
            row.append(t.get("evidence") or "-")
    print(table(headers, rows))

    # 真实数据源的安全提示：保守项 / 交互会话 / 空清单
    conservative = [t for t in tasks if t.get("conservative")]
    if conservative:
        print(f"\n[注意] 其中 {len(conservative)} 条属于「无明确终态、按保守原则恒判 running」:")
        for t in conservative:
            print(f"       - {t.get('task_id')} ({t.get('agent')}) : {t.get('evidence')}")
        print("       只要它们留在快照里，监控就不会判定「全部完成」，也就不会睡眠。")
        print("       这是「绝不打断任务」的必然结果；如需真正触发睡眠，请在人工核对阶段用 scan revise 剔除。")
    interactive = [t for t in tasks if t.get("interactive_session") and t.get("kind") == "session"]
    if interactive:
        ids = ", ".join(str(t.get("task_id")) for t in interactive[:2])
        print(f"\n[说明] 清单里有 {len(interactive)} 条交互式会话（非后台自动化），例如: {ids}")
        print("       夜间值守时交互窗口默认无用户输入：最后一条事件停在结束位置且静默 >= N 分钟即判完成；")
        print("       一旦出现新的用户输入(role=user)，状态立即回到 running，监控不会睡。")
        print("       若确认某条只是空转窗口，也可在人工核对阶段用 `scan revise --remove <task_id>` 剔除。")
    if not tasks:
        print("\n[注意] 本次扫描结果为空。若此刻确实没有 Agent 在跑属正常；但也可能是数据源路径不对/读取失败。")
        print("       空清单会让监控立即满足「全部终结」条件，开启监控前务必人工确认。")

    obj = store.create_from_scan(tasks, adapters_desc=pool.describe(), note=args.note)
    print(f"\n快照 ID   : {obj['snapshot_id']}")
    print(f"快照版本  : v{obj['version']}")
    print(f"任务数    : {obj['task_count']}")
    print(f"落盘路径  : {store.path_of(obj['snapshot_id'])}")
    print("\n>>> 请人工核对清单。如需增删任务用 `scan revise`, 确认无误后用 `scan verify` 锁定版本。")
    print(">>> 扫描仅在此手动触发时执行, 不会后台自动重扫。")
    # 自动观测规则值（只记录，不自动改变任何规则），供 rules 生成草案
    try:
        from . import rules as rules_mod
        seen = rules_mod.sample(cfg)
        ev.emit("RULES_OBSERVED", "scan 已自动观测规则值", echo=False, adapters=len(seen))
    except Exception as exc:  # noqa: BLE001 - 观测失败不能影响扫描
        ev.emit("RULES_OBSERVE_FAILED", repr(exc), echo=False)
    return 0


def cmd_monitor(args):
    cfg, ev, store = _ctx(args.config)
    if args.dry_run is not None:
        cfg.set_dry_run(args.dry_run)
    snap = store.load(args.snapshot)

    if not snap.get("verified") or not snap.get("locked"):
        raise SystemExit(
            f"拒绝启动: 快照 {args.snapshot} 未经人工核对并锁定 (verified/locked=False)。\n"
            f"请先执行: python agent_sleep_guard.py scan verify --snapshot {args.snapshot} ..."
        )
    if snap.get("status") == "completed_pre_sleep":
        raise SystemExit(
            f"拒绝启动: 快照 {args.snapshot} 已完成睡眠流程, 请重新 scan 生成新快照。"
        )
    if snap.get("retry_risk") is None:
        raise SystemExit("拒绝启动: 该快照没有重试风险决定留痕, 请重新核对。")

    lock = lockfile.LockFile(cfg, ev)
    lock.acquire(args.snapshot, os.getpid())

    # 启动前只读探测电源能力：结果仅用于提示/留痕/自检，不改变命令、不补发
    from . import powercap
    caps = powercap.probe()
    powercap.save(cfg, caps)
    ev.emit("POWER_CAPABILITIES", "监控启动前电源能力探测",
            requested_mode=str(cfg["sleep"].get("prefer", "sleep")),
            sleep_available=caps.get("sleep_available"),
            hibernate_available=caps.get("hibernate_available"),
            runtime_fallback="none")

    try:
        pool = AdapterPool(cfg)
        store.mark(args.snapshot, "monitoring", monitor_started_at=util.iso())
        store.add_binding(args.snapshot, os.getpid())
        m = monitor_mod.Monitor(cfg, store, ev, lock, pool, snap)
        print(f"[monitor] 绑定快照 {args.snapshot} (v{snap['version']}), "
              f"dry_run={cfg.dry_run}; Ctrl+C 可安全中止(不会睡眠)。")
        m.start()
    except KeyboardInterrupt:
        lock.release("KeyboardInterrupt")
        print("[monitor] 已中断, 未发出睡眠命令。")
    except Exception as exc:  # noqa: BLE001
        lock.release(f"监控异常退出: {exc!r}")
        raise
    # 正常结束后不主动 exit、不等待退出 (规范第七节第6点)
    return 0


def cmd_selfcheck(args):
    cfg, ev, store = _ctx(args.config)
    lock = lockfile.LockFile(cfg, ev)
    results = selfcheck_mod.run(cfg, store, ev, lock, clear_stale=args.clear_stale_lock)
    print(selfcheck_mod.format_report(results, cfg, ev))
    return 0


def cmd_cases(args):
    from . import cases as cases_mod
    d = cases_mod.load()
    print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else cases_mod.format_text(d))
    return 0


def cmd_metrics(args):
    from . import metrics as metrics_mod
    cfg, _, _ = _ctx(args.config)
    m = metrics_mod.collect(cfg, days=args.days)
    print(json.dumps(m, ensure_ascii=False, indent=2) if args.json else metrics_mod.format_text(m))
    return 0


def cmd_rules(args):
    from . import rules as rules_mod
    cfg, ev, _ = _ctx(args.config)
    book = rules_mod.get_rulebook()
    if args.approve:
        if not args.decision:
            raise SystemExit("确认草案需要同时给 --as <判定>")
        if ":" not in args.approve:
            raise SystemExit("--approve 格式应为 ADAPTER:KEY")
        adapter, key = args.approve.split(":", 1)
        try:
            applied = book.approve(adapter, key, args.decision, args.operator)
        except ValueError as exc:
            raise SystemExit(str(exc))
        ev.emit("RULES_APPROVED", f"{args.approve} -> {args.decision}",
                adapter=adapter, key=key, operator=args.operator or "-")
        print(f"[已确认] {args.approve} -> {applied}")
        print("        适配器下次读取规则表即生效；确认记录已写入 history 留痕。")
        return 0
    if args.approve_noise:
        d = book.draft(None)
        targets = [p for p in d["pending"] if p["suggestion"] == "ignore"]
        for p in targets:
            book.approve(p["adapter"], p["key"], "ignore", args.operator)
        ev.emit("RULES_NOISE_APPROVED", f"批量忽略 {len(targets)} 条噪声事件",
                count=len(targets), operator=args.operator or "-")
        rest = len(d["pending"]) - len(targets)
        print(f"[已确认] 批量忽略 {len(targets)} 条噪声事件；剩余 {rest} 条需逐条判断。")
        print("        已写入 ignored / history 留痕，适配器下次读取即生效。")
        return 0
    if args.add_ignore_pattern or args.remove_ignore_pattern:
        if args.add_ignore_pattern and args.remove_ignore_pattern:
            raise SystemExit("一次只能执行一种前缀操作")
        if args.add_ignore_pattern:
            pat = args.add_ignore_pattern
            pats = book.add_ignore_pattern(args.adapter, pat, args.operator)
            action = "新增"
        else:
            pat = args.remove_ignore_pattern
            pats = book.remove_ignore_pattern(args.adapter, pat, args.operator)
            action = "移除"
        ev.emit("RULES_PATTERN", f"{args.adapter} 前缀忽略规则{action}",
                adapter=args.adapter, pattern=pat, operator=args.operator or "-")
        print(f"[已{action}] {args.adapter}: {pat}")
        print(f"  当前 {len(pats)} 条: " + ", ".join(pats))
        print("  已写入 history 留痕；运行中的 monitor 会在 5 秒内热重载规则表。")
        return 0
    current = None if args.no_sample else rules_mod.sample(cfg, book)
    if current is not None:
        ev.emit("RULES_OBSERVED", "已从真实数据源采样规则观测值", echo=False,
                adapters=len(current))
    draft = book.draft(current)
    if args.json:
        print(json.dumps({"fingerprint": book.version_fingerprint(), **draft},
                         ensure_ascii=False, indent=2))
    else:
        print(rules_mod.format_rules(book, draft, current))
    return 0


def cmd_discover(args):
    from . import discovery
    from . import rules as rules_mod
    cfg, ev, _ = _ctx(args.config)
    report = discovery.discover()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(discovery.format_report(report, show_all=args.all))
        print(f"规则表指纹: {rules_mod.get_rulebook().version_fingerprint()}")
        if any("版本变化" in item for c in report["changes"] for item in c["items"]):
            print("[提示] 检测到应用版本变化，建议运行 "
                  "`python agent_sleep_guard.py rules` 复查规则表是否过时。")
    util.append_line(
        cfg.dir("logs_dir") / f"discover-{util.today_str()}.log",
        f"{util.iso()} | DISCOVER | " + json.dumps(report["summary"], ensure_ascii=False),
    )
    ev.emit("DISCOVER", "代码自动发现完成", echo=False, **report["summary"])
    if args.write_config:
        out = util.abs_path(args.write_config)
        util.atomic_write_json(out, discovery.build_config(report))
        print(f"\n已写出自动发现配置: {out}")
        print(f"可直接运行: python agent_sleep_guard.py scan --config {args.write_config}")
    return 0


def cmd_power(args):
    """探测可用性 / 选择待机方式（与界面 /api/powercap、/api/sleep/preference 同口径）。"""
    from . import powercap
    cfg, ev, _ = _ctx(args.config)
    prefer = cfg.prefer

    if args.action in ("show", "probe"):
        caps = powercap.load(cfg)
        if args.action == "probe" or not caps:
            caps = powercap.probe()
            powercap.save(cfg, caps)
            if args.action == "probe":
                ev.emit("POWER_CAPABILITIES", "命令行刷新电源能力",
                        requested_mode=prefer,
                        sleep_available=caps.get("sleep_available"),
                        hibernate_available=caps.get("hibernate_available"),
                        sleep_kind=caps.get("sleep_kind"))
        out = {"prefer": prefer, "capabilities": caps,
               "options": powercap.options(caps),
               "recommend": powercap.recommend(caps, prefer),
               "summary": powercap.short_summary(caps, prefer),
               "stale": powercap.is_stale(caps)}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        print(f"请求模式      : {prefer}（只发一条, 不补发）")
        print(f"探测时间      : {caps.get('probed_at') or '未探测'}"
              f"{'  [可能已过期]' if powercap.is_stale(caps) else ''}")
        print(f"待机可用      : {caps.get('sleep_available')} ({caps.get('sleep_kind') or '-'})")
        print(f"休眠可用      : {caps.get('hibernate_available')}"
              f"{'  ' + str(caps.get('hibernate_reason')) if caps.get('hibernate_reason') else ''}")
        print("可选项        :")
        for o in out["options"]:
            mark = "可用" if o["available"] else ("不可用" if o["available"] is False else "未检测")
            print(f"  - {o['value']:<9} {o['label']}  [{mark}]"
                  + (f"  原因: {o['reason']}" if o.get("reason") else ""))
        print(f"建议          : {out['recommend']['text']}")
        print("\n注意: 以上只是「请求哪一种」的可用性提示；实际进入 sleep 还是 hibernate"
              " 由系统决定，两者都算成功，且不会补发第二条命令。")
        return 0

    # action == "prefer"
    if not args.prefer:
        raise SystemExit("错误: power prefer 需要 --prefer sleep|hibernate")
    caps = powercap.load(cfg) or powercap.probe()
    chosen = powercap.option_map(caps).get(args.prefer) or {}
    if chosen.get("available") is False and not args.force:
        others = [o for o in powercap.options(caps)
                  if o["value"] != args.prefer and o.get("available")]
        if others:
            raise SystemExit(
                f"错误: {chosen.get('label')} 在本机不可用（{chosen.get('reason')}）。"
                f"建议改为 --prefer {others[0]['value']}；如仍要保存请加 --force")
    old, new = cfg.set_prefer(args.prefer)
    ev.emit("SLEEP_PREFERENCE_CHANGED",
            f"待机方式 {old} -> {new}（只影响发出哪一条命令, 不改变只发一条/不补发）",
            operator=args.operator or "cli", old=old, new=new, forced=bool(args.force))
    print(f"已保存: sleep.prefer {old} -> {new}")
    print(f"建议: {powercap.recommend(caps, new)['text']}")
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "scan":
            return cmd_scan(args)
        if args.cmd == "monitor":
            return cmd_monitor(args)
        if args.cmd == "selfcheck":
            return cmd_selfcheck(args)
        if args.cmd == "discover":
            return cmd_discover(args)
        if args.cmd == "rules":
            return cmd_rules(args)
        if args.cmd == "cases":
            return cmd_cases(args)
        if args.cmd == "metrics":
            return cmd_metrics(args)
        if args.cmd == "power":
            return cmd_power(args)
    except (snapshot.SnapshotError, lockfile.LockBusy, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 0
