<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb：為 agent 團隊打造的知識編譯器"></a>
</p>

<p align="center">
  <b>agent2kb 把您的 agent 找到的一切，變成一個持續更新、附有出處的 Markdown wiki。</b><br>
  由 LLM 在您自己的 bucket 中編譯；agent 透過 MCP 讀取，
  人則用 Obsidian 或任何 Markdown 編輯器閱讀。
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#試用">試用</a> ·
  <a href="docs/first-run.md">首次執行</a> ·
  <a href="#為什麼選擇-agent2kb">為什麼選擇 agent2kb</a> ·
  <a href="#文件">文件</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="授權條款：AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="MCP 伺服器">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="任何 S3 儲存空間">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="可搭配 Ollama 完全在本機執行">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.ko.md">한국어</a> ·
  <b>繁體中文</b>
</p>

<p align="center"><i>本文為翻譯版本；請以<a href="README.md">英文 README</a> 為準，英文版可能較新。</i></p>
<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**為 agent 團隊打造的 headless 知識編譯器。** agent 與 bot 提交原始資料（網頁、影片、PDF、筆記）；LLM 將其編譯成一個能自我維護、由互相連結的 Markdown 頁面組成的 wiki；agent 透過 CLI 或 MCP 讀取，人則用 Obsidian 或其他任何 Markdown 編輯器閱讀。每個專案都在任何相容 S3 的 bucket 中擁有自己獨立的 wiki，而且不需要任何應用程式保持開啟。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="agent2kb 的運作方式：a2kb ingest 將資料放入 raw/，a2kb compile 透過 LLM 將其轉成
    wiki/ 頁面，agent 透過 CLI 與 MCP 讀取 wiki，人在 Obsidian 中閱讀，
    而 outputs/ 中儲存的回答會回饋到下一次編譯">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **匯入一次，無人值守編譯。** cron 會執行 compile、lint、備份與摘要（digest），當某個 LLM 無法使用時，流程會自動改用另一個 LLM。agent 只需要 `a2kb ingest`。
- **每個專案一個知識庫（KB）。** 每個 KB 有自己的 prefix；在儲存服務供應商允許的情況下，也有自己的認證資訊，因此處理某個 KB 的 agent 無法存取其他 KB。
- **agent 與人共用同一個 wiki。** agent 使用 MCP 伺服器或 CLI；人則在 Obsidian 或任何 Markdown 編輯器中閱讀帶有 `[[wikilinks]]` 的純 Markdown。儲存的回答會回饋到下一次編譯，因此 wiki 會在最常被查詢的領域持續成長。
- **針對不受信任的內容與隱私而設計。** 網頁與檔案下載都會經過 SSRF 防護（由 `yt-dlp` 自行抓取的影片 URL 是文件中註明的例外），`claude` 與 `codex` 在不使用任何工具的情況下執行，新的 KB 預設停用雲端旁路服務，並且支援搭配 Ollama 的完全本機設定。

