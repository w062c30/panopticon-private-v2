# Panopticon Agent Directives

## 交付標準
- 所有變更需維持 `shared/contracts/panopticon-event.schema.json` 契約相容。
- 任何交易決策都必須可回溯 `Prior -> Evidence -> Posterior -> Kelly -> Action`。
- 不得在程式碼或文件中硬編碼 API Key / 私鑰。
- Graphify 僅可用於工程輔助（coding/debug/測試缺口提示），不得介入交易決策。
- `graphify-out/*`、`GRAPH_REPORT.md`、`graph.json` 與任何圖譜推導內容一律視為 **HUMAN_READ_ONLY**，不得被 signal/risk/execution 程式路徑讀取。
- 若偵測到 Graphify 來源資料進入決策路徑，必須 fail-closed 並記錄審計事件。

## LLM 後端（Phase 1 / Phase 2）
- **Phase 1（現況）**：遠端推論**暫**統一走 **NVIDIA** `integrate.api.nvidia.com`（`urllib.request`，見 `panopticon_py/llm_backend.py`、`panopticon_py/cognitive.py`、`panopticon_py/hunting/semantic_router.py`）。**不要**為 LLM 引入 `requests` / `openai` SDK。
- **Phase 2（規劃）**：將支援**本機 LLM**（可插拔 backend，如 Ollama／vLLM）；切換方式與依賴以屆時計畫為準。新增 LLM 呼叫前請先全倉庫搜尋既有模式並維持可審計日誌。

## MiniMax API 併發限制
- 使用 MiniMax 模型（`minimaxai/minimax-m2` 或其變體）進行推論時，**必須**將併發控制在 **1-2** 以內。嚴禁發送平行請求。
- 原因：MiniMax API 有嚴格的併發/速率限制，突發調用會觸發 429 錯誤、浪費 token，並影響全體用戶的服務穩定性。
- 實作方式：在任何調用 `post_nvidia_chat_completion` 且使用 MiniMax 模型的地方，使用 `asyncio.Semaphore(2)` 或同步鎖。代碼庫中若已有共用的 semaphore，應復用而非每次創建新的。
- 適用範圍：hunting pipeline、cognitive layer、semantic router 及所有 ad-hoc agent prompting。此限制為全局限制（非模組獨立）。

## UI/UX 規範
- 控制面板實作需遵循 UI/UX best practices（可及性、對比，焦點狀態、資訊層級）。
- 儀表板變更前需確認不破壞既有 KPI 呈現與互動流。

## 時間格式契約（Time Contract）
- 專案內部持久化 UTC 欄位（如 `*_ts_utc`）統一使用 RFC3339 UTC 毫秒字串（例：`2026-04-25T11:03:14.527Z`）。
- 外部 API（Polymarket/Gamma/Data/CLOB WS）時間欄位以官方文件為準，不可盲目統一格式。
- 外部時間寫入內部 DB 前需正規化；必要時保留原始時間於 payload/context 供稽核。
- 時長/TTL/重試等運行時計時使用 `time.monotonic()`，不可當作 UTC 時間落盤。
- 任何時間格式調整需同步更新 `docs/time_contract.md`。

## 審查與評分
- 任務完成後，Codex 會執行程式碼審查與評分。
- 評分項目至少包含：
  - 功能正確性
  - 資料一致性
  - 風控完整性
  - 可維護性
  - 測試覆蓋
  - UI/UX 品質
  - 文件完整度
- 若未達門檻，需依審查結果修正後再提交。

