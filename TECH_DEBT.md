# TECH_DEBT — Panopticon Technical Debt & Decision Records

> Last updated: D125 (2026-05-02)
> Source: https://github.com/w062c30/panopticon-private-v2

---

## Completed Sprints

| Sprint | Scope | Status |
|--------|-------|--------|
| D108 | PROCESS_VERSION ordering, ShadowDB leak, .gitignore | ✅ |
| D109 | ShadowDB UnboundLocalError, radar startup, _WsConnectionManager | ✅ |
| D110 | token_id=None, code dedup in pol_monitor, _sync_pol_tokens return | ✅ |
| D111 | token_id_no column, _extract_token_ids tuple, upsert/fetch update | ✅ |
| D112 | Named dicts in fetch_active_pol_markets, unified migration pattern | ✅ |
| D113 | sqlite3.Row row_factory globally, unified migration consolidation | ✅ |
| D114 | _add_column_if_missing on_locked, remaining migrations unified | ✅ |
| D115 | AsyncDBWriter task_done fix, SQL injection guard, fetch_open_positions | ✅ |
| D116 | AsyncDBWriter drain sentinel, queue depth monitoring, dict(r) in DAL | ✅ |
| D117 | get_link_mapping_* named, AsyncDBWriter.health(), WAL timing | ✅ |
| D118 | async-writer-health wiring, stop() reentry guard, 7 positional cleanups | ✅ |
| D119 | link_resolver_stats caller audit, 3 JOIN queries named, WS dict(r), cross-process writer health | ✅ |
| D120 | import json fix, utc_now_rfc3339_ms alignment, WS idiom cleanup | ✅ |
| D121 | _on_insider_alert WAL fix, AsyncDBWriter.health TypedDict planning | ✅ |
| D122 | WS format cleanup, book counter guard (reverted in D124) | ✅ |
| D123 | t1_market_clock token freshness, entropy window flush on reconnect | ✅ |
| D124 | UnboundLocalError in _ws_runner, count ALL book events | ✅ |
| D125 | Doc: TECH_DEBT + FUNCTION_STATUS + hunting INDEX; unified radar v1.1.47-D125; `real_trade_ticks_60s` heartbeat | ✅ |
| D126 | Debt-3 graph_engine dead code removed; orchestrator v1.1.36-D126; entropy_fires_60s=0 diagnosis (undersupply, not a bug) | ✅ |

---

## Active Debt Observations

### Debt-1: `_on_insider_alert` uses bare `sqlite3.connect`
**File**: `panopticon_py/ingestion/analysis_worker.py` (TBC)
**Problem**: Directly opens `sqlite3.connect(str(db_obj.path))` bypassing ShadowDB DAL, which means it bypasses WAL mode and `busy_timeout=30000`. Native `sqlite3.connect(timeout=5.0)` will fail under high load.
**Non-blocking**: Not hot path, stable in production.
**Suggestion**: Monitor; if `_on_insider_alert` shows timeout errors under high load, migrate to ShadowDB path.

### Debt-2: `AsyncDBWriter.health()` implicit contract (no TypedDict)
**File**: `panopticon_py/db.py` (AsyncDBWriter), `panopticon_py/api/app.py` (AsyncDBWriterStub)
**Problem**: `db_writer.health()` returns a dict with keys (`running`, `thread_alive`, `queue_depth`, `queue_unfinished`) that exactly match the `AsyncDBWriterStub` fallback dict. This contract is implicit — if `AsyncDBWriter.health()` adds a field, the Stub fallback will not sync.
**Non-blocking**: Both sides currently have matching keys; dashboard runs normally.
**Suggestion**: Define a `TypedDict` or dataclass for `AsyncDBWriterHealth` to make the contract explicit.

### Debt-3: `graph_engine` variable shadowing (dead code)
**File**: `run_hft_orchestrator.py:L444`
**Problem**: `main_async()` had two `graph_engine` definitions:
- L444 (now commented out): `graph_engine = HiddenLinkGraphEngine(db=db)` — local variable, **never used**
- L318 (`run_graph_linker`): `global _graph_engine = HiddenLinkGraphEngine(db=db)` — the real graph engine
**Status (D126)**: RESOLVED — dead-code line commented out with `# Debt-3: removed D126` note.
**Code**: `run_hft_orchestrator.py:L443–L446`

### Debt-4: Blocked functions have no status marker
**File**: `FUNCTION_STATUS.md` (index)
**Problem**: Some functions are intentionally blocked in production but have no machine-readable status. Agent cannot distinguish "broken" from "intentionally disabled" without running code.
**Rule (D124 user requirement)**: Any intentionally blocked function must have an entry in `FUNCTION_STATUS.md` (cross-module) and, for `run_radar.py`, in `panopticon_py/hunting/INDEX.md`.
**See**: `FUNCTION_STATUS.md`, `panopticon_py/hunting/INDEX.md`.

