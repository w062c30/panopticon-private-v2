# Signal Engine Architecture Review — Reviewer Brief

**Date:** 2026-04-23
**Author:** Panopticon Agent
**Status:** Pending Review — Please evaluate and advise on unification

---

## Reviewer Task

> Evaluate the current codebase architecture (as-is) vs. the proposed design (to-be) vs. the final corrected architecture (final). Identify any remaining gaps, contradictions, or better approaches. Then advise on the correct unified structure before any code is rewritten.

---

## 一、現有所有追蹤軌跡（Tracks）

| # | Track 名稱 | 運行位置 | 入口檔案 | 當前職責 | 寫入 DB 表格 | 讀取來源 |
|---|---|---|---|---|---|---|
| T1 | **Discovery Loop** | `subprocess.Popen` | `panopticon_py/hunting/discovery_loop.py` | 發現高分錢包：Track A (Gamma CLOB takers) + Track B (Polymarket Leaderboard) | `discovered_entities`, `tracked_wallets`, `wallet_funding_roots`, `wallet_observations`, `hunting_shadow_hits` | Gamma API, Moralis, Polymarket Leaderboard API |
| T2 | **Polymarket Radar** | `asyncio` task (orchestrator) | `panopticon_py/hunting/run_radar.py` | WebSocket 監聽 CLOB orderbook entropy；每30s Data API polling 抓 taker addresses | `wallet_observations`, `hunting_shadow_hits`, **`pending_entropy_signals`** (問題!) | Polymarket WS, Gamma markets API, Data API |
| T3 | **Hyperliquid OFI Engine** | `asyncio` task (orchestrator) | `panopticon_py/hft/hyperliquid_ws_client.py` | 監聽 BTC-USD OFI shock；觸發 ShockHandler | **無 DB 寫入** | Hyperliquid WebSocket |
| T4 | **Graph Linker** | `asyncio` task (orchestrator) | `panopticon_py/hft/graph_linker.py` | HFT 錢包集群；計算 correlation_edges | `correlation_edges` (via `persist_clusters`) | `wallet_observations`, `wallet_funding_roots` |
| T5 | **Analysis Worker** | `threading.Thread` (subprocess) | `panopticon_py/ingestion/analysis_worker.py` | 對 recent wallets 評分；**更新 wallet_market_positions (LIFO)** | `insider_score_snapshots`, **`wallet_market_positions`** | `wallet_observations` (polling, 每25s) |
| T6 | **Signal Engine (v1)** | `subprocess.Popen` | `panopticon_py/signal_engine.py` | 共識貝葉斯決策；讀取 entropy 信號 | `execution_records` | `pending_entropy_signals` (polling), `wallet_observations`, `insider_score_snapshots`, `wallet_market_positions` |

---

## 二、現有進程架構（Orchestrator 視角）

```
run_hft_orchestrator.py (main_async)
│
├── asyncio tasks (共享同一 event loop + 同一 DB):
│   ├── run_polymarket_radar()       ← T2: _live_ticks(ew, db)
│   │       └── _live_ticks() → ws → entropy → db.append_pending_entropy_signal()
│   │                                                  → db.append_hunting_shadow_hit()
│   │                                                  → db.append_wallet_observation()
│   ├── run_hyperliquid_ofi()        ← T3: HyperliquidOFIEngine(on_shock=ShockHandler)
│   │       └── ShockHandler.got_shock() → hft_execution_gate.py (獨立的 L4 gate!)
│   │           └── submit_order_to_ts() (Phase 2)
│   └── run_graph_linker()            ← T4: HiddenLinkGraphEngine
│
├── subprocess (隔離進程):
│   └── discovery_loop.py             ← T1: asyncio run_discovery_cycle() (每2h一次)
│
└── (Signal Engine v1: subprocess — 計劃整合進 orchestrator as async task)
```

---

