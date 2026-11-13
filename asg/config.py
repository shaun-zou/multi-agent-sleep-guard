"""配置加载: 默认值 + config.json 覆盖, 全部路径锚定项目根目录。"""
from __future__ import annotations

import copy
from pathlib import Path

from . import util

DEFAULT_CONFIG = {
    "schema_version": 1,

    # ---- 轮询与判定 ----
    "poll_interval_seconds": 30,          # 常规轮询间隔
    "progress_interval_seconds": 60,      # 进度日志间隔
    "heartbeat_interval_seconds": 3600,   # 存活心跳间隔
    "heartbeat_max_interval_seconds": 3600,  # 上限, 保证"每小时至少一条"
    "stable_required": 3,                 # 终态复核连续稳定次数
    "stability_interval_seconds": 60,     # 稳定复核间隔 (规范 1-2 分钟)
    "terminal_statuses": ["completed", "failed", "cancelled"],
    "unknown_status_is_terminal": False,  # 状态读不到时绝不视为完成

    # ---- 路径 ----
    "paths": {
        "snapshots_dir": "snapshots",
        "logs_dir": "logs",
        "state_dir": "state",
        "data_dir": "data",
    },

    # ---- 睡眠 ----
    # prefer 只决定「发哪一条命令」；只发一条，不做运行时补发 / fallback。
    # 实际进入睡眠还是休眠由本机电源设置与组策略决定，两者都视为成功（见 powercap.py）。
    "sleep": {
        "dry_run": True,                   # 默认安全: 不真正睡眠
        "detached": True,
        "prefer": "sleep",                # sleep | hibernate
        # 界面不再有持久「总闸」：每次开启监控都要在界面做一次即时确认，
        # 由 /api/monitor/start 校验一次性确认令牌（服务端内存、用后即焚）。
        # 命令行 --dry-run 仍保留，用于演练/回归自测。
        # 禁止 rundll32.exe powrprof.dll,SetSuspendState 写法。
        # sleep 用 Windows Forms 显式请求 Suspend。
        "command": [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false);",
        ],
        # hibernate 用系统自带 shutdown /h（需本机启用休眠；不可用也只发这一条，不补发）
        "command_hibernate": ["shutdown.exe", "/h"],
    },

    # ---- selfcheck ----
    "selfcheck": {
        "issued_but_not_slept_wall_seconds": 600,  # 发出命令后超过该墙钟时长仍未睡 -> 告警
        "sleep_duration_threshold_seconds": 60,    # 估算睡眠时长 >= 该值视为真睡过
        "heartbeat_max_gap_seconds": 5400,         # 心跳最大允许断档 (1.5h)
    },

    # ---- 适配器 ----
    "adapters": [
        {
            "type": "mock_file",
            "name": "mock_file",
            "enabled": True,
            "options": {
                "state_file": "data/agents_state.json",
                "status_source": "field",       # field | mtime | both
                "idle_seconds": 0,              # mtime 模式: 日志静默多久视为终态
                "idle_implies_status": "completed",
                "log_dir": "data/logs",
            },
        },
    ],
}


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else util.ROOT / "config.json"
        raw = util.read_json(self.path, {})
        if raw is None:
            raw = {}
        self.raw = util.deep_merge(copy.deepcopy(DEFAULT_CONFIG), raw)

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)

    def dir(self, key) -> Path:
        """返回 paths 中某个目录的绝对路径, 并确保存在。"""
        return util.ensure_dir(util.abs_path(self.raw["paths"][key]))

    def rel(self, key) -> str:
        return self.raw["paths"][key]

    def set_dry_run(self, value: bool):
        self.raw["sleep"]["dry_run"] = bool(value)

    def set_prefer(self, value: str):
        """把待机方式写回**配置文件本身**（只改 sleep.prefer 一个字段）。

        必须只读原文件再写回，不能写 self.raw：self.raw 是「默认值 + 覆盖」的
        深合并结果，整份写回会把所有默认值都固化成用户配置，之后调默认值就失效。
        返回 (旧值, 新值)。
        """
        value = str(value).strip().lower()
        if value not in ("sleep", "hibernate"):
            raise ValueError(f"无效的待机方式: {value!r}（只能是 sleep 或 hibernate）")
        raw = util.read_json(self.path, {})
        if not isinstance(raw, dict):
            raw = {}
        sleep = raw.get("sleep")
        if not isinstance(sleep, dict):
            sleep = {}
        old = str(sleep.get("prefer", "sleep")).lower()
        sleep["prefer"] = value
        raw["sleep"] = sleep
        util.atomic_write_json(self.path, raw)
        self.raw["sleep"]["prefer"] = value
        return old, value

    @property
    def prefer(self) -> str:
        return str(self.raw["sleep"].get("prefer", "sleep")).lower()

    @property
    def dry_run(self) -> bool:
        return bool(self.raw["sleep"]["dry_run"])

    @property
    def heartbeat_interval(self) -> int:
        return max(1, min(int(self.raw["heartbeat_interval_seconds"]),
                          int(self.raw["heartbeat_max_interval_seconds"])))

    @property
    def terminal_statuses(self):
        return set(self.raw["terminal_statuses"])
