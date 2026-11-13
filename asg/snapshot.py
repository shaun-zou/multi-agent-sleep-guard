"""快照存储与生命周期。

版本规则:
  - 每次 scan 生成 SNAP-<时间戳>-v1 (family_id = SNAP-<时间戳>)
  - 每次人工修正 (revise) 在同一 family 下生成 v2/v3/...,
    并记录 parent_id / parent_version / change_log (原版本 -> 新版本, 增了哪些、删了哪些)
  - verify (人工核对) 把**具体版本**标记为 verified + locked, 之后禁止再修订,
    monitor 只接受 verified 且 locked 的快照, 并记录实际绑定的版本号。

快照状态: draft -> verified -> monitoring -> completed_pre_sleep
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import util


RETRY_DECISIONS = ("no_action", "accept_untracked_retry", "watch_retry_chain")


class SnapshotError(RuntimeError):
    pass


class SnapshotStore:
    def __init__(self, cfg, events):
        self.dir = cfg.dir("snapshots_dir")
        self.events = events

    # ---------- 基础 ----------

    def path_of(self, snapshot_id: str) -> Path:
        return self.dir / f"{snapshot_id}.json"

    def load(self, snapshot_id: str) -> dict:
        obj = util.read_json(self.path_of(snapshot_id))
        if obj is None:
            raise SnapshotError(f"快照不存在: {snapshot_id} (路径 {self.path_of(snapshot_id)})")
        return obj

    def list(self):
        items = []
        for p in sorted(self.dir.glob("*.json")):
            obj = util.read_json(p, {}) or {}
            items.append({
                "snapshot_id": obj.get("snapshot_id", p.stem),
                "version": obj.get("version"),
                "source": obj.get("source"),
                "created_at": obj.get("created_at"),
                "task_count": len(obj.get("tasks", [])),
                "verified": obj.get("verified", False),
                "locked": obj.get("locked", False),
                "status": obj.get("status"),
                "retry_decision": (obj.get("retry_risk") or {}).get("decision"),
            })
        return items

    def show(self, snapshot_id: str) -> dict:
        return self.load(snapshot_id)

    # ---------- 阶段一: scan ----------

    def create_from_scan(self, tasks, adapters_desc="", note=""):
        family = f"SNAP-{util.ts_compact()}"
        # 同一秒重复 scan 时避免覆盖
        suffix = 0
        while self.path_of(f"{family}-v1").exists():
            suffix += 1
            family = f"SNAP-{util.ts_compact()}-{suffix}"
        snapshot_id = f"{family}-v1"
        obj = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "family_id": family,
            "version": 1,
            "parent_id": None,
            "parent_version": None,
            "source": "scan",
            "created_at": util.iso(),
            "adapters": adapters_desc,
            "note": note,
            "tasks": copy.deepcopy(list(tasks)),
            "task_count": len(tasks),
            "verified": False,
            "verified_at": None,
            "verified_by": None,
            "locked": False,
            "locked_at": None,
            "retry_risk": None,
            "status": "draft",
            "status_updated_at": util.iso(),
            "monitor_bindings": [],
            "change_log": [],
            "completed_at": None,
            "sleep_prepared_at": None,
        }
        util.atomic_write_json(self.path_of(snapshot_id), obj)
        self.events.emit(
            "SCAN", "手动扫描完成", snapshot_id=snapshot_id, version=1,
            task_count=len(tasks), adapters=adapters_desc,
        )
        for t in obj["tasks"]:
            self.events.emit(
                "SCAN_TASK", "", echo=False,
                snapshot_id=snapshot_id,
                task_id=t.get("task_id"), agent=t.get("agent"),
                start_time=t.get("start_time"), status=t.get("status"),
                source=t.get("source"),
            )
        return obj

    # ---------- 阶段一: 人工修正 ----------

    def revise(self, snapshot_id, adds=None, removes=None, tasks_file=None,
               reason="", operator=""):
        old = self.load(snapshot_id)
        if old.get("verified") or old.get("locked"):
            raise SnapshotError(
                f"快照 {snapshot_id} 已由人工核对并锁定, 不可直接修订。"
                f"请重新执行 scan 生成新快照, 再在新版本上核对。"
            )
        if old.get("status") != "draft":
            raise SnapshotError(f"快照 {snapshot_id} 状态为 {old.get('status')}, 不允许修订")

        if tasks_file:
            payload = util.read_json(util.abs_path(tasks_file))
            if payload is None:
                raise SnapshotError(f"任务清单文件不存在或不是合法 JSON: {tasks_file}")
            new_tasks = payload.get("tasks", payload) if isinstance(payload, dict) else payload
            new_tasks = copy.deepcopy(list(new_tasks))
        else:
            new_tasks = copy.deepcopy(old["tasks"])
            remove_set = {str(x) for x in (removes or [])}
            new_tasks = [t for t in new_tasks if str(t.get("task_id")) not in remove_set]
            existing = {str(t.get("task_id")) for t in new_tasks}
            for item in (adds or []):
                tid = str(item.get("task_id", ""))
                if not tid:
                    raise SnapshotError(f"新增任务缺少 task_id: {item}")
                if tid in existing:
                    raise SnapshotError(f"新增任务 task_id 重复: {tid}")
                normalized = {
                    "task_id": tid,
                    "agent": item.get("agent", "unknown-agent"),
                    "start_time": item.get("start_time", util.iso()),
                    "status": item.get("status", "running"),
                    "last_active": item.get("last_active", item.get("start_time", util.iso())),
                    "source": item.get("source", old["tasks"][0].get("source") if old["tasks"] else "manual"),
                    "manually_added": True,
                }
                for k, v in item.items():
                    normalized.setdefault(k, v)
                new_tasks.append(normalized)
                existing.add(tid)

        old_ids = {str(t.get("task_id")) for t in old["tasks"]}
        new_ids = {str(t.get("task_id")) for t in new_tasks}
        added_ids = sorted(new_ids - old_ids)
        removed_ids = sorted(old_ids - new_ids)
        if not added_ids and not removed_ids:
            raise SnapshotError("修正后任务清单无变化, 不生成新版本")

        new_version = int(old["version"]) + 1
        new_id = f"{old['family_id']}-v{new_version}"
        change = {
            "at": util.iso(),
            "from": snapshot_id,
            "from_version": old["version"],
            "to": new_id,
            "to_version": new_version,
            "added": added_ids,
            "removed": removed_ids,
            "reason": reason,
            "operator": operator,
        }
        obj = copy.deepcopy(old)
        obj.update({
            "snapshot_id": new_id,
            "version": new_version,
            "parent_id": snapshot_id,
            "parent_version": old["version"],
            "source": "revise",
            "created_at": util.iso(),
            "tasks": new_tasks,
            "task_count": len(new_tasks),
            "change_log": list(old.get("change_log", [])) + [change],
            "status": "draft",
            "status_updated_at": util.iso(),
        })
        util.atomic_write_json(self.path_of(new_id), obj)
        self.events.emit(
            "SNAPSHOT_REVISE", "人工修正清单, 已重新生成版本号",
            **{
                "from": snapshot_id,
                "to": new_id,
                "from_version": old["version"],
                "to_version": new_version,
                "added": json.dumps(added_ids, ensure_ascii=False),
                "removed": json.dumps(removed_ids, ensure_ascii=False),
                "reason": reason or "-",
                "operator": operator or "-",
            },
        )
        self.events.emit(
            "SNAPSHOT_VERSION_CHANGE", f"{snapshot_id} -> {new_id}",
            echo=False, added_count=len(added_ids), removed_count=len(removed_ids),
        )
        return obj

    # ---------- 阶段一: 人工核对 / 重试风险决定 ----------

    def verify(self, snapshot_id, retry_reuses_id: bool, decision: str,
               watch_tasks=None, note="", operator="", explicit=True):
        obj = self.load(snapshot_id)
        if obj.get("verified"):
            raise SnapshotError(f"快照 {snapshot_id} 已核对过, 不可重复核对")
        if decision not in RETRY_DECISIONS:
            raise SnapshotError(f"无效的重试风险决定: {decision}; 可选 {RETRY_DECISIONS}")
        if retry_reuses_id and decision != "no_action":
            raise SnapshotError("重试沿用原任务 ID 时, decision 应为 no_action")
        if (not retry_reuses_id) and decision == "no_action":
            raise SnapshotError(
                "重试不沿用原 ID 时存在'重试任务不在快照内、监控不会跟踪'的风险, "
                "必须显式二选一: watch_retry_chain 或 accept_untracked_retry, 不得隐含"
            )

        watch_set = {str(x) for x in (watch_tasks or [])}
        if decision == "watch_retry_chain":
            known = {str(t.get("task_id")) for t in obj["tasks"]}
            unknown = watch_set - known
            if unknown:
                raise SnapshotError(f"需观察重试链的任务不在快照内: {sorted(unknown)}")
            if not watch_set:
                # 未指定则标记全部任务, 便于人工逐条复核
                watch_set = known
            for t in obj["tasks"]:
                t["need_watch_retry_chain"] = str(t.get("task_id")) in watch_set

        retry_risk = {
            "retry_reuses_task_id": bool(retry_reuses_id),
            "decision": decision,
            "decided_at": util.iso(),
            "decided_by": operator or "-",
            "note": note or "-",
            "watch_tasks": sorted(watch_set),
            # 界面主路径用默认值，不算人工逐条判断；CLI 显式决定时为 True。
            "explicit": bool(explicit),
        }
        obj.update({
            "verified": True,
            "verified_at": util.iso(),
            "verified_by": operator or "-",
            "locked": True,
            "locked_at": util.iso(),
            "retry_risk": retry_risk,
            "status": "verified",
            "status_updated_at": util.iso(),
        })
        util.atomic_write_json(self.path_of(snapshot_id), obj)

        self.events.emit(
            "SNAPSHOT_VERIFY", "人工核对确认, 已锁定该具体版本作为监控依据",
            snapshot_id=snapshot_id, version=obj["version"],
            task_count=obj["task_count"], verified=obj["verified"],
            locked=obj["locked"], operator=operator or "-",
        )
        risk_msg = (
            "重试沿用原任务 ID, 无额外处理" if retry_reuses_id
            else "重试会生成新 ID, 该重试任务不在快照内、监控不会跟踪"
        )
        decision_msg = {
            "no_action": "决定=no_action(无需额外处理)",
            "accept_untracked_retry": "决定=accept_untracked_retry(显式接受: 快照内旧任务终结即视为完成, 重试任务不跟踪)",
            "watch_retry_chain": "决定=watch_retry_chain(标记需观察重试链, 用 Agent 级活跃信号兜底)",
        }[decision]
        self.events.emit(
            "RETRY_RISK_DECISION", f"{risk_msg}; {decision_msg}",
            snapshot_id=snapshot_id,
            retry_reuses_task_id=retry_reuses_id,
            decision=decision,
            watch_tasks=json.dumps(sorted(watch_set), ensure_ascii=False),
            note=note or "-",
            operator=operator or "-",
        )
        if decision == "accept_untracked_retry":
            self.events.emit(
                "RISK_ACCEPTED", "已显式接受未跟踪重试任务的风险 (留痕, 非隐含)",
                snapshot_id=snapshot_id,
                consequence="若 Agent 在快照内任务终结后重试出新任务, 系统仍会睡眠",
            )
        return obj

    # ---------- 状态更新 ----------

    def mark(self, snapshot_id, status, **fields):
        obj = self.load(snapshot_id)
        obj["status"] = status
        obj["status_updated_at"] = util.iso()
        for k, v in fields.items():
            obj[k] = v
        util.atomic_write_json(self.path_of(snapshot_id), obj)
        payload = {"snapshot_id": snapshot_id, "version": obj["version"]}
        for k, v in fields.items():
            payload.setdefault(k, str(v)[:120])
        self.events.emit("SNAPSHOT_STATUS", f"status={status}", **payload)
        return obj

    def add_binding(self, snapshot_id, pid):
        obj = self.load(snapshot_id)
        bindings = list(obj.get("monitor_bindings", []))
        bindings.append({"at": util.iso(), "pid": pid, "event": "monitor_start"})
        obj["monitor_bindings"] = bindings
        util.atomic_write_json(self.path_of(snapshot_id), obj)
        return obj
