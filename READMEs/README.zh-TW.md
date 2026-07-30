<p align="center">
  <img src="../assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://oosmetrics.com/repo/Arcadia-1/virtuoso-bridge-lite"><img src="https://api.oosmetrics.com/api/v1/badge/achievement/8d369c0f-7036-4e79-9ed3-a71689ba4660.svg" alt="oosmetrics — Top 5 in Fullstack by acceleration (2026-05-09)"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/stargazers"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542&logo=github&v=20260523" alt="GitHub stars"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/network/members"><img src="https://img.shields.io/github/forks/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542" alt="GitHub forks"/></a>
  <a href="../stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fclones-badge.json&style=flat-square&v=2" alt="複製數"/></a>
  <a href="../stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fviews-badge.json&style=flat-square&v=2" alt="瀏覽量"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/issues"><img src="https://img.shields.io/github/issues/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="開放 Issue"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/commits/main"><img src="https://img.shields.io/github/last-commit/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="最近提交"/></a>
  <a href="https://virtuoso-bridge.tokenzhang.com"><img src="https://img.shields.io/badge/docs-website-blue" alt="網站"/></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="授權條款：MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="歡迎提交 PR"/></a>
</p>

面向**代理式類比與混合訊號電路設計**的新型基礎設施。LLM 代理可以驅動本機或遠端的 Cadence Virtuoso 執行個體，將繁瑣的手動操作轉化為自動化設計流程。

### 為什麼稱其為「新型基礎設施」？

**1. 深度 Virtuoso 整合** — 涵蓋原理圖、版圖、Maestro 和 Spectre 的控制能力。
- **彈性程式設計**：執行內嵌 SKILL、載入 `.il` 檔案，或使用 Python API
- **四個設計領域**：原理圖編輯、版圖產生、模擬設定（Maestro）以及具備 PSF 剖析功能的獨立 Spectre

**2. 可擴充架構** — 面向分散式設計叢集，支援多伺服器、多工作階段。
- 多設定 SSH：連線至 N 台設計伺服器，每台都有獨立通道
- 跨伺服器與帳戶執行平行模擬
- 已在 macOS、Windows 和 Linux 上驗證

**3. AI 原生設計** — 專為透過編碼代理（Claude Code、Cursor 等）驅動 Virtuoso 而建構。
- CLI 優先：`virtuoso-bridge start/status/restart`，不需要 GUI
- 提供預先定義的代理 skill 檔案（`skills/`），代理可以立即了解如何使用橋接器
- 透過持久化 SSH 通道最佳化高頻率代理互動

> **如果你是 AI 代理**，請先閱讀 [`AGENTS.md`](../AGENTS.md)，並遵循其中的設定檢查清單。

## 選擇你的使用方式

| 你的目標 | 使用路徑 | 所需條件 |
|---|---|---|
| 驅動遠端 EDA 伺服器上的 Virtuoso | 遠端模式 | SSH 存取、正在執行的 Virtuoso、在 CIW 中執行 `load(...)` |
| 驅動同一台機器上的 Virtuoso | 本機模式 | 正在執行的 Virtuoso、`VB_REMOTE_HOST=localhost` |
| 從網表執行 Spectre | Spectre 模擬器 | `spectre` 位於 PATH，或設定 `VB_CADENCE_CSHRC` |
| 執行可重現的 IC 最佳化流程 | Optimizer skill + 可選的外部工作流程 CLI | Spectre/OCEAN 設定、需求檔案 |
| 讓編碼代理操作 Cadence | 代理 skills | 將 `skills/` 連結到代理的 skill 目錄 |

Virtuoso SKILL 執行與 Spectre 模擬彼此獨立。你可以在不使用 SKILL 橋接器的情況下執行 Spectre，也可以在不使用 Spectre 的情況下使用 SKILL 橋接器。

### Python 環境選擇

Python 入口會尋找最近的父層 `.env`（其中包含 `VB_REMOTE_HOST` 或 `VB_LOCAL_PORT`），然後以 `override=True` 載入它；這可能會讓長生命週期程序從本機模式切換到遠端模式。在嵌入橋接器並建立用戶端之前，請固定要使用的檔案：

