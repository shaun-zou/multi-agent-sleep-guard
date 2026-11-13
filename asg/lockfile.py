"""监控单例锁 (state/monitor.lock)。

- 启动时若存在 status=running 且 PID 仍存活的锁 -> 拒绝启动
- 若 PID 已死 (监控中途死亡) -> 归档为 lock-stale-*.json 后接管, 并留痕
- 终态流程第 3 步把状态更新为 completed_pre_sleep, 文件保留 (非"遗留锁")
"""
from __future__ import annotations

from . import util


class LockBusy(RuntimeError):
    pass


class LockFile:
    def __init__(self, cfg, events):
        self.dir = cfg.dir("state_dir")
        self.path = self.dir / "monitor.lock"
        self.events = events

    def read(self):
        return util.read_json(self.path)

    def _archive(self, reason):
        obj = util.read_json(self.path, {}) or {}
        dst = self.dir / f"lock-stale-{util.ts_compact()}.json"
        util.atomic_write_json(dst, {"archived_at": util.iso(), "reason": reason, "lock": obj})
        try:
            self.path.unlink()
        except OSError:
            pass
        self.events.emit("LOCK_ARCHIVED", reason, path=str(dst))
        return dst

    def acquire(self, snapshot_id, pid):
        obj = self.read()
        if obj:
            if obj.get("status") == "running" and util.pid_alive(
                    obj.get("pid"), obj.get("started_at")):
                raise LockBusy(
                    f"已有监控实例持有锁 (pid={obj.get('pid')}, "
                    f"snapshot_id={obj.get('snapshot_id')}, started_at={obj.get('started_at')})。"
                    f"拒绝重复启动。"
                )
            if obj.get("status") == "running":
                self._archive("持有锁的进程已死亡, 疑似监控中途退出")
            else:
                self._archive(f"上次监控已结束 (status={obj.get('status')})")
        new = {
            "pid": pid,
            "snapshot_id": snapshot_id,
            "started_at": util.iso(),
            "status": "running",
            "host": util.IS_WINDOWS,
        }
        util.atomic_write_json(self.path, new)
        self.events.emit("LOCK_ACQUIRED", "监控单例锁已获取",
                         pid=pid, snapshot_id=snapshot_id)
        return new

    def update(self, status, **fields):
        obj = self.read() or {}
        obj["status"] = status
        obj["updated_at"] = util.iso()
        obj.update(fields)
        util.atomic_write_json(self.path, obj)
        payload = {"snapshot_id": obj.get("snapshot_id")}
        for k, v in fields.items():
            payload.setdefault(k, str(v)[:120])
        self.events.emit("LOCK_STATUS", f"status={status}", **payload)
        return obj

    def release(self, reason):
        if self.path.exists():
            self._archive(reason)
        self.events.emit("LOCK_RELEASED", reason)
