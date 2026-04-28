# Signal Engine v4-FINAL 架構變更說明書

**Date:** 2026-04-23
**Author:** Panopticon Agent
**Status:** v4-FINAL — 實施完成，待架構師審批

---

## 1. 前置文件對照

| 文件 | 用途 |
|------|------|
| `2026-04-23-signal-engine-design.md` | 最初設計提案（v1 → v2 修正方向） |
| `2026-04-23-signal-engine-architecture-review.md` | 架構審查brief（6個 Q 待裁決） |
| `Panopticon Signal Engine rebuild plan(23Apr 2026).md` | 裁決裁決後的最終執行計劃（已刪除） |

---

## 2. 最終架構圖（v4-FINAL）

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  L1: PERCEPTION LAYER                                                        ║
║                                                                               ║
║  Hyperliquid WS (BTC-USD OFI) ──┐                                             ║
║                                   ├──► asyncio.Queue[SignalEvent]  ⚡ ZERO I/O ║
║  Polymarket Radar (entropy)  ────┘         │                                  ║
║                                             ▼                                  ║
║  ┌───────────────────────────────────────────────────────────────┐           ║
║  │  OFI source: market_id via OFI_MARKET_MAP ("BTC-USD" → "540844")│           ║
║  │  Radar source: market_id direct from Polymarket CLOB           │           ║
║  └───────────────────────────────────────────────────────────────┘           ║
║                                             │                                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L2/L3: signal_engine._run_async (asyncio task — NOT subprocess)             ║
║                                                                               ║
║  READ wallet_observations (last 60s, same market)   O(1)                     ║
║  READ insider_score_snapshots (score ≥ 0.55)       O(1)                     ║
║  READ wallet_market_positions (LIFO avg_entry)      O(1) [READ ONLY]          ║
║                                                                               ║
║  Consensus Bayesian Update → posterior probability                            ║
║  L4 Fast Gate (ev_net = p*qty - cap_in - avg_entry*qty - fees)               ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L4: execution_records (WRITE — 我們的倉位 ONLY)                              ║
║      mode='PAPER'|'LIVE'  ← 區分 paper/live 模式                              ║
║      source='radar'|'ofi' ← 訊號來源                                         ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ASYNC BACKGROUND WORKERS                                                    ║
║                                                                               ║
║  analysis_worker (threading.Thread)                                          ║
║    WRITE: wallet_market_positions (LIFO, 唯一寫入者)                           ║
║    WRITE: insider_score_snapshots                                             ║
║    READ: wallet_observations                                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 兩大啟動器（互斥，不可同時運行）

```
┌─────────────────────────────────────────────────────────────────┐
│  start_shadow_hydration.py  │  run_hft_orchestrator.py            │
│  [純 Observer 預熱工具]       │  [完整實時系統]                    │
│                               │                                  │
│  discovery_loop (T1)         │  Radar → signal_queue              │
│  analysis_worker (T5)         │  OFI   → signal_queue             │
│                               │  Graph → DB                       │
│                               │  SE    ← signal_queue → DB        │
└─────────────────────────────────────────────────────────────────┘
         ▲ DB advisory lock (二選一，不可同時)                       │
         │                                                          │
         └──────── _process_locks table ───────────────────────────┘
```

---

## 3. 變更對照表（相較於原始設計提案）

### 3.1 OFI → Polymarket 市場映射方式

| | 原始設計 (`design.md`) | 最終實作 (`v4-FINAL`) |
|---|---|
| **方式** | `correlation_edges` table 動態查詢 | 靜態 `OFI_MARKET_MAP` dict |
| **原因** | — | Polymarket 市場結構無法用 API 自動映射到 Hyperliquid 標的；`correlation_edges` 表無法提供穩定的市場對應關係 |
| **實作位置** | `signal_engine.py` `_map_ofi_to_polymarket()` | `config/ofi_market_map.py`；映射在 orchestrator `on_shock` callback 中完成 |

