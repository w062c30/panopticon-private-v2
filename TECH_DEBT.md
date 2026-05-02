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
**Problem**: `main_async()` has two `graph_engine` definitions:
- L442: `graph_engine = HiddenLinkGraphEngine(db=db)` — local variable, **never used**
- L318 (`run_graph_linker`): `global _graph_engine = HiddenLinkGraphEngine(db=db)` — the real graph engine
**Non-blocking**: Local `graph_engine` is dead code, no functional impact.
**Suggestion**: Delete L442 assignment, or add `# noqa: F841` if kept for future timing use.

### Debt-4: Blocked functions have no status marker
**File**: `FUNCTION_STATUS.md` (index)
**Problem**: Some functions are intentionally blocked in production but have no machine-readable status. Agent cannot distinguish "broken" from "intentionally disabled" without running code.
**Rule (D124 user requirement)**: Any intentionally blocked function must have an entry in `FUNCTION_STATUS.md`.
**See**: `FUNCTION_STATUS.md` for the live index.

---

## Decision Records (DR)

### DR-D125-a: Rename FEATURE_INDEX to TECH_DEBT
- **Date**: 2026-05-02
- **Decision**: Renamed root-level `FEATURE_INDEX.md` → `TECH_DEBT.md`. Name change reflects actual content (tech debt tracking + decision records), not a "feature list". Aligns with `AGENTS.md` naming convention (uppercase noun).

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

---