```python
from virtuoso_bridge.env import set_runtime_env_file

set_runtime_env_file("/path/to/virtuoso-bridge.env")
```

## 快速開始

```bash
# 0. 取得原始碼
git clone https://github.com/Arcadia-1/virtuoso-bridge-lite.git
cd virtuoso-bridge-lite

# 1. 在虛擬環境中安裝
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# 2. 建立 ~/.virtuoso-bridge/.env
virtuoso-bridge init user@host [-J user@jump-host]
# 或：virtuoso-bridge init      # 空白範本；自行編輯 VB_REMOTE_HOST

# 3. 啟動並驗證
virtuoso-bridge start          # 啟動通道並列印 CIW 的 load(...) 行
virtuoso-bridge status         # 檢查通道、Virtuoso 守護程式和 Spectre 是否可用
```

如果使用 Windows PowerShell，請將啟用命令替換為
`\.venv\Scripts\Activate.ps1`。

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
client.execute_skill("1+2")  # VirtuosoResult(status=SUCCESS, output='3')
```

橋接器啟動後可使用的實用命令：

```bash
virtuoso-bridge windows       # 列出所有開啟的 Virtuoso 視窗
virtuoso-bridge screenshot    # 將 CIW 螢幕擷取儲存到使用者產物目錄
virtuoso-bridge export-visio MyLib MyCell -o MyCell.vsdx  # Windows + Visio
```

……或者完全跳過 Python，直接從 shell 執行 SKILL：

```bash
# 單行命令 — 在 stdout 輸出完整的 VirtuosoResult JSON
virtuoso-bridge eval 'getCurrentTime()'

# 多行 SKILL，透過 heredoc 傳入（自動包裝在 progn 中；回傳最後一個表達式）
virtuoso-bridge eval --stdin <<'EOF'
let((libs)
  libs = mapcar(lambda((l) l~>name) ddGetLibList())
  printf("found %d libraries\n" length(libs))
  libs)
EOF