## 三、各進程間數據依賴關係（誰讀誰寫）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  WRITERS (寫入者)                                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  discovery_loop.py                                                           │
│    → discovered_entities                                                     │
│    → tracked_wallets                                                        │
│    → wallet_funding_roots                                                   │
│    → wallet_observations (type: clob_trade)                                  │
│    → hunting_shadow_hits                                                    │
│                                                                              │
│  run_radar.py (_live_ticks)                                                │
│    → wallet_observations (type: entropy_drop)                              │
│    → hunting_shadow_hits                                                    │
│    → pending_entropy_signals  ⚠ [待移除，改為 queue.put]                    │
│                                                                              │
│  analysis_worker.py (_tick)                                                 │
│    → wallet_market_positions  ✓ [唯一正確的 Observer 寫入者]                │
│    → insider_score_snapshots                                                 │
│                                                                              │
│  graph_linker.py (persist_clusters)                                        │
│    → correlation_edges                                                      │
│                                                                              │
│  signal_engine.py (_process_event)                                          │
│    → execution_records  ✓                                                    │
│    → paper_trades    ✗ [待移除！Observer 不該寫]                          │
│                                                                              │
│  hft_execution_gate.py (ShockHandler.got_shock)                             │
│    → (目前無 DB 寫入，直接調用 submit_order_to_ts Phase 2)                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  READERS (讀取者)                                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  signal_engine._process_event()                                             │
│    ← pending_entropy_signals  [polling, 待改 queue]                        │
│    ← wallet_observations                                                    │
│    ← insider_score_snapshots                                                │
│    ← wallet_market_positions  [讀取代價基礎]                                │
│                                                                              │
│  analysis_worker._tick()                                                    │
│    ← wallet_observations   [polling 每25s]                                 │
│                                                                              │
│  graph_linker.py                                                           │
│    ← wallet_observations                                                    │
│    ← wallet_funding_roots                                                   │
│                                                                              │
│  run_radar._live_ticks()                                                   │
│    ← Polymarket WS (即時)                                                   │
│    ← Gamma API (markets list)                                               │
│    ← Data API (taker trades polling)                                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 四、現有兩條獨立的 L4 Gate 實現

| | `fast_gate.py` | `hft_execution_gate.py` |
|---|---|---|
| **觸發條件** | `signal_engine` 共識決策 | Hyperliquid OFI shock |
| **輸入** | `FastSignalInput(p_prior, quote_price, ...)` | `ShockHandler.got_shock()` 直接計算 |
| **貝葉斯共識** | ✓ 有 | ✗ 沒有（直接進 gate） |
| **LIFO avg_entry** | ✓ `avg_entry_price` 欄位 | ✗ 沒有 |
| **寫入 execution_records** | ✓ signal_engine 寫入 | ✗ 不寫入 |
| **職責** | Signal Engine 的 L4 把關 | OFI Flash 交易的 L4 把關 |

**關鍵問題**: `hft_execution_gate` 繞過了共識貝葉斯決策和 LIFO 成本計算，兩條路徑完全獨立。

---

## 五、現有錢包觀測寫入時機（誰在什麼時候寫入 wallet_observations）