**裁決依據（Q4）：** `correlation_edges` 是一張動態圖譜表，用於集群錢包之間的相關性計算，不是為 OFI→Polymarket 市場映射設計。靜態映射更直接、更可控。

### 3.2 OFI 事件是否經過信號引擎共識

| | 原始設計 | 最終實作 |
|---|---|
| **OFI 是否經 Bayesian 共識** | 是（經 `_map_ofi_to_polymarket` + `_process_event`） | 是（統一經 SE） |
| **`hft_execution_gate.py` 去留** | 合併至 SE | **已刪除** |
| **原因** | — | `hft_execution_gate.py` 讓 OFI 繞過 Bayesian 共識，直接進入獨立 L4 gate，違反「所有訊號源必須經共識」的原則 |

**裁決依據（Q1）：** `ShockHandler` 讓 OFI 繞過共識是 invariant violation。

### 3.3 `execution_records` schema 新增欄位

| | 原始設計 | 最終實作 |
|---|---|
| **`mode` 欄位** | 未提及 | `mode TEXT NOT NULL DEFAULT 'PAPER'` |
| **`source` 欄位** | 未提及 | `source TEXT NOT NULL DEFAULT 'radar'` |
| **原因** | — | 區分 paper/live 交易；明確記錄觸發訊號來源（`radar`/`ofi`），利於事後分析 |

**裁決依據（Q2）：** `paper_trades` 表廢除，合併至 `execution_records.mode='PAPER'`。

### 3.4 DB 寫入模式

| | 原始設計 | 最終實作 |
|---|---|
| **`wallet_market_positions` 寫入** | `analysis_worker` 直接 sync 寫入 | 不變 |
| **`insider_score_snapshots` 寫入** | `AsyncDBWriter` | 不變 |
| **`execution_records` 寫入** | 直接 sync 寫入 | 直接 sync 寫入（决策后安全） |
| **WAL 增強** | `PRAGMA journal_mode=WAL` | `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` |
| **原因** | — | Q6 裁決：`synchronous=NORMAL` 在 WAL mode 下提升寫入可靠性同時不犧牲太多效能 |

### 3.5 `pending_entropy_signals` 表的去留

| | 原始設計 | 最終實作 |
|---|---|
| **`pending_entropy_signals` 表** | 保留作為 degraded fallback | **已刪除** |
| **DB polling fallback** | `_poll_db_fallback()` 在 queue timeout 時觸發 | **已移除** |
| **原因** | 5s degraded fallback 作為最後備援 | Q10 裁決：queue 是唯一訊號源，不允許 polling fallback |

**裁決依據（Q10）：** 零延遲架構不能有 polling fallback，否則等於沒做 queue 化。

### 3.6 `signal_engine` 運行模式

| | 原始設計 | 最終實作 |
|---|---|---|
| **運行模式** | `asyncio.create_task` (async task) | 不變 |
| **額外變更** | — | `start_shadow_hydration.py` 中的 SE subprocess 啟動已移除 |
| **原因** | — | Q3 裁決：`start_shadow_hydration.py` 是純 Observer Launcher，不應承擔 SE職責；SE 由 `run_hft_orchestrator.py` 統一管理 |

**裁決依據（Q3）：** `start_shadow_hydration.py` 降級為純 Observer Launcher，SE subprocess 移除。

### 3.7 雙啟動器並發控制

| | 原始設計 | 最終實作 |
|---|---|---|
| **`start_shadow_hydration.py` 和 `run_hft_orchestrator.py` 同時運行** | 未考慮（未被提出） | **DB advisory lock (`_process_locks`)** |
| **衝突場景** | — | 兩者都會啟動 `discovery_loop` subprocess + 寫入同一 DB |
| **解法** | — | `_process_locks` table + `INSERT OR REPLACE`（WAL-safe）；TTL 3600s |

**衝突根因：** 修復過程中發現兩個腳本同時運行導致 `database is locked`。設計階段未預見此並發問題。

---

## 4. 刪除的元件

