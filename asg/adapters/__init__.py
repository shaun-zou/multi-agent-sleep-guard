"""适配器自动发现与聚合。

扫描 asg/adapters/ 下所有 .py 模块, 收集 BaseAdapter 子类, 按 type 注册。
新增适配器只要丢一个文件进来即可, 核心代码零改动。
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

from .base import BaseAdapter

_HERE = Path(__file__).resolve().parent


def discover():
    registry = {}
    for mod_info in pkgutil.iter_modules([str(_HERE)]):
        if mod_info.name in ("base", "__init__"):
            continue
        try:
            module = importlib.import_module(f"asg.adapters.{mod_info.name}")
        except Exception as exc:  # 不让单个坏适配器拖垮整个工具
            print(f"[WARN] 适配器模块 {mod_info.name} 加载失败: {exc}")
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (issubclass(cls, BaseAdapter) and cls is not BaseAdapter
                    and getattr(cls, "type", None) and cls.__module__ == module.__name__):
                registry[cls.type] = cls
    return registry


# 各适配器对应的进程名（小写）。用于扫描时判定"进程是否在运行"——
# 进程未运行则按"只显示在用的 Agent"规则跳过整个适配器。与 discovery 探针的
# process_names 保持一致；此处单独列出是因为适配器 type 与发现探针 id 并非一一对应。
ADAPTER_PROCESS_NAMES = {
    "workbuddy_local": ("workbuddy.exe",),
    "autoclaw_log": ("autoclaw2.exe", "autoclaw.exe"),
    "trae_agent": ("trae cn.exe", "trae.exe"),
    "claude_code": ("claude.exe",),
}


class AdapterPool:
    """把多个适配器聚合成统一视图, 并把任务路由回原适配器。"""

    def __init__(self, cfg):
        registry = discover()
        self.adapters = []
        for spec in cfg.get("adapters", []):
            if not spec.get("enabled", True):
                continue
            cls = registry.get(spec.get("type"))
            if cls is None:
                raise RuntimeError(
                    f"未找到适配器 type={spec.get('type')!r}; 已注册: {sorted(registry)}"
                )
            self.adapters.append(
                cls(spec.get("name") or spec["type"], spec.get("options", {}), cfg)
            )
        if not self.adapters:
            raise RuntimeError("config.json 中没有启用任何适配器, 无法扫描")
        self.by_name = {a.name: a for a in self.adapters}
        # 最近一次 scan_tasks 因"进程未运行"被跳过的适配器名（供上报/留痕）
        self.skipped_not_running = []

    def describe(self):
        return ", ".join(a.describe() for a in self.adapters)

    def filtered_todos(self):
        """最近一次 scan_tasks 被过滤掉的终态子任务（逐条留痕，防止悄悄吞掉）。"""
        out = []
        for a in self.adapters:
            for item in getattr(a, "filtered_todos", []):
                out.append({"source": a.name, **item})
        return out

    def scan_tasks(self):
        """扫描所有适配器的当前任务。

        关键规则（用户定义）：只显示在用的 Agent —— 已安装但进程未运行的一律不纳入。
        因此每个适配器先比对当前运行进程；进程不在运行则跳过其全部任务，
        避免把"进程已关掉但数据源里还残留 running/working 记录"的 Agent 扫进清单。
        进程枚举失败时（如非 Windows、tasklist 异常）不做过滤，避免误产出空清单。
        """
        from ..discovery import running_processes
        procs, proc_err = running_processes()
        merged = []
        self.skipped_not_running = []
        for adapter in self.adapters:
            names = ADAPTER_PROCESS_NAMES.get(adapter.type, ())
            if names and not proc_err:
                if not any(n in procs for n in names):
                    self.skipped_not_running.append(adapter.name)
                    continue
            for task in adapter.scan_tasks():
                item = dict(task)
                item["source"] = adapter.name
                item.setdefault("task_id", None)
                item.setdefault("agent", "unknown-agent")
                item.setdefault("start_time", "")
                item.setdefault("status", "unknown")
                item.setdefault("last_active", item.get("start_time"))
                if not item["task_id"]:
                    raise RuntimeError(f"适配器 {adapter.name} 返回了无 task_id 的任务: {item}")
                merged.append(item)
        return merged

    def _adapter_for(self, task):
        return self.by_name.get(task.get("source"))

    def get_status(self, task):
        adapter = self._adapter_for(task)
        if adapter is None:
            return "unknown"
        try:
            return adapter.get_task_status(task)
        except Exception:
            return "unknown"

    def get_last_active(self, task):
        adapter = self._adapter_for(task)
        if adapter is None:
            return task.get("last_active") or task.get("start_time")
        try:
            return adapter.get_last_active(task) or task.get("last_active") or task.get("start_time")
        except Exception:
            return task.get("last_active") or task.get("start_time")

    def agent_is_idle(self, agent, source):
        adapter = self.by_name.get(source)
        if adapter is None:
            return None
        try:
            return adapter.agent_is_idle(agent)
        except Exception:
            return None
