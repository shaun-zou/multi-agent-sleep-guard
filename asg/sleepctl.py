"""睡眠顺序日志、睡眠意图记录、睡眠命令下发。

产品口径：只发一条命令，按 sleep.prefer 决定（默认 sleep），**不做运行时补发**。
实际进入睡眠还是休眠由系统决定，两者都算成功；日志只记录"请求模式"，
自检只报告"是否挂起 / 挂起多久"，不断言实际是 sleep 还是 hibernate。

固定顺序 (第七节), 每一步都写入 logs/sleep-sequence-YYYYMMDD.log:
  STEP1_PRE_SLEEP_WRITTEN_AND_CLOSED
  STEP2_MONITOR_STOPPED_JOINED
  STEP3_LOCK_SNAPSHOT_UPDATED_AND_CLOSED
  STEP4_RESIDUAL_WRITES_CONFIRMED_NONE
  STEP5_SLEEP_COMMAND_ISSUED        <-- "已发出"日志, 在真正派发命令前写毕并关闭

所有 seq_write / write_intent 都是 open->write->flush->fsync->close,
即"日志在睡眠指令发出前必须写完并关闭"。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time

from . import util

SEQ_BASENAME = "sleep-sequence"
INTENT_NAME = "sleep_intent.json"

STEPS = [
    "STEP1_PRE_SLEEP_WRITTEN_AND_CLOSED",
    "STEP2_MONITOR_STOPPED_JOINED",
    "STEP3_LOCK_SNAPSHOT_UPDATED_AND_CLOSED",
    "STEP4_RESIDUAL_WRITES_CONFIRMED_NONE",
    "STEP5_SLEEP_COMMAND_ISSUED",
]


def seq_path(cfg):
    return cfg.dir("logs_dir") / f"{SEQ_BASENAME}-{util.today_str()}.log"


def seq_paths(cfg):
    return sorted(cfg.dir("logs_dir").glob(f"{SEQ_BASENAME}-*.log"))


def seq_write(cfg, snapshot_id, step, msg="", echo=True, **kv) -> str:
    line = f"{util.iso()} | {step} | snapshot_id={snapshot_id}"
    if msg:
        line += f" | {msg}"
    if kv:
        line += f" | {util.kv_str(kv)}"
    util.append_line(seq_path(cfg), line)
    if echo:
        print(line)
    return line


def intent_path(cfg):
    return cfg.dir("state_dir") / INTENT_NAME


def choose_command(cfg):
    """按 sleep.prefer 选一条命令。只选一次，不做运行时补发。"""
    s = cfg["sleep"]
    prefer = str(s.get("prefer", "sleep")).lower()
    if prefer == "hibernate":
        return "hibernate", list(s.get("command_hibernate") or ["shutdown.exe", "/h"])
    return "sleep", list(s["command"])


def write_intent(cfg, snapshot_id, version, dry_run, terminal_summary,
                 pre_sleep_at, command, requested_mode="sleep",
                 power_capabilities=None, effect_note="", source="monitor"):
    """记录睡眠触发的时间线基线, 供 selfcheck 判断"系统是否真的进入低功耗"。

    unbiased_interrupt_100ns 不含系统挂起时间; 与墙钟差即为挂起时长。
    注意：该时钟能算出"挂起多久"，但无法区分 sleep 与 hibernate。
    source=monitor 是值守收尾流程；source=manual 是界面手动直接睡眠，
    后者不绑定快照，selfcheck 不应拿它去查快照完成状态。
    """
    obj = {
        "source": source,
        "snapshot_id": snapshot_id,
        "version": version,
        "dry_run": bool(dry_run),
        "about_to_sleep_at": pre_sleep_at,
        "command_issued_at": util.iso(),
        "wall_time_epoch": util.wall_now(),
        "tick64_ms": util.get_tick64_ms(),
        "unbiased_interrupt_100ns": util.get_unbiased_interrupt_100ns(),
        "biased_interrupt_100ns": util.get_biased_interrupt_100ns(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "terminal_summary": terminal_summary,
        "command": " ".join(str(c) for c in command),
        "requested_mode": requested_mode,
        "fallback_enabled": False,
        "runtime_fallback": "none: 只发一条命令, 不根据返回值补发 hibernate",
        "power_capabilities": power_capabilities or {},
        "effect_note": effect_note,
        "note": "本文件及全部日志已在发出睡眠命令前写完并关闭；"
                "实际进入睡眠或休眠由系统决定，两者都视为成功",
    }
    util.atomic_write_json(intent_path(cfg), obj)
    return obj


CALL = "[System.Windows.Forms.Application]::SetSuspendState("


def diag_path(cfg):
    """命令进程自写的诊断日志（按天一个文件，追加）。工具本身不写它。"""
    return cfg.dir("logs_dir") / f"sleep-command-{util.today_str()}.log"


def _diag_ps(path, mode):
    """追加到 PowerShell 命令末尾的诊断脚本，由**命令进程自己**写文件。

    这样既保留了"工具不等待返回值、不依赖返回值判断是否已睡"的规范，
    又能在早上复盘时区分「系统拒绝」与「命令进程根本没跑起来」。
    若诊断文件不存在 = 命令进程没执行到写日志处；存在则可看权限/会话/返回值。
    """
    p = str(path).replace("'", "''")
    return f"""
