<p align="center">
  <img src="../assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://oosmetrics.com/repo/Arcadia-1/virtuoso-bridge-lite"><img src="https://api.oosmetrics.com/api/v1/badge/achievement/8d369c0f-7036-4e79-9ed3-a71689ba4660.svg" alt="oosmetrics — Top 5 in Fullstack by acceleration (2026-05-09)"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/stargazers"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542&logo=github&v=20260523" alt="GitHub stars"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/network/members"><img src="https://img.shields.io/github/forks/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542" alt="GitHub forks"/></a>
  <a href="../stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fclones-badge.json&style=flat-square&v=2" alt="Clone 数"/></a>
  <a href="../stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fviews-badge.json&style=flat-square&v=2" alt="访问量"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/issues"><img src="https://img.shields.io/github/issues/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="开放 Issue"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/commits/main"><img src="https://img.shields.io/github/last-commit/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="最近提交"/></a>
  <a href="https://virtuoso-bridge.tokenzhang.com"><img src="https://img.shields.io/badge/docs-website-blue" alt="网站"/></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="许可证：MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="欢迎提交 PR"/></a>
</p>

面向**智能体模拟与混合信号电路设计**的新型基础设施。LLM 智能体可以驱动本地或远程的 Cadence Virtuoso 实例，将繁琐的手工操作转化为自动化设计流程。

### 为什么称其为“新型基础设施”？

**1. 深度 Virtuoso 集成** — 覆盖原理图、版图、Maestro 和 Spectre 的控制能力。
- **灵活编程**：执行内联 SKILL、加载 `.il` 文件，或使用 Python API
- **四个设计领域**：原理图编辑、版图生成、仿真设置（Maestro）以及带 PSF 解析的独立 Spectre

**2. 可扩展架构** — 面向分布式设计集群，支持多服务器、多会话。
- 多配置 SSH：连接 N 台设计服务器，每台都有独立隧道
- 跨服务器和账户运行并行仿真
- 已在 macOS、Windows 和 Linux 上验证

**3. AI 原生设计** — 专为通过编码智能体（Claude Code、Cursor 等）驱动 Virtuoso 而构建。
- CLI 优先：`virtuoso-bridge start/status/restart`，无需图形操作界面
- 提供预定义的智能体 skill 文件（`skills/`），智能体可以立即了解如何使用桥接器
- 通过持久化 SSH 隧道优化高频智能体交互

> **如果你是 AI 智能体**，请先阅读 [`AGENTS.md`](../AGENTS.md)，并遵循其中的设置检查清单。

## 选择你的使用方式

| 你的目标 | 使用路径 | 所需条件 |
|---|---|---|
| 驱动远程 EDA 服务器上的 Virtuoso | 远程模式 | SSH 访问、正在运行的 Virtuoso、在 CIW 中执行 `load(...)` |
| 驱动同一台机器上的 Virtuoso | 本地模式 | 正在运行的 Virtuoso、`VB_REMOTE_HOST=localhost` |
| 从网表运行 Spectre | Spectre 仿真器 | `spectre` 位于 PATH，或设置 `VB_CADENCE_CSHRC` |
| 运行可复现的 IC 优化流程 | Optimizer skill + 可选的外部工作流 CLI | Spectre/OCEAN 设置、需求文件 |
| 让编码智能体操作 Cadence | 智能体 skills | 将 `skills/` 链接到智能体的 skill 目录 |

Virtuoso SKILL 执行与 Spectre 仿真彼此独立。你可以在不使用 SKILL 桥接器的情况下运行 Spectre，也可以在不使用 Spectre 的情况下使用 SKILL 桥接器。

### Python 环境选择

Python 入口会查找最近的父级 `.env`（其中包含 `VB_REMOTE_HOST` 或 `VB_LOCAL_PORT`），然后以 `override=True` 加载它；这能够将长生命周期进程从本地模式切换到远程模式。在嵌入桥接器并构造客户端之前，请固定要使用的文件：

```python
from virtuoso_bridge.env import set_runtime_env_file

set_runtime_env_file("/path/to/virtuoso-bridge.env")
```

## 快速开始