## 自動化狩獵（Hunting）與影子模式
- **影子優先**：`hunting_shadow_hits` 僅記錄與審計；**不得**因影子勝率或 heuristics 自動開啟實盤；`LIVE_TRADING` 與人為審查仍依 `docs/paper_to_live_gate.md`。
- **禁止 naive MM 規則**：不得以單一條件（例如 raw `daily_trades > N`）剔除做市商；須經 **掃簿聚合**、可選 **跨錢包宏聚合**，再跑 **4D 行為矩陣**（`panopticon_py/hunting/`）。
- **WebSocket 真空**：`entropy_window` 在接收間隙超閾值或 **WS Reconnect** 時必須 **flush** 滾動緩衝並 **鎖死** -4sigma 觸發，直至重新收滿健康視窗；不得將「斷線導致的成交量塌縮」當成秩序降臨。
- **圖譜剪枝**：Moralis / 溯源必須遵守 **頁數與列數上限**，並尊重 `config/cex_dex_routers_blacklist.json`；命中高流量/黑名單節點時標記 **CEX_ANONYMIZED**，資金圖權重歸零並改依 **4D + 影子 PnL**。
- **UMA 元遊戲（Phase 1b）**：若監聽到地址與 **UMA 投票活躍度** 高度關聯，僅提升 **`oracle_meta_risk`** 風險標籤（Market Dictator 語意），仍為審計與風控輸入，不作鏈下「定罪」。

## Committee Shadow 實驗隔離（正式條文）
- 允許新增 `committee_score` 與 `disagreement_index`，但只能作為旁路 shadow 觀測。
- 旁路觀測必須綁定獨立 `experiment_id`，且使用獨立報表/輸出檔，不得寫回主決策 KPI 聚合。
- 旁路觀測不得影響 `StrategyInput`、`decide()`、風控 gate、下單流程、倉位大小。
- 任一報表、儀表板或審查文件必須明確區分 **Baseline Paper Trade** 與 **Experiment Shadow** 數據，不得混算。

## 架構師溝通協議（Architect Agent Handoff）

### ⚠️ GitHub Repo 主動同步（每次 Handoff 前執行）

Architect 可透過 GitHub 閱讀原始碼（含行號對照）。每個 handoff 完成後，**必須**確保 GitHub repo 為最新狀態：

```
# 每次 handoff 完成後（不遲於 Step 4）：
git add -A
git commit -m "D{sprint}: {sprint摘要}"
git push
```

**禁止**：
- `temp_architect_handoffs/*.md` 推入 repo（已列於 .gitignore）
- 推入 `.env`、`secrets/`、`data/*.db`、`run/*.lock`、`run/*.pid`
- 推入超過 100MB 的檔案（GitHub 限制）

**Repo URL**：https://github.com/w062c30/panopticon-private

---

### 觸發時機
- 重大架構決策（刪除模組、重構核心路徑、修改不變量）
- 候選方案需裁決
- 發現 Invariant 衝突

### 操作流程

**Step 1：建立臨時文件**
```
temp_architect_handoff_{date}.md  # 專案根目錄
```

**Step 2：檔案結構（每 session 只建立一個，全部問題一次列出）**

```markdown
# Architect Handoff — {日期}

## 背景
{1-3 句話描述系統現狀與目標}

## 已完成
- {變更} — {檔案}: {摘要}

## 問題

### Q1: {標題}
**背景**：{涉及的 Invariant / 契約 / 現有架構}
**選項**：
- A: {方案} → {優點}/{風險}
- B: {方案} → {優點}/{風險}
**建議**：{傾向方案}
→ 需要裁決：{具體問題}

### Q2: ...

---

**附：背景資料（直接引用程式碼）**
```python
# {相關函數名稱}
{相關程式碼片段}
```
```

**Step 3：copy-paste 給架構師**
將整份 temp 文件內容在 chat 中貼上，附一句「請審查並裁決」即可。

**Step 4：收到回覆後**
- 裁決寫入 `FEATURE_INDEX.md` Decision Records
- 臨時文件刪除或標記 `ARCHIVED`

### 資料夾管理規則
- `temp_architect_handoffs/` 目錄下**只保留最新一份 handoff 檔案**（以 `2026-04-XX_DN` 命名者）
- 所有舊檔案（不包含最新版本）自動移至 `temp_architect_handoffs/old/` 子目錄
- `old/` 目錄無需管理，舊檔案可自由累積，**永不主動刪除**
- `old/` 中的檔案**不可引用**（Architect 只能看到 `temp_architect_handoffs/` 的最新檔案）

