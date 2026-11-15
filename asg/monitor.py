"""监控执行单元。

绑定一个**已人工核对并锁定**的具体快照版本; 只跟踪快照内任务;
清单外新任务一律不处理/不告警/不阻塞; 不设超时阈值; 无中断动作。

终态复核通过后, 严格按第七节固定顺序收尾 (全部单线程、禁止并发):
  STEP1 写"即将睡眠"日志并 flush+close 句柄
  STEP2 设停止标志 + join 优雅停止监控
  STEP3 更新锁/快照状态为"已完成，即将睡眠", 写完关闭
  STEP4 再次确认无残留写操作
  STEP5 写"已发出"日志 -> 发出睡眠命令 (不等待返回值、不主动 exit)
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter

from . import logger, sleepctl, util


class Monitor:
    def __init__(self, cfg, store, events, lock, pool, snapshot):
        self.cfg = cfg
        self.store = store
        self.events = events
        self.lock = lock
        self.pool = pool
        self.snap = snapshot
        self.sid = snapshot["snapshot_id"]
        self.version = snapshot["version"]
        self.terminal = cfg.terminal_statuses
        self.unknown_is_terminal = bool(cfg.get("unknown_status_is_terminal", False))

        log_dir = cfg.dir("logs_dir")
        self.log = logger.FileLogger(
            log_dir / f"monitor-{self.sid}-{util.today_str()}.log"
        )
        # 界面通过写这个标志文件请求停止；monitor 自己走 _abort 优雅收尾，绝不强杀
        self.stop_file = cfg.dir("state_dir") / "monitor.stop"

        self.stop_flag = threading.Event()
        self.ready_event = threading.Event()
        self.worker = None

        # 待完成集合 / 已终结集合
        self.pending = {str(t["task_id"]): dict(t) for t in snapshot["tasks"]}
        self.terminal_tasks = {}
        self.all_tasks = [dict(t) for t in snapshot["tasks"]]

        self.last_heartbeat = 0.0
        self.last_progress = 0.0
        self._no_signal_logged = set()
        self.pre_sleep_at = None
        self.aborted = False

        self.poll_interval = max(1, int(cfg["poll_interval_seconds"]))
        self.progress_interval = max(1, int(cfg["progress_interval_seconds"]))
        self.heartbeat_interval = cfg.heartbeat_interval
        self.stable_required = max(1, int(cfg["stable_required"]))
        self.stability_interval = max(1, int(cfg["stability_interval_seconds"]))

    # ---------------- 日志辅助 ----------------

    def _is_terminal(self, status):
        s = str(status).lower()
        if s in self.terminal:
            return True
        if s == "unknown" and self.unknown_is_terminal:
            return True
        return False

    def _heartbeat(self, force=False):
        now = time.monotonic()
        if not force and (now - self.last_heartbeat) < self.heartbeat_interval:
            return
        self.last_heartbeat = now
        self.log.write(
            "HEARTBEAT", "仍在运行",
            remaining=len(self.pending),
            snapshot_id=self.sid, version=self.version,
            terminal_count=len(self.terminal_tasks),
        )

    def _progress(self, force=False):
        now = time.monotonic()
        if not force and (now - self.last_progress) < self.progress_interval:
            return
        self.last_progress = now
        detail = []
        for tid, task in sorted(self.pending.items()):
            active = self.pool.get_last_active(task)
            detail.append(f"{tid}@{task.get('agent')}:{task.get('status')}:last_active={active}")
        self.log.write(
            "PROGRESS", "还有几个任务未终结、各自最后活跃时间",
            remaining=len(self.pending),
            detail=";".join(detail) if detail else "(无未终结任务)",
        )

    def _wait(self, seconds):
        """可中断等待; 期间仍按节奏写心跳和进度日志 (纯记录、不干预任务)。"""
        deadline = time.monotonic() + seconds
        while not self.stop_flag.is_set():
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            self._heartbeat()
            self._progress()
            self.stop_flag.wait(min(1.0, remain))
        self._heartbeat()
        self._progress()

    # ---------------- 轮询 ----------------

    def _poll_cycle(self):
        for tid, task in list(self.pending.items()):
            status = str(self.pool.get_status(task)).lower()
            active = self.pool.get_last_active(task)
            old = str(task.get("status") or "unknown").lower()
            # 状态迁移留痕：原来只在"到达终态"时写 TASK_TERMINAL，非终态变化
            # (queued -> running 等) 只改内存，事后无法还原"什么时候从什么变成什么"。
            # 这条日志只做观察记录，不参与任何判定。
            if status != old:
                self.log.write(
                    "STATUS_CHANGE", "任务状态发生变化（仅留痕，不改变判定）",
                    task_id=tid, agent=task.get("agent"),
                    from_status=old, to_status=status,
                    observed_at=util.iso(),
                    evidence=str(task.get("evidence") or "-"),
                )
            if self._is_terminal(status):
                done = dict(task)
                done["status"] = status
                done["terminal_at"] = util.iso()
                done["last_active"] = active
                self.terminal_tasks[tid] = done
                del self.pending[tid]
                self.log.write(
                    "TASK_TERMINAL", "任务到达终态, 从待完成集合移除",
                    task_id=tid, agent=done.get("agent"), status=status,
                    start_time=done.get("start_time"), last_active=active,
                )
                if done.get("need_watch_retry_chain"):
                    self.log.write(
                        "RETRY_CHAIN_NOTE",
                        "该任务被标记需观察重试链: 终态仅代表快照内旧任务; "
                        "若重试生成新 ID, 由 Agent 级活跃信号兜底",
                        task_id=tid, agent=done.get("agent"),
                    )
            else:
                task["status"] = status
                task["last_active"] = active

        # 清单外新任务: 只记录一行, 不处理/不告警/不阻塞
        try:
            current = self.pool.scan_tasks()
            known = set(self.pending) | set(self.terminal_tasks)
            outsider = [c for c in current if str(c.get("task_id")) not in known]
        except Exception as exc:  # noqa: BLE001
            outsider = []
            self.log.write("SCAN_FOR_NEW_TASKS_FAILED", repr(exc))
        if outsider:
            self.log.write(
                "IGNORED_NEW_TASKS", "清单外新任务一律不处理/不告警/不阻塞",
                count=len(outsider),
                ids=json.dumps([str(c.get("task_id")) for c in outsider], ensure_ascii=False),
            )

        compact = ";".join(
            f"{tid}:{t.get('status')}" for tid, t in sorted(self.pending.items())
        )
        self.log.write(
            "POLL", "每次轮询状态",
            remaining=len(self.pending),
            terminal_count=len(self.terminal_tasks),
            statuses=compact or "(all terminal)",
        )
        self._progress()
        self._heartbeat()

    def _all_terminal(self):
        if self.pending:
            return False
        # "需观察重试链"兜底: 旧任务虽终结, 但同 Agent 若仍活跃 (疑似重试新 ID) 则不判全终态
        for tid, task in self.terminal_tasks.items():
            if not task.get("need_watch_retry_chain"):
                continue
            agent, source = task.get("agent"), task.get("source")
            idle = self.pool.agent_is_idle(agent, source)
            if idle is False:
                self.log.write(
                    "RETRY_CHAIN_ACTIVE",
                    "Agent 仍有活跃迹象(疑似重试出新任务), 暂不判定全终态",
                    task_id=tid, agent=agent,
                )
                return False
            if idle is None and tid not in self._no_signal_logged:
                self._no_signal_logged.add(tid)
                self.log.write(
                    "RETRY_CHAIN_NO_SIGNAL",
                    "适配器不提供 Agent 级活跃信号, 仅留痕, 不改变终态判定",
                    task_id=tid, agent=agent,
                )
        return True

    def _reverify_all(self):
        """终态复核时重新查一遍已终结任务, 捕捉"任务回到非终态"的瞬态抖动。"""
        regressed = []
        for tid, task in list(self.terminal_tasks.items()):
            status = str(self.pool.get_status(task)).lower()
            if not self._is_terminal(status):
                regressed.append(tid)
                revived = dict(task)
                revived["status"] = status
                revived["last_active"] = self.pool.get_last_active(task)
                self.pending[tid] = revived
                del self.terminal_tasks[tid]
        if regressed:
            self.log.write(
                "JITTER_REGRESSION", "终态复核发现任务回到非终态, 稳定计数重置",
                ids=json.dumps(regressed, ensure_ascii=False),
            )
        return not regressed

    def _confirm_stable(self):
        """连续 N 次稳定成立, 每次间隔 stability_interval_seconds (默认 60s, 规范 1-2 分钟)。"""
        for i in range(1, self.stable_required + 1):
            self._wait(self.stability_interval)
            if self.stop_flag.is_set():
                return False
            self._reverify_all()
            passed = self._all_terminal()
            self.log.write(
                "STABILITY_CHECK", f"终态复核 {i}/{self.stable_required}",
                passed=passed, remaining=len(self.pending),
                snapshot_id=self.sid, version=self.version,
            )
            if not passed:
                self.log.write(
                    "STABILITY_RESET", "连续稳定条件被打破, 回到常规轮询继续等待",
                    snapshot_id=self.sid,
                )
                return False
        self.log.write(
            "STABILITY_CONFIRMED",
            f"连续 {self.stable_required} 次稳定成立, 进入睡眠收尾流程",
            snapshot_id=self.sid, version=self.version,
        )
        return True

    # ---------------- 工作线程 ----------------

    def _loop(self):
        try:
            while not self.stop_flag.is_set():
                self._poll_cycle()
                if self.stop_flag.is_set():
                    break
                if self._all_terminal() and self._confirm_stable():
                    self._step1_pre_sleep()
                    self.ready_event.set()
                    # 等主线程设停止标志, 再让本轮自然结束 (不强杀)
                    while not self.stop_flag.wait(0.2):
                        pass
                    break
                self._wait(self.poll_interval)
        except Exception as exc:  # noqa: BLE001
            if not self.log.closed:
                self.log.write("WORKER_EXCEPTION", repr(exc))
            self.events.emit("MONITOR_WORKER_EXCEPTION", repr(exc), snapshot_id=self.sid)
        finally:
            if not self.log.closed:
                self.log.close()

    # ---------------- STEP 1 ----------------

    def _terminal_summary(self):
        counts = Counter(t.get("status") for t in self.terminal_tasks.values())
        return {
            "total": len(self.terminal_tasks),
            "by_status": dict(counts),
            "stable_required": self.stable_required,
            "stability_interval_seconds": self.stability_interval,
        }

    def _step1_pre_sleep(self):
        self.pre_sleep_at = util.iso()
        summary = self._terminal_summary()
        summary_json = json.dumps(summary, ensure_ascii=False)
        # 1) 写"即将睡眠"日志 (时间戳 / 快照版本 / 任务终结摘要)
        self.log.write(
            "PRE_SLEEP", "即将睡眠: 快照内任务全部终结且连续稳定成立",
            at=self.pre_sleep_at,
            snapshot_id=self.sid, version=self.version,
            summary=summary_json,
        )
        # 2) flush 并 close 日志句柄, 不保留打开状态
        self.log.close()
        self.events.emit(
            "PRE_SLEEP", "即将睡眠 (日志已 flush+close)",
            snapshot_id=self.sid, version=self.version, summary=summary_json,
        )
        sleepctl.seq_write(
            self.cfg, self.sid, sleepctl.STEPS[0],
            "即将睡眠日志已写入并关闭句柄",
            at=self.pre_sleep_at, version=self.version, summary=summary_json,
        )

    # ---------------- 主线程收尾 (STEP 2-5) ----------------

    def _shutdown_sequence(self):
        # STEP2: 设停止标志, 等当前轮询周期自然结束、循环退出、在途检查完成 (join 确认)
        self.stop_flag.set()
        self.worker.join()          # 无超时、不强杀
        worker_alive = self.worker.is_alive()
        sleepctl.seq_write(
            self.cfg, self.sid, sleepctl.STEPS[1],
            "优雅停止监控: stop_flag 已设, join 完成",
            worker_alive=worker_alive, log_closed=self.log.closed,
            polling_stopped=not worker_alive,
        )

        # STEP3: 更新锁/快照状态为"已完成，即将睡眠", 写完关闭
        now = util.iso()
        self.lock.update("completed_pre_sleep", completed_at=now, snapshot_id=self.sid)
        self.store.mark(
            self.sid, "completed_pre_sleep",
            completed_at=now, sleep_prepared_at=now,
            terminal_summary=self._terminal_summary(),
        )
        sleepctl.seq_write(
            self.cfg, self.sid, sleepctl.STEPS[2],
            "锁与快照状态已更新为 completed_pre_sleep 并关闭",
            at=now,
        )

        # STEP4: 再次确认无残留写操作 (日志已关 / 锁已写 / 轮询已停)
        residual = {
            "log_closed": self.log.closed,
            "lock_written": (self.lock.read() or {}).get("status") == "completed_pre_sleep",
            "snapshot_written": self.store.load(self.sid)["status"] == "completed_pre_sleep",
            "worker_stopped": not worker_alive,
        }
        ok = all(residual.values())
        sleepctl.seq_write(
            self.cfg, self.sid,
            sleepctl.STEPS[3] if ok else "ABORT_SLEEP_RESIDUAL_WRITES",
            "确认无残留写操作" if ok else "检测到残留写操作, 放弃睡眠",
            **residual,
        )
        if not ok:
            self.events.emit("SLEEP_ABORTED", "残留写检查未通过, 未发出睡眠命令",
                             snapshot_id=self.sid, **residual)
            return False

        # STEP5: 写"已发出"日志 + 意图基线 -> 发出睡眠命令
        # 说明: STEP1-STEP4 确认的是"监控期在途写操作"已全部结束;
        #       本步的两条记录是同步 open->fsync->close 的最终封存记录,
        #       在真正派发命令之前即已写完并关闭。
        from . import powercap
        dry_run = self.cfg.dry_run
        terminal_summary = self._terminal_summary()
        mode, cmd = sleepctl.choose_command(self.cfg)
        caps = powercap.load(self.cfg)      # 启动监控前已探测并落盘，此处只读
        effect = powercap.effect_note(mode)
        sleepctl.write_intent(
            self.cfg, self.sid, self.version, dry_run, terminal_summary,
            self.pre_sleep_at, cmd,
            requested_mode=mode, power_capabilities=caps, effect_note=effect,
        )
        sleepctl.seq_write(
            self.cfg, self.sid, sleepctl.STEPS[4],
            f"已按 {mode} 发出一条命令(记录先于命令派发写毕并关闭)",
            dry_run=dry_run, at=util.iso(),
            requested_mode=mode, fallback_enabled=False,
            effect="进入低功耗; 实际可能为 sleep 或 hibernate; 两者都算成功",
            command=" ".join(str(c) for c in cmd)[:160],
            diag_log=sleepctl.diag_path(self.cfg).name,
            detached=bool(self.cfg["sleep"].get("detached", True)),
        )
        if dry_run:
            sleepctl.seq_write(
                self.cfg, self.sid, "DRY_RUN_SLEEP_SKIPPED",
                "dry-run: 不真正睡眠, 但已完整走完日志与状态更新流程",
            )
            self.events.emit(
                "DRY_RUN_SLEEP_SKIPPED",
                "dry-run 模式, 未真正发出睡眠命令; 日志与状态更新已全部完成",
                snapshot_id=self.sid, version=self.version,
            )
            print("[dry-run] 未真正睡眠。状态与日志已按固定顺序全部落盘并关闭。")
            return True

        ok_spawn, info = sleepctl.issue_sleep(self.cfg)
        # 不等待返回值、不依赖命令返回判断是否已睡、不等待退出、不主动 exit
        print(f"[sleep] 睡眠命令已派发 (pid/info={info}); 不等待返回值, 不等待进程退出。")
        if not ok_spawn:
            print(f"[WARN] 睡眠命令进程创建失败: {info}; 请运行 selfcheck 查看。")
        # 不再有持久「总闸」需要关回：真睡授权是 /api/monitor/start 的一次性
        # 确认令牌，用后即焚、不落盘，值守结束天然回到安全态。
        return True

    # ---------------- 外部中止 (绝不睡眠) ----------------

    def _abort(self, reason):
        self.aborted = True
        self.stop_flag.set()
        # 先写中止日志并 flush+close，再 join：工作线程退出时会检查 closed 并跳过，
        # 否则主日志里会缺少"为什么停了"这一条，早上无法复盘。
        if not self.log.closed:
            self.log.write("MONITOR_ABORTED", f"监控中止, 未发出睡眠命令: {reason}")
            self.log.close()
        if self.worker is not None:
            self.worker.join()
        self.lock.release(f"监控中止: {reason}")
        # 输出失败（无效 stdout / 日志 fd 已关等）不能把一次成功的优雅中止
        # 冒泡成 CLI 的「监控异常退出」，否则会多归档一个 stale lock、污染事件日志。
        try:
            self.events.emit("MONITOR_ABORTED", f"{reason}; 未发出睡眠命令", snapshot_id=self.sid)
            print(f"[abort] 监控已中止, 未发出睡眠命令。原因: {reason}")
        except Exception:  # noqa: BLE001 - 中止已经完成，输出只是尽力而为
            pass
        return False

    # ---------------- 入口 ----------------

    def start(self):
        try:
            self.stop_file.unlink()      # 清理上次遗留的停止请求
        except OSError:
            pass
        # 今晚要请求哪一种待机，必须在启动这一行就落进日志：
        # 早上复盘的第一个问题就是"这次到底按 sleep 还是 hibernate 发的"。
        requested_mode, _ = sleepctl.choose_command(self.cfg)
        self.log.write(
            "MONITOR_START", "监控启动",
            pid=os.getpid(), snapshot_id=self.sid, version=self.version,
            task_count=len(self.pending),
            adapters=self.pool.describe(),
            poll_interval_seconds=self.poll_interval,
            progress_interval_seconds=self.progress_interval,
            heartbeat_interval_seconds=self.heartbeat_interval,
            stable_required=self.stable_required,
            stability_interval_seconds=self.stability_interval,
            dry_run=self.cfg.dry_run,
            requested_mode=requested_mode,
        )
        self.log.write(
            "POLICY",
            "无超时阈值: 不对任何未终结任务做中断动作; 即使 Agent 死循环也持续等待, "
            "接受整夜不睡",
            timeout_enabled=False, interrupt_enabled=False,
        )
        self.log.write(
            "BOUND_SNAPSHOT", "监控绑定的人工核对版本",
            snapshot_id=self.sid, version=self.version,
            verified=self.snap.get("verified"), locked=self.snap.get("locked"),
            verified_at=self.snap.get("verified_at"),
            retry_decision=(self.snap.get("retry_risk") or {}).get("decision"),
        )
        self.events.emit(
            "MONITOR_START", "监控绑定指定快照版本启动",
            pid=os.getpid(), snapshot_id=self.sid, version=self.version,
            dry_run=self.cfg.dry_run, requested_mode=requested_mode,
        )
        self._heartbeat(force=True)
        self._progress(force=True)

        self.worker = threading.Thread(target=self._loop, name="monitor-worker", daemon=False)
        self.worker.start()

        try:
            while not self.ready_event.wait(0.5):
                if self.stop_flag.is_set():
                    return self._abort("收到外部停止信号")
                if self.stop_file.exists():
                    self.stop_flag.set()
                    return self._abort("界面请求停止监控（优雅停止，不发出睡眠命令）")
        except KeyboardInterrupt:
            return self._abort("KeyboardInterrupt (用户 Ctrl+C)")

        return self._shutdown_sequence()
