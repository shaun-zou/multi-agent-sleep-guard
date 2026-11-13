"""通用工具: 时间、原子落盘、fsync、进程存活、Windows 无偏中断时间。

路径有两套, 打包(PyInstaller)前后都必须正确:

  ROOT    可写数据根。源码运行 = 项目目录; 冻结成 exe 后 = **exe 所在目录**。
          config*.json / logs / snapshots / state / rules 全部落在这里, 所以
          用户把 exe 复制到 D:\\夜间值守 也能正常读写自己的数据。
  BUNDLE  只读资源根。源码运行 = 项目目录; 冻结后 = PyInstaller 解包目录
          sys._MEIPASS (ui/ 静态页面打在这里, 不随 exe 复制到外部)。

两者在源码模式下是同一个目录, 只有在 frozen 时才分叉。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _resolve_root() -> Path:
    """可写数据根。冻结后 __file__ 在 _MEIPASS 临时目录里, 绝不能用它当锚点。"""
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_bundle() -> Path:
    """只读资源根 (ui/ 等)。"""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    return ROOT


# 项目根目录 = 包含 asg/ 与 config.json 的目录。所有相对路径都锚定在这里,
# 因此从任意工作目录调用入口脚本都不会把日志/快照写到别处。
ROOT = _resolve_root()
BUNDLE = _resolve_bundle()


def cli_argv(args) -> list[str]:
    """构造「跑一次 CLI 子进程」的完整命令行。

    界面/托盘/自检都靠子进程复用 CLI, 这里必须是唯一入口:
      - 源码运行: [python, <项目>/agent_sleep_guard.py, ...]
      - 冻结运行: [<exe>, --asg-cli, ...]  (exe 自己是分发器, 见 pack_entry.py)

    冻结态不能用 sys.executable + 脚本路径: 那个 .py 根本不在 exe 旁边。
    """
    args = [str(a) for a in args]
    if IS_FROZEN:
        return [sys.executable, "--asg-cli"] + args
    return [sys.executable, str(ROOT / "agent_sleep_guard.py")] + args


CREATE_NO_WINDOW = 0x08000000


def spawn_kwargs() -> dict:
    """后台子进程的创建参数：Windows 下一律无窗口。

    为什么是「一律」而不是只有打包版才加：控制台子系统的程序被拉起时，
    如果父进程没有可继承的控制台，Windows 会给它**新开一个黑框**。界面点
    「扫描/自检/规则确认」时调用的是 `python agent_sleep_guard.py …`（源码版）
    或 exe（打包版），都不该弹窗；更隐蔽的是**嵌套**——扫描内部还要跑
    `tasklist`、电源探测要跑 `powercfg`、自检要跑 `powershell`，这些孙进程
    同样会各弹一次，所以每个 spawn 点都必须带上这个标志。

    调用方都已用管道/文件接管 stdin/stdout，不依赖控制台，因此隐藏窗口不影响
    输出采集（`capture_output=True` 照常工作）。
    """
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def abs_path(p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p)


def resource_path(rel) -> Path:
    """随包只读资源（如 cases/ 用例集）的定位。

    优先**exe 旁边**的同名文件（允许用户不重新打包就替换用例集/模板），
    找不到再回落到包内副本（BUNDLE）。源码模式下两者是同一个目录。
    """
    p = abs_path(rel)
    if p.exists():
        return p
    q = Path(rel)
    return q if q.is_absolute() else (BUNDLE / q)


def now_dt():
    return datetime.now().astimezone()


def iso(dt=None) -> str:
    return (dt or now_dt()).isoformat(timespec="seconds")


def ts_compact(dt=None) -> str:
    return (dt or now_dt()).strftime("%Y%m%dT%H%M%S")


def today_str(dt=None) -> str:
    return (dt or now_dt()).strftime("%Y%m%d")


def ensure_dir(p) -> Path:
    path = Path(p)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_json(path, obj) -> Path:
    """写临时文件 -> flush + fsync -> os.replace -> 关闭。

    任何时刻崩溃都不会留下半截 JSON; 返回时文件句柄已关闭。
    """
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def append_line(path, line: str) -> None:
    """单条日志: open('a') -> write -> flush -> fsync -> close。

    每次写入后句柄立即关闭, 满足"睡眠指令发出前所有日志写完并关闭"。
    """
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_lines(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


class _FileTime(ctypes.Structure):
    """Windows FILETIME (100ns 为单位, 起点 1601-01-01 UTC)。"""
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


# 1601-01-01 -> 1970-01-01 的 100ns 间隔数, 用于把 FILETIME 换成 Unix 秒
_FT_UNIX_EPOCH = 116444736000000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def process_created_at(pid):
    """返回进程创建时间 (Unix 秒, float); 取不到返回 None。

    Windows: OpenProcess + GetProcessTimes, 零第三方依赖。
    用途: 识别 **PID 复用** —— 系统会把已退出进程的 PID 分配给新程序，
    只判断「该 PID 是否存在」会把无关程序误认成我们的监控。
    """
    if not IS_WINDOWS:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation, exit_t, kernel_t, user_t = (_FileTime() for _ in range(4))
            ok = k32.GetProcessTimes(
                ctypes.c_void_p(handle), ctypes.byref(creation),
                ctypes.byref(exit_t), ctypes.byref(kernel_t), ctypes.byref(user_t))
            if not ok:
                return None
            ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return (ft - _FT_UNIX_EPOCH) / 1e7
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:  # noqa: BLE001 - 取不到就降级, 不影响主流程
        return None


def _pid_exists(pid):
    """只判断 PID 当前是否对应一个仍在运行的进程。"""
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def pid_alive(pid, started_at=None, tolerance=120.0) -> bool:
    """判断「我们启动的那个监控进程」是否还在跑。

    - 不传 started_at: 保持旧行为, 只判断 PID 是否存在 (向后兼容)。
    - 传 started_at: 额外校验进程创建时间与记录时间是否一致 (容差默认 120s),
      防止系统回收并复用 PID 后, 把 `parfait-helper` 之类的无关进程误判成监控。
    - 拿不到进程创建时间时降级为「只判存在」, 不擅自否定存活 (保守, 不误启动第二个实例)。
    """
    exists = _pid_exists(pid)
    if not exists or started_at is None:
        return exists
    created = process_created_at(pid)
    if created is None:
        return True
    try:
        dt = datetime.fromisoformat(str(started_at))
        if dt.tzinfo is None:          # 没有时区的旧记录按本地时区解释
            dt = dt.astimezone()
        expected = dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        return True
    return abs(created - expected) <= tolerance


def pid_reused(pid, started_at, tolerance=120.0) -> bool:
    """显式识别 PID 复用: PID 仍存在, 但对应的已经不是我们启动的进程。"""
    if not _pid_exists(pid) or started_at is None:
        return False
    created = process_created_at(pid)
    if created is None:
        return False
    try:
        dt = datetime.fromisoformat(str(started_at))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        expected = dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        return False
    return abs(created - expected) > tolerance


def pid_created_iso(pid):
    """返回进程创建时间的本地 ISO 字符串, 供诊断留痕; 取不到返回 None。"""
    t = process_created_at(pid)
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return None


def get_tick64_ms():
    """GetTickCount64 (自开机起的毫秒, 含睡眠时间)。用于识别系统是否重启。"""
    if not IS_WINDOWS:
        return None
    try:
        return int(ctypes.windll.kernel32.GetTickCount64())
    except Exception:
        return None


def get_unbiased_interrupt_100ns():
    """QueryUnbiasedInterruptTime: 单位 100ns, **不含系统睡眠/休眠时间**。

    与墙钟时间对比即可精确估算"系统实际睡了多久", 用于 selfcheck 识别
    "睡眠命令已发出但系统未睡"。Win7+ 可用, 无需管理员权限。
    """
    if not IS_WINDOWS:
        return None
    try:
        value = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value))
        return int(value.value) if ok else None
    except Exception:
        return None


def get_biased_interrupt_100ns():
    """QueryInterruptTime: 含睡眠时间, 作为辅助信号 (Win10+)。"""
    if not IS_WINDOWS:
        return None
    try:
        value = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.QueryInterruptTime(ctypes.byref(value))
        return int(value.value) if ok else None
    except Exception:
        return None


def deep_merge(base, override):
    """递归合并字典, override 优先。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_bool(s, default=False):
    if s is None:
        return default
    if isinstance(s, bool):
        return s
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on")


def kv_str(d) -> str:
    return " ".join(f"{k}={v}" for k, v in (d or {}).items())


def wall_now() -> float:
    return time.time()