### Debt-5: `real_trade_ticks_60s` semantics must stabilise before entering API schema
**File**: `run_radar.py`, `metrics_schema.py`, `RvfMetricsPanel.tsx`
**Problem**: DR-D125-c records dual-channel double-count risk; field semantics do not yet meet API schema publication standard.
**Non-blocking**: Log output only; dashboard unaffected.
**Unlock condition**: Introduce deduplication key (e.g. `trade_id` or `(asset_id, trade_price, timestamp_ms)`) on `last_trade_price` to confirm no overlap with embedded book trades; run ≥24h baseline confirming `real_trade_ticks_60s / trade_ticks_60s` ratio is stable (~0.5–0.8); then update `metrics_schema.py` + `metrics_collector.py` + `RvfMetricsPanel.tsx` in one PR.
**Blocked by**: DR-D125-c

---

## Decision Records (DR)

### DR-D125-a: Rename FEATURE_INDEX to TECH_DEBT
- **Date**: 2026-05-02
- **Decision**: Renamed root-level `FEATURE_INDEX.md` → `TECH_DEBT.md`. Name change reflects actual content (tech debt tracking + decision records), not a "feature list". Aligns with `AGENTS.md` naming convention (uppercase noun).

### DR-D125-b: Unified radar `PROCESS_VERSION` after handoff drift
- **Date**: 2026-05-02
- **Decision**: Handoff referenced `v1.1.47-D124` while code stayed `v1.1.46-D124`. Canonical radar version unified to **`v1.1.47-D125`** with matching `run/versions_ref.json`.
- **Code**: `panopticon_py/hunting/run_radar.py:L3311`

### DR-D125-c: Dual WS heartbeat counters (`trade_ticks_60s` vs `real_trade_ticks_60s`)
- **Date**: 2026-05-02
- **Decision**: `_ws_trade_count` continues to measure broad WS activity (every `book` + every qualifying `last_trade_price`). Added `_ws_real_trade_count`: increments when `book` carries parsed `embedded_trade_price is not None`, and when `last_trade_price` fires with `trade_size > 0`. **Same physical fill may increment both paths** — acceptable for coverage telemetry; interpret `real_trade_ticks_60s` as an upper-bound proxy, not unique-trade cardinality.
- **Code**: `panopticon_py/hunting/run_radar.py:L2099–L2115`, `L2317–L2319`, `L2460–L2462`, `L3028–L3044`

### DR-D124-a: Count ALL book events for `trade_ticks_60s`
- **Date**: 2026-05-02
- **Decision**: D122 introduced a guard `if not embedded_trade_price` around `_ws_trade_count++`. Polymarket BTC 5m `book` events always carry embedded `last_trade_price`, so the guard excluded all events → `trade_ticks_60s=0`. Reverted to count every book event.
- **Code**: `panopticon_py/hunting/run_radar.py:L2263–L2279`

### DR-D124-b: `_ws_1009_last_failure` must be `global`, not `nonlocal`
- **Date**: 2026-05-02
- **Decision**: `_ws_runner()` declared `_ws_1009_last_failure` in `nonlocal` but never assigned a value inside the function → Python treated it as a local variable expecting an assignment → `UnboundLocalError` crashed the task silently on first iteration. Fixed by changing to `global`.
- **Code**: `panopticon_py/hunting/run_radar.py:L2902`

### DR-D118-a: AsyncDBWriterStub as read-only backend stub
- **Date**: 2026-05-01
- **Decision**: Backend process is read-only; real `AsyncDBWriter` lives in orchestrator. Stub reads orchestrator-written JSON snapshot (`data/async_writer_health.json`) every 30s.
- **Implication**: `/api/async-writer-health` reflects real writer state, not always `running=False`.

### DR-D118-b: `link_resolver_stats` snake_case keys
- **Date**: 2026-05-01
- **Decision**: Migrated `link_resolver_stats()` return keys from camelCase to snake_case (`mapping_count`, `unresolved_count`, `resolved_count`).
- **Breaking Change**: `system_health.py` and `report.py` callers updated to new keys.

### DR-D117: WAL contention timing in `_dispatch`
- **Date**: 2026-04-30
- **Decision**: Added elapsed_ms timing in `AsyncDBWriter._dispatch()`; warn if >200ms to detect WAL contention.

### DR-D126-a: EntropyWindow `window_sec=5.0` not adjusted for POL T2
- **Date**: 2026-05-03
- **Decision**: T2-POL market arrival rate < 1 trade/5s is insufficient to accumulate `min_history_for_z=12` samples in the 5s rolling window. Decision: do not adjust `window_sec`. T2 Smart Money signal relies on Kyle λ + insider score accumulation, not entropy fire triggers. EntropyWindow remains a high-frequency T1 signal generator. If per-tier EntropyWindow parameterization is needed in the future, open a separate DR.
- **Code**: `panopticon_py/hunting/run_radar.py` (global shared `ew` + per-token `_entropy_windows` T1 path)

---