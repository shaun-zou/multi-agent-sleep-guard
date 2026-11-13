"""异常与恢复验收用例集（可下发）。数据在 cases/acceptance-cases.json。

用例只描述「期望行为 + 证据关键字」，不改变产品行为；可用于人工验收，也可按
evidence 里的日志关键字做自动回归评测。
"""
from __future__ import annotations

from . import util

CASES_FILE = "cases/acceptance-cases.json"


def load():
    # resource_path: 优先 exe 旁边的 cases/（可不重打包就换用例集），否则用包内副本
    path = util.resource_path(CASES_FILE)
    d = util.read_json(path)
    if not isinstance(d, dict) or not d.get("cases"):
        raise RuntimeError(f"用例集读取失败: {path}")
    return d


def format_text(data):
    cases = data["cases"]
    by_group = {}
    for c in cases:
        by_group.setdefault(c.get("group", "其他"), []).append(c)
    lines = [
        "=" * 76,
        f"  {data.get('title','异常与恢复验收用例集')}",
        f"  共 {len(cases)} 条 | 版本 {data.get('schema_version')} | 更新 {data.get('updated_at')}",
        data.get("usage", ""),
        "=" * 76,
    ]
    for group, items in by_group.items():
        lines += ["", f"【{group}】({len(items)} 条)"]
        for c in items:
            mode = c.get("mode", "manual")
            lines.append("")
            lines.append(f"  {c['id']} [{mode}] {c['title']}")
            lines.append(f"    前置: {c.get('precondition','-')}")
            for i, s in enumerate(c.get("steps", []), 1):
                lines.append(f"    步骤{i}: {s}")
            lines.append(f"    期望: {c.get('expected','-')}")
            ev = c.get("evidence", [])
            if ev:
                lines.append("    证据: " + " | ".join(ev))
            if c.get("command"):
                lines.append(f"    复现: {c['command']}")
            if c.get("risk"):
                lines.append(f"    风险: {c['risk']}")
    lines += ["", "=" * 76]
    return "\n".join(lines)


def auto_evaluable(cases):
    """返回可按日志关键字做自动评测的用例。"""
    return [c for c in cases if c.get("mode") == "auto" and c.get("evidence")]