### 規則
- **每 session 一個檔案**，所有 Q 一次提出，不追加
- **禁止 commit** 臨時文件至 git
- **保持簡潔**：只含事實與選項，不解釋已知內容
- **附：背景資料**：直接附上相關程式碼片段（function signature、關鍵變數、Schema 欄位），不得只寫路徑/行號（架構師無法讀取本機檔案）

---

## PRICE DATA — Polymarket CLOB API

### Primary: GET /book (one call, all data)
`https://clob.polymarket.com/book?token_id=<token_id>`
Returns: `bids[]`, `asks[]`, `last_trade_price`
Ref: https://docs.polymarket.com/api-reference/market-data/get-order-book

### Price selection rule (mirrors Polymarket UI):
```
spread = best_ask - best_bid
if spread <= 0.10:  use mid_price = (best_bid + best_ask) / 2
if spread >  0.10:  use last_trade_price (if != "0.5")
else:               return None → NO_PRICE_DATA
```
**NEVER** return `0.0` to mean "unavailable" — causes silent EV miscalculation.

### Batch: POST /last-trades-prices (up to 500 tokens)
Ref: https://docs.polymarket.com/api-reference/market-data/get-last-trade-prices-request-body

### Python SDK (read-only reference):
https://github.com/Polymarket/py-clob-client
https://github.com/Polymarket/py-clob-client-v2

