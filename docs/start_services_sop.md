# Panopticon 服務啟動 SOP

## 概述

本系統包含三個核心服務：
1. **Shadow Hydration Pipeline** — 背景錢包發現與資料庫填充（資料收集，無交易）
2. **Backend API** — FastAPI 服務，提供資料查詢 REST API
3. **Frontend Dashboard** — Vite React 前端，展示監控儀表板

所有服務預設執行於 **Shadow Mode**（`LIVE_TRADING=false`），不進行任何真實交易。

---

## 啟動順序

### Step 1：確認環境變數

```powershell
# 確認 .env 存在且已填入必要 key
code .env
```

**必要環境變數：**

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `DISCOVERY_PROVIDER` | 錢包發現 provider | `dual_track` |
| `LIVE_TRADING` | 是否允許真實交易（必須為 `false`） | `false` |
| `MORALIS_API_KEY` | 錢包歷史查詢（可選，影響 Track B 數據完整性） | - |
| `CLOB_KEY` | Polymarket CLOB API Key（用於 REST trades，公共 Data API 不需要） | - |
| `CLOB_WS_SUBSCRIBE_JSON` | WebSocket 初始訂閱，清單從 Gamma API 動態更新 | - |
| `WS_SUBSCRIBE_REFRESH_SEC` | Gamma API 主動刷新間隔（秒） | `300` |
| `DISCOVERY_COLD_START_INTERVAL_HOURS` | 初期錢包發現間隔 | `2` |
| `DISCOVERY_RELAXED_INTERVAL_HOURS` | 放寬後發現間隔 | `6` |

### Step 2：啟動 Shadow Hydration Pipeline（錢包發現 + 分析）

```powershell
cd d:\Antigravity\Panopticon
python scripts/start_shadow_hydration.py
```

**⚠️ 重要：不可與 run_hft_orchestrator.py 同時運行**
- `start_shadow_hydration.py` 和 `run_hft_orchestrator.py` 不可同時對同一個 DB 運行
- 兩者都會嘗試啟動 `discovery_loop` subprocess，同時運行會導致 `database is locked`
- 如果同時啟動，第二個程序會立即退出並顯示錯誤信息
- 正確流程：先執行 hydration 預熱 → Ctrl+C 停止 → 執行 orchestrator

**說明：**
- 同時啟動兩個子程序：
  - `discovery_loop.py` — 雙軌錢包發現（Track A: CLOB taker + Track B: Leaderboard whale）
  - `analysis_worker.py` — LIFO 倉位追蹤 + insider scoring
- 發現間隔：初期 2 小時，累積 100 個 Tier-1 實體後自動放寬至 6 小時
- 此程序應長期運行，使用 `Ctrl+C` 停止

**終端输出關鍵日誌：**
```
[SYSTEM_STATUS] Shadow Mode Active (Observer Only). Hydrating Seed_Whitelist...
[SYSTEM_STATUS] DISCOVERY_PROVIDER is set to: dual_track
[SYSTEM_STATUS] Observer processes: ['discovery_loop', 'analysis_worker']
2026-04-23 19:54:05,500 INFO __main__ [ANALYSIS_WORKER] tick: 30 wallets to analyze
2026-04-23 19:54:06,242 INFO httpx HTTP Request: GET https://data-api.polymarket.com/v1/leaderboard ... HTTP/1.1 200 OK
```

### Step 3：啟動 HFT Orchestrator（可選，完整實時系統）

```powershell
# 在另一個終端（先停止 Step 2 的 hydration）
python run_hft_orchestrator.py
```

**⚠️ 重要：先停止 Step 2（Ctrl+C），再執行此步驟。不可同時運行。**

**包含的 tracks：**
  - Radar — Polymarket CLOB WebSocket feed → signal_queue
  - OFI — Hyperliquid BTC-USD OFI engine → signal_queue
  - Graph — HiddenLinkGraphEngine
  - Signal Engine — asyncio task（L2/L3 共識貝氏決策 + L4 Fast Gate）

**終端输出關鍵日誌：**
```
2026-04-23 19:54:53,106 [INFO] orchestrator —— Panopticon HFT Orchestrator starting at 2026-04-23T11:54:53
2026-04-23 19:54:53,472 [INFO] orchestrator —— [RADAR] Starting Polymarket CLOB WebSocket feed → signal_queue
2026-04-23 19:54:53,943 [INFO] orchestrator —— [OFI] Starting Hyperliquid BTC-USD OFI Engine → signal_queue
2026-04-23 19:54:54,001 [INFO] orchestrator —— [GRAPH] HiddenLinkGraphEngine ready
2026-04-23 19:54:53,445 [INFO] orchestrator —— [ORCH] Signal engine running as asyncio task (not subprocess)
```

### Step 4：啟動 Backend API（可選，若需要 REST API 查詢）

```powershell
# 在另一個終端
cd d:\Antigravity\Panopticon
python -m uvicorn panopticon_py.api.app:app --host 127.0.0.1 --port 8001 --reload
```

