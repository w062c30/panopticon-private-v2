# FUNCTION_STATUS — Function Runtime State Index

> Last updated: D125 (2026-05-02)
> Rule (D124): Any function intentionally blocked in production must have an entry here.

---

## Status Markers

| Marker | Meaning |
|--------|---------|
| ✅ ACTIVE | Hot path, running normally |
| ⚙️ LOGGED_ONLY | Executes but only writes log/DB; does not emit signals or trades |
| 🔒 DISABLED_IN_PROD | Code exists but production never reaches this path |
| ⏰ BACKGROUND_{interval} | Background periodic task |
| 🚧 BLOCKED_D{N} | Blocked by a specific patch; reason field is required |

---

## panopticon_py/hunting/run_radar.py

`run_radar.py` 完整列（含 ⚙️ STARTUP_ONLY）見 `panopticon_py/hunting/INDEX.md`。

| Function | Status | Reason | Since |
|---------|--------|--------|-------|
| `_live_ticks()` | ✅ ACTIVE | Main WS event loop | D50 |
| `_synthetic_ticks()` | 🔒 DISABLED_IN_PROD | Only runs in --synthetic mode; production uses `_live_ticks` | D0 |
| `_backward_lookback()` | ⚙️ LOGGED_ONLY | Phase 2 catalyst: writes `series_violations` records, no trade emission | D21 |
| `_fetch_missing_event_names()` | ⏰ BACKGROUND_1H | Called by `metrics_json_loop` every 3600s | D65 |
| `_batch_fill_link_map()` | ⏰ BACKGROUND_STARTUP | One-shot call during `_main_async` startup | D65 |
| `_btc5m_resolve_loop()` | ⏰ BACKGROUND_5M | Resolves BTC 5m window tokens every 300s | D70 |
| `_on_message()` | ✅ ACTIVE | Primary WS message handler; dispatches book/price_change/last_trade_price/best_bid_ask | D50 |
| `_ws_runner()` | ✅ ACTIVE | WS connection manager with 1009 retry backoff; `on_reconnect` calls `ew.mark_reconnect()` | D50 |

---

## panopticon_py/utils/watchdog.py

| Function | Status | Reason | Since |
|---------|--------|--------|-------|
| `run_watchdog()` | ✅ ACTIVE | 30s polling of manifest; circuit breaker protection | D113 |
| `_daemon_double_fork()` | 🔒 DISABLED_IN_PROD | Windows does not support double-fork; production runs in foreground mode | D114 |

---

## run_hft_orchestrator.py

| Function | Status | Reason | Since |
|---------|--------|--------|-------|
| `_process_event()` | ✅ ACTIVE | Main event processor; routes to signal_engine | D50 |
| `run_graph_linker()` | ✅ ACTIVE | Graph linker background task; sets global `_graph_engine` | D50 |
| `main_async()` | ✅ ACTIVE | Orchestrator entry point; acquires singleton, starts all tasks | D50 |
| `graph_engine` (L442) | 🔒 DISABLED_IN_PROD | Dead code: local variable shadowing global; see TECH_DEBT.md Debt-3 | D118 |

---

## panopticon_py/ingestion/analysis_worker.py

| Function | Status | Reason | Since |
|---------|--------|--------|-------|
| `_on_insider_alert()` | ⚙️ LOGGED_ONLY | Writes to `whale_alerts` table; does not trigger trades | D50 |
| `_run_analysis_loop()` | ✅ ACTIVE | Main analysis loop | D50 |

---

## Adding New Entries

When blocking or changing the status of a function:

1. Add entry with exact status marker from the table above
2. Include `reason` field (required for 🚧 BLOCKED_D{N})
3. Include `Since` sprint tag
4. Do NOT rely on code comments alone — this file is the authoritative source

---