### NEVER:
- Use Gamma API as primary price source (unreliable, mid-only)
- Use mid_price when spread > 0.10 (distorted, matches Polymarket's own logic)
- Return `0.0` to mean "unavailable"

---

## PROCESS RESTART (MANDATORY)
Python has no hot reload. After ANY code change to `panopticon_py/**/*.py`, `run_hft_orchestrator.py`, or `run_radar.py`, you MUST kill and restart ALL running processes before validating.

⚠️ **CRITICAL**: `-WorkingDirectory` is **MANDATORY** for all `Start-Process python` commands.
Without it, Python runs from the system temp directory → `ModuleNotFoundError: No module named 'panopticon_py'`.
Confirmed broken in D46, fix documented in D49.

Restart sequence (PowerShell):
```
$projDir = "d:\Antigravity\Panopticon"
Get-Process python | Where-Object {
    $_.CommandLine -like "*run_hft_orchestrator*" -or $_.CommandLine -like "*run_radar*"
} | Stop-Process -Force
Start-Sleep 3
Remove-Item -ErrorAction SilentlyContinue "$projDir\data\orchestrator.lock"
$env:PANOPTICON_WHALE = "1"
Start-Process python -ArgumentList "panopticon_py\hunting\run_radar.py" -WorkingDirectory $projDir -NoNewWindow -PassThru | Tee-Object -Variable radarProc
Start-Process python -ArgumentList "run_hft_orchestrator.py" -WorkingDirectory $projDir -NoNewWindow -PassThru | Tee-Object -Variable orchProc
Start-Sleep 10
```

Always confirm restart by checking process PIDs and log file timestamps before checking metrics.

---

## PROCESS SINGLETON PROTOCOL (MANDATORY — ALL AGENTS)

### Overview
Every managed process enforces a singleton via PID lock files in run/.
The utility is: `panopticon_py/utils/process_guard.py`
DO NOT bypass, remove, or comment out `acquire_singleton()` calls.

### Managed Processes
| Name | Entry Point | Guard Name |
|------|-------------|-----------|
| backend | panopticon_py/api/app.py | "backend" |
| radar | panopticon_py/hunting/run_radar.py | "radar" |
| orchestrator | run_hft_orchestrator.py | "orchestrator" |
| frontend | dashboard/ (node/vite) | PowerShell only |

### Rule: acquire_singleton() Call Location
EVERY entry-point file MUST have these as the FIRST executable lines after imports. No exceptions.

    from panopticon_py.utils.process_guard import acquire_singleton
    PROCESS_VERSION = "v{MAJOR}.{MINOR}.{PATCH}-D{sprint}"
    acquire_singleton("process_name", PROCESS_VERSION)

If `acquire_singleton()` is missing from any entry point → that file is in violation. Fix it before adding other changes.

### Rule: Only Use restart_all.ps1
NEVER start individual processes by hand during a session.
ALWAYS use: `scripts/restart_all.ps1`
This script: kills all → clears PID files → starts all 4 → verifies singletons.

### Singleton Count Verification (D54)
DO NOT count raw python processes with `Get-Process` — Cursor IDE and other tools also run python.exe, creating false "DUPLICATE" warnings.
ALWAYS verify singletons using `run/process_manifest.json` (written by `acquire_singleton()`):
```powershell
$m = Get-Content run/process_manifest.json | ConvertFrom-Json
foreach ($svc in @("backend","radar","orchestrator")) {
    $entry = $m.PSObject.Properties[$svc].Value
    if ($null -ne $entry) {
        $pid = $entry.pid
        $alive = $null -ne (Get-CimInstance Win32_Process -Filter "ProcessId=$pid")
        Write-Host "[$svc] PID=$pid alive=$alive version=$($entry.version)"
    }
}
```
This reads the authoritative manifest and verifies each PID is alive via `Get-CimInstance` (compatible across PowerShell sessions).

---

## PROCESS VERSION PROTOCOL (MANDATORY — ALL AGENTS)

### The Source of Truth
File: `run/versions_ref.json`
This file tells the system what version SHOULD be running.
ALL agents MUST update it whenever they modify a process file.

### Mandatory Version Bump Rules
**RULE-VER-1**: BEFORE modifying any process file, READ its current `PROCESS_VERSION` constant and note the version.

**RULE-VER-2**: AFTER modifying any process file, INCREMENT the `PROCESS_VERSION` constant in its entry point:
- Bug fix / refactor / config change → bump PATCH (0.0.X)
- New feature, new endpoint, new DB field → bump MINOR (0.X.0)
- Breaking inter-process API change → bump MAJOR (X.0.0), escalate to Architect
Always append current sprint: `-D{N}` e.g. `-D52`

**RULE-VER-3**: AFTER bumping `PROCESS_VERSION`, update `versions_ref.json`:
- Set the process key to the new version string
- Update "last_updated" to current UTC ISO timestamp
- Set "updated_by_sprint" to current sprint number
- Set "updated_by_agent" to a brief descriptor e.g. "D52-coding-agent"

**RULE-VER-4**: `versions_ref.json` MUST be updated in the SAME commit / session as the code change. Never leave them out of sync.

**RULE-VER-5**: In the Architect Handoff, list ALL version changes:
| Process | Old Version | New Version | Reason |

### Version Mismatch Behavior
If a process starts and its `PROCESS_VERSION` != `versions_ref.json` expected:
- Logs CRITICAL warning at startup (cannot be missed)
- Still starts (not fatal) — prevents deployment deadlocks
- Architect must investigate: either code was not redeployed, or versions_ref.json was not updated by the previous agent

### Checking Versions at Runtime
`GET http://localhost:8001/api/versions`
→ Returns `process_manifest.json` (all PIDs, versions, start times, `version_match` flags)
→ Use this in Zero-Trust verification checks

### Inter-process Version Checks
When one process depends on another (e.g. orchestrator reads radar data):

    from panopticon_py.utils.process_guard import check_peer_version
    peer = check_peer_version("radar")
    if peer and not peer["version_match"]:
        logger.warning("Radar may be running stale code: %s", peer["version"])

Version mismatch is WARN only. Never block business logic on version checks.

### ZERO-TRUST VERIFICATION CHECKLIST (updated)

After EVERY session restart, verify ALL of:

```
# Singleton check (MUST each be 1)
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*run_radar*" } | Measure-Object | Select-Object -ExpandProperty Count   # = 1
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*run_hft_orchestrator*" } | Measure-Object | Select-Object -ExpandProperty Count  # = 1
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*uvicorn*" } | Measure-Object | Select-Object -ExpandProperty Count       # = 1
Get-Process node | Measure-Object | Select-Object -ExpandProperty Count       # = 1

# Version check
curl -s http://localhost:8001/api/versions | python -m json.tool
# All version_match fields must be true
# All status fields must be "running"

# PID files exist
Get-ChildItem run/*.pid   # expect: backend.pid, radar.pid, orchestrator.pid
```

If ANY process shows count ≠ 1: STOP, kill duplicates, escalate.
