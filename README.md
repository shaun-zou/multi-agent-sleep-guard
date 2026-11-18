# agent-sleep-guard

**夜间无人值守场景**：盯住多个 AI Agent 的当前任务，确认它们全部终结、进入空闲后，安全地让 Windows 进入睡眠。

> **开源与从零开始**：本项目以 MIT 协议发布（见 `LICENSE`）。仓库只含源码、文档、配置模板与验收用例——`config.real.json`、`logs/`、`snapshots/`、`state/`、`acceptance/`、`data/`、`dist/` 等本机运行数据由 `.gitignore` 排除，不会被提交。
> clone 后先复制配置模板：Windows `copy config.json config.real.json`，macOS/Linux `cp config.json config.real.json`，再按 3.3 节把适配器路径改成本机实际位置。模板默认 `dry_run=true`，不会真的让电脑睡眠；其余入口见第 3 节。

---

## 0. 三条不可简化的设计原则

| 原则 | 在代码里的落点 |
| --- | --- |
| **人负责判断，脚本负责盯梢** | `scan` 只在手动触发时执行，绝不后台自动重扫；快照必须由人 `scan verify` 标记"已核对"并锁定后，`monitor` 才肯启动（否则直接拒绝）。 |
| **绝不打断任务** | `monitor` 里没有任何超时阈值、没有任何 kill/cancel/中断调用。状态读不到一律判 `unknown`（非终态）。即使 Agent 死循环，也一直等，接受"整夜不睡"。 |
| **睡眠是流程终点** | 先写"即将睡眠"日志并 `flush+fsync+close`，再 `join` 优雅停掉监控，再更新锁/快照，再确认无残留写操作，最后才发睡眠命令。所有日志在命令发出前已写完并关闭。 |

流程：

```
[1 手动 scan] → [2 人工核对 / 修正快照] → [3 手动 monitor] → [4 轮询跟踪]
             → [5 终态复核 + 睡眠触发] → [6 唤醒后 selfcheck]
```

`scan`、`monitor`、`selfcheck` 是三个**完全解耦、各自手动触发**的入口。监控只认人工确认过的**冻结快照**。

---

## 1. 安装与依赖

- **Python 3.9+**（已在 Python 3.13 / Windows 验证）。
- **零第三方依赖**：只用标准库（`ctypes`、`argparse`、`json`、`threading`、`subprocess`…）。不需要 `pip install`，不需要 `psutil`。
- **唯一例外：打包 exe 需要 PyInstaller，且只在构建期**。`python -m pip install pyinstaller` → `python build_exe.py`；生成的 exe 里不含 PyInstaller，运行时依旧是纯标准库，日常源码运行完全不需要它（见 3.15）。
- 平台：**Windows**（`QueryUnbiasedInterruptTime`、`GetTickCount64`、`OpenProcess`、PowerShell 睡眠命令均针对 Windows）。
- 无需管理员权限即可运行；`selfcheck` 的事件日志佐证在权限不足时会自动降级并标注。

```bash
# 不需要安装任何东西，直接跑
python agent_sleep_guard.py --version
python agent_sleep_guard.py scan
```

---

## 2. 目录结构

```
agent-sleep-guard/
├── agent_sleep_guard.py        # 唯一入口脚本
├── config.json                 # 正式配置（默认 dry_run=true，安全）
├── config.demo.json            # 演示/验收配置（小间隔 + mtime 自动终态，约 20 秒跑完）
├── README.md
├── ui_server.py                # 本地控制台后端（标准库 http.server）
├── desktop_launcher.py         # 独立窗口入口（复用同一服务与前端）
├── run_acceptance.py           # 真机非演练闭环验收（唯一会真睡的入口）
├── config.acceptance.json      # 验收专用配置（dry_run=false）
├── 开始真机验收.bat            # 双击触发验收，需输入 SLEEP 确认
├── acceptance/                 # 验收产物（快照 / 日志 / 报告），与正式数据隔离
├── 启动界面.bat                # Web 版：双击打开浏览器标签
├── 启动桌面窗口.bat            # 独立窗口版：双击打开无地址栏窗口
├── 托盘常驻.bat                # P2：pythonw 后台常驻托盘（无黑窗）
├── 安装托盘.bat / 卸载托盘.bat # P2：一键安装/卸载快捷方式与开机自启
├── tray.py                     # P2：托盘入口（ctypes 零依赖，含状态气泡通知）
├── install.py                  # P2：快捷方式 / 开机自启安装卸载
├── pack_entry.py               # P2：打包分发入口（一个 exe 分发到 console/tray/desktop/install/CLI）
├── build_exe.py                # P2：构建单文件 exe（PyInstaller，仅构建期依赖）
├── verify_exe.py               # P2：产物回归——把 exe 扔进空目录真跑一遍（专抓 _MEIPASS 路径故障）
├── dist/夜间值守.exe           # 生成：单文件分发产物（9.1 MB，见 3.15）
├── ui/                         # 独立前端工程（3.9）：index.html / app.css / app.js / api-contract.json
├── asg/
│   ├── config.py               # 配置加载（默认值 + 覆盖，路径锚定项目根）
│   ├── util.py                 # 原子落盘 / fsync / 进程存活 / Windows 时钟 API
│   ├── logger.py               # 日志句柄（支持 close 后禁止再写）
│   ├── events.py               # 全局事件日志
│   ├── snapshot.py             # 快照：版本、修正变更、核对、重试风险决定
│   ├── lockfile.py             # 监控单例锁
│   ├── monitor.py              # 轮询 / 进度 / 心跳 / 稳定复核 / 固定顺序收尾
│   ├── sleepctl.py             # 顺序日志、睡眠意图基线、睡眠命令下发
│   ├── selfcheck.py            # 唤醒后自检
│   ├── discovery.py            # 代码自动发现本机 Agent 与状态数据源（产品化）
│   ├── rules.py                # 规则表：观测、草案、人工确认、驱动适配器
│   ├── powercap.py             # 电源能力探测 + 待机方式可选性（不改变「只发一条」）
│   ├── cli.py                  # 命令行
│   └── adapters/
│       ├── base.py             # 适配器抽象基类
│       ├── __init__.py         # 自动发现 + 聚合路由
│       └── mock_file.py        # 可运行示例适配器（JSON 状态文件 + 日志 mtime）
├── data/
│   ├── agents_state.json       # 示例数据源
│   └── logs/*.log              # 示例 Agent 日志（mtime 活跃信号）
├── snapshots/                  # 生成：SNAP-<时间戳>-v<N>.json
├── logs/                       # 生成：events / monitor / sleep-sequence / selfcheck / discover 日志
├── state/                      # 生成：monitor.lock / sleep_intent.json / discovery-state.json
└── rules/                      # 生成：agent-rules.json（规则表，人工确认后更新）
```

所有状态与日志都是**本地文件**，无数据库、无网络请求。

---

## 3. 运行方式

### 3.1 一键演示（推荐，不需要懂命令行）

**直接双击项目文件夹里的 `run_demo.bat`。** 会弹出一个黑色窗口，自动跑完整个流程，约 20 秒后显示自检报告，按任意键关闭窗口。

- 全程是**演练模式（dry-run）**，电脑不会、也绝不会真的睡眠，可以放心试。
- 窗口里的内容会另存一份到 `logs/演示报告.txt`，用记事本打开排版更整齐。
- 前置条件只有一条：电脑装了 **Python 3.9 或更高版本**（已装好就什么都不用做）。若双击后提示 `Python not found`，安装 Python 时记得勾选 **Add Python to PATH**。
- 演示用的是**示例数据**，不是真实 Agent 任务。要接入真实 Agent，见第 11 节。

### 3.2 手动逐步运行（正式使用，六步）

```bash
# ① 手动扫描：输出任务清单 + 生成快照版本号
python agent_sleep_guard.py scan
#    -> 快照 ID: SNAP-20261108T015856-v1

# ② 人工核对清单（这一步的主体是人，不是脚本）
#    如需增删任务，用 revise；每次修正都会生成新版本号并记录"原版本 → 新版本"
python agent_sleep_guard.py scan show   --snapshot SNAP-20261108T015856-v1
python agent_sleep_guard.py scan revise --snapshot SNAP-20261108T015856-v1 \
    --remove T-2001 \
    --add '{"task_id":"T-3001","agent":"agent-gamma","start_time":"2026-11-08T01:20:00+08:00","status":"running","source":"mock_file"}' \
    --reason "T-2001 已取消；补漏 T-3001" --operator yourname
#    -> 新快照: SNAP-20261108T015856-v2 (v1 -> v2)

# ③ 人工核对确认 + 显式记录重试风险决定，锁定这个具体版本（未锁定则 monitor 拒绝启动）
#    情况A：Agent 重试沿用原任务 ID
python agent_sleep_guard.py scan verify --snapshot SNAP-20261108T015856-v2 \
    --retry-reuses-id true --decision no_action --operator yourname
#    情况B：重试会生成新 ID（重试任务不在快照内、监控不会跟踪）——必须显式二选一
python agent_sleep_guard.py scan verify --snapshot SNAP-20261108T015856-v2 \
    --retry-reuses-id false --decision accept_untracked_retry \
    --note "接受旧任务终结即视为完成，重试任务不跟踪" --operator yourname
#    或：
python agent_sleep_guard.py scan verify --snapshot SNAP-20261108T015856-v2 \
    --retry-reuses-id false --decision watch_retry_chain \
    --watch-task T-1001 --operator yourname

# ④ 手动开启监控（默认 dry-run，不会真睡）
python agent_sleep_guard.py monitor --snapshot SNAP-20261108T015856-v2
#    真要睡：加 --no-dry-run，或把 config.json 的 sleep.dry_run 改为 false

# ⑤ 监控自动完成终态复核并触发睡眠（详见第 6 节）

# ⑥ 早上唤醒后自检
python agent_sleep_guard.py selfcheck
```

**手动跑通全链路的演示**（等价于 `run_demo.bat`，不改动正式配置、不真睡）：

```bash
python -c "import glob,os; [os.utime(p,None) for p in glob.glob('data/logs/*.log')]"
python agent_sleep_guard.py scan --config config.demo.json            # 记下输出的快照 ID
python agent_sleep_guard.py scan verify --config config.demo.json --snapshot <ID> \
    --retry-reuses-id false --decision accept_untracked_retry --operator demo
python agent_sleep_guard.py monitor --config config.demo.json --snapshot <ID>
python agent_sleep_guard.py selfcheck --config config.demo.json
```

`config.demo.json` 把间隔压到秒级、并让示例适配器用日志 mtime 自动判终态，仅用于验收；**正式夜间值守请用 `config.json`**。

---

### 3.3 真实扫描：本机三个 Agent 数据源（阶段一）

`config.real.json` 同时启用 3 个**只读**适配器，绝不修改被监控应用：

| 适配器 | 数据源 | 能拿到的明确终态 | 可靠性 |
| --- | --- | --- | --- |
| `workbuddy_local` | `~/.workbuddy/workbuddy.db`（SQLite `sessions` 表）、`~/.workbuddy/tasks/<会话ID>/*.json`、`~/.workbuddy/projects/**/<会话ID>.jsonl`、`~/.workbuddy/sessions/<pid>.json` | DB 终态 `completed / terminated`；`working` 时按会话事件位置判定；待办 `pending / in_progress / completed`；心跳 `lastHeartbeat` | **高**：任务 ID、开始时间、状态、用户输入事件齐全 |
| `autoclaw_log` | `%APPDATA%/AutoClaw-official/logs/zwork-main-*.log`（结构化 JSON，事件 `zwork.tool.started / completed / failed`） | 工具调用配对 + **事件位置规则**（结束事件 + 确认静默 → 会话完成） | **中高**：事件明确，但任务边界靠工具调用推断 |
| `trae_agent` | `%APPDATA%/Trae CN/ModularData/ai-agent/command-execution/*/execution.json` | 命令级 `startedAtMs / finishedAtMs` + 事件位置规则；任务级状态在 295 MB 私有库里读不到 | **中**：粒度较细 |

```bash
python agent_sleep_guard.py scan --config config.real.json
python agent_sleep_guard.py scan show --config config.real.json --snapshot <快照ID>
```

`scan` 清单新增「状态依据」列：`explicit:*` = 有明确终态字段；`event_position:*` = 按事件位置 + 确认静默判定；`event_position:no_*` = 无事件可分析、保守判 `unknown`。数据源读不到时直接报错，绝不静默返回空清单；也不读取任务标题/描述，避免把工作内容写进快照。

#### 日志数据源的终态规则：**看事件位置，不是只看静默时长**

「日志静默不算完成」的准确含义是：**不能只因为日志安静了 N 分钟就判完成**，必须先分析日志最后停在任务的哪个阶段。实现见 `asg/event_position.py`，三个真实适配器共用：

| 日志最后一条任务事件 | 判定 | 「状态依据」列 |
| --- | --- | --- |
| `START`（任务开始）或 `MIDDLE`（进行中） | `running`，**静默多久都不算完成** | `event_position:started` / `event_position:in_progress` |
| `END`（明确结束事件） | 再看之后静默时长 | |
| └ 静默 ≥ `idle_confirm_seconds`（默认 300s） | `completed`：停在上一任务结束且确认无新任务 | `event_position:ended+idle>=300s` |
| └ 静默 < `idle_confirm_seconds` | `running`：还在确认窗口内 | `event_position:ended+idle<300s(confirming)` |
| 一条任务事件都没有 | `unknown`（非终态），并作为保守项提示 | `event_position:no_events` |

即：**必须同时满足「看到明确的结束事件」+「之后 N 分钟没有新的开始/中间事件」**。各数据源的事件映射：

| 数据源 | START | MIDDLE | END |
| --- | --- | --- | --- |
| WorkBuddy `projects/**/<sid>.jsonl` | `type=message & role=user`（**用户输入**） | `function_call` / `function_call_result` / `reasoning` | `assistant & status=completed`（回复结束） |
| AutoClaw2 日志 | `zwork.tool.started` / `zwork.model.request.issued` | 其他 `zwork.tool.*` | `zwork.tool.completed / failed` / `model.token_usage.record` |
| Trae `execution.json` | `startedAtMs` | — | `finishedAtMs` 或 `status=exited` |

WorkBuddy 会话若数据库里已是 `completed/terminated` 则直接采信，不再看事件位置；`sessions/<pid>.json` 的 `lastHeartbeat` 只是应用心跳（窗口开着就更新），**不能**当用户输入信号。背景噪声（网络请求成功、配置加载、性能打点）不参与判定。`N` 可在各适配器的 `options.idle_confirm_seconds` 调整（默认 300 秒）。

`scan` 只在 `unknown`（无事件可分析）时才把条目标为保守项并汇总提示；其余情况由规则给出可复核的判定依据，人仍可在核对阶段用 `scan revise --remove <task_id>` 剔除任何不认可的条目。

#### 清单瘦身：过滤「已终态」与「未开始」的待办子任务

WorkBuddy 适配器除会话级主任务 `wb-session:*` 外，还会把该会话下积累的待办 `~/.workbuddy/tasks/<sid>/*.json` 列成 `wb-todo:*` 子任务。这些文件不会随任务完成而删除，会越积越多（本机曾出现 1 条主任务 + 9 条已完成子任务）。

