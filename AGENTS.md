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

## ✅ Coding Agent 強制規則（D101–D120 提煉，2026-05-01）

> 以下規則與 `.cursorrules` 中的 `Agent Hard Rules` 完全對應，此處以任務語境重述，供 coding agent 在收到任務時自查。

### 1. Import 完整性（RULE-IMPORT-1/2）
- 新增任何函數前，確認函數體用到的所有標準庫已在頂層 import
- `except Exception` **不能**捕捉 `NameError` 的假設是錯的——它可以，且會靜默吞掉
- Handoff 驗證清單必須包含一條「觸發函數體路徑」測試，不得只靠 `python -c "import X"`

### 2. 時間工具唯一來源（RULE-TIME-1）
- 永遠用 `from panopticon_py.time_utils import utc_now_rfc3339_ms`
- 發現任何 `_utc_now_rfc3339_ms()` 局部定義立即刪除並替換

### 3. SQLite 存取方式（RULE-SQLITE-1）
- 所有 `sqlite3.Row` 使用 `r["col_name"]` 或 `dict(r)`
- 新增 DAL method 時 SELECT 欄位必須明確 `AS alias`

### 4. asyncio.Semaphore 使用（RULE-SEMAPHORE-1/2）
- 永遠在協程內初始化，不在模組頂層
- `await asyncio.sleep()` 必須在 `async with semaphore:` 區塊**外**

### 5. 外部 API 資料防禦（RULE-API-1）
- `list[0].get(...)` 前必須 `isinstance(list[0], dict)` 守衛
- 無論 Gamma、Polymarket 還是任何外部 API，均適用

### 6. Worker stop() 重入（RULE-REENTRY-1）
- 所有 background thread/worker 的 `stop()` 首行必須檢查 `if not self._running: return`

### 7. 跨 class 介面契約（RULE-CONTRACT-1）
- Stub/Adapter 與原 class 共用的方法回傳結構必須定義 `TypedDict`
- 短期豁免：加 docstring `# implicit contract, see OriginalClass.method()`

### 8. 跨進程 JSON 路徑（RULE-PATH-1）
- 寫入方與讀取方必須使用完全相同的 `os.getenv("..._PATH", default)`
- 禁止任一方硬編碼路徑，即使預設值相同

### 9. Dead code 清理（RULE-DEAD-1）
- sprint 完成前 grep 確認所有新增賦值有 reader
- Shadow variable（同名局部變數遮蓋模組級 global）必須消除

### 10. 閉包提升規則（RULE-CLOSURE-1）
- 提升為模組級前確認所有引用變數的 scope
- 無法提升則改用依賴注入參數

### 11. WebSocket payload 格式（RULE-WS-1）
- `payload_json` 在 WS handler 永遠傳字串，不在 handler 內做 `json.loads()`

---

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
```

**Step 3：copy-paste 給架構師**

**Step 4：收到回覆後**
- 裁決寫入 `FEATURE_INDEX.md` Decision Records
- 臨時文件刪除或標記 `ARCHIVED`

### 資料夾管理規則
- `temp_architect_handoffs/` 目錄下**只保留最新一份 handoff 檔案**
- 所有舊檔案自動移至 `temp_architect_handoffs/old/`
- `old/` 中的檔案**不可引用**

### 規則
- **每 session 一個檔案**，所有 Q 一次提出，不追加
- **禁止 commit** 臨時文件至 git
- **保持簡潔**：只含事實與選項，不解釋已知內容
- **附：背景資料**：直接附上相關程式碼片段，不得只寫路徑/行號

---

## PRICE DATA — Polymarket CLOB API

### Primary: GET /book (one call, all data)
`https://clob.polymarket.com/book?token_id=<token_id>`
Returns: `bids[]`, `asks[]`, `last_trade_price`

### Price selection rule:
```
spread = best_ask - best_bid
if spread <= 0.10:  use mid_price = (best_bid + best_ask) / 2
if spread >  0.10:  use last_trade_price (if != "0.5")
else:               return None → NO_PRICE_DATA
```
**NEVER** return `0.0` to mean "unavailable" — causes silent EV miscalculation.