**必要時才啟動**，若只需要資料收集（Step 2），此步驟可跳過。

**API Endpoints：**
- `GET http://127.0.0.1:8001/api/performance?period=all` — 績效數據
- `GET http://127.0.0.1:8001/api/system_health/readiness` — 系統就緒狀態
- `GET http://127.0.0.1:8001/api/system_health/status` — 系統狀態
- `GET http://127.0.0.1:8001/api/report/current` — 當前報告
- `GET http://127.0.0.1:8001/api/recommendations?limit=20` — 推薦列表

### Step 5：啟動 Frontend Dashboard（可選，若需要視覺化介面）

```powershell
# 在另一個終端
cd d:\Antigravity\Panopticon\dashboard
npm run dev
```

**URL：** `http://localhost:5173`

Frontend 會自動輪詢 Backend REST API（每 10 秒）以更新監控面板。

---

## 自動 Insight 報告

系統已配置每 2 小時自動生成 Insight 報告：

```powershell
# 啟用自動報告排程
powershell -File scripts/schedule_insight_report.ps1

# 查看已排程的任務
Get-ScheduledTask | Where-Object {$_.TaskName -like '*Panopticon*'}

# 移除排程任務（如需）
Unregister-ScheduledTask -TaskName "PanopticonInsightReport" -Confirm:$false
```

手動生成報告：
```powershell
python scripts/report_insights.py -o data/insight_reports/report_latest.json
```

---

## 停止服務

```powershell
# Ctrl+C 停止 shadow hydration
# 或終止程序
Get-Process -Name python | Where-Object {$_.CommandLine -like "*start_shadow_hydration*"} | Stop-Process -Force
Get-Process -Name uvicorn | Stop-Process -Force
```

---

## Agent Handoff（新 Agent 接手流程）

當新 Agent 接手專案時，請按以下順序確認系統狀態：

### 1. 確認目前運行的服務

```powershell
# 列出所有 Python 進程
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*Antigravity*" } | Format-Table Id, ProcessName, StartTime

# 檢查 port 8001（Backend API）
netstat -ano | Select-String ":8001\s"

# 檢查是否有僵屍進程
Get-Process python -ErrorAction SilentlyContinue | Where-Object { -not $_.Responding } | Format-Table Id, ProcessName
```

### 2. 確認 DB 目前被誰使用

```powershell
# 嘗試啟動 hydration，如果已有進程佔用，會立即顯示錯誤
python scripts/start_shadow_hydration.py
# 預期輸出（無衝突）: [SYSTEM_STATUS] Observer processes: ['discovery_loop', 'analysis_worker']
# 預期輸出（有衝突）: [ERROR] Another Panopticon process is already running.
```

### 3. 讀取系統狀態

```powershell
# 檢查 execution_records
python -c "import sqlite3; c=sqlite3.connect('data/panopticon.db').cursor(); print('exec_records:', c.execute('SELECT COUNT(*) FROM execution_records').fetchone()[0]); print('wallet_obs:', c.execute('SELECT COUNT(*) FROM wallet_observations').fetchone()[0])"
```

### 4. 恢復運行

```
# Scenario A: 只想預熱數據 → 只啟動 hydration
python scripts/start_shadow_hydration.py

# Scenario B: 想運行完整系統 → 先 Ctrl+C hydration，再啟動 orchestrator
# (Ctrl+C hydration)
python run_hft_orchestrator.py
```

### 5. 驗證系統正常

```powershell
# Backend API 狀態
curl http://127.0.0.1:8001/api/system_health/status

# DB 實時數據
python -c "
import sqlite3, os
db = os.getenv('PANOPTICON_DB_PATH', 'data/panopticon.db')
c = sqlite3.connect(db).cursor()
tables = ['wallet_observations', 'insider_score_snapshots', 'wallet_market_positions', 'execution_records', 'discovered_entities']
for t in tables:
    n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    recent = c.execute(f\"SELECT COUNT(*) FROM {t} WHERE 1=1\").fetchone()[0]
    print(f'{t}: {n}')
"
```

---

## 環境變數配置文件

`.env.example` 為模板，`.env` 為運行時配置（gitignore）。

統一環境變數：
```powershell
python scripts/unify_env.py
```

---

## 常見問題

### Q: `gamma_candidates_fetched: 0`
A: 檢查 Gamma API 是否正常返回資料，確認 `DISCOVERY_PROVIDER=dual_track`。

### Q: Radar WebSocket 403
A: 正常現象。dashboard 的 `WebSocketLiveAdapter` 會在 WebSocket 失敗時 fallback 到 REST polling。

### Q: `entropy_state.events: 0`
A: Entropy window 未收到有效事件。檢查 `HUNT_ENTROPY_GAP_FLUSH_SEC` 和 `HUNT_ENTROPY_MAX_INTERNAL_GAP_SEC` 是否設定過短。