**关键语义**：这些 todo 是**人工勾选的计划清单**，`pending` 只表示「这条计划没勾完成」，不代表「有个任务正在排队执行」；会话结束也不会自动把它改成 `completed`。11-14 那轮值守死等 11 小时，卡的就是一条 11-11 创建、实际代码早已做完却忘了勾掉的开发待办（subject：改造 util 支持冻结态路径解析）。把它当成未终结任务既无收益，又会让监控永远等下去。

`config.real.json` 里 `adapters[workbuddy_local].options` 现设 `include_completed_todos=false` + `include_pending_todos=false`，扫描时：

| 条目 | 是否保留 | 原因 |
| --- | --- | --- |
| `wb-session:*` 会话级主任务 | **始终保留** | 真正决定「这个 Agent 忙不忙」的兜底项；会话还在跑时监控仍会等它 |
| 子任务 `running`（`in_progress`） | 保留 | 确实正在执行 |
| 子任务 `unknown`（状态读不到） | **强制保留** | 宁可整夜不睡，绝不因解析失败把任务悄悄删掉 |
| 子任务 `queued`（`pending`） | **过滤** | 计划清单里尚未勾选开始的项，不会自动变终态 |
| 子任务 `completed / failed / cancelled` | 过滤 | 已是终态，留着不产生任何阻塞作用 |

**风险与回退**：只排除映射后的 `queued`（原始 `pending`），`running` / `unknown` 和会话级主任务一律保留，所以「会话正在跑」的场景不会被误判提前睡。如果某个数据源的 `pending` 确实代表「已排队、马上要执行」的真实任务，把 `include_pending_todos` 设为 `true` 即可恢复保守行为。代码默认值现为 `false`（排除），`config.real.json` 与自动发现生成的配置（`discovery.py` 的 `spec()`）都显式写了 `false`，所以点「自动发现并扫描」不会把开关又打开。

过滤绝不悄悄生效：`scan` 打印 `[已过滤] N 条子任务未纳入清单（原因 completed=… pending=…）` 并逐条列出 `reason`（超过 8 条列前 8 条 + 计数），同时写 `TODO_FILTERED` 事件（带 `by_reason`）。逐条留痕存在 `filtered_todos`，不会静默吞掉。

> 快照是冻结的：此改动只对**之后新生成的快照**生效，旧快照不会自动变干净。点界面「重新扫描」生成新快照即可；旧快照仍可用 `scan revise --remove` 逐条剔除。

#### 真实工作场景（重要）

夜间无人值守时的实际情况是：**睡前还有若干 Agent 正在跑任务、不能关掉它们**；用户运行本工具盯着这些 Agent，等它们全部完成后让系统睡眠。此时**交互对话窗口默认不会再有用户输入**——除非用户回来主动退出 monitor 模式（`Ctrl+C` 会写 `MONITOR_ABORTED`、释放锁，**绝不发出睡眠命令**）。

> 开发调试时跟 AI 对话的那个窗口**不是**将来值守的场景；不要用它来推断产品行为。

因此交互会话**不是永久阻塞项**：

| 情况 | 会话状态 | 会不会睡 |
| --- | --- | --- |
| 用户已离开，最后事件停在「回复结束」且静默 ≥ N 分钟 | `completed` | 可以睡（前提是快照内其余任务也全部终结） |
| 用户回来输入一句（`role=user`） | 立即回到 `running` | **不会睡**，稳定复核被打破 |
| Agent 仍在跑工具 / 推理 | `running` | 不会睡 |
| 数据库已标 `completed / terminated` | 直接采信 | 可以睡 |

**2026-11-08 夜间值守演练**：模拟交互窗口静默 8 分钟 → 真实扫描 8 条任务全部 `completed`（含交互会话 `event_position:ended+idle>=300s`）→ 无需人工剔除 → `verify` 锁定 → `monitor` 走完 STEP1–STEP5 → `selfcheck` **PASS=6 / FAIL=0**。全程 dry-run，未真正睡眠。

**仍需人工把关的两点**：

1. 清单里的条目是否真属于"要盯的任务"，由人在核对阶段决定（可用 `scan revise --remove <task_id>` 剔除）。
2. 空清单会让监控立刻满足终态条件，`scan` 会额外告警，开启监控前必须确认不是数据源失效。

> Claude Code 已安装但当前未运行，尚未接入；其会话记录为 `~/.claude/projects/*.jsonl`，需要时可按第 11 节新增适配器与探针。

### 3.4 产品化：让代码自己发现 Agent，而不是靠人工/AI 调研

**先澄清一个容易混淆的点**：`scan` 运行时**本来就是纯代码**——`python agent_sleep_guard.py scan` 独立执行，不需要 AI 参与、不读对话、不联网。前面几轮我做的只是**开发期一次性**调研"本机有哪些 Agent、状态文件在哪"，结果写进 `config.real.json`；这不是运行时依赖。

但"路径写死在配置里"对本机工具确实不够产品化：换台机器、换个安装目录、换个版本就失效。因此新增 `discover` 入口，把**发现动作本身也变成代码**。

```bash
# ① 只显示在用的 Agent（默认）；首次运行建立基线
python agent_sleep_guard.py discover
python agent_sleep_guard.py discover --all          # 连「已安装但未在用」的一起列
python agent_sleep_guard.py discover --json         # 机器可读输出

# ② 全自动：scan 前由代码自己发现并应用数据源，无需任何手写配置
python agent_sleep_guard.py scan --auto-discover

# ③ 需要留档时，生成一份可人工复核的配置（仍默认 dry_run=true）
python agent_sleep_guard.py discover --write-config config.auto.json
python agent_sleep_guard.py scan --config config.auto.json
```

#### 发现层的四条规则

| 规则 | 实现 |
| --- | --- |
| **只显示在用的** | 只有「已安装 + 有进程 + 适配器已实现 + 至少一个数据源健康检查通过」算在用；默认只列出/只启用这些。装了没跑（Claude Code）、没接适配器、数据源全不可用的，一律不显示不启用；`--all` 才可见 |
| **变更可发现** | 上次结果存到 `state/discovery-state.json`，每次对比出：新增安装 / 已卸载 / 版本变化 / 数据源降级或升级 / 开始使用 / 已停用。版本号从 `last-launch.json`、`package.json` 读，读不到则用 exe 大小+mtime 指纹 |
| **解析不了就换源** | 每个 Agent 有多个候选源，健康检查失败自动降级；报告逐条显示「采用 / 备选 / 不可用 + 原因」。本机 Trae 的 `database.db`（284 MB 私有格式）解析不了 → 自动采用 `execution.json` |
| **按精度排序** | P4 明确终态 > P3 事件位置 > P2 活动迹象 > P1 仅进程；取精度最高的可用源，并把 `primary_source` + `source_fallbacks` 写进适配器配置 |

**运行时也会降级**：`workbuddy_local` 主源（SQLite）失效时，自动从 `projects/**/<sid>.jsonl` 找近期会话并按事件位置判定，任务「状态依据」列会带 `+fallback:projects_jsonl` 留痕；两个源都不可用时直接报错，绝不静默返回空清单。

本机实测（2026-11-08）：自动发现 **4 个已安装 / 3 个在用 / 1 个发生降级**，自动启用 3 个适配器；Claude Code 被正确识别为「已安装、未运行、适配器未实现」而隐藏。变更检测五类（版本/卸载/新增/数据源/停用）逐一验证；主源失效模拟下 `scan` 仍扫出 6 条任务并标注 fallback。`scan --auto-discover` 与 `--write-config` 两条路径都扫出 9 条真实任务。

**职责边界（重要）**：`discover` 只回答"数据源在哪、能不能读"，**不判断任务、不生成快照、不决定是否睡眠**。发现结果仍要由人在核对阶段确认，符合"人负责判断"。

**产品化仍需维护的真实成本**：`discover` 能自动找到文件；"哪个事件算任务开始/结束"由规则表维护，但规则漂移与更新草案已能自动生成，只需人工点确认（见 3.5）。私有/加密数据源（如 Trae 的 `database.db`）无法自动解析任务级状态，需要厂商开放接口才能提升精度。

### 3.5 规则表：由 discover / scan 的观测驱动更新（半自动）

规则已从代码里搬出来，放到 `rules/agent-rules.json`（事件分类、状态映射、字段名规则）。三个真实适配器都改为**读规则表**，不再硬编码事件名 / 状态名 / 字段名。

**可以全自动的部分**：`scan` 每次自动观测真实数据源里出现的事件名、状态值、字段名并累计统计；`discover` 检测到应用版本变化时提示复查规则；`rules` 命令对比"观测到的值"与"已归类的规则"，自动生成：

- **待确认草案**：新出现的事件/状态，带出现次数与建议（`ignore` 背景噪声 / `start` `end` 疑似任务边界 / `review` 新状态值）
- **规则漂移**：规则表里有、本次没观测到的事件名或字段名 → 版本升级可能改了名字

**必须人工确认的部分**：把某个值归类为 `start / middle / end` 或某个终态状态。因为这是**语义不是统计**——把 MIDDLE 误学成 END，会在 Agent 还在跑时误触发睡眠，直接违反「绝不打断任务」。

安全闭环：

| 情形 | 处理 | 后果 |
| --- | --- | --- |
| 未归类事件 | `middle`（进行中） | **不睡**，由 `rules` 提示人确认 |
| 未归类状态 | `unknown`（非终态） | 不睡 |
| 已确认噪声 | 忽略，不进事件流 | 不影响判定 |
| 人工确认后 | 写入规则表 + `history` 留痕，指纹变化 | 适配器下次读取即生效 |

```bash
python agent_sleep_guard.py rules --config config.real.json          # 采样 + 看草案
python agent_sleep_guard.py rules --config config.real.json --approve-noise
python agent_sleep_guard.py rules --config config.real.json \
    --approve "autoclaw_log:event:zwork.xxx" --as end --operator yourname
python agent_sleep_guard.py rules --config config.real.json --json
```

本机实测（2026-11-08）：首次观测 55 种 key、31 条待确认；`--approve-noise` 批量确认 27 条背景噪声后剩 4 条，逐条确认后**草案归零、漂移归零**，规则表指纹 `a707080515b4 -> e3d0a728f51c`。把 `status:stopped` 确认为 `cancelled` 后，scan 里那条一直卡住的 Trae 命令被正确判为终态。

**验证规则表确实生效**：把 `event:zwork.tool.started` 从 `start` 改成 `end`，适配器分类立即跟着变；把 Trae 的 `end_field` 从 `finishedAtMs` 改成 `completedAtMs`，字段判定立即按新字段工作。

> 取舍：被批量标为 `ignore` 的事件不再出现在草案里；若某天应用把噪声事件改造成任务事件，需要人工重新确认。这是"不自动改变终态语义"的代价。

**规则表热加载**：`rules --approve` 写入 `agent-rules.json` 后，**运行中的 monitor 会在最长 5 秒内自动重载**（按文件 mtime 检查），不必重启进程。早期版本是进程内单例、只在启动时加载一次，会出现"规则已确认但监控仍用旧表继续判 running"；注意这只对**修完之后新启动的**监控生效，已运行的旧进程仍持有旧代码。

**反复出现的维护点**：每次系统真正睡眠/唤醒后，AutoClaw 日志会新增一批电源/网络/埋点类事件（`lock-screen`、`suspend`、`resume`、`unlock-screen`、`zwork.network.request.failed`、`zwork.promotion_config.load_failed`、`zwork.instrumentation.operation_failed` 等）。它们未被规则表收录时，会按"未归类一律 MIDDLE"被当成任务进行中，使会话一直 `running`、挡住下一次睡眠。2026-11-09 已把这批事件标为噪声（`operator=post-sleep-noise-review`，AutoClaw 共 31 条），并把 `failed` / `completed` 类草案的建议从 `end` 收紧为 `review`——**网络请求失败不等于任务结束**。**前缀忽略规则（已实现）**：`rules/agent-rules.json` 的 `ignore_patterns` 按命名空间批量忽略背景噪声，支持 `*` 通配。默认给 `autoclaw_log` 预置 18 条，覆盖 `zwork.network.*`、`zwork.promotion_config.*`、`zwork.instrumentation.*`、`zwork.perf.*`、`zwork.browser.*`、`zwork.model.image_host.*` 以及 `lock-screen` / `suspend` / `resume` / `unlock-screen` 等系统电源事件；**绝不列入** `zwork.tool.*`、`zwork.model.request.*`、`zwork.model.token_usage.*`（任务事件）。

优先级：**显式 `event_kinds` 归类 > 人工逐条 `ignored` > 前缀忽略 > 未归类 MIDDLE**。所以已确认的任务事件不会被前缀吞掉，未知命名空间的新事件仍按保守规则判"进行中"。命中前缀的观测值不进草案，但会在 `rules` 报告里逐条列出（含命中模式与次数），便于发现误吞。

```bash
python agent_sleep_guard.py rules --add-ignore-pattern "zwork.foo.*" --adapter autoclaw_log
python agent_sleep_guard.py rules --remove-ignore-pattern "zwork.foo.*" --adapter autoclaw_log
```

若某天发现某个前缀下出现了**真实任务事件**，用 `--remove-ignore-pattern` 去掉该前缀即可恢复保守判定；所有增删都写入 `history` 留痕，运行中的 monitor 会在 5 秒内热重载。

### 3.6 本地控制台（P0 界面）

**双击 `启动界面.bat`** → 自动打开 `http://127.0.0.1:8765/`。只监听 `127.0.0.1`、不联网、零第三方依赖、数据不出本机；**关闭浏览器不影响监控进程**。

| 界面区域 | 能做什么 |
| --- | --- |
| 顶部状态 | 状态灯、待机方式徽标（不可用时转告警色）、监控进程 pid、锁状态、最近快照 |
| 四步流程 | 扫描 → 人工核对 → 监控 → 早上自检，当前步骤高亮 |
| 任务清单 | 勾选保留 / 取消勾选剔除，显示任务 ID、Agent、开始时间、状态、**状态依据** |
| 核对区 | 版本与变更摘要、操作人/备注、核对并锁定；重试风险收在「高级选项」折叠里，默认不用改 |
| 待机方式 | 睡眠 / 休眠两个单选钮 + 可用性徽标 + 不可用原因与修复清单 + 「检测可用性」；监控运行中锁定不可改（见 3.10） |
| 值守操作 | 开启监控、优雅停止、早上自检报告 |
| 规则表卡片 | 草案逐条确认（判定下拉 + 风险警示弹窗）、批量忽略噪声、前缀忽略规则增删、重新采样（见 3.11） |
| 日志检索卡片 | 文件下拉 + 关键词过滤 + 行数选择 + 自动刷新，只读 logs/ 下 *.log（见 3.12） |
| 右侧 | 自动发现报告、隐私说明、用例集、指标周报 |

**三条关键设计**：