| 檔案 | 刪除原因 |
|------|----------|
| `panopticon_py/hft/hft_execution_gate.py` | OFI 繞過共識；Invariant violation |
| `panopticon_py/signal_engine._poll_db_fallback()` | Queue 唯一源，禁用 polling fallback |
| `panopticon_py/signal_engine._map_ofi_to_polymarket()` | 映射職責移至 orchestrator `on_shock` |
| `panopticon_py/signal_engine.db.append_paper_trade()` | 已合併至 `execution_records.mode='PAPER'` |
| `panopticon_py/signal_engine.db.upsert_wallet_market_position_lifo()` | Observer/Executor 職責混淆；SE 不可寫 |

---

## 5. 新增的元件

| 檔案 | 用途 |
|------|------|
| `config/__init__.py` | 啟用 `config` 目錄作為 Python package |
| `config/ofi_market_map.py` | 靜態 Hyperliquid → Polymarket 市場 ID 映射 |
| `db.py` `_process_locks` table | DB advisory lock 實作 |
| `db.py` `mode`/`source` columns | `execution_records` 擴充欄位 |
| `docs/start_services_sop.md` Agent Handoff 段落 | 新 Agent 接手流程指南 |

---

## 6. 關鍵決策記錄（6 Q 裁決）

| Q# | 問題 | 裁決 | 影響 |
|----|------|------|------|
| Q1 | `hft_execution_gate.py` 去留 | 刪除；OFI 統一經 SE 共識 | 刪除檔案；統一 L4 gate |
| Q2 | paper_trades 寫入者 | 廢除 `paper_trades` 表，合併至 `execution_records.mode='PAPER'` | Schema 變更 |
| Q3 | `start_shadow_hydration.py` 定位 | 降級純 Observer Launcher；移除 SE subprocess | `start_shadow_hydration.py` 簡化 |
| Q4 | OFI → Polymarket mapping | 靜態 `OFI_MARKET_MAP`；不用 `correlation_edges` | 新增 `config/ofi_market_map.py` |
| Q5 | 兩個 gate 參數一致性 | 統一使用 `fast_gate.py` | 刪除 `hft_execution_gate.py` |
| Q6 | analysis_worker DB 寫入方式 | sync direct write + WAL；`insider_score_snapshots` 可 async | `PRAGMA synchronous=NORMAL` |
| Q10 | pending_entropy_signals 去留 | 刪除；queue 為唯一訊號源 | 移除 DB polling fallback |
| Q11 | signal_engine 運行模式 | asyncio task（非 subprocess） | orchestrator 直接 `create_task` |
| Q12 | wallet_market_positions double-write | SE READ ONLY | SE 移除所有寫入調用 |
| Q13 | execution_records 寫入時機 | decision 後 sync 寫入安全 | AsyncDBWriter 用於 `insider_score_snapshots` |

---

## 7. 驗證清單

- [ ] `python run_hft_orchestrator.py` 啟動無 `ModuleNotFoundError`
- [ ] `python scripts/start_shadow_hydration.py` 單獨運行無 `database is locked`
- [ ] 兩個腳本不可同時運行（advisory lock 強制互斥）
- [ ] OFI shock → `signal_queue.put` → signal engine logs 顯示處理中
- [ ] `execution_records` 新記錄包含 `mode='PAPER'` 和 `source` 欄位
- [ ] entropy drop → `_live_ticks` → `queue.put` → signal engine logs 顯示處理中
- [ ] `signal_engine` 不再寫入 `wallet_market_positions`（驗證：grep 無此調用）
- [ ] `pending_entropy_signals` 表不再被寫入

---

## 8. 待解決的已知限制

| 限制 | 說明 |
|------|------|
| `OFI_MARKET_MAP` 需手動維護 | Polymarket 市場結構不支持自動映射；未來需開發自動化腳本 |
| Redis Pub/Sub 未實作 | 目前 `asyncio.Queue` 僅單進程；多機部署需 Redis |
| Polymarket CLOB order submission 未實作 | Phase 2；`LIVE_TRADING=true` 時無實際下單 |