```bash
# 0. 获取源代码
git clone https://github.com/Arcadia-1/virtuoso-bridge-lite.git
cd virtuoso-bridge-lite

# 1. 在虚拟环境中安装
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# 2. 创建 ~/.virtuoso-bridge/.env
virtuoso-bridge init user@host [-J user@jump-host]
# 或：virtuoso-bridge init      # 空模板；自行编辑 VB_REMOTE_HOST

# 3. 启动并验证
virtuoso-bridge start          # 启动隧道并打印 CIW 的 load(...) 行
virtuoso-bridge status         # 检查隧道、Virtuoso 守护进程和 Spectre 是否可用
```

如果使用 Windows PowerShell，请将激活命令替换为
`\.venv\Scripts\Activate.ps1`。

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
client.execute_skill("1+2")  # VirtuosoResult(status=SUCCESS, output='3')
```

桥接器启动后可使用的实用命令：

```bash
virtuoso-bridge windows       # 列出所有打开的 Virtuoso 窗口
virtuoso-bridge screenshot    # 将 CIW 截图保存到用户产物目录
virtuoso-bridge export-visio MyLib MyCell -o MyCell.vsdx  # Windows + Visio
```

……或者完全跳过 Python，直接从 shell 运行 SKILL：

```bash
# 单行命令 — 在 stdout 输出完整的 VirtuosoResult JSON
virtuoso-bridge eval 'getCurrentTime()'

# 多行 SKILL，通过 heredoc 传入（自动包装在 progn 中；返回最后一个表达式）
virtuoso-bridge eval --stdin <<'EOF'
let((libs)
  libs = mapcar(lambda((l) l~>name) ddGetLibList())
  printf("found %d libraries\n" length(libs))
  libs)
EOF

# 完整的 .il 文件 — 在 SSH 模式下自动上传
virtuoso-bridge load my_script.il
```

详细设置（跳板机、多配置、本地模式）请参阅 [`AGENTS.md`](../AGENTS.md)。

## CLI 参考

所有命令都接受 `-p PROFILE` / `--env PATH`，用于选择非默认配置；运行 `virtuoso-bridge <cmd> --help` 查看完整选项。

| 命令                                                                | 作用                                                                           |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **隧道 / 生命周期**                                                     |                                                                              |
| `init [user@host] [-J jump]`                                      | 写入起始 `.env`（不带参数时生成空模板）                                                      |
| `start [--bind-venv]`                                             | 启动 SSH 隧道并部署守护进程；`--bind-venv`（与 `-p X` 一起使用时）还会将当前虚拟环境绑定到配置 `X`             |
| `stop`                                                            | 停止 SSH 隧道                                                                    |
| `restart`                                                         | 重启隧道并刷新已部署的守护进程设置                                                            |
| `status`                                                          | 检查隧道、守护进程和 Spectre 的状态                                                       |
| `license`                                                         | 检查 Spectre 许可证是否可用                                                           |
| **配置绑定**                                                          |                                                                              |
| `profile show`                                                    | 打印解析后的配置、其来源以及当前虚拟环境绑定路径                                                     |
| `profile bind PROFILE --venv`                                     | 将当前虚拟环境固定到 `PROFILE`（该虚拟环境中未显式指定配置的 `from_env()` 调用会解析到它）                    |
| `profile clear --venv`                                            | 移除当前虚拟环境的配置绑定                                                                |
| **SKILL 执行**                                                      |                                                                              |
| `load FILE.il`                                                    | 在 Virtuoso 中运行 `.il` 文件（SSH 模式下会上传文件）。适合 VS Code 任务；输出 `VirtuosoResult` JSON |
| `eval 'EXPR'` / `eval --stdin`                                    | 运行内联 SKILL 表达式；支持多语句，并自动包装在 `progn(...)` 中                                   |
| **交互 / 诊断**                                                       |                                                                              |
| `windows`                                                         | 列出所有 Virtuoso 窗口（编号和名称）                                                      |
| `screenshot [ciw\|current\|N] [-o DIR\|FILE]`                     | 截取窗口；默认保存到用户产物截图目录                                                           |
| `dismiss-dialog`                                                  | X11 路径：查找并关闭阻塞性的 GUI 对话框（在 SKILL 通道死锁时很有用）                                   |
| `list-windows [--json]`                                           | X11 路径：枚举 Virtuoso 相关窗口，包括框架/子窗口 ID 和建议的模态操作                                 |
| `dismiss-window WINDOW_ID [--action enter\|escape\|alt-y\|alt-n]` | 对 `list-windows` 返回的窗口 ID 发送指定操作                                             |
| `snapshot [-o DIR] [--history H]`                                 | 转储当前聚焦的 Virtuoso 窗口（maestro/schematic/...）；默认简要转储，完整转储到磁盘                    |
| **导出**                                                            |                                                                              |
| `export-visio LIB CELL -o OUT.vsdx`                               | 将 Virtuoso 原理图渲染为 Microsoft Visio 文件（Windows + pywin32）                      |
| **SKILL 查找器**                                                     |                                                                              |
| `skill-find <query>`                                              | 搜索 SKILL 函数                                                                  |
| `skill-info <fn>`                                                 | 获取 SKILL 函数的详细 `More Info` 文档                                                |
| `doc-search <query>`                                              | 通过活动桥接器搜索已安装的 Cadence 文档，或使用 `--doc-root` 进行本地/离线搜索                          |

## 导出 Maestro 运行快照

将当前聚焦的 Maestro 会话的设置和最近一次运行的产物拉取到本地文件夹：

```bash
virtuoso-bridge snapshot -o output                       # 自动选择最新历史记录
virtuoso-bridge snapshot -o output --history Interactive.160   # 固定某个历史记录
```

输出目录树（示例）：

```
output/20260422_142137__MyLib__myTB/
├── maestro.sdb, active.state                    # 原始 Cadence 文件
├── state_from_sdb.xml, state_from_active_state.xml  # 过滤后的高信号 XML
├── state_from_skill.txt                         # SKILL 探测设置摘要
└── Interactive.N/
    ├── Interactive.N.{log,rdb,msg.db}           # 运行级文件（rdb = SQLite）
    └── <pt>/<tb>/
        ├── netlist/   → netlist, input.scs, qpInformation.ils, paramInfo.ils
        └── psf/       → spectre.out, logFile, dcOp.dc, *.ac, *.tran, ...