1. **界面只是观察窗 + 操作面板**：所有动作都复用已验证的 CLI 入口，日志与留痕完全一致，不另写一套业务逻辑。
2. **monitor 是独立进程**：pid 记在 `state/ui-monitor.json`，界面关掉它继续跑。
3. **优雅停止**：界面写 `state/monitor.stop` 标志 → monitor 主循环检测到后自己走 `_abort` → **先写 `MONITOR_ABORTED` 并 flush+close 日志，再 join 工作线程** → 释放锁 → 退出。全程不强杀进程、不截断日志、**绝不发出睡眠命令**。为避免"子进程启动慢、把刚写的停止标志当成残留删掉"的竞态，界面在启动前清理标志、停止时持续重写直到进程真正退出。
4. **防 PID 复用**：判断「监控是否还在跑」不能只看 PID 是否存在。系统会把已退出进程的 PID 分配给新程序，只判断存在会把 `parfait-helper` 之类无关进程误认成监控，导致界面显示「监控运行中」、「开启监控」被禁用、「优雅停止」空转。因此 `util.pid_alive(pid, started_at)` 额外用 `GetProcessTimes` 比对**进程创建时间**与记录的启动时间（容差 120s）；不匹配时 `pid_reused=True`，状态按「未运行」处理并在界面提示，可直接重新开启监控。启动检查、停止、单例锁、自检、隐私归档、验收前置检查六处统一使用。取不到创建时间时降级为「只判存在」，保守不误杀真实监控。

**安全护栏**：真睡授权只有「开启监控」时的一次确认，由后端一次性令牌校验（见 3.8）；未核对锁定的快照，后端直接拒绝启动监控；`config.json` 默认 `dry_run=true` 只约束命令行。

> 页面首屏会在状态刷新完成后再自动载入快照清单（`refresh().then(loadSnapshot)`），避免异步竞态导致「尚未选择快照」而清单空白。

本机实测（2026-11-08）：`GET /` 返回界面 → 扫描 8 项 → 勾选剔除 1 项生成 v2 → 核对锁定 → 启动监控（pid 独立）→ 优雅停止，监控主日志含 `MONITOR_ABORTED`、不含 `PRE_SLEEP`；`selfcheck` PASS=6 / FAIL=0。全程 dry-run。

> P0/P1 已知边界已收敛：**界面内规则确认与日志检索（原 P1）已实现**（见 3.11、3.12）；**托盘常驻、开机自启、系统通知、一键安装（原 P2 前半）已实现**（见 3.13、3.14）；**单文件 exe 打包已实现**（见 3.15），只剩代码签名。

### 3.7 Web 版与独立窗口版并存（一套前端 + 两种壳）

**先澄清概念**：现在这个"Web 版"本身就是前端（HTML/CSS/JS），只是跑在浏览器标签里、由本机 Python 服务提供。所谓"单独前端界面"，多数情况下**不是重写一套界面**，而是给同一份前端换一个壳。

| 业界做法 | 代表 | 前端代码 | 成本 | 我们 |
| --- | --- | --- | --- | --- |
| 一套前端 + 两种壳 | VS Code / Notion / Slack / 飞书桌面版 | 一份 | 低 | **已采用** |
| 前后端分离 + 独立前端工程 | 多数 SaaS + 桌面/移动端 | 一份或多份，靠 API 契约 | 中 | 下一步可选 |
| 两套完全独立界面 | 少数老牌软件 | 两份 | 高 | 不建议 |

我们的两个入口：

| 入口 | 壳 | 结果 |
| --- | --- | --- |
| `启动界面.bat` | 浏览器标签页 | 现有 Web 版，完全保留 |
| `启动桌面窗口.bat` | Edge/Chrome `--app` 独立窗口 | 无地址栏 / 无标签页，零第三方依赖 |

**关键机制**：谁先启动谁前台承载 `127.0.0.1:8765` 服务；后启动的检测到端口已占用，就只打开自己的窗口或标签，**绝不起第二个服务**。因此两个界面可以同时打开，共用同一份 `ui/index.html`、同一套 `/api/*`、同一份状态。

**能这么做的唯一前提**：后端只暴露 JSON API，界面不含业务规则——"是否该睡"的判断始终留在 Python 后端。如果把规则写进前端，两个端迟早不一致，而且直接威胁睡眠安全。

本机实测：8765 只有 **1 个监听进程**；两个入口在服务已运行时都返回 0 并复用；API 与同一份前端均可正常读取。

**取舍与边界**：

- 两个界面是**同一后端的两个视图**，不是两个独立实例；同时操作会立刻互相看到对方结果（例如两边都点"扫描"会生成两个快照）。
- 独立窗口版仍是浏览器内核，不是打包 exe；托盘常驻、开机自启与系统通知由 3.13 的托盘入口提供（零依赖实现），**无需** Tauri/Electron。
- 打包 exe **已做**（3.15）：单文件 exe 直接复用这套 API 与前端，一行业务代码没改；**代码签名仍未做**（需购证书），首次运行会被 SmartScreen 拦一次。
- 若要升级成"独立前端工程"（React/Vue + 构建），下一步是把内联 CSS/JS 抽成 `ui/app.css`、`ui/app.js` 并给服务加静态资源路由，后端 API 不用改。

#### 3.7.1 界面上的动作不弹黑框（无窗口子进程）

**现象**：托盘版（`pythonw`，本身没有控制台）里点「扫描」或「自检」，会有一个 cmd 黑框闪一下就没。

**根因**：Windows 里控制台子系统程序启动时，若父进程没有可继承的控制台，系统会给它**新开一个控制台窗口**。而界面上的每个动作都是「服务进程 → 子进程调 CLI」，CLI 内部还要再调 `tasklist`（进程检测）、`powercfg`（电源能力）、`powershell`（事件日志）——**每一层都可能各弹一次**。打包成 exe 后父进程有控制台，主路径被继承所以不明显，但嵌套层仍会闪。

**修法**：`asg/util.spawn_kwargs()` 在 Windows 下**一律**带 `CREATE_NO_WINDOW`（不再只在打包版生效），并统一用于所有后台 spawn 点：`ui_server` 的 CLI 与 monitor、`discovery` 的 tasklist、`powercap` 的 powercfg、`selfcheck` 的 powershell、`install` 的 powershell、`sleepctl` 的睡眠命令。所有调用方本来就用管道/文件接管了 stdin/stdout，因此隐藏窗口不影响输出采集。

**验证（A/B 探针，父进程 = 无控制台的 pythonw）**：不加标志时子进程自报 `GetConsoleWindow()=69676`（确实弹了窗），加 `CREATE_NO_WINDOW` 后为 `0`；实测**单次扫描与单次自检期间 conhost/OpenConsole 零新增**，扫描/自检结果与快照生成均正常。

### 3.8 真睡授权：每个入口各确认一次

界面主路径就是**真实值守**：重新扫描 → 勾选要盯的任务 → 核对并锁定 → 点「开启监控」时确认一次。此外还有「**直接睡眠**」——不经过任务终态判定、现在就让电脑睡（见 3.10.1）。**两个真睡入口共用同一枚一次性令牌池，各自都必须确认一次**；没有演练开关，也没有持久「总闸」。

**为什么收敛掉它们**（2026-11-10 产品决策）：

| 原机制 | 去掉的原因 |
| --- | --- |
| 真实睡眠总闸 | 只是 UI 层状态，`cli.py` 的 `--no-dry-run` 本来就不检查它；作为持久状态还会残留，11-09 出现过"一启用就被误判自动关回"的 bug |
| 演练模式开关 | 状态存在 `localStorage`，关过一次长期记住；且与「开启监控」时的确认弹窗重复 |
| 重试风险三选一 | 实际使用与验收脚本都固定选 `reuse`，从未真正做过该决策；无人值守时重试需人工触发 |

**授权链**（每个真睡入口各走一次，都不可绕过）：

1. `/api/state` 下发一枚**一次性确认令牌**，只存在于服务进程内存，不落盘、TTL 10 分钟、用后即焚；未消费时复用同一枚以兼容 3 秒轮询和多标签页。
2. 界面点「开启监控」弹出一次确认，写清**快照 ID、任务数、后果**；确认后才把令牌回传。
3. `POST /api/monitor/start` 校验令牌有效且未过期、未用过，才以 `--no-dry-run` 启动；否则**拒绝启动**（不产生监控进程），返回明确原因。校验通过写 `MONITOR_CONFIRMED` 事件，`state/ui-monitor.json` 记 `confirmed_at`。
4. 值守结束**天然回到安全态**：没有需要关回的开关。下次要值守，重新确认一次即可。
5. 「直接睡眠」走同一套令牌：`POST /api/sleep/now` 校验令牌通过后，直接按 `sleep.prefer` 发一条命令，写 `SLEEP_MANUAL_ISSUED` 事件。**不因为是手动入口就降级**——同样拒绝在监控运行时触发，同样只发一条、不补发。两个入口共用令牌池，谁先消费谁用掉，另一个入口下次刷新拿到新令牌。

`selfcheck` 的「一次确认护栏」项会报告上次监控是否为非演练、是否留有 `confirmed_at`、确认事件累计次数；若非演练却没有确认记录（例如命令行直接启动）会报 WARN 提示人工复核。

> 演练能力保留在**命令行**：`monitor --dry-run`、`run_demo.bat`、`run_acceptance.py --rehearsal`。演练请求不需要确认令牌，界面也不再暴露该选项。真机验收脚本自身要求输入大写 `SLEEP`，即为它的一次确认。
>
> 取舍依据：睡眠/休眠是可恢复的低功耗状态，不丢数据；真正的安全边界在"快照人工核对锁定 + 终态复核 + 连续稳定次数"，而不是多一道开关。因此把交互简化到一次确认，同时用一次性的后端令牌保证这一步真做过。

### 3.9 独立前端工程（前后端按 API 契约解耦）

`ui/` 现在是独立前端目录，**零构建步骤、零第三方依赖**：

| 文件 | 职责 |
| --- | --- |
| `ui/index.html` | 纯结构，只留 DOM 与两个外部引用 |
| `ui/app.css` | 全部样式 |
| `ui/app.js` | 全部交互逻辑，只通过 `/api/*` 与后端通信 |
| `ui/api-contract.json` | **前后端接口契约**：29 个端点的请求/响应、安全约束、替换指南 |

`ui_server.py` 用**静态资源白名单**提供 `/app.css`、`/app.js`、`/api-contract.json`，不接受任意路径（防目录穿越）。Web 版与独立窗口版共用这一份 `ui/`。

契约的意义：以后换成 React/Vue 或任何前端框架，只要保持 `/api/*` 契约，后端一行都不用改；反过来改后端判定规则也不影响前端。**业务规则（终态判定、稳定复核、睡眠触发顺序、真睡授权）始终留在 Python 后端，前端不得实现**——这是睡眠安全的底线。

验证：三个静态资源均 200 且 MIME 正确；目录穿越返回 404；契约 29 个端点可用；扫描与自检回归 `PASS=8 / FAIL=0`。

> 取舍：不引入 npm / Vite。这是单人本机工具，"双击即用、零安装"比构建链路更重要；前端仍是标准三文件，随时可迁入带构建的工程。

#### 3.9.1 界面设计系统与信息层级（2026-11-12 重构）

**品类推理**：本地系统工具类控制台（企业级工具）→ 风格取 **Minimalism / Soft UI Evolution**，靠层级而非装饰表达。**AVOID（逐条排除）**：AI 紫粉渐变、霓虹/刺激色、暗黑模式、装饰性动效、低对比灰字、混用图标风格。

| 设计系统项 | 取值 |
| --- | --- |
| 模式 | 侧栏一级导航 + 顶部状态条 + 状态 Hero + 工作台主/次分区 + 可切换视图 |
| 主色 / 中性 / 语义 | `#1D4ED8` / `#111827 · #475569 · #64748B` / 成功 `#047857`、警告 `#B45309`、危险 `#B91C1C` |
| 字号阶梯 | 12 / 13 / 14 / 16 / 20 / 28px（正文 14，Hero 28），行高 1.6 |
| 间距 | 4 的倍数：4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 |
| 圆角 / 阴影 | 6 / 10 / 14 / 999px；两级柔和阴影，Hero 单独一级 |
| 字体 | 系统 UI 栈（中文 PingFang / 微软雅黑回退），不引外部字体 |
| 图标 | 一套 24×24 线性 SVG sprite（21 处引用），无 emoji |

**信息架构（主次分明）**：一级导航把 4 类任务分开——工作台（今晚要做的）/ 规则表（配置）/ 日志检索（查证）/ 诊断与文档（只读参考）；工作台内唯一的最高权重是**状态 Hero**（当前状态 + 3 个关键指标 + 当前阶段提示，动作按钮已移到所属步骤分区，见 3.9.3），左侧是按阶段切换的主流程卡片，右侧是只读状态；危险操作单独放进「诊断与文档」的独立危险区。

**改动 → 解决的问题**：

| 改动 | 解决的问题 |
| --- | --- |
| 侧栏导航 + 4 个视图切换（记住上次视图） | 原来一张大卡塞了扫描/核对/待机/监控/自检/规则/日志/诊断 8 类内容，全部同权重、无分区 |
| 状态 Hero + 3 个指标 + 当前阶段提示 | 原来「当前状态」只是右栏一堆 12px 键值，看不出今晚该做什么；15 个按钮视觉权重相同 |
| 流程步骤条三态：当前蓝 / 已执行浅绿带勾 / 未开始中性 | 原来只高亮当前一步，做过的步骤看不出来，也没有进度感（见下） |
| 令牌化配色/字号/间距/圆角，字号差从 14/13 拉到 28/14 | 字号几乎无差、颜色散落各处硬编码、留白靠随手 margin，缺乏视觉层次 |
| 按钮分级：主色填充 / 次要描边 / 幽灵文字 / 危险红；危险操作移出主流程 | 破坏性操作（归档清空、批量忽略）与「重新扫描」并排同款，误触风险 |
| 自定义 `<dialog>` 确认框（焦点管理、确认词校验、危险态）+ 轻提示 + 忙碌条 | 原生 `confirm`/`prompt` 阻塞、样式割裂；操作无进行中反馈；跨视图操作看不到结果 |
| 语义化标签、`<label>`/`aria-*`、`aria-live`、`:focus-visible` | 表单无标签、键盘焦点不可见、状态变化不播报 |
| 7 个断点（1400/1280/1024/900/620/380/340）+ 表格横向滚动容器 + 弹性文本 | 原来只有 900/620 两个断点，窄屏整页横向溢出（实测 320px 时 `scrollWidth=481`） |
| 空状态、Badge 文字+颜色双通道、计数带语境、`prefers-reduced-motion` | 空表格一片空白；状态只靠颜色；快速连点/缩放/ Reduced Motion 下体验不稳 |

**整体验证（真实浏览器，非看代码）**：CDP 精确设置 320 / 390 / 768 / 1200 / 1440 五档视口，页面级 `scrollWidth` 分别为 305 / 375 / 753 / 1185 / 1425（差值均为滚动条宽度，**无横向溢出**）；4 个视图切换与 `aria-current` 正确；确认弹窗打开时焦点落在确认按钮、取消后回到触发按钮；错误确认词被拦下并标 `aria-invalid`；`prefers-reduced-motion` 下 transition/animation 压缩到 0.01ms、终态与焦点不变；关键文字对比度 10 组全部达标（最低 4.55:1，正文 17.74:1）；0 个未标注输入框、0 个 emoji 图标。

