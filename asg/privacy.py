"""隐私与数据边界：说明本工具读取什么、落盘什么、如何清空。

核心承诺：只读结构字段（事件名 / 时间戳 / 状态 / 脱敏 ID），**不读消息正文**；
不联网、不上传（控制台只监听 127.0.0.1）；全部生成物留在项目目录内。
清空采用「归档」而非删除：把生成物移到 archive-purge-<时间戳>/，随时可找回。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import util

# 读取的本机数据源（只列结构字段；明确写出不读的字段，便于合规评审）
READ_SOURCES = [
    {
        "app": "WorkBuddy",
        "paths": [
            "~/.workbuddy/workbuddy.db (sessions 表)",
            "~/.workbuddy/projects/**/*.jsonl",
            "~/.workbuddy/tasks/<会话ID>/*.json",
            "~/.workbuddy/sessions/<pid>.json",
        ],
        "reads": ["会话/任务 ID", "status", "created_at / updated_at", "lastHeartbeat",
                  "事件 type / role / status / timestamp", "cwd"],
        "never_reads": ["会话标题", "消息正文 / content / rawContent", "subject / description",
                        "工具调用参数与输出", "AI 推理内容"],
        "purpose": "判断任务是否到达终态、交互会话是否还有用户输入",
    },
    {
        "app": "AutoClaw2 (智谱)",
        "paths": ["%APPDATA%/AutoClaw-official/logs/zwork-main-*.log"],
        "reads": ["event 名称", "timestamp", "context 中的脱敏 ID（ownerId / toolId / operationId）"],
        "never_reads": ["日志消息正文", "用户输入内容", "网络请求 URL / 响应内容"],
        "purpose": "按事件位置判定会话是否停在结束事件后已静默",
    },
    {
        "app": "Trae CN",
        "paths": ["%APPDATA%/Trae CN/ModularData/ai-agent/command-execution/*/execution.json"],
        "reads": ["executionId", "startedAtMs", "finishedAtMs", "status", "文件 mtime",
                  "%APPDATA%/Trae CN/ModularData/ai-agent/database.db 是否存在（健康检查）",
        ],
        "never_reads": ["output / stderr 日志内容", "cwd 之外的项目内容"],
        "purpose": "命令级终态判定",
    },
    {
        "app": "Windows 电源能力",
        "paths": ["powercfg /a", "powercfg /requests", "QueryUnbiasedInterruptTime / GetTickCount64"],
        "reads": ["可用待机状态", "电源请求列表", "无偏中断时间（判断挂起时长）"],
        "never_reads": ["任何用户文件内容"],
        "purpose": "给出客户提示、事后判断系统是否真的挂起",
    },
]

STORED_ARTIFACTS = [
    {"path": "snapshots/", "content": "冻结快照：任务 ID、Agent、开始时间、状态、状态依据；不含任务正文"},
    {"path": "logs/", "content": "事件日志、监控日志、固定顺序日志、自检报告、命令诊断日志"},
    {"path": "state/", "content": "单例锁、睡眠意图基线、界面监控状态（含一次确认时间）、电源能力缓存"},
    {"path": "rules/agent-rules.json", "content": "事件分类规则与人工确认历史（产品资产，清空时保留）"},
    {"path": "acceptance/", "content": "可选脚本自测的产物", "optional": True},
    {"path": "dist/夜间值守.exe", "content": "构建产物（单文件分发版），由源码打包而来；不含任何用户数据",
     "optional": True},
]

COMPLIANCE_NOTES = [
    "本工具是本机任务监控与电源管理自动化工具，**不是医疗器械**，不做诊断、治疗建议或健康状态判断。",
    "本工具不调用任何生成式 AI 模型，生成式 AI 服务备案不适用。",
    "产品文案只描述任务监控与电源管理，不作疗效、功效或健康改善宣称。",
    "被读取的会话记录/日志可能包含敏感个人信息；工具只读结构字段、不解析正文、不把正文写入任何文件。",
    "控制台只监听 127.0.0.1，不联网、不上传、不埋第三方 SDK。",
    "数据默认保留在本机；可用界面的「归档清空生成物」把生成物移入 archive-purge-* 目录（不删除，可找回）。",
    "卸载时只需删除项目目录；工具不写注册表、不建计划任务、不修改 Agent 应用与系统电源设置。",
]

# 归档清空只动工具自己生成的目录；绝不触碰 config*.json / data/ / asg/ / ui/ / 入口脚本。
PURGE_DIRS = ["snapshots", "logs", "state", "acceptance"]
PROTECTED = {"config.json", "config.real.json", "config.demo.json", "config.acceptance.json",
             "data", "asg", "ui", "cases", "agent_sleep_guard.py", "ui_server.py",
             "desktop_launcher.py", "tray.py", "install.py", "pack_entry.py", "build_exe.py",
             "demo_run.py", "run_acceptance.py", "README.md", "rules",
             "dist", "build"}


def report(cfg):
    """返回结构化隐私说明（供界面展示 / 评审导出）。"""
    return {
        "schema_version": 1,
        "network": "仅监听 127.0.0.1；不联网、不上传、不接第三方 SDK。",
        "read_sources": READ_SOURCES,
        "stored_artifacts": STORED_ARTIFACTS,
        "compliance_notes": COMPLIANCE_NOTES,
        "purge_policy": {
            "dirs": PURGE_DIRS,
            "protected": sorted(PROTECTED),
            "method": "移动归档，不删除",
            "refuses_when_monitor_running": True,
        },
    }


def format_text(rep):
    lines = [
        "=" * 74,
        "  隐私与数据边界（本工具只在本机工作）",
        "=" * 74,
        "",
        "【读取的本机数据源】",
    ]
    for s in rep["read_sources"]:
        lines.append(f"  {s['app']}")
        for p in s["paths"]:
            lines.append(f"    路径: {p}")
        lines.append(f"    读取: {', '.join(s['reads'])}")
        lines.append(f"    绝不读: {', '.join(s['never_reads'])}")
        lines.append(f"    用途: {s['purpose']}")
        lines.append("")
    lines.append("【落盘的生成物】")
    for a in rep["stored_artifacts"]:
        lines.append(f"  {a['path']}: {a['content']}")
    lines += ["", "【数据边界与合规口径】"]
    for n in rep["compliance_notes"]:
        lines.append(f"  - {n}")
    lines += ["", "=" * 74]
    return "\n".join(lines)


def monitor_running():
    lock = util.read_json(util.abs_path("state") / "monitor.lock") or {}
    pid = lock.get("pid")
    return bool(pid and util.pid_alive(pid, lock.get("started_at"))
                and lock.get("status") == "running")


def purge_preview(cfg):
    files = []
    for d in PURGE_DIRS:
        p = util.abs_path(d) if not os.path.isabs(d) else Path(d)
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.is_file()]
    return {"file_count": len(files), "dirs": PURGE_DIRS,
            "monitor_running": monitor_running()}


def purge(cfg, operator="ui"):
    """把生成物归档到 archive-purge-<ts>/，不删除任何文件；monitor 运行时拒绝执行。"""
    if monitor_running():
        raise RuntimeError("有监控正在运行，拒绝清空生成物（请先停止监控）")
    preview = purge_preview(cfg)
    if preview["file_count"] == 0:
        return {"archived_files": 0, "archive_dir": None,
                "message": "没有可归档的生成物"}
    dest_root = util.abs_path("archive-purge-" + time.strftime("%Y%m%dT%H%M%S"))
    dest_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    for d in PURGE_DIRS:
        src = util.abs_path(d)
        if not src.is_dir():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(util.abs_path("."))
            target = dest_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                f.rename(target)
                moved += 1
            except OSError:
                continue   # 被占用则保留原处，不影响整体归档
    # 写一份归档说明
    note = {
        "at": util.iso(), "operator": operator or "-",
        "archived_files": moved, "method": "移动归档，不删除",
        "source_dirs": PURGE_DIRS,
        "note": "规则表 rules/agent-rules.json 保留（产品资产）；config*.json / data / asg / ui / 入口脚本均未触碰。",
    }
    util.atomic_write_json(dest_root / "purge-note.json", note)
    return {"archived_files": moved, "archive_dir": str(dest_root),
            "message": f"已归档 {moved} 个生成物到 {dest_root.name}/（未删除，可找回）"}