### NEVER:
- Use Gamma API as primary price source
- Use mid_price when spread > 0.10
- Return `0.0` to mean "unavailable"

---

### Pre-fix Protocol
1. **Search EXPERIENCE_PLAYBOOK.md first**
2. If matching EXP entry found → follow documented fix exactly
3. If no match → proceed with analysis, document in EXPERIENCE_PLAYBOOK after fix
4. **Hard stop at 2 failed attempts** → write escalation handoff

---

## Current System Status (as of D125)

| Process | Version | Notes |
|---------|---------|-------|
| Orchestrator | v1.1.34-D120 | import json fix + utc_now_rfc3339_ms alignment |
| Backend | v1.1.24-D120 | WS idiom cleanup |
| Radar | v1.1.47-D125 | WS runner fix + book/real trade heartbeat counters |

**Active Technical Debt:**
| ID | 問題 | 優先級 | 目標 Sprint |
|----|------|--------|------------|
| Debt-1 | `_on_insider_alert` 裸 `sqlite3.connect`，繞過 WAL/busy_timeout | P0 | D121 |
| Debt-2 | `AsyncDBWriter.health()` 隱式契約，無 TypedDict | P1 | D121 |
| Debt-3 | `graph_engine` 局部 dead code，與 global `_graph_engine` 影子 | P2 | D122 |

---

## PROCESS RESTART (MANDATORY)
Python has no hot reload. After ANY code change to `panopticon_py/**/*.py`, `run_hft_orchestrator.py`, or `run_radar.py`, you MUST kill and restart ALL running processes before validating.

⚠️ **CRITICAL**: `-WorkingDirectory` is **MANDATORY** for all `Start-Process python` commands.

Restart: **ALWAYS use `scripts/restart_all.ps1`**

---

## PROCESS SINGLETON PROTOCOL (MANDATORY — ALL AGENTS)

Every managed process enforces a singleton via PID lock files in `run/`.
Utility: `panopticon_py/utils/process_guard.py`
DO NOT bypass, remove, or comment out `acquire_singleton()` calls.

| Name | Entry Point | Guard Name |
|------|-------------|-----------|
| backend | panopticon_py/api/app.py | "backend" |
| radar | panopticon_py/hunting/run_radar.py | "radar" |
| orchestrator | run_hft_orchestrator.py | "orchestrator" |

EVERY entry-point MUST have as FIRST executable lines after imports:

    from panopticon_py.utils.process_guard import acquire_singleton
    PROCESS_VERSION = "v{MAJOR}.{MINOR}.{PATCH}-D{sprint}"
    acquire_singleton("process_name", PROCESS_VERSION)

---

## PROCESS VERSION PROTOCOL (MANDATORY — ALL AGENTS)

**RULE-VER-1**: BEFORE modifying any process file, READ its current `PROCESS_VERSION`.
**RULE-VER-2**: AFTER modifying, INCREMENT `PROCESS_VERSION` (bug fix → PATCH; new feature → MINOR; breaking → MAJOR). Always append `-D{N}`.
**RULE-VER-3**: AFTER bumping, update `run/versions_ref.json` in the SAME commit.
**RULE-VER-4**: `versions_ref.json` must never be out of sync with code.
**RULE-VER-5**: In every Architect Handoff, include version table.

### ZERO-TRUST VERIFICATION CHECKLIST

```
curl -s http://localhost:8001/api/versions | python -m json.tool
# All version_match fields must be true
# All status fields must be "running"
```

---

## Documentation Index

All agents should read `AGENTS.md` first for onboarding. This index points to additional files:

| File | Purpose |
|------|---------|
| `TECH_DEBT.md` | Tech debt observations + completed sprint history + decision records |
| `FUNCTION_STATUS.md` | Function runtime state index — blocked/active/logged_only status (D124 rule) |
| `panopticon_py/hunting/INDEX.md` | `run_radar.py` function status markers (same rule; hunting-focused) |
| `PANOPTICON_CORE_LOGIC.md` | System core logic — invariants, signal flow, risk controls |
| `EXPERIENCE_PLAYBOOK.md` | Verified operational patterns — bug fixes, known failure modes |