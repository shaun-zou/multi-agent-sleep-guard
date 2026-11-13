"""Agent 适配器抽象基类。

新增一个 Agent 数据源只需:
  1. 在 asg/adapters/ 下新建 .py 文件, 定义 BaseAdapter 的子类, 设置 type;
  2. 实现 scan_tasks() 与 get_task_status();
  3. (可选) 实现 get_last_active() 与 agent_is_idle();
  4. 在 config.json 的 adapters 中加一项 {"type": "...", "enabled": true, ...}。

适配器会被自动发现, 无需改动任何核心代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    #: 唯一类型标识, 对应 config.json 中的 adapters[].type
    type = "base"

    def __init__(self, name: str, options: dict, cfg):
        self.name = name
        self.options = options or {}
        self.cfg = cfg

    # ---------- 必须实现 ----------

    @abstractmethod
    def scan_tasks(self):
        """扫描该数据源的当前任务。

        返回 list[dict], 每条至少包含:
            task_id, agent, start_time, status
        可附带: last_active, log_file, 以及任意自定义字段 (会原样进快照)。
        """

    @abstractmethod
    def get_task_status(self, task: dict) -> str:
        """按快照内任务重新查询当前状态, 返回归一化状态字符串。

        归一化建议: running / queued / completed / failed / cancelled / unknown。
        查询不到应返回 "unknown" (非终态), 宁可整夜不睡。
        """

    # ---------- 可选实现 ----------

    def get_last_active(self, task: dict):
        """任务最后活跃时间 (ISO 字符串), 用于进度日志。"""
        return task.get("last_active") or task.get("start_time")

    def agent_is_idle(self, agent: str):
        """Agent 级活跃信号, 用于 "需观察重试链" 判定。

        返回 True  -> 该 Agent 当前无活跃迹象
        返回 False -> 该 Agent 仍有活跃迹象 (疑似重试出新任务), 暂不判全终态
        返回 None  -> 本适配器不支持该检测 (仅留痕, 不改变终态判定)
        """
        return None

    def describe(self) -> str:
        return f"{self.type}:{self.name}"