> 无头 Edge 用 `--window-size=390` 会把布局视口钳到约 500px 再裁剪截图，**不能用来判断窄屏溢出**；本轮用 CDP `Emulation.setDeviceMetricsOverride` + `document.documentElement.scrollWidth` 才拿到真实数据。

#### 3.9.2 流程步骤条三态（当前蓝 / 已执行浅绿）

当前步骤保持蓝色高亮（`aria-current="step"`），已执行步骤为浅绿底 `#E3F5EC` + 勾图标 + 绿色文字，未开始为中性白。三条规则：① **当前蓝色优先**——即使当前步骤本身也被标记过（例如跑完早上自检），仍显示蓝色数字，不被浅绿覆盖；② **「执行过」是历史事件**——`refresh()` 每 3 秒按真实状态推导当前步，停止监控/状态回退时会把当前步推回前面，单独维护的 `executedSteps` 集合保证已完成后面的步骤不会跟着变灰；③ 四个动作成功后才标记（扫描 / 核对锁定 / 开启监控 / 生成自检报告），失败不标记。状态不只靠颜色传达：勾图标 + `aria-label`（"已完成/当前步骤/未开始"）+ `title`。DOM 只在状态签名变化时更新，避免轮询每 3 秒重画。

**验证**：CDP 实测四种场景颜色分别为 `rgb(227,245,236)` 浅绿 / `rgb(239,244,255)` 蓝 / 白，且自检完成后第 4 步被标记过仍为蓝、状态回退到第 2 步时第 3/4 步仍为浅绿；浅绿底文字对比度 4.84:1。

> 测颜色时踩过一坑：`.step` 有 160ms 过渡，切换后立刻读 `getComputedStyle` 拿到的是**过渡起始色**（看起来像"颜色滞后一帧"），要等过渡结束再测。

#### 3.9.3 工作台两个子页面（动作回到所属步骤）

**问题**：「开启监控 / 优雅停止」原本孤悬在右上 Hero，而下方标着 STEP 03 的卡片只有「待机方式」，动作与所属步骤分区分离。

**结构**：工作台主区拆成两个子页面，由步骤条驱动，右列「当前状态」两页常驻。

| 子页面 | 内容框 | 显示时机 |
| --- | --- | --- |
| `prepare`（步骤 1、2） | 任务清单与人工核对卡片：快照选择 / 扫描 / 任务表 / 高级选项 / 核对锁定 | 当前步骤 1、2；点步骤 1 / 2 标签 |
| `watch`（步骤 3、4） | STEP 03「开启监控」卡片（待机方式 + **直接睡眠 / 开启监控 / 优雅停止** + 检测可用性）、STEP 04「早上自检」卡片 | 当前步骤进入 3（含 4）；点步骤 3 / 4 标签 |

Hero 右侧改为**当前阶段提示**（阶段编号 / 名称 / 说明 / 下一步），不再放操作按钮；界面上不出现「子页面一 / 二」字样，完全由步骤条表达。开启监控的**一次性确认弹窗与确认令牌逻辑保持原样**，只换了按钮位置。

**五条切换规则**：

1. 点步骤标签 = 用户主动查看：只切到对应子页面（1/2→`prepare`，3/4→`watch`），**不做滚动定位**（原实现会滚到 secScan / secVerify / secWatchAction / secSelfcheck，已去掉；`data-target` 属性同步移除）。**点击不篡改「当前步骤」**——蓝色高亮代表系统真实阶段，手动查看不该让状态说谎。
2. 手动选择后 `pageLock=true`，**每 3 秒的 `refresh()` 绝不抢页面**（否则用户点回步骤 1 想改清单，3 秒后又被推走）。
3. 自动切换有两处显式入口：**人工核对锁定成功后** `setPage('watch')` 并解除手动锁、把 `prevStepForPage` 对齐当前步（让核对完紧接着就能开启监控）；以及开启监控成功后 `setStep(3) + setPage('watch')`。两处都不依赖轮询的跨越检测，初始当前步已是 3 时也能正确切页。
4. 轮询侧的自动切换是兜底：当前步骤真的从 ≤2 跨到 ≥3 且**未被手动锁定**（`!pageLock`）时切到 `watch` 一次；首次刷新只记基线，不把初始化当跃迁。
5. 自检成功后显式切到 `watch`，保证报告可见。

**初始化落位**：首屏 `refresh()` + `loadSnapshot()` 完成后按真实 `currentStep` 落位——≥3 直接显示子页面二（含开启监控 / 直接睡眠），1/2 显示子页面一，并把 `prevStepForPage` 对齐，之后只认真正发生的 2→3 跨越。这样无论「清单已核对」还是「监控已在跑」，页面都与步骤条一致，不会出现高亮第 3 步却停在准备清单页、找不到值守按钮的情况。想重新扫描点步骤 1 即可。

**验证（CDP）**：初始 `watch` / Hero 内按钮数 0 / `btnMonitor` 位于 `watch` / 状态卡常驻；点 3→watch、1→prepare、2→prepare、4→watch 且当前步骤始终不变；手动点 1 后等待 4.2 秒仍为 `prepare`（`pageLock=true`）；预设基线 2 后等待轮询自动切 `watch` 且解锁；确认弹窗标题/令牌/焦点回归（`btnMonitor`）全部正常；五档视口 320/390/768/1200/1440 `scrollWidth == innerWidth`，无横向溢出。

### 3.10 待机方式：可用性 → 选择 → 效果

三层必须分清，界面上也是按这个顺序摆的：

| 层 | 回答的问题 | 落点 | 是否影响发出的命令 |
| --- | --- | --- | --- |
| **可用性** capability | 这台机器**做得到**哪几种待机？ | `powercfg /a` → `state/power-capabilities.json` | 否，只用于提示 |
| **选择** preference | 今晚**请求**哪一种？ | `config.sleep.prefer`（界面「待机方式」） | 是，决定发哪一条 |
| **效果** outcome | 到底**睡没睡**、挂了多久？ | `state/sleep_intent.json` + `selfcheck` | 否，事后判定 |

**为什么选择要单独做成一层**：不同机器的固件/组策略差别很大。桌面机常见「S3 可用、休眠未启用」；笔记本可能「休眠可用、S3 被组策略禁用」。不给用户选择，就只能让他改配置文件——那等于把最关键的一步藏起来。

具体位置：

| 位置 | 内容 |
| --- | --- |
| `asg/powercap.py` | `probe()` 解析 `powercfg /a`（可用段 / 不可用段 + 每条原因，兼容中英文与制表符缩进）、`powercfg /requests` 阻塞持有者、`is_admin`；`options()` 给出可选项与可用性；`recommend()` 给一句「该怎么选」 |
| `POST /api/powercap` | 重新探测并落盘，返回 `options` / `recommend` / 原始输出 |
| `POST /api/sleep/preference` | 把选择写回配置文件的 `sleep.prefer`（只改这一个字段） |
| `GET /api/state` 的 `power` | `prefer` / `options` / `recommend` / `stale` / `age_seconds` / `locked_by_monitor` |
| 界面「待机方式」卡片 | 两个单选钮 + 「可用/不可用/未检测」徽标 + 不可用原因 + 修复清单（`powercfg /hibernate on` 等） |
| `selfcheck` 第 9 项 | 选择的待机方式在本机不可用时判 **WARN**（不是 INFO），并给出修复项 |
| `sleep-sequence` STEP5 / `MONITOR_START` / `MONITOR_CONFIRMED` | 都写 `requested_mode`，早上复盘第一眼就能看到「这次按哪个模式发的」 |

三条硬规则（后端强制，前端无法绕过）：

1. **不可用的项不许保存**。目标不可用而另一种可用时，直接拒绝并建议改用另一种（命令行需 `--force` 才写盘，并留痕 `forced=True`）。
2. **监控运行中不许改**。否则「这一次到底按哪个模式睡」无法复盘；必须先优雅停止。
3. **两种都不可用时允许保存但必须告警**。值守仍然照跑（任务照常等待），但界面、接口返回、自检都会明说「命令可能被系统忽略，实际不会进入低功耗」，并给出修复清单。

另外还有一道**睡前复核**：点「开启监控」时，若探测结果已过期（默认 6 小时），后端会重探一次再启动；若此时发现所选方式已不可用，仍按用户确认继续，但会在返回消息里明说并写 `SLEEP_MODE_UNAVAILABLE` 事件。理由是系统更新/组策略随时可能关掉 S3，不能让用户拿着三天前的结论去睡。

口径不变：选择只决定**发哪一条**，仍然**只发一条、不补发**；实际进入 sleep 还是 hibernate 由系统决定，**两者都算成功**；事后只判「是否挂起、挂起多久」，不断言具体状态。

本机验证（全程 dry-run）：`powercfg /a` 解析出「待机 (S3) 可用 / 休眠不可用（尚未启用休眠）」；`options()` 给出 sleep=可用、hibernate=不可用；保存 hibernate 被拒绝并建议改用 sleep；`--force` 可保存且只改 `sleep.prefer` 一个字段（不把默认值 materialize 进配置文件）；`power prefer` 与 `POST /api/sleep/preference` 同口径；`selfcheck` 第 9 项 INFO、心跳连续性 PASS。

#### 3.10.1 直接睡眠（不经值守流程的手动入口）

**需求**：想让系统立刻睡眠时，不必再去找系统菜单或敲命令。按钮放在工作台子页面二 STEP 03 的「值守操作」区，**在「开启监控」之前**。

| | 开启监控 | 直接睡眠 |
| --- | --- | --- |
| 触发条件 | 任务全部终结 + 连续稳定复核 | 用户点一下就发命令 |
| 命令 | 监控进程在收尾 STEP5 发出 | 界面请求 `POST /api/sleep/now`，后端立即发出 |
| 快照 | 绑定已核对锁定的版本 | **不绑定**，intent 写 `snapshot_id=""` + `source=manual` |
| 授权 | 一次性令牌 + 确认弹窗 | **同一套**，不降级 |

**四条护栏**（手动入口不因为"省事"就放松）：① 必须带 `/api/state` 下发的一次性令牌，后端 `consume_confirm_token` 校验后立即作废；② 已有监控在运行时直接拒绝，前端同时把按钮置灰并写明「请先优雅停止」——否则系统唤醒后监控可能再发一条命令，违反「只发一条、不补发」；③ 仍按 `sleep.prefer` **只发一条、不补发**，不改任何系统电源设置；④ 前端确认弹窗写明「正在运行的 Agent 任务会被暂停」「请先保存文件」，取消不发命令。两个入口共用令牌池，谁先消费谁用掉，另一个入口下次刷新拿到新令牌。

**为什么 intent 要标 `source=manual`**：`selfcheck` 原来会用 intent 的 `snapshot_id` 查「快照完成状态」，查不到且非演练就判 FAIL。手动睡眠没有快照，若沿用「回退到最近 STEP1 快照」的逻辑，会拿上一次值守的快照来判，把一次正常的手动睡眠误报成事故。现在 `selfcheck` 见到 `source=manual` 就把 `sid` 置空 → 快照项判 INFO，而第 5 项「效果: 系统是否进入低功耗」照常用时间基线算出挂起时长。同时写 `SLEEP_MANUAL_ISSUED` 事件留痕。

**命令发出后**：前端设 30 秒轮询静默期（系统挂起瞬间请求注定失败，不该刷出假的「状态刷新异常」），并用全局轻提示告知「电脑即将进入低功耗」；唤醒后轮询自动恢复。`config.real.json` 的 `sleep.dry_run=true` 只约束命令行监控流程，`issue_sleep()` 不看它，所以按钮会真睡。

`dry_run=true` 是**仅供回归演练**的请求参数：只校验令牌、不写任何文件、不发命令，前端不暴露。用例 C56 覆盖取消、无令牌、演练、真机四个层次。

### 3.11 界面内规则确认（P1）

原来规则草案只能看不能点：界面「刷新规则」只打印草案列表，确认要去敲 CLI。P1 把**确认动作本身**搬进界面，原则不变——**界面只是操作面板，动作全部复用 CLI 子进程**（`rules --approve / --approve-noise / --add-ignore-pattern / --remove-ignore-pattern --config config.real.json`），留痕与命令行完全一致。

| 界面元素 | 行为 |
| --- | --- |
| 草案列表 | 每条 `adapter:key ×次数 [建议徽标]` + 判定下拉（默认值按建议预选：噪声建议预选 ignore，其余预选 middle）+ 「确认」按钮 |
| 判定下拉 | 与 CLI `--as` 完全一致的 9 个值：start / middle / end / running / queued / completed / failed / cancelled / ignore |
| 风险警示 | 选终态判定（end/completed/failed/cancelled）时弹窗明说「该值一出现就视任务结束，若 Agent 实际仍在运行会提前判完成、存在提前睡眠风险」；选 ignore 说明「不再参与判定」 |
| 批量忽略 | 「批量忽略建议为噪声的项」只处理建议为 ignore 的草案，其余仍需逐条判断；没有可忽略项时按钮禁用 |
| 前缀忽略管理 | 现有规则按 `adapter:pattern` 徽标列出，可单个删除（删除前确认「恢复保守判定」）；可新增（支持 `*` 通配） |
| 热重载 | 增删/确认后**运行中的 monitor 在 5 秒内自动重载规则表**（按 mtime），不必重启进程 |

安全边界：后端独立校验 adapter/key 格式（key 里的冒号是合法的，如 `status:failed`）与判定值合法性，前端传什么都拦得住；确认动作写 `RULES_APPROVED` / `RULES_NOISE_APPROVED` / `RULES_PATTERN` 事件 + 规则表 `history` 双留痕。

本机验证：前缀规则 add → remove 往返后指纹复原（`a50276dbbb6c`）；非法判定 / adapter 带空白 / 非法 action 均被后端拒绝；GET `/api/rules` 5 秒内热重载到新指纹。

### 3.12 日志检索（P1）

原来界面只有「日志尾」——最近几条聚合、每 3 秒刷一次，想找昨晚某条记录只能去翻文件。P1 把日志卡升级为可检索：

| 能力 | 说明 |
| --- | --- |
| 文件选择 | `/api/logs` 列出 `logs/` 下全部 `*.log`（含大小，新文件在前），下拉切换；默认选中最新的 `events-*` |
| 关键词过滤 | 不区分大小写的子串匹配，先过滤再截尾，回车即查 |
| 行数 | 末 100 / 200 / 500 / 2000 行四档，服务端硬限制 50–2000 |
| 自动刷新 | 勾选后每 3 秒按当前 文件+关键词+行数 重查；取消勾选即冻结 |
| 安全 | **白名单正则 `^[A-Za-z0-9_\-\.]+\.log$`**，只允许 logs/ 下的纯文件名——目录穿越（`../config.json`）、带路径（`no/such.log`）、非 .log 后缀一律 400；超过 8MB 的文件拒绝整读，提示用关键词或文本编辑器 |