```

每个点的 `netlist/` 只保留实际描述设计的 4 个文件（主 SPICE 网表、测试平台顶层、FOM 定义和角标）。Psf 保留标准输出、日志及非二进制分析结果。完整规则（包括注释掉的内容及其原因）位于 [`src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml`](../src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml)；编辑 YAML（取消注释或注释行）即可增删文件，无需修改代码。二进制波形（`*.raw`、`wavedb/`）不会被拉取；请改用 `client.maestro.read_results()` 读取标量结果。

## 向编码智能体公开 skills

`skills/` 目录提供 [Claude Code](https://claude.com/claude-code) skills
（`virtuoso`、`spectre`、`netlist`、`optimizer`）。这些目录**有意不**链接到仓库的
`.claude/skills/` 中——因为仓库跟踪的符号链接在 Windows 上会失效，并且会硬编码用户的绝对路径。
相反，每个用户克隆后只需将它们链接到自己的 `~/.claude/skills/` 一次：

```bash
# macOS / Linux
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/virtuoso"  ~/.claude/skills/virtuoso
ln -s "$(pwd)/skills/spectre"   ~/.claude/skills/spectre
ln -s "$(pwd)/skills/netlist"   ~/.claude/skills/netlist
ln -s "$(pwd)/skills/optimizer" ~/.claude/skills/optimizer
```

```powershell
# Windows（PowerShell、开发者模式或提升权限的 shell）
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\virtuoso"  -Target "$PWD\skills\virtuoso"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\spectre"   -Target "$PWD\skills\spectre"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\netlist"   -Target "$PWD\skills\netlist"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\optimizer" -Target "$PWD\skills\optimizer"
```

Cursor 以及其他从用户级目录加载 skills 的智能体也遵循相同模式——将它们的 skills 路径指向本仓库中的 `skills/`。

## 架构

<p align="center">
  <img src="../assets/arch.png" alt="架构" width="100%"/>
</p>

- **Virtuoso Client** — 纯 TCP SKILL 客户端。以 JSON 发送 SKILL，并接收结果。不关心 SSH。
- **Spectre Simulator** — 在本地或通过 SSH 运行独立 Spectre，然后将 PSF ASCII 结果解析为 Python 数据。
- **SSH Client** — 为 TCP 端口转发、远程 shell 命令和文件传输维护持久化 ControlMaster 连接。在本地模式下可选且会被绕过。

各组件完全解耦：Virtuoso Client 可使用任意 TCP 端点——SSH 隧道、VPN、直接局域网连接或本地连接。支持多连接配置，每个配置都管理到独立设计服务器的独立隧道。

> 想了解底层机制？请从 [`src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il`](../src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il) 和 [`src/virtuoso_bridge/virtuoso/basic/bridge.py`](../src/virtuoso_bridge/virtuoso/basic/bridge.py) 开始。

> 想在本地使用 Virtuoso，不使用 SSH ？请参阅 AGENTS.md 中的[本地模式](../AGENTS.md#local-mode)。

## 与 skillbridge 的比较

| 特性 | virtuoso-bridge-lite | [skillbridge](https://github.com/unihd-cag/skillbridge) |
|---|---|---|
| **核心机制** | `ipcBeginProcess` + `evalstring` | `ipcBeginProcess` + `evalstring` |
| **本地模式** | 支持 | 支持 |
| **远程执行** | SSH 隧道、跳板机、自动重连 | 不支持 |
| **调用方式** | 基于字符串：`execute_skill("dbOpenCellViewByType(...)")` | Python 式映射：`ws.db.open_cell_view_by_type(...)` |
| **加载 .il 文件** | `client.load_il()` | 不支持 |
| **版图 / 原理图 API** | `client.layout.create()` / `modify()` 上下文管理器 | 仅支持原始 SKILL |
| **Spectre 仿真** | 内置运行器 + PSF 解析器 | 不支持 |
| **AI 智能体支持** | Skill 文件、CLI 优先、命令日志 | 并非为智能体设计 |
| **Python ↔ SKILL 类型** | 基于字符串 | 自动双向映射 |
| **IDE 代码补全** | 无（智能体不需要） | 有（Jupyter、PyCharm 存根） |

**简而言之：**两个项目都建立在相同的 Cadence SKILL IPC 机制上，使用相同的核心机制：`ipcBeginProcess` + `evalstring` + `ipcWriteProcess`。以下是两者的核心代码：

<details>
<summary><b>virtuoso-bridge-lite</b> — <code>src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il</code></summary>

```skill
RBIpc = ipcBeginProcess(
  sprintf(nil "%s %L %L %L" RBPython RBDPath host RBPort)
  "" 'RBIpcDataHandler 'RBIpcErrHandler 'RBIpcFinishHandler "")

procedure(RBIpcDataHandler(ipcId data)
  if(errset(result = evalstring(data)) then
    ipcWriteProcess(ipcId sprintf(nil "%c%L%c" 2 result 30))
  else
    ipcWriteProcess(ipcId sprintf(nil "%c%L%c" 21 errset.errset 30))
  )
)
```
</details>

<details>
<summary><b>skillbridge</b> — <code>skillbridge/server/python_server.il</code></summary>

```skill
pyStartServer.ipc = ipcBeginProcess(
  executableWithArgs "" '__pyOnData '__pyOnError '__pyOnFinish pyStartServer.logName)

defun(__pyOnData (id data)
  foreach(line parseString(data "\n")
    capturedWarning = __pyCaptureWarnings(errset(result=evalstring(line)))
    ipcWriteProcess(id lsprintf("success %L\n" result))
  )
)
```
</details>

两者的差异在于其上层构建：skillbridge 保持轻量，是用于交互式本地使用的 Python 式 RPC 客户端；virtuoso-bridge-lite 则增加了 SSH 远程访问、高层次的版图/原理图 API、Spectre 仿真以及面向 AI 智能体的工具框架。

## 引用

如果你在学术工作中使用 virtuoso-bridge，请引用：

```bibtex
@article{zhang2025virtuosobridge,
  title   = {Virtuoso-Bridge: An Agent-Native Bridge for Remote Analog and Mixed-Signal Design Automation},
  author  = {Zhang, Zhishuai and Li, Xintian and Sun, Nan and Jie, Lu},
  year    = {2025}
}
```

## 作者

- **Zhishuai Zhang** — 清华大学
- **Xintian Li** — 清华大学
- **Nan Sun** — 清华大学
- **Lu Jie** — 清华大学

## Star 历史

<a href="https://star-history.com/#Arcadia-1/virtuoso-bridge-lite&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date&theme=dark"/>
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date"/>
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date"/>
  </picture>
</a>
