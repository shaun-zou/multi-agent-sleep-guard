"""电源能力探测 + 待机方式选择（只决定「请求哪一种」，绝不改变「只发一条」）。

三层概念，界面上必须分清（用户明确的产品口径）：
  1. 可用性（capability）：本机到底支不支持 待机(S1/S2/S3)、S0 现代待机、休眠。
     来源 powercfg /a，只读探测、只用于提示与留痕。
  2. 选择（preference）：sleep.prefer = sleep | hibernate，只在设置层决定「发哪一条命令」。
  3. 效果（outcome）：是否挂起、挂起多久 —— 只能事后判定，且**不断言**到底是 sleep 还是
     hibernate（Kernel-Power 107 在休眠恢复时也会产生）。

产品口径（用户明确，不能违反）：
  - 只发**一条**命令，按 sleep.prefer 决定（默认 sleep），不做运行时补发 / fallback；
  - 实际进入睡眠还是休眠，由本机电源设置、组策略、休眠开关等决定，两者都视为成功；
  - 因此本模块的探测结果不参与选择命令，也不触发任何重试。
"""
from __future__ import annotations

import ctypes
import re
import subprocess
from datetime import datetime

from . import util

CAP_NAME = "power-capabilities.json"

# 探测结果的有效期：超过这个时长，界面会提示"结果可能已过期，建议重新检测"。
# 只影响提示文案，不影响任何命令。
STALE_SECONDS = 6 * 3600

EFFECT_TEMPLATE = (
    "命令按 {mode} 发出，只发一条、不补发。预期效果是电脑进入低功耗状态；"
    "由于电源设置、组策略或休眠开关，实际可能表现为睡眠(S3/S0)或休眠(hibernate)，"
    "两者都视为成功。"
)

FORCE_SLEEP_HINTS = [
    "保持 sleep.prefer = sleep（本工具只发一条命令，不补发）",
    "管理员运行 powercfg /a，确认 Standby (S3) 或 S0 Low Power Idle 出现在可用段",
    "若因休眠启用而被系统转为休眠：powercfg /hibernate off（会同时关闭快速启动）",
    "检查组策略「允许待机状态(S1-S3)」是否禁用，更新 BIOS / 芯片组 / 显卡驱动",
    "powercfg /requests 查并清掉持有电源请求的进程或设备",
]

PREFER_HIBERNATE_HINTS = [
    "把 sleep.prefer 设为 hibernate，命令改用 shutdown /h",
    "管理员运行 powercfg /hibernate on，确认 hiberfil.sys 有足够磁盘空间",
    "提醒：休眠唤醒比睡眠慢，且会影响 Windows 快速启动",
]

# 状态名 -> 规范键。顺序重要：先匹配更具体的（快速启动/混合/S0），再匹配通用待机。
_STATE_RULES = (
    ("fast_startup", ("快速启动", "fast startup")),
    ("hybrid", ("混合睡眠", "hybrid sleep")),
    ("modern_standby", ("s0", "低电量待机", "low power idle")),
    ("hibernate", ("休眠", "hibernate")),
    ("sleep_s3", ("待机 (s3)", "待机(s3)", "standby (s3)")),
    ("sleep_s1s2", ("待机 (s1)", "待机(s1)", "待机 (s2)", "待机(s2)",
                    "standby (s1)", "standby (s2)")),
)

_STATE_LABELS = {
    "sleep_s3": "待机 S3",
    "sleep_s1s2": "待机 S1/S2",
    "modern_standby": "S0 现代待机",
    "hibernate": "休眠 Hibernate",
    "hybrid": "混合睡眠",
    "fast_startup": "快速启动",
}


def _cmd_output(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=15,
                           **util.spawn_kwargs())
        raw = p.stdout or b""
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return f"[探测失败: {exc!r}]"


def _lines(txt):
    return txt.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_states(txt):
    """把 powercfg /a 解析成 {规范键: {name, available, reasons}}。

    结构：缩进 0 = 段标题（可用 / 不可用）；缩进 1~6 = 状态名；
    更深（或用制表符）= 该状态的原因说明。
    """
    states = {}
    section = None
    cur = None
    for raw in _lines(txt):
        s = raw.strip()
        if not s:
            continue
        # powercfg /a 的"原因"行用的是制表符缩进（不是 8 个空格），
        # 必须显式区分，否则会把原因行误判成状态名、把原因全丢掉。
        pad = re.match(r"[ \t]*", raw).group(0)
        is_reason = ("\t" in pad) or len(pad) >= 7
        indent = len(pad)
        low = s.lower()
        if indent == 0:
            if any(k in low for k in ("没有以下", "not available", "not supported")):
                section = "unavailable"
            elif any(k in low for k in ("有以下", "available on this system")):
                section = "available"
            cur = None
            continue
        if section is None:
            continue
        if not is_reason:
            cur = {"name": s, "available": section == "available", "reasons": []}
            key = None
            for k, tokens in _STATE_RULES:
                if any(t in low for t in tokens):
                    key = k
                    break
            if key and key not in states:      # 同一状态只记第一次（S3 优先于 S1/S2）
                states[key] = cur
        elif cur is not None:
            cur["reasons"].append(s)
    return states


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return None


def _blockers(req_txt):
    """从 powercfg /requests 里挑出真正持有电源请求的行（去掉表头与"无"）。"""
    out = []
    for raw in _lines(req_txt):
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        if low in ("none", "无", "无。") or "powercfg" in low:
            continue
        if s.endswith(":") or s.endswith("："):     # 分类小标题
            continue
        if len(s) > 4:
            out.append(s[:200])
    return out[:6]