接口：`GET /api/logs`（清单）、`GET /api/logs/read?file=&q=&tail=`（读取）。只读，不改任何文件；`/api/state` 里的 `events[]` 聚合尾保留（契约兼容），但日志卡已改用专用接口。

**排查「为什么没睡」时看这三类记录**（11-14 卡 11 小时那次复盘后补齐）：

| 关键字 | 含义 |
| --- | --- |
| `STATUS_CHANGE` | 任务状态迁移，带 `from_status` / `to_status` / `observed_at` / `evidence`。原来只在到达终态时写 `TASK_TERMINAL`，非终态变化（`queued → running`）只改内存，事后无法还原「什么时候从什么变成什么」；现在每次变化都留痕，**只记录不参与判定** |
| `POLL` / `PROGRESS` | 每轮 `remaining`、各任务当前状态与最后活跃时间。todo 类任务的 `last_active` 取它自己文件的 `updatedAt`，会话类取会话心跳——两者不再混用（旧实现统一返回全局心跳，会让一条从未变过的 `pending` 待办看起来每分钟都在活动） |
| `TASK_TERMINAL` / `STABILITY_CHECK` / `PRE_SLEEP` | 到达终态、连续稳定复核、睡前收尾。一个都没有说明连稳定复核的入口都没进 |

注意：**界面「任务清单」是扫描时冻结的快照，任务状态不会随监控实时更新**（`asg/snapshot.py` 只有整体状态流转，没有任务级回写）；实时状态要看监控日志。快照卡片已加这条提示。`source=manual` 的 intent 不绑定快照，`selfcheck` 会跳过快照完成状态与版本一致性两项，避免把手动直接睡眠误判成值守事故。

#### 3.12.1 运行期状态时间线（只读视图）

任务清单卡片下方新增可折叠的「运行期状态时间线」，回答「这条任务从 queued 到后来发生过什么」：

| 项 | 说明 |
| --- | --- |
| 数据来源 | `GET /api/task-timeline?task_id=&snapshot=&scope=` 聚合 `logs/monitor-*.log` 里的 `STATUS_CHANGE` / `TASK_TERMINAL` / `RETRY_CHAIN_NOTE`，按时间升序 |
| 扫描时基线 | 从冻结快照取该任务扫描时的 `status` / `created_at` / `evidence`，作为轨迹第一点。这样即使一次迁移都没有，也能一眼看出「从扫描起就是 queued、之后 0 次变化」 |
| 默认范围 | `scope=snapshot`，**只看当前快照绑定的那份 monitor 日志**。很多 `task_id`（如 `trae-session:ai-agent`）跨多轮值守复用，全量聚合会把不同日子的终态混成一条轨迹，看起来像同一任务终态了多次；`other_runs` 会提示其他值守还有几条，勾选「包含其他值守历史」才展开 `scope=all` |
| 边界 | 只读：不参与终态判定、不回写快照、不改 pending 集合。没有监控在跑就无人回查，此时时间线只有基线 + 空状态说明；重新扫描生成的是另一份快照，不会补写旧轨迹 |
| 安全 | `task_id` 白名单 `^[A-Za-z0-9_.:-]{1,200}$`、`scope` 只允许 `snapshot|all`、`snapshot` 复用 `load_snapshot` 的 `SNAP-[\w-]+` 校验；单文件超 8MB 跳过，最多扫 30 份日志、返回 200 条 |

用例 C57 覆盖空轨迹、终态轨迹、跨值守切换、非法参数与窄屏。

本机验证：`events-20261111.log` 全文 18 行、关键词 `selfcheck` 匹配 10 行；四类非法文件名全部 400；`tail=abc` 回退默认 200 不报错。

### 3.13 托盘常驻与系统通知（P2）

工具此前每次使用都要「记得双击 bat」。托盘把它变成**常驻入口**：双击图标在，右键即操作，值守结束主动气泡告知。

`tray.py` 零第三方依赖：托盘图标、右键菜单、气泡通知全部用 `ctypes` 调 Win32（`Shell_NotifyIconW` / `CreatePopupMenuW` / `TrackPopupMenu`），图标也是程序化生成的新月 `.ico`（写入 `state/tray-icon.ico`，不引入二进制资源文件）。服务承载与 3.7 对称：**谁先启动谁承载 8765**，托盘通常就是那个常驻进程；已运行时只复用。

| 托盘动作 | 行为 |
| --- | --- |
| 打开控制台（双击图标同） | 复用 `desktop_launcher.open_window()`：有 Edge/Chrome 开独立窗口，否则回退网页；**单实例**——已有窗口就置前，绝不再开第二个（见下） |
| 查看当前状态 | 气泡显示「监控运行中（pid）… 任务全部终结后请求睡眠」或「待命 · 待机方式」 |
| 优雅停止监控 | `POST /api/monitor/stop`（可逆动作，可直接发起），气泡回显结果 |
| 早上自检 | 后台线程跑 `POST /api/selfcheck`，气泡报 `PASS/WARN/FAIL` 汇总，FAIL 转错误图标 |
| 退出托盘 | 移除图标退出；**不影响正在运行的监控进程** |

**主动通知**（5 秒轮询 `/api/state`）：监控启动、值守结束且发出了睡眠命令（`sleep_intent.command_issued_at` 变化）、被停止且未发出命令，三种状态变化都气泡告知——早上不用打开页面就知道昨晚睡没睡成。

**独立窗口单实例（3.13.1）**：托盘每点一次都调 `open_window()`，而 `--app` 窗口不像普通标签页会被浏览器去重——不处理就是「点 N 下开 N 个相同窗口」。现在 `open_window()` 的顺序是：① 开窗后 5 秒保护期内直接忽略连点（浏览器建窗约需 1 秒，这期间进程查询还查不到）；② 用 PowerShell 查「命令行同时含 `--app=<本服务地址>` 和我们专用 `--user-data-dir`」的浏览器进程（专用 profile 是这个窗口的身份证，渲染子进程不带 `--app`，不会误判）；③ 找到进程后还必须找到**可见窗口**才置前（Edge 关窗后常驻后台进程，只看 PID 会永远打不开）；④ 都没有才真的新开。查询失败一律放行新开——宁可重复，不能堵死入口。实测：窗口存在时连点 3 次进程数不变；关窗重开、保护期内连点、保护期后再点，最终窗口恒为 1。

**权限边界（关键设计）**：托盘**不能**开启真实监控——菜单里只有「打开控制台（去确认开启）」，源码里不存在 `monitor/start` 与确认令牌逻辑。真睡授权仍只有网页里的一次确认，托盘零参与。

Win32 实现的坑都写在代码注释里：64 位下不声明 argtypes，`str` 会被当 ANSI 指针传、句柄会被截断成 32 位；`TrackPopupMenu` 前必须 `SetForegroundWindow`，否则点外部不关菜单；`WNDPROC` 回调必须持有引用防止被 GC。

自测入口：`python tray.py --selftest`（真实创建窗口/图标/消息循环，3 秒后自动退出）。

### 3.14 一键安装与分发边界（P2）

**安装**（`install.py` / `安装托盘.bat` / `卸载托盘.bat`，零依赖）：

| 动作 | 内容 |
| --- | --- |
| `python install.py install` | 桌面 + 开始菜单快捷方式「夜间值守」→ `pythonw tray.py`（无黑窗常驻） |
| `python install.py install --autostart` | 上述 + Startup 文件夹自启快捷方式（删文件即取消，**不写注册表**） |
| `python install.py status` | 查看各项安装状态；**会读出 .lnk 的 TargetPath/Arguments 与当前入口比对**（源码版与 exe 版切换、或整个文件夹搬家后显示「已安装，但指向别处 → 请重装」） |
| `python install.py uninstall` | 只删快捷方式与自启项；程序目录/日志/快照/配置一律不动 |

实现细节：快捷方式用 PowerShell `WScript.Shell` COM 创建；桌面路径用 `[Environment]::GetFolderPath('Desktop')` 解析（兼容 OneDrive 重定向）；**PowerShell 输出跟随控制台代码页（GBK），必须拿原始字节自行解码**——`text=True` 的 UTF-8 读取线程会直接崩；开始菜单子目录不存在时 COM 静默失败，创建前必须 `mkdir -p`。

**分发边界（显式留痕的取舍）**：源码形态 = 项目文件夹 + 快捷方式，换机器要整个拷过去（需该机器有 Python 3.10+）。若目标机器不想装 Python，用 **3.15 的单文件 exe**——同一个 exe 覆盖全部入口，复制即用。托盘/自启/通知这条零依赖路线证明了：除打包本身，桌面集成不需要任何第三方框架。

本机验证：`--selftest` 通过（窗口/图标/消息循环/清理）；`install --autostart` 三项快捷方式全部创建、`uninstall` 往返干净；`pythonw tray.py` 实测常驻并承载服务（API 正常）；托盘源码无 `monitor/start` 与令牌逻辑。

### 3.15 打包成单文件 exe（P2 收尾）

**构建（两条命令）**：

```bash
python -m pip install pyinstaller    # 仅构建期；运行期不需要
python build_exe.py                  # -> dist/夜间值守.exe  (9.1 MB)
```

`build_exe.py` 是四段流水线，任何一段不过就返回非 0：① 调 PyInstaller 构建 → ② 改成交付名 → ③ **冒烟**（`--version` / `--help` / `--asg-cli cases --json` 三条，证明分发器与内部 CLI 通道 + 中文编码都活着）→ ④ **空目录回归**（调 `verify_exe.py`）。

第 ④ 步才是真正的把关：它把 exe 复制到一个全新空目录，真跑「首次初始化 → 起服务 → 拉 4 个静态资源 → 走一次自检 → 确认数据留在 exe 旁边」共 16 项。**冒烟测试发现不了 `_MEIPASS` 路径类故障**（那种故障的表现是"能启动、界面也能开，但配置和日志每次重启都归零"），只有真跑一遍才看得见。也可单独跑：`python verify_exe.py [--keep]`。

**一个 exe 顶替原来的 5 个 bat**（`pack_entry.py` 只做参数分发，业务代码一行没搬）：

| 命令 | 等价于 |
| --- | --- |
| `夜间值守.exe` | `启动界面.bat`（承载本机服务 + 打开网页） |
| `夜间值守.exe --tray` | `托盘常驻.bat`（隐藏控制台窗口） |
| `夜间值守.exe --desktop` | `启动桌面窗口.bat`（Edge/Chrome `--app`） |
| `夜间值守.exe --install [--autostart]`、`--uninstall`、`--status` | `安装托盘.bat` / `卸载托盘.bat` |
| `夜间值守.exe --asg-cli <cmd> …` | `python agent_sleep_guard.py <cmd> …`（内部接口） |

**打包必须改的三处**（冻结态与源码态的真实差异，都是"能启动但功能静默失效"型故障）：

| 问题 | 症状 | 修法 |
| --- | --- | --- |
| 冻结后 `__file__` 在 `_MEIPASS` 临时目录里 | 配置/日志写进临时目录，退出即清空，表现为「每次启动都像第一次用」 | `util.ROOT` 冻结时改用 `sys.executable` 所在目录（可写数据）；`util.BUNDLE` 指向 `_MEIPASS`（`ui/` 从包内读） |
| exe 旁边没有 `agent_sleep_guard.py` | 界面每个动作（扫描/自检/规则确认/开监控）全部失败——它们都靠子进程调 CLI | 统一收敛到 `util.cli_argv()`：冻结时产出 `[exe, "--asg-cli", …]`；`util.spawn_kwargs()` 顺带加 `CREATE_NO_WINDOW`，避免每次调 CLI 弹黑窗 |
| 冻结后 stdout 接管道用的是控制台代码页（GBK），而界面按 UTF-8 读 | 自检报告 / 规则确认结果在界面上是乱码 | `pack_entry` 入口第一件事把 stdout/stderr `reconfigure(encoding="utf-8")`；接真实控制台时 Python 本来走 `WriteConsoleW`，这步是空操作，不影响在 cmd 里看中文 |

**数据边界**：`ui/`、`cases/` 打进 exe（只读，走 `BUNDLE` / `resource_path`）；`config.real.json`、`logs/`、`snapshots/`、`state/`、`rules/` **绝不打包**，首次运行在 exe 旁边自动生成。把 exe 复制到 `D:\夜间值守\` 就是一套独立环境，删文件夹即彻底卸载。用例集还支持「exe 旁边放一份 `cases/` 覆盖包内版本」——不重新打包也能换用例。

**打开控制台的三级兜底（3.15.1，真机踩坑后加的）**：首晚真机出现「服务正常、控制台窗口也在，但浏览器就是没弹出来」——旧版 `webbrowser.open` 挂在 `threading.Timer` 线程里，失败了**没有任何留痕**，无从查证。现在 `ui_server.open_console()` 依次：① `webbrowser.open`（默认浏览器）→ ② 失败则换**机制**重试：`desktop_launcher.open_window()` 用 Edge/Chrome `--app` 开独立窗口（走 CreateProcess，与 ShellExecute 完全不同通路）→ ③ 仍失败则把地址醒目打在控制台里。每一步的结果同时打到控制台**和**追加到 `logs/ui-open.log`（`outcome=webbrowser / app-window / failed` + 异常原文）——隔着管道/重定向看不见控制台输出时也能事后取证。

**已知取舍（不藏）**：单文件每次启动要把自身解包到 `%TEMP%\_MEIxxxx`，因此**每次调 CLI 子进程多约 0.5–1.5 秒**；`/api/state` 这类 3 秒轮询走的是进程内读文件，不经过子进程，不受影响。想要更快就改 `--onedir`（一个文件夹），代价是不再是"单文件"。**未做代码签名**：首次运行会被 SmartScreen 拦一下（"更多信息 → 仍要运行"）；签名需购买证书，不属于技术选型。

**单文件其实是双进程**：PyInstaller 的引导进程先把自己解包到 `_MEIPASS`，再拉起真身进程，所以任务管理器里会看到同名的两个进程（只关引导进程会留下真身继续跑，要 `taskkill /T` 连子树收）。**对功能没有影响**：停止监控走 `state/monitor.stop` 标志文件，monitor 只判断文件在不在、不比对 pid；`state/ui-monitor.json` 里记的是引导进程 pid，`monitor-*.log` 的 `MONITOR_START` 记的是真身 pid，两边对不上属正常，别当故障排查。

**源码版与 exe 版共存**：两套入口会抢同一个 `127.0.0.1:8765`，行为是「谁先启动谁承载，后启动只复用」——不会起第二个实例，看到的是同一份状态。但它们的**数据目录不同**（源码版 = 项目目录，exe 版 = exe 所在目录），所以切换形态后要先跑一次 `--status` 重装快捷方式（否则双击的还是旧入口），并且旧形态下的快照/日志不会自动搬过去。

## 4. CLI 参考

| 命令 | 作用 |
| --- | --- |
| `scan` / `scan run` | 手动扫描所有启用的 Agent，打印任务清单并生成 `SNAP-…-v1` |
| `scan list` | 列出全部快照：版本、来源、任务数、是否已核对/锁定、状态、重试决定 |
| `scan show --snapshot ID` | 打印该版本的完整信息与任务表（含 `watch_retry` 列） |
| `scan revise --snapshot ID` | 人工增删任务 → 生成新版本；`--add JSON`、`--remove TID`、`--tasks-file PATH`、`--reason`、`--operator` |
| `scan verify --snapshot ID` | 记录"已核对"标记 + 重试风险默认留痕，锁定该具体版本；`--implicit` 采用界面默认（`no_action`、`explicit=false`），或显式给 `--retry-reuses-id` + `--decision` |
| `monitor --snapshot ID` | 绑定该版本启动监控（含轮询、复核、睡眠触发）；`--dry-run` / `--no-dry-run` |
| `selfcheck` | 唤醒后自检；`--clear-stale-lock` 归档并清理陈旧锁 |
| `discover` | 只显示在用的 Agent + 变更对比 + 数据源按精度降级（仅写自身 state 文件）；`--all`、`--write-config PATH`、`--json` |
| `scan --auto-discover` | 扫描前自动发现并应用数据源，无需手写配置 |
| `rules` | 观测规则漂移、生成草案、维护前缀忽略；`--approve ADAPTER:KEY --as 判定`、`--approve-noise`、`--add-ignore-pattern` / `--remove-ignore-pattern`（配 `--adapter`）、`--no-sample`、`--json` |
| `power show` / `power probe` | 查看 / 重新探测睡眠与休眠可用性（`powercfg /a`、`/requests`），给出可选项与建议；`--json` |
| `power prefer --prefer sleep\|hibernate` | 设置监控结束后请求哪一种待机方式；目标不可用时拒绝（`--force` 可强制写盘并留痕） |
| 双击 `启动界面.bat` | Web 版本地控制台（3.6），浏览器标签打开 `127.0.0.1:8765` |
| 双击 `启动桌面窗口.bat` | 独立窗口版（3.7），无地址栏 / 无标签页；与 Web 版共用同一服务 |
| 双击 `托盘常驻.bat` | P2 托盘常驻（3.13）：pythonw 后台运行，图标在系统托盘，右键操作 + 状态气泡 |
| `python install.py install --autostart` / `uninstall` / `status` | P2 安装（3.14）：桌面/开始菜单快捷方式 + 开机自启；卸载只删快捷方式不动数据 |

所有入口都支持 `--config PATH` 指定配置文件（相对路径锚定项目根目录）。

### `scan revise` 的变更留痕

修正会生成同 family 的新版本，并在**快照 `change_log`** 与**全局事件日志**两处记录：

```
SNAPSHOT_REVISE | from=SNAP-…-v1 to=SNAP-…-v2 from_version=1 to_version=2
                | added=["T-3001"] removed=["T-2001"] reason=… operator=…