### Q: 錢包發現停滯
A: 檢查 `.env` 中 `DISCOVERY_PROVIDER=dual_track` 是否正確設定。

---

## 架構圖

### v4-FINAL Architecture（2026-04-23）

```
┌─────────────────────────────────────────────────────────────────┐
│  start_shadow_hydration.py  │  run_hft_orchestrator.py          │
│  [純 Observer 預熱工具]       │  [完整實時系統]                    │
│                               │                                  │
│  discovery_loop (T1)         │  Radar → signal_queue             │
│  analysis_worker (T5)         │  OFI   → signal_queue             │
│                               │  Graph → DB                      │
│                               │  SE    ← signal_queue → DB        │
└─────────────────────────────────────────────────────────────────┘

L1: PERCEPTION LAYER
  Hyperliquid OFI (BTC-USD) ──┐
                                 ├──► asyncio.Queue[SignalEvent] ⚡ ZERO DISK I/O
  Polymarket Radar (entropy) ──┘
                                 │
                                 ▼
L2/L3: signal_engine._run_async (asyncio task)
  READ: wallet_observations (last 60s)
  READ: insider_score_snapshots (score >= 0.55)
  READ: wallet_market_positions (LIFO avg_entry, READ ONLY!)
  Consensus Bayesian Update
  L4 Fast Gate (fast_gate.py — unified)
                                 │
                                 ▼
L4: execution_records (WRITE — our trades ONLY)
    wallet_market_positions: FORBIDDEN in SE
    paper_trades: FORBIDDEN (merged into mode=PAPER)

OBSERVER: analysis_worker (T5, threading.Thread)
  WRITE: wallet_market_positions (LIFO, SYNC + WAL)
  WRITE: insider_score_snapshots (AsyncDBWriter)
  READ: wallet_observations
```

---

## 快速啟動腳本

| 腳本 | 功能 |
|------|------|
| `scripts/restart_all.ps1` | PowerShell 一鍵重啟所有服務（single source of truth，D79+ 標準入口） |
| `scripts/start_shadow_hydration.py` | 單獨啟動 Shadow Hydration Pipeline |
| `scripts/schedule_insight_report.ps1` | 排程每2小時生成 Insight 報告 |
| `scripts/report_insights.py` | 手動生成 Insight 報告 |

> `restart_all.ps1` 管理所有程序：Backend (uvicorn)、Orchestrator、Radar（asyncio task 內嵌於 Orchestrator）。

---

## 版本歷史

| 日期 | 更新內容 |
|------|----------|
| 2026-04-22 | 初始版本，新增 dual_track 發現模式與雷達自動刷新 |
| 2026-04-23 | v4-FINAL 重構：刪除 hft_execution_gate.py；新增 asyncio.Queue signal bus；SE 改 asyncio task；新增 DB lock 防止雙進程衝突；更新啟動 SOP |
| 2026-04-25 | D55 後：雷達模組重構，`run_radar.py` 獨立程序 + entropy gate；Graphify 隔離；BTC 5m resolver loop |
| 2026-04-26 | D68：usdcSize bug 修復（POST trades 回應無 usdcSize 欄位）；`discovered_entities.insider_score` 欄位新增 |
| 2026-04-27 | D70：Polymarket CLOB 直接串接；移除 `main_loop.py`；`run_hft_orchestrator.py` 成單一入口 |
| 2026-04-28 | D79：雷達改為 orchestrator 內部 asyncio task（移除 `Start-Radar`）；stderr redirect；D80：f-string 修復、ShadowDB.execute() 新增 |
| 2026-04-29 | D81：Python scope chain 修復（`_live_ticks` 三層模型）；`insider_score` 欄位遷移；D82：知識管理基礎設施（EXPERIENCE_PLAYBOOK 更新、.cursorrules新規則） |

---

## 已知 P1 問題（D82 修復中）

| 問題 | 說明 | 修復計劃 |
|------|------|----------|
| `insider_score` 欄位缺失 | `metrics_collector.py` 查詢 `discovered_entities.insider_score` 但欄位不存在 | D82 在 `_ensure_discovery_tables()` 加入 `ALTER TABLE` migration |

---

## 診斷日誌說明

| 日誌標籤 | 頻率 | 說明 |
|----------|------|------|
| `D77_LOOP_ALIVE` | 每 0.1s | 確認雷達 WS loop 存活，穩定後可移除 |
| `D77_LOOP_TICK` | 每 10s | 雷達 tick 計數，穩定後可移除 |
| `D78_60S_BLOCK` | 每 60s | 60 秒診斷區塊（Z-score、entropy gate 狀態） |
| `D75_HEARTBEAT` | 每 60s | WS 心跳，確認資料流健康 |
| `D75_ENTROPY_GATE` | 每 60s | Entropy gate 觸發統計 |

> 出現 `D78_60S_BLOCK` 即表示系統正常運行。若持續無輸出超過 3 分鐘，檢查 `orchestrator.err.log`。