$asgErr = 'none'
if ($Error.Count -gt $asgErrBase) {{ $asgErr = [string]$Error[0].Exception.Message }}
$asgSession = 'unknown'
try {{ $asgSession = (Get-Process -Id $PID).SessionId }} catch {{}}
$asgAdmin = 'unknown'
try {{ $asgAdmin = (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }} catch {{}}
$asgDiag = @()
$asgDiag += "----"
$asgDiag += "at=$((Get-Date).ToString('o'))"
$asgDiag += "mode={mode}"
$asgDiag += "pid=$PID"
$asgDiag += "session=$asgSession"
$asgDiag += "userInteractive=$([Environment]::UserInteractive)"
$asgDiag += "isAdmin=$asgAdmin"
$asgDiag += "setSuspendStateReturn=$asgSetOk"
$asgDiag += "callError=$asgCallErr"
$asgDiag += "powershellErrors=$asgErr"
try {{ Add-Content -LiteralPath '{p}' -Value ($asgDiag -join [Environment]::NewLine) -Encoding UTF8 -ErrorAction Stop }} catch {{}}
"""


def build_command(cfg, mode=None, cmd=None):
    """构造实际派发的命令，并注入诊断留痕（不改动配置文件里的原始命令）。"""
    if mode is None or cmd is None:
        mode, cmd = choose_command(cfg)
    diag = diag_path(cfg)
    if mode == "hibernate":
        body = ("$asgErrBase = $Error.Count\n$asgCallErr = 'none'\n"
                "shutdown.exe /h | Out-Null; $asgSetOk = $LASTEXITCODE")
        return ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command",
                body + "\n" + _diag_ps(diag, mode)]
    # 只有标准 PowerShell -Command 形式才注入；用户自定义命令原样派发
    if len(cmd) < 2 or "powershell" not in str(cmd[0]).lower() or "-Command" not in cmd:
        return list(cmd)
    inner = cmd[-1]
    if CALL in inner:
        inner = inner.replace(CALL, "$asgSetOk = " + CALL, 1)
        # 把 API 调用包进 try/catch：即使抛异常，脚本也会继续写到诊断日志
        inner = re.sub(
            r"\$asgSetOk = (\[System\.Windows\.Forms\.Application\]::SetSuspendState\([^;]*\);)",
            lambda m: ("$asgCallErr = 'none'\ntry { $asgSetOk = " + m.group(1) +
                       " } catch { $asgSetOk = 'EXCEPTION'; "
                       "$asgCallErr = [string]$_.Exception.Message }"),
            inner, count=1)
    else:
        inner = inner + "\n$asgSetOk = 'custom-command'\n$asgCallErr = 'none'"
    inner = "$asgErrBase = $Error.Count\n" + inner
    inner += "\n" + _diag_ps(diag, mode)
    return list(cmd[:-1]) + [inner]


def issue_sleep(cfg):
    """发出一条命令, 不等待返回值、不等待退出、不主动 exit、不补发。

    默认按 sleep 请求（WinForms SetSuspendState('Suspend')）；
    prefer=hibernate 时用 shutdown /h；绝不使用 rundll32 写法。

    规范保持：工具自身的日志/意图记录已在调用前写完并关闭；
    本函数不写任何文件、不等待子进程、不读取诊断结果、不据此判断是否已睡。
    诊断日志由**命令进程自己**追加，仅用于事后复盘。
    """
    mode, chosen = choose_command(cfg)
    cmd = build_command(cfg, mode, chosen)
    flags = 0
    if util.IS_WINDOWS:
        # 实测：DETACHED_PROCESS (0x8) 会让 PowerShell 子进程（无控制台）根本不执行，
        # Popen 成功但脚本一行都没跑，导致"命令已发出却没睡"且无诊断日志。
        # 改用 CREATE_NO_WINDOW (0x08000000)：不弹控制台窗口，子进程可正常执行；
        # 父进程立即退出也不影响它（已验证）。detached 配置现语义为"不弹窗口"。
        flags = 0x08000000 if cfg["sleep"].get("detached", True) else 0
    try:
        proc = subprocess.Popen(
            cmd,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True, proc.pid
    except Exception as exc:  # noqa: BLE001 - 不能写文件, 只能回报调用方
        return False, repr(exc)