```

已 `verify` 锁定的版本**不允许再修订**（防止"核对的是 A 版、监控跑的是 B 版"），只能重新 `scan`。

### `scan verify` 的重试风险决定（界面默认，CLI 可显式）

界面主路径不再要求人工逐条选择：`/api/verify` 默认走 `--implicit`，写 `retry_risk.decision=no_action`、`explicit=false`，并在 `note` 说明「无人值守场景重试需人工触发，沿用原任务 ID，无额外处理」。留痕仍在，`monitor` 仍要求该字段存在。

**高级/CLI 路径**保留完整三选一，收在界面的「高级选项」折叠里：

| `--retry-reuses-id` | `--decision` | 是否允许 | 含义 |
| --- | --- | --- | --- |
| `true` | `no_action` | ✅ | 重试沿用原 ID，无额外处理（界面默认） |
| `false` | `no_action` | ❌ **拒绝** | 存在"重试任务不在快照内、监控不会跟踪"的风险，必须显式表态 |
| `false` | `accept_untracked_retry` | ✅ | 显式接受：快照内旧任务终结即视为完成，重试任务不跟踪 |
| `false` | `watch_retry_chain` | ✅ | 标记任务"需观察重试链"，运行时用 Agent 级活跃信号兜底 |

`--decision no_action` 搭配 `false` 会被直接拒绝（见 `SnapshotError`）。`watch_retry_chain` 未指定 `--watch-task` 时默认标记全部任务。仅当 Agent 会**自动重试并生成新任务 ID** 时才需要动这个选项。

---

## 5. 配置说明（`config.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `poll_interval_seconds` | 30 | 常规轮询间隔 |
| `progress_interval_seconds` | 60 | 进度日志间隔（"还有几个任务未终结、各自最后活跃时间"） |
| `heartbeat_interval_seconds` | 3600 | 存活心跳间隔 |
| `heartbeat_max_interval_seconds` | 3600 | 心跳间隔上限，保证**每小时至少一条**（实际取两者较小值） |
| `stable_required` | 3 | 终态复核连续稳定次数 |
| `stability_interval_seconds` | 60 | 稳定复核间隔，规范 1–2 分钟；不要用低于 60 的值做正式值守 |
| `terminal_statuses` | `completed, failed, cancelled` | 终态集合 |
| `unknown_status_is_terminal` | `false` | 状态读不到时**绝不**视为完成 |
| `paths.*` | `snapshots/ logs/ state/ data/` | 目录，相对项目根 |
| `sleep.dry_run` | `true` | 默认安全：不真睡，但完整走完日志与状态更新 |
| `sleep.prefer` | `sleep` | 只决定发哪一条命令（sleep / hibernate）；界面「待机方式」直接改它，**不可用的一项会被后端拒绝保存**；**只发一条、不补发**；实际进入哪种状态由系统决定，两者都算成功（见 3.10、第 7 节） |
| `sleep.detached` | `true` | 以 `CREATE_NO_WINDOW` 派发命令（不弹控制台窗口），不等待返回；见 7.1 |
| `sleep.command` | PowerShell + WinForms `SetSuspendState` | 见第 7 节 |
| `selfcheck.issued_but_not_slept_wall_seconds` | 600 | 命令发出后超过该墙钟时长仍无睡眠迹象 → 告警 |
| `selfcheck.sleep_duration_threshold_seconds` | 60 | 估算睡眠时长 ≥ 该值视为真睡过 |
| `selfcheck.heartbeat_max_gap_seconds` | 5400 | 心跳最大允许断档（1.5h）；**只判同一次监控内**的间隔与「未收尾且静默」，跨次空档不计 |
| `adapters[]` | 示例 `mock_file` | 启用的 Agent 数据源 |
| `adapters[].options.idle_confirm_seconds` | 300 | 日志停在**结束事件**后，需再静默多少秒才判完成；停在任务开始/中间时该值完全不生效（见 3.3） |
| `adapters[workbuddy_local].options.include_completed_todos` | `false`（`config.real.json`；代码默认 `true`） | 是否把已终态的 `wb-todo:*` 子任务纳入清单；`false` = 只保留主任务与未完成子任务（见 3.3） |
| `adapters[workbuddy_local].options.include_pending_todos` | `false`（代码默认 / `config.real.json` / 自动发现生成配置均为 `false`） | 是否把 `queued`（原始 `pending`，计划清单里尚未勾选开始）的 `wb-todo:*` 纳入清单；`true` = 恢复保守行为。会话级主任务与 `running`/`unknown` 子任务不受影响（见 3.3） |
| `adapters[workbuddy_local].options.todo_terminal_statuses` | 同 `terminal_statuses` | 自定义「已终态」集合；`unknown` 恒被硬排除，绝不因读不到状态而删除子任务 |

---

## 6. 监控与终态复核（内部逻辑）

1. **绑定版本**：`monitor` 只接受 `verified=true && locked=true` 的快照，并在日志写 `BOUND_SNAPSHOT snapshot_id=… version=…`，杜绝版本错配。
2. **只跟踪快照内任务**：轮询按 `task_id` 回查；数据源里冒出来的清单外任务只写一行 `IGNORED_NEW_TASKS`，**不处理、不告警、不阻塞**。
3. **终态标记**：命中 `terminal_statuses` 即从待完成集合移除，写 `TASK_TERMINAL`。状态由适配器每轮回查给出；三个真实适配器都支持 3.3 的「事件位置」规则动态计算，不是只看日志静默时长。WorkBuddy 会话还会每轮检查是否出现新的 `role=user` 用户输入——一旦有人回来，状态回到 `running`，稳定复核被打破，不会睡。
4. **不设超时**：`POLICY | timeout_enabled=False interrupt_enabled=False`。没有任何基于时长的中断/杀进程动作。
5. **进度日志**：`PROGRESS | remaining=N detail=<task>@<agent>:<status>:last_active=…`，纯记录。
6. **存活心跳**：`HEARTBEAT | 仍在运行 | remaining=N`，每小时至少一条；早上按「同一次监控内」的间隔是否连续判断监控是否中途死亡（跨次空档不算，见第 8 节）。
7. **终态复核**：全部终结后进入稳定复核，间隔 `stability_interval_seconds` 连查 `stable_required` 次；每次重新查一遍已终结任务，若发现"任务回到非终态"的瞬态抖动则 `STABILITY_RESET` 并回到常规轮询。
8. **固定顺序收尾**（单线程、禁止并发）：

| 步 | 日志 STEP | 动作 |
| --- | --- | --- |
| 1 | `STEP1_PRE_SLEEP_WRITTEN_AND_CLOSED` | 写"即将睡眠"日志（时间戳 / 快照版本 / 任务终结摘要），**flush + fsync + close**，不保留打开句柄 |
| 2 | `STEP2_MONITOR_STOPPED_JOINED` | 设 `stop_flag`，等当前轮询周期自然结束、循环退出、在途检查完成，`worker.join()` 确认，**不强杀进程** |
| 3 | `STEP3_LOCK_SNAPSHOT_UPDATED_AND_CLOSED` | 锁与快照状态更新为 `completed_pre_sleep`，写完关闭 |
| 4 | `STEP4_RESIDUAL_WRITES_CONFIRMED_NONE` | 再次确认：日志已关 / 锁已写 / 快照已写 / 轮询已停；任一项不满足则 `ABORT_SLEEP`，**不睡眠** |
| 5 | `STEP5_SLEEP_COMMAND_ISSUED` | 写"已发出"日志 + 睡眠意图基线（`state/sleep_intent.json`）→ 发出睡眠命令 |

> STEP4 确认的是**监控期在途写操作**已全部结束；STEP5 的两条记录是同步 `open→flush→fsync→close` 的最终封存记录，在真正派发命令**之前**即已写完并关闭。因此"所有日志在睡眠指令发出前写完并关闭"成立。
>
> 第 5 步**不等待返回值、不依赖返回码判断是否已睡、不等待退出、不做主动 exit**。dry-run 下跳过命令派发，但 STEP1–STEP5 的日志与状态更新**一步不少**。

### `watch_retry_chain` 的兜底

重试生成新 `task_id` 时，新任务不在冻结快照里，按原则不该被跟踪。本工具的做法是：被标记的任务在终态判定时额外查询适配器的 **Agent 级活跃信号** `agent_is_idle()`；若同 Agent 又出现活跃迹象（例如写了新的日志文件），则 `RETRY_CHAIN_ACTIVE`，暂不判全终态，继续等。适配器不支持该信号时写 `RETRY_CHAIN_NO_SIGNAL` 留痕、**不改变终态判定**（取舍见第 10 节）。

---

## 7. 睡眠命令与效果口径（重要）

### 7.1 只发一条命令

**禁止**使用 `rundll32.exe powrprof.dll,SetSuspendState ...`（参数语义混乱、部分配置下变休眠）。

配置 `sleep.prefer` 决定发哪一条，默认 `sleep`（界面「待机方式」两个单选钮写的就是它）：

| prefer | 命令 | 界面标签 |
| --- | --- | --- |
| `sleep` | `powershell.exe ... SetSuspendState('Suspend', $false, $false)` | 睡眠（S3 / S0 现代待机） |
| `hibernate` | `shutdown.exe /h` | 休眠（Hibernate） |

选哪个由用户按「本机可用性」决定（见 3.10）；不可用的一项界面上是禁用的，后端也会拒绝保存。

**只发一条、不补发、不重试**。命令发出后不等待返回值、不依赖返回值判断是否已睡、不主动 exit。

**派发方式**：使用 `CREATE_NO_WINDOW`（不弹控制台窗口）。本机实测 `DETACHED_PROCESS` 会让 PowerShell 子进程**根本不执行**（`Popen` 成功、但脚本一行没跑、连诊断日志都写不出来），表现就是"日志显示已发出、系统却没睡"；换成 `CREATE_NO_WINDOW` 后子进程正常执行，父进程立即退出也不影响。配置 `sleep.detached` 的语义现在是"不弹控制台窗口"。

### 7.2 效果口径（不看命令，看效果）

命令按 `sleep` 发出，但**实际进入睡眠还是休眠由本机决定**：休眠开关、组策略、固件/驱动、Modern Standby 等都可能让结果表现为 hibernate。**两者都视为成功**，产品上不再纠结实际是哪种。

事前由代码自动探测电源能力（`powercfg /a`、`powercfg /requests`），结果写 `state/power-capabilities.json`，**只用于「能不能选」的提示与留痕**；真正决定发哪条命令的是 `sleep.prefer`。事后由 `selfcheck` 报告效果。

| 能可靠判断 | 不能可靠判断 |
| --- | --- |
| 系统是否进入低功耗、挂起时长（无偏时钟） | 实际是 sleep 还是 hibernate —— Kernel-Power 107「恢复」在休眠恢复时同样产生，42/107 只能佐证进出低功耗 |

因此报告只写「本次请求模式 + 实际观测效果」，不断言具体状态。

### 7.3 不再要求人工实测

原「正式使用前必须手动实测一次」的前提已取消，改为四道代码化保障：

1. 监控启动前自动跑 `powercfg /a` / `/requests`，结果落到界面「待机方式」卡片与自检第 9 项；也可点「检测可用性」手动刷新
2. 命令发出后由 `selfcheck` 用无偏时钟 + 事件日志判定效果
3. 若「命令已发出但系统未挂起」，自检 FAIL 并列出可能原因
4. 首次因策略静默失败也能被识别，不需要人工先试一次

### 7.4 给客户的提示

**先按界面显示的结果选待机方式**：`powercfg /a` 的「可用」段决定哪一项能选。不可用的项会被禁用并在后端拒绝保存——不是限制，是避免你以为今晚会睡。

**坚持只 sleep**（希望尽量别变成休眠）：

- 保持 `sleep.prefer = sleep`；管理员运行 `powercfg /a`，确认 Standby (S3) 或 S0 Low Power Idle 出现在「可用」段
- 若因休眠启用被系统转为休眠：`powercfg /hibernate off`（注意会同时关闭快速启动）
- 检查组策略「允许待机状态(S1-S3)」是否被禁用；更新 BIOS / 芯片组 / 显卡驱动
- `powercfg /requests` 查并清掉持有电源请求的进程或设备
- 仍不可用时工具**放弃并告警**，不会偷偷改发别的命令

**首选 hibernate**：

- 界面把「待机方式」切到休眠（等价于 `sleep.prefer = hibernate`，命令改为 `shutdown.exe /h`）
- 管理员 `powercfg /hibernate on`，确认 `hiberfil.sys` 有足够空间；改完点「检测可用性」复核
- 提醒：唤醒比睡眠慢，且会影响 Windows 快速启动

