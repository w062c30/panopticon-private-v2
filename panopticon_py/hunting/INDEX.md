# Hunting — `run_radar.py` 函數狀態索引

> Last updated: D125 (2026-05-02)
> **Rule**: 若函數被刻意封鎖（`🔒 DISABLED_IN_PROD` / `🚧 BLOCKED_D{N}`），**原因**欄必填。  
> 跨模組彙總（orchestrator / analysis_worker / watchdog）見 repo root：`FUNCTION_STATUS.md`。

---

## 標記語義

| 標記 | 意義 |
|------|------|
| ✅ ACTIVE | 熱路徑，正常運行 |
| ⚙️ LOGGED_ONLY | 執行但只寫 log/DB，不產生 signal 或 trade |
| ⚙️ STARTUP_ONLY | 僅啟動時呼叫一次 |
| 🔒 DISABLED_IN_PROD | 代碼存在但 production 不會到達此路徑 |
| ⏰ BACKGROUND_{interval} | 背景週期任務 |
| 🚧 BLOCKED_D{N} | 被特定 sprint patch 刻意封鎖（原因必填） |

---

## `run_radar.py`

| 函數 | 狀態 | 原因 | 自 |
|------|------|------|----|
| `_live_ticks()` | ✅ ACTIVE | 主 WS 事件迴圈與 `_on_message` | D50 |
| `_ws_runner()` | ✅ ACTIVE | WS 連線與重連、1009 退避；`on_reconnect` → `ew.mark_reconnect()`（entropy 視窗 semantics） | D50 |
| `_on_message()` | ✅ ACTIVE | WS 訊息分派（book / price_change / last_trade_price 等） | D50 |
| `_refresh_active_subscription()` | ✅ ACTIVE | 一般市場訂閱刷新（sync，由 gather 調用） | D30 |
| `_refresh_all_subscriptions()` | ✅ ACTIVE | 併發刷新 T1/T2/T3/T5/POL 後重掛 WS | D42 |
| `_backward_lookback()` | ⚙️ LOGGED_ONLY | Phase 2 預催化劑偵測 — 僅寫 `series_violations`，不產生 trade signal | D21 |
| `_poll_data_api_for_takers()` | ✅ ACTIVE | 週期性以 data API 補 taker / identity | D96 |
| `_poll_single_market_identity()` | ✅ ACTIVE | 單市場 identity 輪詢（async） | D96 |
| `_synthetic_ticks()` | 🔒 DISABLED_IN_PROD | 僅 `--synthetic` 模式；production 永遠走 `_live_ticks` | D0 |
| `_batch_fill_link_map()` | ⚙️ STARTUP_ONLY | 僅 `_main_async()` 啟動執行緒時呼叫一次 | D65 |
| `_fetch_missing_event_names()` | ⏰ BACKGROUND_1H | 由 `_metrics_json_loop` 每 3600s 觸發 | D65 |
| `_metrics_json_loop()` | ⏰ BACKGROUND_5S | RVF snapshot + consensus sync，5s cadence | D76 |
| `_btc5m_resolve_loop()` | ⏰ BACKGROUND_5M | BTC 5m window token 解析 | D70 |
| `_refresh_tier1_tokens()` | ✅ ACTIVE | T1 slug 刷新（含 `market_resolved` 觸發） | D70 |
| `_main_async()` | ✅ ACTIVE | Radar  async 入口：訂閱、任務、`acquire_singleton` | D50 |

---

## 維護約束

- 變更某函數的封鎖／可達性時，**更新本表對應列**（不必每次全表重寫）。  
- 若行為跨檔案（非 `run_radar`），在 `FUNCTION_STATUS.md` 同步新增列。