本專案遵循 Andrej Karpathy 的
[LLM wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，並將其做成伺服器端工具。

### 您的團隊在 wiki 編輯器中看到的樣子

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="示意圖：在 Obsidian 中開啟的一個 agent2kb 編譯頁面，包含 frontmatter 標籤、
    帶有 wikilinks 的文章、相關頁面的關聯圖（graph view）以及反向連結面板">
</p>

每個編譯好的頁面都是純 Markdown，附有屬性（`source`、`date`、標籤、`source_kind`）、摘要、結構化的段落與 `[[wikilinks]]`，因此 Obsidian 的關聯圖、反向連結與標籤搜尋開箱即用，而 `index.md` 開頭會有一段「Past 7 days」回顧，列出 KB 這一週學到的內容。尚未解析的連結會保留為幽靈節點（ghost node），直到之後的編譯或 `a2kb stubs` 將其補上。*示意圖根據 [examples/first-note.md](examples/first-note.md) 中的範例製作；連接您的 vault 請參閱 [docs/obsidian.md](docs/obsidian.md)。*

Obsidian 是 agent2kb 文件中說明並推薦的閱讀工具，因為它能呈現 `[[wikilinks]]`、關聯圖與反向連結，並可透過 Remotely Save 外掛從 S3 同步。這些檔案都是一般的 Markdown，因此任何能讀取它們的編輯器都可以使用；不過 Obsidian 以外的工具需要自行處理從 S3 同步的方式，且尚未經過測試。

### 自動為您撰寫的新內容回顧

每次編譯都會更新 `wiki/index.md` 的開頭，加入過去 7 天的回顧：先是一段由 LLM 根據新頁面撰寫、概述這些頁面共同主題的簡短摘要，接著列出每個頁面的連結。沒有人需要動手撰寫，而且它會跟著 KB 一起更新，因此休假回來的同事，或每週才查看一次的學習者，都能一眼看出 KB 學到了什麼。以下是一個範例（內容為虛構）：

```markdown
## Past 7 Days (4 articles)

> This week the KB focused on safer releases. Two articles compare canary and blue-green
> deployments and agree that automated rollback matters more than the strategy itself; a
> third shows how feature flags keep unfinished work out of a release, and an incident review
> traces an outage to a missing health check, linking it to [[Rollback]] and [[Monitoring]].

- 2026-10-20 [[20261020-Canary-Releases-in-Practice-1a2b3c4d|Canary Releases in Practice]]
- 2026-10-19 [[20261019-Blue-Green-Deployments-5e6f7a8b|Blue-Green Deployments]]
- 2026-10-17 [[20261017-Feature-Flags-for-Safe-Releases-9c0d1e2f|Feature Flags for Safe Releases]]
- 2026-10-15 [[20261015-Incident-Review-Missing-Health-Check-3a4b5c6d|Incident Review: Missing Health Check]]
```

回顧的時間範圍可以設定（`recent_days`：設為 `30` 即為每月回顧，設為 `0` 則關閉），另外也可選用每週摘要，將較長的回顧寫入 `outputs/weekly/`。

### 每台裝置、每個 agent，共用一個 KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="一個位於您 S3 bucket 中的 KB，由使用桌機、iPhone 與 Android 的人透過 Obsidian 共用，
    也由 agent 透過 CLI 與 MCP 共用；編譯則在旁邊依排程執行">
</picture>

KB 存放在您的 S3 bucket 中，而不是某一台電腦上，因此同一個編譯好的 wiki 能送達每一台裝置：macOS、Windows 與 Linux 上的 Obsidian、iPhone 與 iPad 上的 Obsidian、Android 上的 Obsidian（透過 Remotely Save 外掛），以及任何筆電、伺服器或 CI runner 上的 agent（透過 CLI 或 MCP）。編譯在儲存空間旁依排程執行，因此即使所有裝置都關機，頁面也會持續改進。手機需要 HTTPS 端點（AWS S3、Cloudflare R2，或啟用 HTTPS profile 的 Docker 後端）；請參閱 [docs/obsidian.md](docs/obsidian.md)。維護者曾在較早的版本中，以自架的 SeaweedFS 後端搭配多台桌機與行動裝置上的 Obsidian 執行過這套設定；目前版本的操作步驟尚未重新完整驗證過。

## 試用

備妥下列先決條件後，產生第一個頁面只需要幾分鐘；建立雲端帳號、bucket 與金鑰則需要較長時間，相關說明請見[儲存服務供應商](docs/storage-providers.md)。

先決條件：具備 POSIX shell 的 Linux 或 macOS（套件所宣告支援的平台；Windows 尚未測試）、Python 3.10 以上版本及其 `venv` 模組（Debian 與 Ubuntu 上：`sudo apt install python3-venv`），以及 `git`。接著：

- **儲存空間：** 一個相容 S3 的 bucket 及其金鑰（AWS S3 與 Cloudflare R2 的設定步驟：[儲存服務供應商](docs/storage-providers.md)），或已內含儲存空間的 [Docker 後端](#10-分鐘架設-docker-後端)。
- **一個 LLM：** 已安裝並登入的 `claude` 或 `codex` CLI（`npm i -g @anthropic-ai/claude-code` 與 `claude auth login`，或 `npm i -g @openai/codex` 與 `codex login`）；或一組 API 金鑰（Gemini、Groq、OpenRouter）；或一台本機 Ollama 伺服器。

### 安裝

請安裝到虛擬環境中，讓 agent2kb 及其相依套件與系統的 Python 分開（許多 Linux 發行版會以 `externally-managed-environment` 錯誤拒絕全系統的 `pip install`）：

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # 只需一次：在 .venv/ 建立獨立環境
source .venv/bin/activate                   # 每開一個新終端機都要執行（zsh 與 bash）
python -m pip install --upgrade pip
python -m pip install ".[all]"              # 在首次發佈到 PyPI 之前，從原始碼安裝
a2kb --help                                 # 應該會列出指令清單
```

`python -m pip` 會安裝到目前啟用的環境，不論您 `PATH` 上第一個 `pip` 是哪一個。之後開啟新的終端機時，請先在 checkout 目錄中再次執行 `source .venv/bin/activate` 再使用 `a2kb`，或直接呼叫 `.venv/bin/a2kb`（例如從 cron 呼叫）。`deactivate` 可離開該環境。

`[all]` 會安裝所有供應商 SDK、文件與影片匯入功能，以及 MCP 伺服器。若只需處理文字、Markdown 與網頁，並使用已登入的 `claude` 或 `codex` CLI，較小的安裝就足夠：

```bash
python -m pip install .                     # 僅核心（已確認：可安裝，`a2kb --help` 可執行）
python -m pip install ".[mcp]"              # 核心加上供 agent 使用的 MCP 伺服器
```

各項功能在核心安裝之外還需要什麼（`a2kb doctor` 會列出缺少的項目）：

| 功能 | Python extra | 外部程式 |
|---|---|---|
| Markdown、文字、網頁 | 無 | 無 |
| 以 `claude` / `codex` CLI 作為 LLM | 無 | 該 CLI，且已登入 |
| Gemini、Groq、OpenRouter、Ollama | `gemini`、`groq`、`openai`、`ollama` | 使用 Ollama 時需要 Ollama 伺服器 |
| Embeddings 與 rerank（選用） | `voyage`、`openai`、`gemini` 或 `ollama`；`fast` 可加快向量搜尋 | 無 |
| `.docx`、`.xlsx` 文件 | `media` | 無 |
| PDF | `media` | 無 |
| 其他文件格式（例如 `.epub`） | 無 | `pandoc` |
| 影片 URL（YouTube 等） | `media`（yt-dlp） | `ffmpeg` |
| 本機音訊與影片轉錄 | `whisper`（體積龐大；**不**包含在 `all` 中） | `ffmpeg` |
| MCP 伺服器（`a2kb mcp`） | `mcp` | 無 |

extra 可以組合使用：`python -m pip install ".[mcp,media]"`。Python 套件永遠不會安裝 `ffmpeg` 或 `pandoc`；請使用系統的套件管理工具（`apt install ffmpeg pandoc`、`brew install ffmpeg pandoc`）。

### 第一個 KB

```bash
a2kb setup                        # 精靈：儲存空間、LLM、語言、隱私、您的第一個 KB（notes）
a2kb doctor                       # 檢查設定、S3 存取、LLM 登入、外部工具

a2kb ingest --file examples/first-note.md       # repo 內附的小型範例
a2kb compile                      # raw/ -> wiki/（增量編譯；--force 會全部重新編譯）
a2kb query "What is the codename of the alpha project?"
```

[首次執行教學](docs/first-run.md)會帶您走過同樣的步驟，並附上每一步的預期輸出（raw 檔名、wiki 頁面、附出處的回答），也會說明如何連接 Obsidian，並提供常見問題的排解對照表。要匯入您自己的資料，請使用 `a2kb ingest --url <url>`、`--file <path>` 或 `--folder <dir>`。

若您在任何 KB 存在之前執行任何指令，`a2kb` 會提議幫您啟動 `a2kb setup`（僅限在終端機中；腳本、cron 與 agent 會收到一般的錯誤訊息）。

`a2kb doctor` 會將未登入 `claude` 或 `codex` 回報為失敗。在您登入之前這是正常現象；API 金鑰與 Ollama 不需要登入。當您只有一個 KB 時，可以省略 `-p` 選項；有多個 KB 時，請使用 `a2kb -p notes ...`。`a2kb list` 會列出您的 KB，而 `a2kb new myproject --here` 會寫入一個 `.kb/config.yaml` 指標檔，讓 `a2kb` 在專案資料夾中不需要 `-p` 就能運作。套件名稱是 `agent2kb`，它安裝的唯一指令是 `a2kb`（可用 `alias kb=a2kb` 提供較短的寫法）。

## 為什麼選擇 agent2kb

agent 團隊的知識散落在聊天記錄、臨時筆記與瀏覽器分頁中。把這些全部放進向量資料庫，得到的是片段而不是理解；單一共用的 vault 則會混雜各個專案，也沒有任何流程能維持其一致性；而這兩種做法都無法讓依賴這些知識的人加以稽核。

agent2kb 將工作分成三個階段。**收集**刻意保持簡單：任何程序都能執行 `ingest`，把資料放進 `raw/`。**理解**就是編譯步驟：LLM 將資料轉成帶有標題、摘要、標籤與連結的頁面，只改寫有變動的部分，並標記哪些內容是它自行推導出來的，讓原始資料在兩者不一致時優先。**使用**則透過兩種介面存取同一批檔案：agent 使用 `a2kb query` 與 MCP，人使用 Obsidian。所有內容都以 Markdown 儲存在您自己的 bucket 中，因此可以閱讀、比對、備份，也能手動編輯。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="agent2kb 的不同之處：1. 會編譯，而不只是檢索；2. 每個專案一個獨立的 KB；
    3. agent 與人共用一個 wiki；4. 知識會持續累積；5. 使用您自己的 bucket，
    不被綁定；6. 能安全處理不受信任的內容">
</picture>

### 比較

| | 向量資料庫 / RAG | 共用筆記 vault | Agent session 記憶 | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| 可閱讀的頁面，而非片段 | ❌ chunks | ✅ 人工撰寫 | ❌ 對話紀錄 | ✅ **LLM 編譯** |
| 無需人工即可保持一致 | ❌ | ❌ | ❌ | ✅ **增量編譯 + lint** |
| 每個專案各自獨立的知識 | ⚠️ 依 index | ❌ 單一 vault | ⚠️ 依 agent | ✅ **每個 KB 一個 prefix 與金鑰** |
| agent 與人使用同一來源 | ❌ 僅 agent | ⚠️ 僅限人 | ❌ 僅 agent | ✅ **MCP + Markdown 編輯器** |
| 每個回答都附出處 | ⚠️ | ❌ | ❌ | ✅ |
| 存放在您擁有的儲存空間中的純檔案 | ❌ | ✅ | ❌ | ✅ **任何 S3 bucket** |
| 可完全不依賴任何第三方執行 | ⚠️ | ✅ | ❌ | ✅ **Ollama + 本機 S3** |

每個檔案都帶有 YAML frontmatter（`title`、`source`、`date`、`tags`、`source_kind`）。`source_kind` 記錄信任程度：原始資料與經整理的主題屬於主要來源；概念頁面與儲存的回答則屬於衍生內容。請參閱 [docs/compounding-loop.md](docs/compounding-loop.md)。

## 功能

| | |
|---|---|
| **匯入** | URL、YouTube 及其他影片、音訊、PDF/DOCX/XLSX、圖片（OCR）、Markdown、Obsidian Web Clipper 擷取內容；重複偵測、抓取前的安全檢查 |
| **編譯** | 從 raw 到 wiki 的增量編譯，產生 `[[wikilinks]]`、概念頁面與幽靈連結的 stub 頁面；開頭附有「Past 7 days」回顧的索引，以及選用的每週摘要；輸出可為英文或您指定的任何語言 |
| **查詢** | 關鍵字搜尋，可選用 embeddings 與 reranking，提供附出處的回答與搜尋詞擴充；儲存在 `outputs/` 的回答讓知識累積的循環得以閉合 |
| **Agent 存取** | MCP 伺服器（`a2kb mcp`，stdio 或受 bearer token 保護的 HTTP），預設唯讀；`ask` 與 `ingest_url` 需自行啟用 |
| **隔離** | 由用戶端強制執行的 per-KB prefix；在供應商能限定範圍時，提供 per-KB 認證資訊 |
| **LLM** | `claude` / `codex` CLI 登入（在 sandbox 中執行，並有備援 CLI）、Gemini、Groq、OpenRouter、Ollama；大量編譯與輔助（aux）工作可使用不同模型 |
| **維運** | `a2kb doctor`、`lint`、`backup` 與 `restore`、每次編譯的執行摘要、`scripts/` 中的 cron 包裝腳本 |
| **Docker 後端** | 一個 `docker compose up`：具 per-KB 金鑰的 SeaweedFS、cron、每個 KB 一個 MCP 伺服器、選用的 HTTPS |

## 快速開始（本機）

[試用](#試用)涵蓋完整流程，[首次執行教學](docs/first-run.md)則列出每一步的預期結果。補充說明：

- `a2kb setup --non-interactive` 會從旗標與 `KB_*` 變數取得所有設定，適用於 CI 與自動佈建（[旗標](docs/configuration.md#non-interactive-setup)）。
- 儲存空間預設組態：AWS S3、Cloudflare R2、Backblaze B2、MinIO、SeaweedFS、Wasabi、其他（[儲存服務供應商](docs/storage-providers.md)）。
- 編譯、lint、備份與 stubs 依排程執行：請參閱[持續編譯 wiki](#持續編譯-wiki)。
- 所有指令與旗標：[docs/commands.md](docs/commands.md)。

## 持續編譯 wiki

`a2kb ingest` 只會把資料存進 `raw/`；頁面要在 `a2kb compile` 執行後才會出現。您可以手動執行，但常見的設定是**依排程**執行，讓 agent 或人匯入的任何內容都會成為頁面，不需要有人等待：

| 您如何執行 agent2kb | 由誰排程編譯 |
|---|---|
| Docker 後端 | 已為您設定好：每 15 分鐘編譯一次、每天 lint、每天備份兩次、每天產生 stubs（`KB_CRON_DIGEST=1` 會加上每週摘要） |
| 原生安裝（Linux、macOS） | 由您使用 `scripts/` 中的包裝腳本新增 cron 項目 |
| 偶爾使用 | 匯入後自行執行 `a2kb compile` |

原生安裝的最小 crontab（`crontab -e`；請調整路徑）：

```cron
# 每 15 分鐘：編譯新增或變動的資料（沒有變動時不使用 LLM）
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# 每天：結構性 lint，以及為經常被連結但缺少的主題建立預留頁面
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# 每天兩次：KB 的本機備份封存檔
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

這些包裝腳本會把記錄寫入專案資料夾中的 `kb-*.log`，並以 `--strict` 呼叫編譯，因此編譯失敗時會以非零值結束，方便監控。重疊執行是安全的：當某個 KB 的一次編譯在儲存空間中持有其 lease 時，同一 KB 的另一次執行會略過該次排程（在 `--strict` 下以 75 結束），工作則由下一次排程接手。完整範本為 `scripts/kb-crontab.example`；排程細節請見 [docs/project-workflow.md](docs/project-workflow.md#cron)。

**由哪個 LLM 編譯是一項設定，而不是排程的一部分。** cron 只會執行 `a2kb compile`；實際由誰來做，取決於 KB 的 `llm:` 區塊：

| `llm.provider` | 由什麼編譯 | 費用與隱私 |
|---|---|---|
| `cli` 搭配 `command: claude` 或 `codex` | 您已登入的 Claude Code 或 Codex CLI，在 sandbox 中且不使用任何工具執行 | 使用您的訂閱；內容會傳送給該供應商 |
| `ollama` | 本機 Ollama 模型（`model: llama3.3:70b`，或任何您已下載的模型） | 完全在本機；需要一台能執行該模型的電腦 |
| `gemini`、`groq`、`openrouter` | 使用您的金鑰呼叫該供應商的 API | 按 API 用量計費（或免費額度）；內容會傳送給該供應商 |

使用 `provider: cli` 時，`fallback_command` 指定第二個 CLI（例如在 `claude` 之後備援的 `codex`），當第一個 CLI 額度用完或登出時，由它接手該次執行的剩餘部分。`model` 與 `aux_model` 分別選擇頁面編譯與較輕量工作（概念頁面、lint 報告、摘要）所用的模型；兩者預設都是該供應商建議的選擇。詳情：[docs/llm-providers.md](docs/llm-providers.md)。

## 設定與自訂

所有設定都集中在一個使用者層級的檔案 `~/.kb/config.yaml`（權限 0600；由 `a2kb setup` 寫入，也可用 `KB_CONFIG` 指向其他位置）。`defaults:` 底下的設定套用到所有 KB；在某個 KB 底下設定相同的鍵，則只會覆寫該 KB 的設定：

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # 預設為英文；任何語言名稱皆可
  privacy_mode: false          # true：匯入時完全不呼叫雲端服務
  allow_jina_reader: false     # true：難以讀取的網頁透過 jina.ai 抓取
  allow_cloud_vision: false    # true：圖片會傳送到雲端模型進行 OCR
kbs:
  notes: {}                    # 使用預設值
  research:
    output_language: Spanish   # 此 KB 以西班牙文撰寫頁面與回答
  client-work:
    llm: { provider: cli, command: codex }   # 此 KB 改由 Codex CLI 編譯
```

最快的入門方式是使用您已登入的 `claude` 或 `codex` CLI：不需要 API 金鑰，也不需要額外的服務。若某個 KB 的內容絕不能離開您的電腦，請設定 `llm: { provider: ollama }` 並用 Ollama 執行本機模型（需要一台能執行該模型的電腦；請參閱 [LLM 供應商](docs/llm-providers.md#ollama)）。

| 要變更的項目 | 設定 | 更多資訊 |
|---|---|---|
| 頁面與回答的語言 | `output_language`（可針對個別 KB 或在 `defaults` 中設定） | [Output language](docs/configuration.md#output-language) |
| 索引中的「Past N days」回顧 | `recent_days`（預設為 7，`0` = 關閉） | [Recent-pages recap](docs/configuration.md#recent-pages-recap) |
| 由哪個 LLM 編譯與回答 | `llm:` 區塊（可針對個別 KB 設定） | [LLM 供應商](docs/llm-providers.md) |
| 哪些內容可以離開您的網路 | `privacy_mode`、`allow_jina_reader`、`allow_cloud_vision` | [隱私](docs/privacy.md) |
| KB 存放在哪裡 | `storage:`（共用），或 KB 自己的 `storage:` 與 `prefix` | [儲存服務供應商](docs/storage-providers.md) |
| 某個資料夾使用哪個 KB | 在該資料夾中放一個含有 `kb: <name>` 的 `.kb/config.yaml`（`a2kb new <name> --here`） | [設定](docs/configuration.md#using-a-kb-from-a-project-folder) |
| 單次覆寫 | `KB_*` 環境變數，例如 `KB_OUTPUT_LANGUAGE=ja` | [環境變數](docs/configuration.md#environment-variables) |

`a2kb config show` 會印出每項設定的實際生效值，以及它來自哪個檔案或變數；`a2kb doctor` 則會檢查結果是否可正常運作。機密資訊（儲存空間金鑰、API 金鑰）只能放在這個使用者層級的檔案或環境變數中，絕不能放在專案資料夾裡。

## 10 分鐘架設 Docker 後端

```bash
cp .env.example .env                  # 為您的 KB 命名（KB_NAMES=research,work），並選擇 LLM
docker compose up -d                  # SeaweedFS（S3）+ cron 工作 + MCP 伺服器；金鑰會自動產生
docker compose exec kb claude auth login                                # 只需一次，若 KB_LLM=claude
docker compose exec kb a2kb connect obsidian research --show-secret   # Remotely Save 設定
docker compose exec kb a2kb connect agent research --show-secret      # 含 URL 與 token 的 `claude mcp add ...`
```

這會產生為每個 KB 各自配發限定範圍金鑰的 S3 儲存空間、bucket、設定檔、cron 排程，以及每個 KB 一個唯讀 MCP 伺服器。設定 `DOMAIN=kb.example.com` 並使用 `--profile https` 即可取得自動化的 HTTPS（供手機上的 Obsidian 與遠端 agent 使用），您也可以改為設定自己的 S3 儲存空間。compose 服務名稱為 `kb` 與 `kb-mcp`；其中的指令是 `a2kb`。完整操作說明、金鑰輪替與測試紀錄請見 [docs/docker.md](docs/docker.md)。

## 連接 Obsidian

```bash
a2kb connect obsidian notes                # KB notes 的 Remotely Save 設定
a2kb connect obsidian notes --show-secret  # 同上，並包含 secret key
```

這適用於所有設定方式：AWS S3、Cloudflare R2、其他供應商以及 Docker 後端（在 Docker 中：`docker compose exec kb a2kb connect obsidian <kb>`）。它會為社群外掛 **Remotely Save**（「S3 or compatible」）印出端點、region、bucket、URL 樣式以及 KB 的遠端 prefix（例如 `kbs/notes/`）。設定好 prefix 後，vault 的根目錄就是該 KB：`raw/`、`wiki/` 與 `outputs/` 會以資料夾形式出現。若 KB 使用 AWS 認證鏈（credential chain），設定表會以預留位置顯示金鑰，因為每台裝置都應該取得自己的金鑰，而不是使用這台電腦的 AWS 登入。

請依裝置的用途為外掛選擇金鑰：

- **僅供瀏覽：** 唯讀金鑰（list 與 read），並使用僅拉取（pull-only）的同步方向。
- **編輯與 Web Clipper：** 雙向同步會上傳與刪除檔案，因此需要一把能在 KB 的 prefix 中 list、read、write 與 delete 的金鑰：請為該裝置配發一把獨立的金鑰，而不是 agent2kb 所用的那把，這樣才能單獨撤銷。

請將該 KB 的外掛加密密碼留空（agent2kb 讀取的是純 Markdown），並從一個新的空 vault 開始。編譯與同步依各自的時程執行：頁面會在編譯後的第一次同步時送達裝置，而同步只會在 Obsidian 開啟時進行。完整指南（包含兩種設定方式、資料夾結構與首次同步檢查）請見 [docs/obsidian.md](docs/obsidian.md)。

尚未解析的 `[[wikilinks]]` 是刻意保留的幽靈節點，會由之後的編譯解析；`assets/obsidian-snippets/dim-ghost-links.css` 可讓它們變淡，而 `a2kb stubs` 可為最常被引用的連結建立預留頁面。

## 連接您的 agent（MCP）

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # 唯讀：search、read_page、list_recent、kb_info
```

`--allow-llm` 會加入 `ask` 工具，`--allow-write` 會加入 `ingest_url`；HTTP 模式需要 bearer token。Codex 與 Claude Desktop 的設定片段請見 [docs/mcp.md](docs/mcp.md)。若要告訴 agent 何時該查閱 KB，`a2kb scaffold rules --target <file>` 會在其指示檔（例如 `AGENTS.md`）中寫入一段「KB query discipline」章節。

## 與其他做法的比較

- **單純的 Obsidian 同步。** 同步 vault 能提供儲存空間與閱讀工具，但沒有任何東西會撰寫頁面，而且所有專案共用同一個資料夾。agent2kb 加上了編譯器、專案間的隔離與無人值守的運作，同時仍以 Obsidian 作為閱讀工具。如果您只有一個本機 vault，並想在編輯器中查詢它，那麼在 vault 內執行的外掛是較簡單的選擇。
- **向量 RAG 服務。** 這類服務會回傳與問題最接近的文字片段。agent2kb 則將資料編譯成帶有連結與出處的可閱讀頁面，因此結果可以瀏覽、修正與比對，檢索也是在這些頁面上進行。Embeddings 為選用功能。
- **Agent session 記憶工具。** 這類工具記錄 agent 做了什麼、說了什麼。agent2kb 則將外部資料以及您選擇儲存的回答，編譯成共用的參考 wiki。兩種做法相輔相成：session 記憶回答「嘗試過什麼」，agent2kb 回答「已知道什麼」。

## 安全性與隱私

- 編譯與查詢會將頁面文字傳送給所設定的 LLM，而 Gemini 的免費方案可能會將 prompt 用於訓練。新的 KB 預設**停用** Jina Reader 與雲端圖片 OCR，且每次編譯與匯入都會印出一則標示實際使用之供應商的橫幅。處理敏感資料時，請使用 `llm.provider: ollama`。詳情：[docs/privacy.md](docs/privacy.md)。
- 網頁與檔案下載會經過 SSRF 防護，它會封鎖私有位址、檢查每一次重新導向，並固定連線目標。**影片 URL 不在保護範圍內：** `yt-dlp` 會自行解析名稱並跟隨重新導向，因此請勿在能連到內部服務的電腦上匯入來自不受信任主機的影片 URL。透過 MCP 時，只接受來自 YouTube 或 `--ingest-allow-domain` 指定主機的影片 URL。詳情：[docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) 與 [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf)。
- `claude` 與 `codex` 在不使用工具、也不使用 MCP 的情況下執行。
- KB 隔離分為兩層：agent2kb 中的 prefix 檢查，以及由儲存服務供應商限定範圍的 per-KB 認證資訊。只有第二層能在金鑰外洩時提供保護。**4.34 以前版本的 SeaweedFS 受 CVE-2026-55874 影響**，該漏洞讓 per-KB 金鑰得以複製其他 KB 的物件；Docker 後端固定使用 4.48 版。請參閱 [docs/storage-providers.md](docs/storage-providers.md#seaweedfs)。
- 請以私下方式回報安全漏洞：[SECURITY.md](SECURITY.md)。

## 常見問題

**我的資料會被用於訓練嗎？** agent2kb 沒有任何遙測（telemetry），只會將資料傳送到您所設定的儲存空間與 LLM。LLM 供應商是否會以 prompt 進行訓練，取決於該供應商的政策：Gemini 的免費方案可能會這麼做，而本機 Ollama 伺服器則會將資料保留在您的電腦上。您仍須自行確保擁有所匯入內容的相關權利。

**支援哪些 LLM？** 已登入的 `claude` 或 `codex` CLI（不需要 API 金鑰）、Gemini、Groq、OpenRouter 與 Ollama。請參閱 [docs/llm-providers.md](docs/llm-providers.md)。

**可以完全在本機執行嗎？** 可以。編譯與 embeddings 使用 Ollama，儲存空間使用 SeaweedFS 或 MinIO，並啟用 `privacy_mode`。如此一來，您的內容就永遠不會傳送給第三方。

**需要 GPU 嗎？** 不需要。只有本機 Ollama 模型能因 GPU 受益，因為透過 CLI 或 API 的編譯是在遠端執行的。本機音訊轉錄（`whisper` extra）為選用功能。

**人可以編輯頁面嗎？** 可以，在 Obsidian 或任何 Markdown 編輯器中都行。頁面只有在其原始資料重新編譯時才會重新產生。對於絕不能被重新產生的手寫頁面，請使用 `a2kb promote --as wiki-topic`。

**支援哪些語言？** 原始資料可以是任何語言。頁面與回答預設以英文撰寫，除非您針對某個 KB 或所有 KB 將 `output_language` 設為其他語言。

**如何查看有什麼新內容？** 開啟 `wiki/index.md`：它的開頭是過去 7 天的自動回顧（請參閱[範例](#自動為您撰寫的新內容回顧)）。可針對個別 KB 設定 `recent_days` 來變更時間範圍，或設為 `0` 將其關閉。選用的每週摘要（`scripts/kb-cron-digest.sh`，或在 Docker 中設定 `KB_CRON_DIGEST=1`）會將該週較長的回顧寫入 `outputs/weekly/`。

## 開發藍圖

agent2kb 目前是 1.0 之前的版本。CLI、設定格式與 S3 結構已由 contract 測試固定，但仍可能有一些不夠完善之處。

- 將套件發佈到 PyPI（在那之前，請從原始碼安裝）。
- 驗證更多儲存服務供應商：目前只有 SeaweedFS 經過實際伺服器測試，R2、B2、MinIO、AWS 與 Wasabi 則是依照其文件設定。
- 針對此版本重新執行完整的 `docker compose` 流程，以及在多台裝置上的 Obsidian 同步，包括真實的 HTTPS 憑證（兩者都曾在較早的版本中使用過）。
- 不在規劃中：網頁介面（閱讀透過 Obsidian、CLI 或 MCP 進行）以及 agent session 記憶。

## 文件

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | 以範例檔進行首次執行：匯入、編譯、查詢、Obsidian、問題排解 |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian 與 Remotely Save：金鑰、同步方向、加密、時程 |
| [docs/configuration.md](docs/configuration.md) | 設定檔、KB、admin 角色、`KB_*` 變數、輸出語言 |
| [docs/commands.md](docs/commands.md) | 所有指令與旗標 |
| [docs/llm-providers.md](docs/llm-providers.md) | 供應商、CLI sandbox、路由與備援 |
| [docs/storage-providers.md](docs/storage-providers.md) | AWS S3 與 R2 設定步驟、B2、MinIO、SeaweedFS、Wasabi、隔離、常見錯誤 |
| [docs/docker.md](docs/docker.md) | Docker 後端：compose、Obsidian、agent、HTTPS、金鑰輪替 |
| [docs/mcp.md](docs/mcp.md) | MCP 伺服器、工具、HTTP 模式、安全模型 |
| [docs/privacy.md](docs/privacy.md) | 各階段會將什麼傳送給哪個第三方 |
| [docs/project-workflow.md](docs/project-workflow.md) | 日常使用、`source_meta`、cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | 儲存的回答如何回饋到下一次編譯 |
| [docs/operations.md](docs/operations.md) | 備份、manifest、執行摘要、lint 維護 |
| [CHANGELOG.md](CHANGELOG.md) | 版本發佈與升級說明 |

## 參與貢獻

開發環境設定、相容性約定（compatibility contract）與程式碼規則請參閱 [CONTRIBUTING.md](CONTRIBUTING.md)；coding agent 應閱讀 [CLAUDE.md](CLAUDE.md)。請遵守[行為準則](CODE_OF_CONDUCT.md)。

## 商標

agent2kb 是一個獨立的開源專案，與 Obsidian、Anthropic、OpenAI、Google、Amazon Web Services、Cloudflare 或本 repository 中提及的任何其他公司或產品均無隸屬關係，亦未獲其背書或贊助。所有產品名稱、標誌與商標均為其各自所有者的財產，僅用於說明相容性。

## 授權條款

agent2kb 以 [GNU Affero General Public License v3.0](LICENSE)（`AGPL-3.0-only`）授權。您可以依照這些條款使用、修改與重新散布本軟體；若您修改了本軟體，並讓他人透過網路使用（例如作為託管服務或共用的 MCP 伺服器），AGPL 要求您向他們提供您修改版本的原始碼。

**商業授權。** 若 AGPL 的條款不適合您的用途，例如將 agent2kb 嵌入閉源產品或服務中，維護者可能可以另外提供商業授權：請參閱 [agent2kb.com](https://agent2kb.com) 或寫信至 [licensing@agent2kb.com](mailto:licensing@agent2kb.com)。

貢獻須依照[貢獻者授權協議](CLA.md)接受，該協議讓本專案得以同時提供這兩種授權。
