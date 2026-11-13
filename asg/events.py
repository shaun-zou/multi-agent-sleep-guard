"""全局事件日志 (logs/events-YYYYMMDD.log)。

记录扫描、快照版本变更、已核对标记、重试风险决定、监控生命周期、自检结果等。
每条写入都是 open->write->flush->fsync->close, 不长期持有句柄。
"""
from __future__ import annotations

from . import util


class EventLog:
    def __init__(self, cfg):
        self.dir = cfg.dir("logs_dir")

    @property
    def path(self):
        return self.dir / f"events-{util.today_str()}.log"

    def emit(self, event: str, msg: str = "", echo: bool = True, **kv) -> str:
        line = f"{util.iso()} | {event.upper()}"
        if msg:
            line += f" | {msg}"
        if kv:
            line += f" | {util.kv_str(kv)}"
        util.append_line(self.path, line)
        if echo:
            print(line)
        return line
