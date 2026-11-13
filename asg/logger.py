"""日志句柄封装。

监控主日志在进程内保持打开 (逐行 flush+fsync); 触发睡眠前必须 close,
close 之后所有 write 静默返回 False, 杜绝"日志已关还在写"的残留写操作。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import util


class FileLogger:
    def __init__(self, path, header=None):
        self.path = Path(path)
        util.ensure_dir(self.path.parent)
        self._f = open(self.path, "a", encoding="utf-8")
        self._closed = False
        if header:
            self.write("HEADER", header)

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, level: str, msg: str, **kv) -> bool:
        if self._closed:
            return False
        parts = [util.iso(), level.upper(), msg]
        if kv:
            parts.append(util.kv_str(kv))
        self._f.write(" | ".join(parts) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())
        return True

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._f.flush()
            os.fsync(self._f.fileno())
        finally:
            self._f.close()
            self._closed = True