# 完整的 .il 檔案 — 在 SSH 模式下自動上傳
virtuoso-bridge load my_script.il
```

詳細設定（跳板機、多設定、本機模式）請參閱 [`AGENTS.md`](../AGENTS.md)。

## CLI 參考

所有命令都接受 `-p PROFILE` / `--env PATH`，用於選擇非預設設定；執行 `virtuoso-bridge <cmd> --help` 查看完整選項。

| 命令                                                                | 作用                                                                           |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **通道 / 生命週期**                                                     |                                                                              |
| `init [user@host] [-J jump]`                                      | 寫入起始 `.env`（不帶參數時產生空白範本）                                                     |
| `start [--bind-venv]`                                             | 啟動 SSH 通道並部署守護程式；`--bind-venv`（與 `-p X` 一起使用時）還會將目前虛擬環境繫結到設定 `X`             |
| `stop`                                                            | 停止 SSH 通道                                                                    |
| `restart`                                                         | 重新啟動通道並重新整理已部署的守護程式設定                                                        |
| `status`                                                          | 檢查通道、守護程式和 Spectre 的狀態                                                       |
| `license`                                                         | 檢查 Spectre 授權是否可用                                                            |
| **設定繫結**                                                          |                                                                              |
| `profile show`                                                    | 列印解析後的設定、其來源以及目前虛擬環境繫結路徑                                                     |
| `profile bind PROFILE --venv`                                     | 將目前虛擬環境固定到 `PROFILE`（該虛擬環境中未明確指定設定的 `from_env()` 呼叫會解析到它）                    |
| `profile clear --venv`                                            | 移除目前虛擬環境的設定繫結                                                                |
| **SKILL 執行**                                                      |                                                                              |
| `load FILE.il`                                                    | 在 Virtuoso 中執行 `.il` 檔案（SSH 模式下會上傳檔案）。適合 VS Code 工作；輸出 `VirtuosoResult` JSON |
| `eval 'EXPR'` / `eval --stdin`                                    | 執行內嵌 SKILL 表達式；支援多語句，並自動包裝在 `progn(...)` 中                                   |
| **互動 / 診斷**                                                       |                                                                              |
| `windows`                                                         | 列出所有 Virtuoso 視窗（編號和名稱）                                                      |
| `screenshot [ciw\|current\|N] [-o DIR\|FILE]`                     | 擷取視窗；預設儲存到使用者產物螢幕擷取目錄                                                        |
| `dismiss-dialog`                                                  | X11 路徑：尋找並關閉阻塞性的 GUI 對話方塊（在 SKILL 通道死結時很有用）                                  |
| `list-windows [--json]`                                           | X11 路徑：列舉 Virtuoso 相關視窗，包括框架/子視窗 ID 和建議的模態操作                                 |
| `dismiss-window WINDOW_ID [--action enter\|escape\|alt-y\|alt-n]` | 對 `list-windows` 回傳的視窗 ID 傳送指定操作                                             |
| `snapshot [-o DIR] [--history H]`                                 | 傾印目前聚焦的 Virtuoso 視窗（maestro/schematic/...）；預設簡要傾印，完整傾印到磁碟                    |
| **匯出**                                                            |                                                                              |
| `export-visio LIB CELL -o OUT.vsdx`                               | 將 Virtuoso 原理圖轉譯為 Microsoft Visio 檔案（Windows + pywin32）                      |
| **SKILL 尋找器**                                                     |                                                                              |
| `skill-find <query>`                                              | 搜尋 SKILL 函式                                                                  |
| `skill-info <fn>`                                                 | 取得 SKILL 函式的詳細 More Info 文件                                                  |
| `doc-search <query>`                                              | 透過作用中的橋接器搜尋已安裝的 Cadence 文件，或使用 `--doc-root` 進行本機/離線搜尋                        |

## 匯出 Maestro 執行快照

將目前聚焦的 Maestro 工作階段設定和最近一次執行的產物拉取到本機資料夾：

```bash
virtuoso-bridge snapshot -o output                       # 自動選擇最新歷史記錄
virtuoso-bridge snapshot -o output --history Interactive.160   # 固定某個歷史記錄
```

輸出目錄樹（範例）：

```
output/20260422_142137__MyLib__myTB/
├── maestro.sdb, active.state                    # 原始 Cadence 檔案
├── state_from_sdb.xml, state_from_active_state.xml  # 過濾後的高訊號 XML
├── state_from_skill.txt                         # SKILL 探測設定摘要
└── Interactive.N/
    ├── Interactive.N.{log,rdb,msg.db}           # 執行層級檔案（rdb = SQLite）
    └── <pt>/<tb>/
        ├── netlist/   → netlist, input.scs, qpInformation.ils, paramInfo.ils
        └── psf/       → spectre.out, logFile, dcOp.dc, *.ac, *.tran, ...