**接受自动效果（默认推荐）**：`prefer = sleep`，实际 sleep 或 hibernate 都算成功。

### 7.5 静默失败与诊断留痕

`SetSuspendState` 可能因系统策略静默失败（唤醒锁、策略禁止、权限不足、驱动异常）。工具不等待、不检查返回值，由 `selfcheck` 事后识别。

为区分「系统拒绝了命令」和「命令进程根本没跑起来」，`issue_sleep` 会在 PowerShell 命令末尾注入一段**由命令进程自己执行**的诊断脚本，把结果追加到 `logs/sleep-command-YYYYMMDD.log`：时间、请求模式、PID、会话 ID、是否交互桌面、是否管理员、`SetSuspendState` 返回值、PowerShell 错误。

规范不变：工具自身的日志/意图记录仍在派发前写完关闭；工具不等待子进程、不读取该诊断文件、不据此判断是否已睡；文件由子进程写，仅用于早上复盘。`selfcheck` 新增「睡眠命令诊断」项展示最近一次内容，验收报告也会附上。

判读：**文件不存在** = 命令进程没执行到写日志处（派发方式有问题 / 被回收）；**`callError` 非 `none`** = API 抛异常（已用 `try/catch` 包住，异常信息会记录且诊断仍会写）；**`setSuspendStateReturn=False` 或 `isAdmin=False`** = 权限或 API 被拒；**返回 `True` 但系统未挂起** = 系统侧拒绝（查 `powercfg /requests`、组策略、驱动）。dry-run 不派发命令，因此不会产生诊断日志。

### 7.6 可选：脚本自测（**真实夜间值守不需要运行**）

> 日常值守请走第 7.7 节的 SOP，用界面直接操作。`config.acceptance.json` / `run_acceptance.py` / `开始真机验收.bat` 只用于验证"脚本本身是否可靠"（受控假数据、快进间隔），已完成使命，保留作为回归自测工具。真机闭环已在 2026-11-09 连续验证通过（15:50 / 16:53 / 17:31 三次真实睡眠，命令诊断 `setSuspendStateReturn=True`、自检 `FAIL=0`）。

前面所有验证都是 dry-run。要确认脚本本身真的能用，需要一次受控的真机闭环。准备产物已就绪：

| 产物 | 作用 |
| --- | --- |
| `config.acceptance.json` | 独立配置：`dry_run=false`、快进间隔、受控数据源（开局即全部终态）、产物全落 `acceptance/`。**正式配置 `config.json` / `config.real.json` 仍是 `dry_run=true`，不受影响** |
| `run_acceptance.py` | 前置检查 → 要求输入大写 `SLEEP`（脚本内一次确认）→ 扫描/核对 → 非演练监控 → STEP5 派发一条 sleep 命令 → 唤醒后自动 `selfcheck` → 写 `acceptance/验收报告.txt` |
| `开始真机验收.bat` | 双击入口 |
| `python run_acceptance.py --rehearsal` | 演练模式：强制 dry-run，只验证脚本链路、不睡 |

**操作步骤**：① 保存所有工作、关闭正在编辑的文件、确认电源/电池正常 → ② 双击 bat，阅读警告后输入大写 `SLEEP` → ③ 约 10 秒后系统进入低功耗 → ④ 按电源键唤醒，脚本自动继续并生成报告。

| 验收标准 | 期望 |
| --- | --- |
| `acceptance/state/sleep_intent.json` | `dry_run=false`、`requested_mode=sleep`、`fallback_enabled=false` |
| `sleep-sequence` | 恰好 1 条 `dry_run=False` 的 STEP5，且 STEP5 带 `diag_log` 字段 |
| `acceptance/logs/sleep-command-*.log` | 存在，且能看到 `setSuspendStateReturn` / `isAdmin` / `session` 等字段 |
| `selfcheck`「效果: 系统是否进入低功耗」 | **PASS** 且报告挂起时长（不是 INFO / FAIL） |
| `selfcheck` 汇总 | `FAIL=0` |
| 一次确认 | 输入大写 `SLEEP`（脚本内确认，非持久开关） |
| 正式配置 | 仍为 `dry_run=true` |

**风险与边界**：不修改任何系统电源设置（不跑 `powercfg /hibernate on|off`、不建唤醒计划任务），唤醒靠人工按电源键；只发一条命令、不补发，实际 sleep 或 hibernate 都算成功；若静默失败，等待 45 秒后 `selfcheck` 会判 FAIL 并列出原因；想中止可在输入确认词前直接关闭窗口，监控期间 `Ctrl+C` 会优雅停止且不睡。

### 7.7 夜间值守 SOP（主路径）

这是日常使用的唯一推荐路径，共四步，全部在界面完成：

| 步 | 操作 | 系统行为 |
| --- | --- | --- |
| 1 | 双击 `启动界面.bat`（或 `启动桌面窗口.bat`） | 本地控制台打开，只监听 `127.0.0.1` |
| 2 | 点「重新扫描」→ 勾选要盯的任务 → 点「核对并锁定此版本」 | 生成冻结快照，写入核对留痕；重试风险默认 `no_action`，一般不用动「高级选项」 |
| 3 | 确认「待机方式」正确 → 点「开启监控」，在弹窗里确认一次 | 弹窗会写明本次待机方式；校验一次性确认令牌 → 写 `MONITOR_CONFIRMED`（含 `requested_mode`）→ 独立进程启动；关闭浏览器不影响它 |
| 4 | 结束对话、去睡 | 交互会话静默 5 分钟后判完成 → 3 次稳定复核 → STEP1–5 → 发一条按 `sleep` 的命令；值守结束无需关任何开关 |

早上回来跑一次 `python agent_sleep_guard.py selfcheck --config config.real.json`，看「效果: 系统是否进入低功耗」是否 PASS、挂起多久、`FAIL=0`，以及「一次确认护栏」是否 PASS。

> 安全默认：正式配置 `dry_run=true`（只约束命令行）；界面真睡授权是一次性令牌，用后即焚、不落盘，值守结束自动回到安全态。连续多晚值守也只需每晚各自确认一次，没有需要手动关回的开关。

---

## 8. 唤醒后自检（`selfcheck`）

```
[PASS] 关键日志成对             "即将睡眠"(STEP1) 与 "已发出"(STEP5) 是否成对存在；未进入收尾时判 INFO 并说明原因（见下）
[PASS] 终态收尾固定顺序         STEP1 -> 2 -> 3 -> 4 -> 5 顺序完整且无并发重排
[PASS] 遗留锁文件               completed_pre_sleep 为预期保留；running+存活=FAIL；running+已死=WARN
[PASS] 快照完成状态             绑定快照是否标记 completed_pre_sleep
[PASS] 快照版本一致性           核对版本 / 监控 BOUND_SNAPSHOT 版本 / 睡眠记录版本 是否一致
[FAIL] 效果: 系统是否进入低功耗 判断是否真的挂起、挂起多久；不断言 sleep / hibernate（见下）
[INFO] 电源能力与待机方式        powercfg /a 能力摘要、当前选择是否可用、选择建议；选择不可用时判 WARN
[PASS] 监控存活心跳连续性        同一次监控内最大间隔 > heartbeat_max_gap_seconds 即 WARN；未收尾且静默超限也 WARN
[INFO] 电源事件佐证              42 / 107 仅佐证进出低功耗；休眠恢复也产生 107，不能区分两种状态
[PASS] 一次确认护栏              上次监控演练模式、确认时间、确认事件累计次数
```

**心跳判定的两条判据**（第 6 项）：同一次监控内相邻心跳间隔超限 → 进程卡死/被挂起；某次监控既没收尾事件（`PRE_SLEEP` / `MONITOR_ABORTED`）又静默超限 → 进程已死且没人知道。**跨次空档不参与判定**——上一次监控结束后到下一次开始之间（含整夜睡眠）本来就没有进程会写心跳，把它算成断档只会每天早上一声假警报。

**第 1 项的三种 INFO（不再误判为崩溃）**：`即将睡眠` 只在监控真正进入收尾时才写。因此「新装机器一次都没跑过」「任务整夜没全部终结（按设计本来就不发睡眠命令）」这两种良性情形都判 **INFO** 并说明具体是哪一种，不再报 FAIL 让人以为出了事故。真正的「收尾时崩溃」由「已写 STEP1 但没写 STEP5」这种形态判 FAIL 兜住；「监控死在半路」由心跳那项判 WARN。

报告同时写入 `logs/selfcheck-YYYYMMDD.log`，每项异常都给出可能原因。

### "效果: 系统是否进入低功耗"如何识别

`state/sleep_intent.json` 在派发命令前记录墙钟时间、`GetTickCount64`、以及 **`QueryUnbiasedInterruptTime`**（Windows 的"无偏中断时间"，**不含睡眠/休眠时长**，单位 100ns）。唤醒后：

```
墙钟经过量   = now - wall_time_epoch
醒着时间量   = (unbiased_now - unbiased_issued) / 1e7
估算睡眠时长 = 墙钟经过量 - 醒着时间量
```

- 估算睡眠 ≥ `sleep_duration_threshold_seconds` → **PASS：确实睡过**；
- 估算睡眠 ≈ 0 且墙钟经过 ≥ `issued_but_not_slept_wall_seconds` → **FAIL：命令已发出但系统未睡**，并列出可能原因（唤醒锁 / 电源策略 / SetSuspendState 静默失败 / 实际是 hibernate）；
- 时间太短 → INFO：请稍后重跑；
- 检测到 `TickCount64` 回退（系统重启过）→ INFO：改用事件日志判定，避免误报；
- 时钟 API 不可用 → 退回 System 事件日志 Kernel-Power 107 恢复事件判定；仍不可读则明确提示人工核对。

dry-run 记录带 `dry_run=true`，自检**不会**误报"未睡"。

---

## 9. 日志与状态文件

| 文件 | 内容 | 关闭时机 |
| --- | --- | --- |
| `logs/events-YYYYMMDD.log` | 扫描结果、快照版本变更、已核对标记、重试风险决定、监控启动/中止、状态更新 | 每条 open→fsync→close |
| `logs/monitor-<快照ID>-YYYYMMDD.log` | 启动/策略/绑定版本、每次 `POLL`、`PROGRESS`、`HEARTBEAT`、`TASK_TERMINAL`、`IGNORED_NEW_TASKS`、`STABILITY_CHECK`、`PRE_SLEEP` | STEP1 后 **flush+fsync+close**，之后禁止再写 |
| `logs/sleep-sequence-YYYYMMDD.log` | STEP1–STEP5 固定顺序日志（供自检核对顺序与成对） | 每条 open→fsync→close |
| `logs/selfcheck-YYYYMMDD.log` | 自检报告全文 | 追加后立即关闭 |
| `snapshots/SNAP-…-vN.json` | 冻结任务清单、版本链、变更日志、核对标记、重试决定、状态 | `atomic_write_json`（临时文件 + fsync + replace） |
| `state/monitor.lock` | 单例锁：pid / 快照 ID / 启动时间 / 状态 | 原子写，不长期持有句柄 |
| `state/sleep_intent.json` | 睡眠触发时间线基线（供"未睡"判定） | 命令派发前写完并关闭 |

所有写操作都做 `flush + os.fsync`，避免"杀在写日志瞬间导致文件损坏"。

---

## 10. 已知风险与取舍（显式留痕，不隐含）

| # | 风险 | 本工具的处理 | 仍然存在的取舍 |
| --- | --- | --- | --- |
| 1 | **快照版本一致性**：修正后版本变了、核对的 A 版却跑了 B 版 | 每次 `revise` 生成新版本 + 记录 `原版本→新版本`（增/删明细）；`verify` 锁定具体版本；`monitor` 只接受已锁定版本并在 `BOUND_SNAPSHOT` 记录实际版本；`selfcheck` 交叉比对三个版本号 | 已锁定版本不能再修订，只能重新 scan；这是刻意的刚性约束 |
| 2 | **重试任务 ID 变化**：重试任务不在快照内、监控不会跟踪 | `verify` 强制二选一：`accept_untracked_retry`（显式接受，写 `RISK_ACCEPTED`）或 `watch_retry_chain`（标记任务 + Agent 级活跃信号兜底）；`--retry-reuses-id false --decision no_action` 会被**拒绝** | `watch_retry_chain` 只能靠 Agent 级信号间接发现重试，若适配器不提供该信号则退化为"仅留痕、不改变判定"；新 ID 任务仍不会被逐条跟踪 |
| 3 | **监控自身死亡**：永远不睡且无人知晓 | 每小时至少一条 `HEARTBEAT`；`selfcheck` 按「同一次监控」检查心跳最大断档 + 「未收尾且静默超限」，超限告警；陈旧锁（`running` + PID 已死）也会告警 | 心跳只能事后诊断，不能自动拉起监控（也不应该自动拉起——那会违背"人负责判断"） |
| 4 | **睡眠命令静默失败** | `selfcheck` 用 `QueryUnbiasedInterruptTime` 对比墙钟时间线识别"效果: 系统是否进入低功耗"，并用 Kernel-Power 事件佐证 | 判定是启发式的：Modern Standby / 短暂睡眠 / 事件日志权限不足时可能给出 INFO 而非确定结论，需人工核对 |
| 5 | **监控停止的优雅性** | `stop_flag` + `worker.join()` 等当前周期自然结束；日志先关再停；STEP4 再确认无残留写；全程不强杀 | STEP5 的两条最终封存记录是同步写、在命令派发前关闭，不属于"在途写操作" |
| 6 | **日志静默误判** | 禁止"只看静默 N 分钟就判完成"。用事件位置状态机：最后一条任务事件必须是明确的 `END`，且之后 N 分钟无新的 `START`/`MIDDLE` 事件才判 `completed`；停在任务开始或进行中一律 `running`（见 3.3） | 事件分类来自规则表；应用升级换了事件名会被 `rules` 自动列为漂移/草案，未确认前按 `middle` 保守不睡（见 3.5） |

**另外两条需要人自己承担的判断**：
- 快照是**冻结**的：监控期间新提交的任务一律不跟踪，这是设计前提，不是 bug。所以"开启监控前把清单核对准"这件事必须由人做完。
- 失败/取消也算终态（`terminal_statuses` 默认含 `failed`、`cancelled`）。如果业务上"失败但会自动重试"不算完成，请在核对阶段用 `watch_retry_chain` 或直接把该任务剔除。

---

## 11. 新增一个 Agent 适配器

适配器采用**自动发现**：在 `asg/adapters/` 下丢一个 `.py`，定义 `BaseAdapter` 子类并设置 `type`，核心代码零改动。