| 來源 | `obs_type` | 何時寫入 | 寫入內容 |
|---|---|---|---|
| `discovery_loop.py` | `clob_trade` | 每次 REST trades fetch | taker address, side, size, price |
| `run_radar.py` | `entropy_drop` | entropy fire 時 | z-score, virtual_entities, market_id |
| `run_radar.py` (`_poll_data_api_for_takers) | `clob_trade` | 每30s polling | taker proxyWallet, side, size, price |
| (TBD) | (待新增: OFI taker) | OFI shock 後500ms | 待整合 |

**analysis_worker 只讀取 `obs_type == "clob_trade"` 的記錄來維護 `wallet_market_positions`。**

---

## 六、pending_entropy_signals 的產生和消費（待移除）

```
產生者: run_radar.py (_live_ticks, line 323)
        db.append_pending_entropy_signal({signal_id, market_id, token_id,
                                         entropy_z, trigger_address, trigger_ts_utc})

消費者: signal_engine.py (_main_loop, line 219)
        signals = db.fetch_unconsumed_entropy_signals(limit=20)
        → 5s polling迴圈
        → 處理後標記 consumed

設計意圖: asyncio.Queue 零延遲
現狀差距: 仍是 DB 輪詢 (5s interval)
```

---

## 七、職責混淆的具體位置

| 位置 | 錯誤行為 | 正確行為 |
|---|---|---|
| `signal_engine.py` line 361-368 | `db.upsert_wallet_market_position_lifo(...)` — 我們的交易不該更新 Insider 的倉位 | 移除；Observer (analysis_worker) 獨有寫入 |
| `signal_engine.py` line 349-360 | `db.append_paper_trade(...)` — 我們的模擬交易不該寫入 | 移除或限制於獨立的 paper trade 記錄系統 |
| `hft_execution_gate.py` | `ShockHandler` 完全繞過 signal_engine 共識和 LIFO | 統一經過 signal_engine 的 async task |

---

## 八、asyncio.Queue 事件契約（待實作）

```python
@dataclass
class SignalEvent:
    source: str              # "radar" | "ofi" | "db_poll_fallback"
    market_id: str          # Polymarket market_id
    token_id: str | None
    entropy_z: float | None = None   # Polymarket 來源
    ofi_shock_value: float | None = None  # Hyperliquid OFI 來源
    trigger_address: str = "system"
    trigger_ts_utc: str | None = None
```

**生產者**:
- Radar `_live_ticks`: `await queue.put(SignalEvent(source="radar", ...))`
- OFI `on_shock`: `await signal_queue.put(SignalEvent(source="ofi", ...))`

**消費者**:
- Signal Engine `_run_async(queue, db)`: `event = await queue.get()`

---

## 九、Orchestrator 整合點（待重構）

```
signal_queue: asyncio.Queue[SignalEvent]  ← 全域共享

on_shock callback (目前):
    async def on_shock(shock):
        logger.info(...)  ← 只打日誌
        # TODO: 沒有寫入 signal_queue

on_shock callback (待改):
    async def on_shock(shock):
        await signal_queue.put(SignalEvent(
            source="ofi",
            market_id=shock.market_id,  # Hyperliquid BTC-USD
            ofi_shock_value=shock.ofi_value,
            trigger_address="hyperliquid",
            trigger_ts_utc=datetime.now(timezone.utc).isoformat(),
        ))
```

---

## 十、完整架構對照圖（現狀 vs 設計意圖 vs 最終修正）

### 10.1 現狀架構圖（CURRENT — 有問題）

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  L1: PERCEPTION (感知層)                                                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  [Hyperliquid WS]────► HyperliquidOFIEngine(on_shock=ShockHandler)          ║
║                                   │                                         ║
║                                   ▼                                         ║
║                        ShockHandler.got_shock()                               ║
║                        → hft_execution_gate.py (獨立的L4)                    ║
║                        → submit_order_to_ts() (Phase 2)                      ║
║                                   ⚠ 繞過貝葉斯共識!                           ║
║                                                                               ║
║  [Polymarket WS]────► _live_ticks()                                        ║
║                                   │                                         ║
║                                   ▼                                         ║
║              db.append_pending_entropy_signal()  ⚠ DB寫入! (5s後才被讀)    ║
║              db.append_hunting_shadow_hit()                                  ║
║              db.append_wallet_observation()                                   ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L2/3: signal_engine (subprocess, 每5s輪詢db)                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  while True:                                                                 ║
║    signals = db.fetch_unconsumed_entropy_signals()  ◄── 5s輪詢!              ║
║    for sig in signals:                                                        ║
║      consensus_bayesian()                                                      ║
║      L4_fast_gate()                                                          ║
║      db.append_execution_records()      ✓                                     ║
║      db.append_paper_trades()          ✗ Observer不該寫!                      ║
║      db.upsert_wallet_market_pos_lifo() ✗ Observer/Executor混淆!            ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ASYNC WORKERS                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  analysis_worker (threading, 每25s)                                          ║
║    for addr in wallets:                                                      ║
║      obs = fetch_recent_wallet_obs(addr)                                     ║
║      upsert_wallet_market_position_lifo()  ✓ 正確(Observer唯一寫入者)        ║
║      rank_insider() → insider_score_snapshots                                ║
║                                                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 10.2 設計意圖架構圖（PROPOSED — 修正了核心問題）

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  L1: PERCEPTION (感知層) — 零延遲事件源                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  Hyperliquid WS ──┐                                                          ║
║                   ├──► asyncio.Queue[SignalEvent] ⚡ ZERO I/O LATENCY        ║
║  Polymarket WS ───┘        │                                                   ║
║                             ▼                                                   ║
║              signal_engine (_run_async, asyncio task)                          ║
║                             │                                                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L2/3: COGNITION + DECISION                                                ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  READ wallet_observations (last 60s)              O(1)                   ║
║  READ insider_score_snapshots (score ≥ 0.55)       O(1)                   ║
║  READ wallet_market_positions (LIFO avg_entry)       O(1)                   ║
║      ⚠ READ ONLY — NEVER WRITE HERE                                          ║
║  Consensus Bayesian Update → posterior                                        ║
║  L4 Fast Gate → ev_net = p_adj*qty - cap_in - avg_entry*qty - fees         ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L4: EXECUTION                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  WRITE execution_records (our system trades ONLY)                           ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ASYNC BACKGROUND WORKERS (Observer — 嚴格隔離)                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  analysis_worker                                                            ║
║    for each clob_trade obs:                                                  ║
║        upsert_wallet_market_position_lifo()  ✓ 唯一寫入者                    ║
║    rank_insider() → insider_score_snapshots                                  ║
║                                                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 10.3 最終確認架構圖（FINAL — 經過用戶3大修正後）

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  L1: PERCEPTION LAYER (感知層) — 零延遲事件源                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   ┌────────────────────┐          ┌────────────────────────────────────┐    ║
║   │ Hyperliquid WS    │──────────►│ asyncio.Queue[SignalEvent]           │    ║
║   │ BTC-USD OFI      │ immediate  │  ⚡ ZERO disk I/O latency             │    ║
║   └────────────────────┘ put()    │                                    │    ║
║                                   │  source="ofi"                       │    ║
║   ┌────────────────────┐          │  ofi_shock_value=0.042             │    ║
║   │ Polymarket WS      │──────────►│  market_id="BTC-USD"               │    ║
║   │ CLOB book ticks   │ immediate  │                                    │    ║
║   └────────────────────┘ put()    └──────────────┬────────────────────┘    ║
║                                                   │                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L2/L3: COGNITION + DECISION LAYER (signal_engine async task)              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   async def _run_async(queue, db):                                          ║
║       event = await queue.get()   # 阻塞直到事件到達                          ║
║       _process_event(event, db):                                             ║
║                                                                               ║
║   _process_event:                                                            ║
║   ┌─────────────────────────────────────────────────────────────────────┐     ║
║   │ 1. READ wallet_observations (last 60s, same market)   O(1)       │     ║
║   │ 2. READ insider_score_snapshots (score ≥ 0.55)       O(1)       │     ║
║   │ 3. READ wallet_market_positions (LIFO avg_entry)      O(1)       │     ║
║   │    ⚠ READ ONLY — NEVER WRITE HERE                                 │     ║
║   │                                                                     │     ║
║   │ 4. OFI shock: map market_id via correlation_edges                  │     ║
║   │    → "BTC-USD" → Polymarket correlated market                      │     ║
║   │                                                                     │     ║
║   │ 5. Consensus Bayesian Update                                      │     ║
║   │    posterior = geometric_mean_LR(sources)                        │     ║
║   │                                                                     │     ║
║   │ 6. L4 Fast Gate                                                  │     ║
║   │    ev_net = p_adj*qty - capital_in - avg_entry*qty - fees         │     ║
║   └─────────────────────────────────────────────────────────────────────┘     ║
║                               │                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  L4: EXECUTION LAYER                                                        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   WRITE execution_records (our system trades ONLY)                            ║
║       accepted=1, reason=PASS → signal accepted                              ║
║       accepted=0, reason=GATE_ABORT → signal rejected                        ║
║                                                                               ║
║   LIVE_TRADING=true → submit EIP-712 FOK to Polymarket CLOB (Phase 2)      ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ASYNC BACKGROUND WORKERS (Observer — 嚴格隔離)                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   analysis_worker (threading, 每25s)                                         ║
║   ┌─────────────────────────────────────────────────────────────────────┐    ║
║   │ for addr in wallet_observations (last 30 wallets):               │    ║
║   │     for o in fetch_recent_wallet_observations(addr):              │    ║
║   │         if o.obs_type == "clob_trade":                          │    ║
║   │             # ⚠ Observer — 唯一可寫入 wallet_market_positions  │    ║
║   │             upsert_wallet_market_position_lifo(                  │    ║
║   │                 side=BUY/SELL, price, qty, timestamp)            │    ║
║   │             # LIFO: SELL 先扣最新 BUY，avg_entry 不變             │    ║
║   │     feats = aggregate_from_observations(obs)                       │    ║
║   │     score, reasons = rank_insider(feats)                          │    ║
║   │     writer.submit("insider_score", {...})                         │    ║
║   └─────────────────────────────────────────────────────────────────────┘    ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  run_hft_orchestrator.py — 單一整合點                                        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   signal_queue: asyncio.Queue[SignalEvent] = asyncio.Queue()                  ║
║                                                                               ║
║   async def on_shock(shock):                                                 ║
║       await signal_queue.put(SignalEvent(                                    ║
║           source="ofi",                                                      ║
║           market_id=shock.market_id,                                        ║
║           ofi_shock_value=shock.ofi_value,                                  ║
║           trigger_address="hyperliquid",                                      ║
║           trigger_ts_utc=datetime.now(timezone.utc).isoformat(),            ║
║       ))                                                                   ║
║                                                                               ║
║   OFI engine: HyperliquidOFIEngine(on_shock=on_shock)                      ║
║   Radar: _live_ticks(ew, db) → 重構接受 queue.put 回調                      ║
║   SE task: asyncio.create_task(_run_async(signal_queue, db))                 ║
║                                                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DELETED: hft_execution_gate.py (ShockHandler) — OFI統一經SE共識            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   DELETE: pending_entropy_signals DB polling — 改為 asyncio.Queue             ║
║   DELETE: signal_engine寫入wallet_market_positions                            ║
║   DELETE: signal_engine寫入paper_trades                                      ║
║   DELETE: signal_engine subprocess → 改為 asyncio task                       ║
║                                                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 十一、差距分析表

| 元件 | 現狀 (Current) | 設計意圖 (Design) | 差距 (Gap) |
|---|---|---|---|
| **Radar → SE 信號傳遞** | `db.append_pending_entropy_signal()` (DB寫入) | `asyncio.Queue.put(SignalEvent)` (記憶體) | **Gap 1**: 仍是磁碟I/O，非零延遲 |
| **signal_engine 運行模式** | Subprocess (`subprocess.Popen`) | Async task (`asyncio.create_task`) 在同一個 event loop | **Gap 2**: 仍是 subprocess，無法共享 queue |
| **OFI → SE 信號** | `HyperliquidOFIEngine(on_shock=ShockHandler)` 繞過共識 | `HyperliquidOFIEngine(on_shock=lambda: queue.put(OFI_event))` | **Gap 3**: OFI 完全繞過 signal_engine |
| **wallet_market_positions 更新** | `analysis_worker` ✓ 正確；`signal_engine` ✗ 也寫入 | 只有 `analysis_worker` 可以寫入 | **Gap 4**: 職責混淆已部分修復 |
| **L4 Gate** | `fast_gate.py` + `hft_execution_gate.py` 兩套獨立實現 | 統一使用 `fast_gate.py` + `avg_entry_price` | **Gap 5**: OFI 路徑繞過貝葉斯共識 |
| **execution_records** | `signal_engine` 寫入 ✓ | `signal_engine` 獨有寫入者 ✓ | **已修復** |
| **paper_trade** | `signal_engine` 寫入 ✗ | `signal_engine` **禁止**寫入 | **已修復** |

---

## 十二、待重構清單

| # | 檔案 | 改動 |
|---|---|---|
| A | `run_hft_orchestrator.py` | 創建 `signal_queue`；將 `on_shock` callback 改為 `queue.put`；將 SE 改為 `asyncio.create_task`；重構 `run_polymarket_radar` 接受 `queue.put` 回調 |
| B | `run_radar.py` | 重構 `_live_ticks()` 接受 `asyncio.Queue` 作為參數；移除 `db.append_pending_entropy_signal()` 改為 `queue.put(SignalEvent(...))` |
| C | `hft_execution_gate.py` | **移除** — OFI 路徑統一使用 `signal_engine` 的 L4 Fast Gate，不再需要獨立的 `ShockHandler` |
| D | `signal_engine.py` | 暴露 `_run_async(queue, db)` 作為 async task 入口；`main()` 僅保留 subprocess 兼容模式 |
| E | `analysis_worker.py` | **保持不變** — 已是正確的 Observer 實現（唯一寫入 `wallet_market_positions` 的地方） |
| F | `start_shadow_hydration.py` | 移除 `signal_engine` subprocess（已整合進 orchestrator）；僅保留 discovery + radar + analysis_worker |

---

## 十三、Reviewer 請回答以下問題

1. **hft_execution_gate.py 的去留**: `ShockHandler` 目前是完全繞過 signal_engine 的獨立 L4 gate實現。如果刪除它，OFI shock 是否應該統一經過 signal_engine 的共識決策？還是目前「OFI = 快速直行」和「Polymarket共識 = 慢速」的分離是合理的？

2. **paper_trade 的寫入者**: `execution_records` 記錄「我們系統的倉位」，`paper_trades` 記錄「我們的模擬交易歷史」。如果 signal_engine 不該寫 `paper_trades`，那麼 paper 模式的倉位變更應該記錄在哪裡？還是需要新建一個獨立的 paper positions 表？

3. **start_shadow_hydration.py 的定位**: 如果 signal_engine 整合進了 `run_hft_orchestrator.py`（asyncio task），那麼 `start_shadow_hydration.py` 是否應該完全棄用？還是它仍需要一個簡化版的 signal_engine 實例？

4. **OFI → Polymarket correlation**: OFI shock 是基於 Hyperliquid BTC-USD 的市場數據，如何準確映射到 Polymarket 的相關市場？`correlation_edges` 表目前是否已有足夠的數據支持這個映射？還是需要一個固定的映射表？

5. **兩個 gate 的參數一致性**: `fast_gate.py` 和 `hft_execution_gate.py` 對同一個風險指標（EV、slippage、Kelly cap）使用不同的參數。如果合併，是否應該以哪一個為準？

6. **職責隔離的邊界**: analysis_worker 更新 `wallet_market_positions` 是通過直接 DB 寫入（`self.db.upsert_wallet_market_position_lifo()`），而不是通過 `AsyncDBWriter` queue。這個設計是否有意為之（確保同步寫入）？還是應該改成異步 queue？

---

_請在以上問題上給出裁決，我將根據您的答案執行最終的代碼重寫。_