def probe():
    """只读探测本机电源能力。返回结果 dict；任何失败都降级为空结果，绝不抛异常。"""
    a_txt = _cmd_output(["powercfg.exe", "/a"])
    req_txt = _cmd_output(["powercfg.exe", "/requests"])
    states = parse_states(a_txt)

    def ok(key):
        st = states.get(key)
        return bool(st and st["available"])

    sleep_keys = [k for k in ("sleep_s3", "modern_standby", "sleep_s1s2") if ok(k)]
    hib = states.get("hibernate")
    # 兼容旧字段：sleep_available 表示"任意一种待机可用"
    sleep_available = bool(sleep_keys)
    # 现代待机(S0ix) 优先展示，其次 S3
    sleep_kind = ("S0" if "modern_standby" in sleep_keys
                  else ("S3" if "sleep_s3" in sleep_keys
                        else ("S1/S2" if "sleep_s1s2" in sleep_keys else None)))
    return {
        "probed_at": util.iso(),
        "powercfg_a": a_txt.strip()[:2400],
        "powercfg_requests": req_txt.strip()[:1200],
        "states": states,
        "sleep_available": sleep_available,
        "sleep_kind": sleep_kind,
        "hibernate_available": ok("hibernate"),
        "hibernate_reason": (hib["reasons"][0] if hib and hib["reasons"] else ""),
        "modern_standby": ok("modern_standby"),
        "hybrid_available": ok("hybrid"),
        "blockers": _blockers(req_txt),
        "is_admin": _is_admin(),
        "heuristic": True,
        "note": "启发式解析（兼容中英文输出），仅用于提示与留痕；不改变发出的命令、不触发补发。",
    }


def path(cfg):
    return cfg.dir("state_dir") / CAP_NAME


def save(cfg, data):
    util.atomic_write_json(path(cfg), data)
    return data


def load(cfg):
    return util.read_json(path(cfg), {}) or {}


def age_seconds(data):
    """探测结果距今多少秒；取不到返回 None。"""
    ts = (data or {}).get("probed_at")
    if not ts:
        return None
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(ts)
                         .replace(tzinfo=None)).total_seconds())
    except (ValueError, TypeError):
        return None


def is_stale(data):
    age = age_seconds(data)
    return bool(age is not None and age > STALE_SECONDS)


def effect_note(mode):
    return EFFECT_TEMPLATE.format(mode=mode)


def hints(prefer="sleep"):
    return {
        "prefer": prefer,
        "effect": effect_note(prefer),
        "force_sleep": FORCE_SLEEP_HINTS,
        "prefer_hibernate": PREFER_HIBERNATE_HINTS,
        "no_runtime_fallback": "只发一条命令；不根据返回值补发 hibernate",
    }


def _option(value, label, available, reason, fix):
    return {"value": value, "label": label,
            "available": available,          # True / False / None(未探测，未知)
            "reason": reason, "fix": fix}


def options(caps):
    """界面可选项 + 每项可用性。available=None 表示还没探测过，不下结论。"""
    caps = caps or {}
    known = bool(caps.get("probed_at"))
    sleep_ok = bool(caps.get("sleep_available")) if known else None
    hib_ok = bool(caps.get("hibernate_available")) if known else None
    kind = caps.get("sleep_kind")
    sleep_label = f"睡眠（{kind}）" if kind else "睡眠（S3 / S0 现代待机）"
    if known and not sleep_ok:
        sleep_reason = "本机没有可用的待机状态（powercfg /a 可用段为空）"
    else:
        sleep_reason = ""
    hib_reason = ""
    if known and not hib_ok:
        hib_reason = caps.get("hibernate_reason") or "本机未启用或固件不支持休眠"
    return [
        _option("sleep", sleep_label, sleep_ok, sleep_reason, FORCE_SLEEP_HINTS),
        _option("hibernate", "休眠（Hibernate）", hib_ok, hib_reason,
                PREFER_HIBERNATE_HINTS),
    ]


def option_map(caps):
    return {o["value"]: o for o in options(caps)}


def recommend(caps, prefer="sleep"):
    """给一句"该怎么选"的结论，避免用户在最关键的一步上自己猜。"""
    opts = option_map(caps)
    cur = opts.get(prefer) or {}
    if not caps or not caps.get("probed_at"):
        return {"prefer": prefer, "suggest": prefer,
                "level": "unknown", "text": "还没检测过电源能力，建议先点「检测可用性」"}
    if cur.get("available"):
        return {"prefer": prefer, "suggest": prefer, "level": "ok",
                "text": f"当前选择可用（{cur.get('label')}）"}
    others = [o for o in opts.values() if o["value"] != prefer and o.get("available")]
    if others:
        return {"prefer": prefer, "suggest": others[0]["value"], "level": "switch",
                "text": f"当前选择不可用（{cur.get('reason')}），建议改用「{others[0]['label']}」"}
    return {"prefer": prefer, "suggest": prefer, "level": "none",
            "text": "睡眠与休眠在本机都不可用：值守仍会运行，但命令可能被系统忽略，"
                    "请先按提示修复电源设置"}


def short_summary(data, prefer):
    caps = data or {}
    known = bool(caps.get("probed_at"))
    if not known:
        return f"请求模式={prefer} | 未探测电源能力"
    kind = caps.get("sleep_kind") or "-"
    return (f"请求模式={prefer} | 可用: 待机={caps.get('sleep_available')}({kind}) "
            f"休眠={caps.get('hibernate_available')} | 仅探测、不改命令、不补发")