```python
# asg/adapters/my_agent.py
from .base import BaseAdapter

class MyAgentAdapter(BaseAdapter):
    type = "my_agent"          # 对应 config.json 的 adapters[].type

    def scan_tasks(self):
        # 从 API / 日志文件 / 进程列表 / SQLite ... 取当前任务
        # 返回 list[dict]，每条至少含 task_id / agent / start_time / status
        return [
            {"task_id": "X-1", "agent": "my-agent-01",
             "start_time": "2026-11-08T01:00:00+08:00", "status": "running",
             "last_active": "2026-11-08T01:30:00+08:00"},
        ]

    def get_task_status(self, task):
        # 按 task["task_id"] 重新查当前状态，返回归一化状态字符串
        # 查不到请返回 "unknown"（非终态），宁可整夜不睡
        return query_api(task["task_id"]).get("state", "unknown")

    def get_last_active(self, task):
        # 可选：进度日志里的"最后活跃时间"
        return query_api(task["task_id"]).get("updated_at")

    def agent_is_idle(self, agent):
        # 可选：Agent 级活跃信号，供 watch_retry_chain 兜底
        # True=空闲 / False=仍有活跃迹象 / None=本适配器不支持
        return None
```

然后在 `config.json` 里启用（可同时启用多个适配器，任务会合并）：

```json
"adapters": [
  { "type": "my_agent", "name": "prod-agent", "enabled": true,
    "options": { "api_base": "http://127.0.0.1:9000", "token": "..." } },
  { "type": "mock_file", "name": "mock_file", "enabled": false, "options": {} }
]
```

约定：
- 状态归一化到 `running / queued / completed / failed / cancelled / unknown`；终态集合在 `terminal_statuses` 配置。
- `get_task_status` 必须能按 `task_id` **重复查询**（监控每轮都会回查）。
- 任务字典里 `source` 字段由聚合层自动填入，用于把回查路由回原适配器，无需自己写。
- 适配器抛异常时聚合层统一降级为 `unknown`，不会误判完成、也不会让监控崩溃。

### 内置适配器

| `type` | 用途 |
| --- | --- |
| `mock_file` | 示例/演示适配器，用 JSON 状态文件或日志 mtime 模拟任务 |
| `workbuddy_local` | 真实适配器，只读本机 WorkBuddy 的 `workbuddy.db` + `tasks/` + `sessions/`（用法见 3.3） |
| `autoclaw_log` | 真实适配器，解析 AutoClaw2 结构化日志的工具调用 started/completed 配对（用法见 3.3） |
| `trae_agent` | 真实适配器，读 Trae CN `execution.json` 的命令级 startedAtMs/finishedAtMs（用法见 3.3） |

### 新增 Agent 自动发现探针（产品化）

`asg/discovery.py` 里加一个 `AgentProbe` 子类并加入 `PROBES` 列表即可：

```python
class MyAgentProbe(AgentProbe):
    id = "my_agent"; name = "My Agent"; adapter_type = "my_agent"
    process_names = ("myagent.exe",)

    def sources(self):                     # 按精度降序，解析不了自动降级
        root = Path(os.environ["APPDATA"]) / "MyAgent"
        return [
            SourceSpec("state_db", "state/tasks.db 明确终态", 4,
                       [root / "state" / "tasks.db"], "dir_exists", "state_dir"),
            SourceSpec("event_log", "logs/*.jsonl 事件日志", 3,
                       [root / "logs"], "any_jsonl", "logs_dir"),
        ]

    def version_files(self):               # 用于版本升级检测
        return [Path(os.environ["LOCALAPPDATA"]) / "Programs" / "MyAgent"
                / "resources" / "app" / "package.json"]

    def spec(self, found, selected, attempts):
        return {"type": self.adapter_type, "name": self.adapter_type,
                "enabled": True,
                "options": {"state_dir": found.get("state_dir"),
                            "logs_dir": found.get("logs_dir"),
                            "primary_source": selected.key if selected else "",
                            "source_fallbacks": self._fallbacks(attempts)}}
```

把类加入 `PROBES` 列表即可。要点：

- `SourceSpec.precision` 用 4/3/2/1（明确终态 / 事件位置 / 活动迹象 / 仅进程）；`check` 填 `CHECKS` 里已注册的健康检查器名，需要新格式时加一个 `_chk_xxx(path) -> (ok, detail)` 并注册。
- `discover` 会逐个健康检查，取精度最高的可用者，并把降级链写进报告与配置。
- `adapter_type` 必须同时有已注册的适配器（见上文），否则会被标为「数据源可发现、适配器未实现」而不启用。`adapter_available = False` 可做"先发现、后实现"的占位探针（Claude Code 就是这样）。
- 版本号优先读 `version_files()` 里的 `version` 字段，读不到就用 `exe_files()` 的文件指纹，因此应用升级一定能被 diff 检出。

### 内置示例适配器 `mock_file`

数据源 `data/agents_state.json`（`tasks` 数组），支持三种 `status_source`：

| 值 | 行为 |
| --- | --- |
| `field` | 直接读 JSON 的 `status` 字段，手工改文件即可模拟任务终结 |
| `mtime` | 任务日志文件 mtime 静默超过 `idle_seconds` 即判为 `idle_implies_status`（默认 `completed`）——全自动演示用 |
| `both` | 字段为终态则终态，否则再看 mtime |

`agent_is_idle()` 会扫描 `log_dir` 中名字含该 Agent 的所有日志 mtime，能捕获"重试生成新 ID 后写了新日志文件"的情况。

---

## 12. 验收对照

| 验收项 | 实现位置 | 验证方式 |
| --- | --- | --- |
| `scan` 输出任务 ID / Agent / 开始时间 / 状态，生成快照版本号 | `cli.cmd_scan` + `SnapshotStore.create_from_scan` | `python agent_sleep_guard.py scan` |
| 人工修改后生成新版本号 + 记录变更日志 | `SnapshotStore.revise` | `scan revise` → `SNAPSHOT_REVISE` 事件 + 快照 `change_log` |
| 记录"已核对"标记与重试风险决定 | `SnapshotStore.verify` | `scan show` 的 `verified/locked/retry_risk`；`RETRY_RISK_DECISION` 事件 |
| `monitor` 绑定指定版本、只跟踪快照内任务、清单外不处理 | `Monitor._poll_cycle` 的 `BOUND_SNAPSHOT` / `IGNORED_NEW_TASKS` | 监控日志 |
| 重复启动被锁拦截 | `LockFile.acquire` | 同时开两个 `monitor`，第二个退出码 2 |
| 周期性进度日志 + 每小时心跳 | `PROGRESS` / `HEARTBEAT` | 监控日志；`selfcheck` 心跳连续性 |
| 全部终结且连续 3 次稳定后按序执行，日志先写完关闭 | `Monitor._confirm_stable` + `_shutdown_sequence` | `logs/sleep-sequence-*.log` 的 STEP1–STEP5 |
| dry-run 不真睡但走完日志与状态更新 | `sleepctl` + `Monitor._shutdown_sequence` | `DRY_RUN_SLEEP_SKIPPED` + `completed_pre_sleep` |
| `selfcheck` 输出各项结果，含"效果: 系统是否进入低功耗"识别 | `selfcheck.run` | `python agent_sleep_guard.py selfcheck` |
| 睡眠命令不使用 `rundll32` 写法 | `config.json` `sleep.command` | 配置为 WinForms `SetSuspendState('Suspend', …)` |
| README 覆盖安装 / 用法 / 适配器扩展 / 睡眠效果口径 / 风险取舍 | 本文 | — |

---

## 13. 故障排查

| 现象 | 先看哪里 |
| --- | --- |
| 早上发现机器没睡 | `logs/monitor-*.log` 的 `PROGRESS`（还有几个任务没终结、各自最后活跃时间）→ `HEARTBEAT` 是否连续 → `logs/sleep-sequence-*.log` 有没有 STEP1–STEP5 |
| 怀疑监控中途死了 | `selfcheck` 的"监控存活心跳连续性" + `state/monitor.lock` 是否 `running` 但 PID 已死 |
| 怀疑睡了但没睡成 | `selfcheck` 的"效果: 系统是否进入低功耗"项 + `powercfg /requests` + System 事件日志 |
| "拒绝启动：快照未经人工核对" | 先 `scan verify --snapshot <ID> --retry-reuses-id … --decision …` |
| "已有监控实例持有锁" | 确认真有实例在跑；若 PID 已死，下次启动会自动归档陈旧锁，或用 `selfcheck --clear-stale-lock` |
| "快照已由人工核对并锁定，不可修订" | 重新 `scan` 生成新 family 的 v1，再核对 |
| 想立刻停掉监控且**不睡** | 在监控窗口按 `Ctrl+C`：会写 `MONITOR_ABORTED`、释放锁，**绝不发出睡眠命令** |

> 安全提示：`config.json` 默认 `dry_run=true`（只约束命令行）。界面真睡只需在「开启监控」时确认一次，由后端一次性令牌校验；确认适配器与快照核对流程无误后再开启。实际进入 sleep 或 hibernate 都算成功。

---

## 14. 可下发产品包

核心链路稳定后（2026-11-09 连续 3 次真实值守成功），交付物从"功能"转为"可评审、可交付、可迭代"：

| 交付物 | 位置 | 用途 |
| --- | --- | --- |
| 值守 SOP + 入口收敛 | 第 7.7 节 | 日常唯一主路径；验收脚手架降级为可选自测；一次确认替代持久总闸 |
| 隐私与数据边界 | 界面「隐私与数据」 + `asg/privacy.py` | 合规评审、数据清单、归档清空 |
| 异常与恢复验收用例集 | `cases/acceptance-cases.json`（59 条） | 下发给开发/测试逐条验收 |
| 产品指标口径与周报 | `asg/metrics.py` + `metrics` CLI | 迭代依据 |
| 托盘常驻 + 一键安装 | `tray.py` / `install.py` / 三个 bat（3.13、3.14） | 「记得来用」变成「一直在」；开机自启可选；分发边界见 3.14 |

### 14.1 入口收敛与一次确认生命周期

- 主路径：**启动界面 → 重新扫描 → 核对锁定 → 开启监控（确认一次）→ 去睡 → 早上 selfcheck**（第 7.7 节）。
- `config.acceptance.json` / `run_acceptance.py` / `开始真机验收.bat` 保留为**脚本自测**，日常值守不需要运行；脚本自身要求输入大写 `SLEEP` 作为一次确认。
- 真睡授权 = `/api/monitor/start` 的一次性确认令牌：进程内存、TTL 10 分钟、用后即焚、不落盘。校验通过写 `MONITOR_CONFIRMED` + `ui-monitor.json.confirmed_at`；值守结束无需关任何开关，天然回到安全态。
- 高级/CLI 路径仍可 `monitor --dry-run` 演练，不需要令牌；`scan verify --implicit` 采用界面默认重试风险留痕。

### 14.2 隐私与数据边界

**读取的本机数据源**（只读结构字段，绝不解析正文）：

| 应用 | 路径 | 读取 | 绝不读 |
| --- | --- | --- | --- |
| WorkBuddy | `~/.workbuddy/workbuddy.db`、`projects/**/*.jsonl`、`tasks/<sid>/*.json`、`sessions/<pid>.json` | ID / status / 时间戳 / `lastHeartbeat` / 事件 type、role | 标题、消息正文、`content`、`subject`、`description`、工具输入输出、AI 推理内容 |
| AutoClaw2 | `%APPDATA%/AutoClaw-official/logs/zwork-main-*.log` | `event` 名、`timestamp`、脱敏 ID | 日志正文、用户输入、URL/响应内容 |
| Trae CN | `%APPDATA%/Trae CN/ModularData/ai-agent/command-execution/*/execution.json` | `executionId` / `startedAtMs` / `finishedAtMs` / `status` | `output` / `stderr` 内容 |
| Windows | `powercfg /a`、`/requests`、无偏中断时间 | 可用待机状态、电源请求、挂起时长 | 任何用户文件内容 |

**落盘**：`snapshots/`、`logs/`、`state/`、`rules/agent-rules.json`、`acceptance/`，全部本地；控制台只监听 `127.0.0.1`，不联网、不上传、不接第三方 SDK。

**合规口径**：本工具不是医疗器械，不做诊断/治疗建议/健康状态判断；不调用生成式 AI 模型，生成式 AI 服务备案不适用；文案只描述任务监控与电源管理，不作疗效或健康改善宣称。

**归档清空**：界面「归档清空生成物」输入 `PURGE` 后，把生成物移动到 `archive-purge-<时间戳>/`（**不删除，可找回**），并写 `purge-note.json`。规则表 `rules/agent-rules.json`、`config*.json`、`data/`、`asg/`、`ui/` 与入口脚本均保留；有监控运行时拒绝执行。

### 14.3 异常与恢复验收用例集

`cases/acceptance-cases.json` 共 **59 条**，分 5 组：快照与人工核对 12、监控与终态 11、睡眠触发与效果 13、规则表与数据源 9、自检与恢复 14。每条含 `id / group / title / precondition / steps / expected / evidence / mode / command / risk`；`mode=auto` 的可按 `evidence` 里的日志关键字做自动回归。

```bash
python agent_sleep_guard.py cases --json          # 导出评审用 JSON
python agent_sleep_guard.py cases                 # 可读版
```

覆盖：未核对拒绝启动、已锁定不可修订、空清单、重试风险（界面默认 / CLI 显式）、版本一致性、无超时阈值、状态 unknown、清单外任务、瞬态抖动、用户回来、心跳断档、单例锁、优雅停止、STEP1–5 顺序、dry-run、只发一条不补发、命令诊断留痕、系统挂起效果、一次确认令牌（缺失/复用/过期）、主源失效降级、进程未运行跳过、已终态子任务过滤、前缀忽略与撤销、日志轮转与 UTC 转本地、版本升级复查、陈旧锁、系统重启、归档清空等。

### 14.4 产品指标口径与周报

```bash
python agent_sleep_guard.py metrics --days 7
python agent_sleep_guard.py metrics --json
```

从已有事件/监控/顺序/诊断/自检日志**离线聚合**，不改造运行链路、不新增埋点 SDK。只统计 `logs/`（正式值守），`acceptance/` 为脚本自测不计入。

| 指标 | 口径 | 方向 |
| --- | --- | --- |
| 值守启动 / 真实值守次数 | `MONITOR_START` 条数 / 其中 `dry_run=False` | 越高越好 |
| 非演练睡眠触发 / 确认挂起 | `STEP5 dry_run=False` / 自检「系统确实挂起过」 | 越高越好 |
| 值守成功率 | 确认挂起 ÷ 非演练睡眠触发 | 越高越好 |
| 静默失败 / 命令被系统拒绝 | 自检 FAIL 条数 / `setSuspendStateReturn=False` | 越低越好 |
| 差点误睡被拦住 | `JITTER_REGRESSION` / `STABILITY_RESET` 条数 | 上下文指标 |
| 平均等待时长 | `MONITOR_START → PRE_SLEEP` 均值（分钟） | 上下文指标 |
| 单次监控内心跳最大间隔 | 同一次监控内相邻心跳的最大间隔（分钟），跨次空档不计 | 越低越好 |
| 平均快照任务数 / 规则人工确认次数 | 快照 `task_count` 均值 / `rules` history 条数 | 上下文指标 |

**明确标注"暂不可自动计算"**：误睡率（缺"用户输入事件 + 命令发出时间"的交叉证据，当前用"差点误睡被拦住次数"替代）；留存/转化（单机工具无账号体系，不适用）。不允许把拍脑袋的数字写进周报。