```

每個點的 `netlist/` 只保留實際描述設計的 4 個檔案（主 SPICE 網表、測試平台頂層、FOM 定義和角落標籤）。Psf 保留標準輸出、日誌及非二進位分析結果。完整規則（包括註解掉的內容及其原因）位於 [`src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml`](../src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml)；編輯 YAML（取消註解或註解行）即可增刪檔案，無需修改程式碼。二進位波形（`*.raw`、`wavedb/`）不會被拉取；請改用 `client.maestro.read_results()` 讀取純量結果。

## 讓編碼代理使用 skills

`skills/` 目錄提供 [Claude Code](https://claude.com/claude-code) skills
（`virtuoso`、`spectre`、`netlist`、`optimizer`）。這些目錄**有意不**連結到儲存庫的
`.claude/skills/` 中——儲存庫追蹤的符號連結在 Windows 上會失效，並且會硬編碼使用者的絕對路徑。
相反地，每個使用者複製後只需將它們連結到自己的 `~/.claude/skills/` 一次：

```bash
# macOS / Linux
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/virtuoso"  ~/.claude/skills/virtuoso
ln -s "$(pwd)/skills/spectre"   ~/.claude/skills/spectre
ln -s "$(pwd)/skills/netlist"   ~/.claude/skills/netlist
ln -s "$(pwd)/skills/optimizer" ~/.claude/skills/optimizer
```

```powershell
# Windows（PowerShell、開發人員模式或提升權限的 shell）
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\virtuoso"  -Target "$PWD\skills\virtuoso"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\spectre"   -Target "$PWD\skills\spectre"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\netlist"   -Target "$PWD\skills\netlist"
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.claude\skills\optimizer" -Target "$PWD\skills\optimizer"
```

Cursor 以及其他從使用者層級目錄載入 skills 的代理也遵循相同模式——將它們的 skills 路徑指向本儲存庫中的 `skills/`。

## 架構

<p align="center">
  <img src="../assets/arch.png" alt="架構" width="100%"/>
</p>

- **Virtuoso Client** — 純 TCP SKILL 用戶端。以 JSON 傳送 SKILL，並接收結果。不瞭解 SSH。
- **Spectre Simulator** — 在本機或透過 SSH 執行獨立 Spectre，然後將 PSF ASCII 結果剖析為 Python 資料。
- **SSH Client** — 為 TCP 連接埠轉送、遠端 shell 命令和檔案傳輸維護持久化 ControlMaster 連線。在本機模式下可選且會被繞過。

各元件完全解耦：Virtuoso Client 可使用任意 TCP 端點——SSH 通道、VPN、直接區域網路連線或本機連線。支援多連線設定，每個設定都管理到獨立設計伺服器的獨立通道。

> 想了解底層機制？請從 [`src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il`](../src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge.il) 和 [`src/virtuoso_bridge/virtuoso/basic/bridge.py`](../src/virtuoso_bridge/virtuoso/basic/bridge.py) 開始。

> 想在不使用 SSH 的情況下本機使用 Virtuoso？請參閱 AGENTS.md 中的[本機模式](../AGENTS.md#local-mode)。

## 與 skillbridge 的比較

| 特性 | virtuoso-bridge-lite | [skillbridge](https://github.com/unihd-cag/skillbridge) |
|---|---|---|
| **核心機制** | `ipcBeginProcess` + `evalstring` | `ipcBeginProcess` + `evalstring` |
| **本機模式** | 支援 | 支援 |
| **遠端執行** | SSH 通道、跳板機、自動重新連線 | 不支援 |
| **呼叫方式** | 以字串為基礎：`execute_skill("dbOpenCellViewByType(...)")` | Python 式對映：`ws.db.open_cell_view_by_type(...)` |
| **載入 .il 檔案** | `client.load_il()` | 不支援 |
| **版圖 / 原理圖 API** | `client.layout.create()` / `modify()` 內容管理器 | 僅支援原始 SKILL |
| **Spectre 模擬** | 內建執行器 + PSF 剖析器 | 不支援 |
| **AI 代理支援** | Skill 檔案、CLI 優先、命令日誌 | 並非為 AI 代理設計 |
| **Python ↔ SKILL 型別** | 以字串為基礎 | 自動雙向對映 |
| **IDE 程式碼補全** | 無（代理不需要） | 有（Jupyter、PyCharm 存根） |

**簡而言之：**兩個專案都建立在相同的 Cadence SKILL IPC 機制上，使用相同的核心機制：`ipcBeginProcess` + `evalstring` + `ipcWriteProcess`。以下是兩者的核心程式碼：

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

兩者的差異在於其上層建構：skillbridge 保持輕量，是用於互動式本機使用的 Python 式 RPC 用戶端；virtuoso-bridge-lite 則增加了 SSH 遠端存取、高階版圖/原理圖 API、Spectre 模擬以及面向 AI 代理的工具框架。

## 引用

如果你在學術工作中使用 virtuoso-bridge，請引用：

```bibtex
@article{zhang2025virtuosobridge,
  title   = {Virtuoso-Bridge: An Agent-Native Bridge for Remote Analog and Mixed-Signal Design Automation},
  author  = {Zhang, Zhishuai and Li, Xintian and Sun, Nan and Jie, Lu},
  year    = {2025}
}
```

## 作者

- **Zhishuai Zhang** — 清華大學
- **Xintian Li** — 清華大學
- **Nan Sun** — 清華大學
- **Lu Jie** — 清華大學

## Star 歷史

<a href="https://star-history.com/#Arcadia-1/virtuoso-bridge-lite&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date&theme=dark"/>
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date"/>
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Arcadia-1/virtuoso-bridge-lite&type=Date"/>
  </picture>
</a>
