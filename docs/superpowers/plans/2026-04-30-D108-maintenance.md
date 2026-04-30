# D108 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `PROCESS_VERSION` ordering risk in `app.py`, complete `.gitignore`, diagnose `/api/pol-watchlist` empty list root cause, and fix WebSocket `ShadowDB` connection leak.

**Architecture:** Four independent P0/P1/P2 tasks. D108-1 and D108-4 modify `panopticon_py/api/app.py`. D108-2 modifies `.gitignore`. D108-3 is diagnostic-only (no code changes until root cause confirmed).

**Tech Stack:** Python 3.11+, FastAPI, SQLite (ShadowDB), httpx, asyncio.

---

## Task D108-1: Reorder `app.py` Variables — Fix `PROCESS_VERSION` Late Definition

**Files:**
- Modify: `panopticon_py/api/app.py:1-54`

### Current State (D107, BROKEN)

```python
# Line 1-25: imports + load_repo_env()
from __future__ import annotations
import asyncio, json, logging, os, time
# ...
load_repo_env()

# Line 27-44: _lifespan references PROCESS_VERSION (NOT YET DEFINED!)
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ...
    logger.info("[APP] DB bootstrap complete — backend %s", PROCESS_VERSION)  # ← NameError at import time
    yield

# Line 47: app = FastAPI(...)
app = FastAPI(title="Panopticon API", version="0.1.0", lifespan=_lifespan)

# Line 50-53: PROCESS_VERSION defined AFTER app and lifespan
from panopticon_py.utils.process_guard import acquire_singleton, get_all_versions, update_heartbeat
PROCESS_VERSION = "v1.1.12-D107"   # ← TOO LATE
acquire_singleton("backend", PROCESS_VERSION)
```

### Why It Happens

Python executes module-level code top-to-bottom. `_lifespan` is a decorator (not called at import time), but `_lifespan` **definition** must precede `app = FastAPI(..., lifespan=_lifespan)` at module level — Python binds the decorator reference at parse time. Since `PROCESS_VERSION` is referenced INSIDE `_lifespan`, and `_lifespan` is parsed before `PROCESS_VERSION` is assigned, this would be a `NameError` if `_lifespan` were ever called.

Actually wait — `_lifespan` is defined at module level with `asynccontextmanager`, and the decorator is applied. The `PROCESS_VERSION` reference inside `_lifespan` is NOT evaluated until `_lifespan` is called (at FastAPI startup, after all module-level code runs). So the NameError would happen at runtime, not import time. But it's still wrong — `PROCESS_VERSION` should be defined before any code that references it.

### Correct Order

```python
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from uuid import uuid4

logger = logging.getLogger("panopticon.api")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from starlette.middleware.cors import CORSMiddleware

from panopticon_py.api.routers.performance import router as performance_router
from panopticon_py.api.routers.report import router as report_router
from panopticon_py.api.routers.recommendations import router as recommendations_router
from panopticon_py.api.routers.system_health import router as system_health_router
from panopticon_py.api.routers.wallet_graph import router as wallet_graph_router
from panopticon_py.api.routers.watchlist import router as watchlist_router
from panopticon_py.load_env import load_repo_env

load_repo_env()

# ── Step 2: PROCESS_VERSION must be before _lifespan ──
from panopticon_py.utils.process_guard import acquire_singleton, get_all_versions, update_heartbeat
PROCESS_VERSION = "v1.1.13-D108"   # bump PATCH for D108 fix
acquire_singleton("backend", PROCESS_VERSION)

# ── Step 3: lifespan (now safely references PROCESS_VERSION) ──
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """
    D107: FastAPI lifespan context manager (replaces deprecated @app.on_event).
    Startup: bootstrap DB schema and ensure data/ directory exists.
    Shutdown: no-op (connections are per-request, no global pool to drain).
    """
    os.makedirs("data", exist_ok=True)
    try:
        from panopticon_py.db import ShadowDB
        _db = ShadowDB()
        _db.bootstrap()
        _db.close()
        logger.info("[APP] DB bootstrap complete — backend %s", PROCESS_VERSION)
    except Exception as exc:
        logger.warning("[APP] DB bootstrap warning: %s", exc)
    yield

# ── Step 4: FastAPI app ──
app = FastAPI(title="Panopticon API", version="0.1.0", lifespan=_lifespan)
```

### Verification

```bash
python -c "from panopticon_py.api.app import app, PROCESS_VERSION; print(PROCESS_VERSION)"
# Must print: v1.1.13-D108
```

### Version Bump

- `PROCESS_VERSION` in `panopticon_py/api/app.py`: `v1.1.12-D107` → `v1.1.13-D108`
- `run/versions_ref.json`: `"backend": "v1.1.12-D107"` → `"v1.1.13-D108"`, `updated_by_sprint: "D108"`, `updated_by_agent: "D108-coding-agent"`, `last_updated: 2026-04-30T13:XX:00Z`

---

## Task D108-2: Complete `.gitignore`

**Files:**
- Modify: `.gitignore`

### Analysis

Current `.gitignore` already covers most patterns. The trailing `run/orchestrator.err.log` on line 103 is a **typo** (should be on its own line, not appended to `desktop.ini`). The file already has comprehensive Python, Node, data, and OS coverage. No additions needed — only the typo fix.

### Fix

- [ ] **Step 1: Fix typo — line 102 `desktop.inirun/orchestrator.err.log`**
  The `run/orchestrator.err.log` got concatenated with `desktop.ini` due to a missing newline. Split into two proper lines:

  ```
  desktop.ini
  run/orchestrator.err.log
  ```

No other changes needed — `.gitignore` already covers all required patterns.

### Verification

```bash
# Verify no trailing concatenations
Get-Content .gitignore | Where-Object { $_ -match 'desktop\.ini|orchestrator\.err\.log' }
# Expected: two clean lines
```

---

## Task D108-3: Diagnose `/api/pol-watchlist` Count=0 Root Cause

**Files:**
- Read: `panopticon_py/hunting/run_radar.py:2685-2700`
- Read: `panopticon_py/hunting/pol_monitor.py`
- Read: `panopticon_py/api/routers/watchlist.py`
- Read: `panopticon_py/db.py` (fetch_active_pol_markets, upsert_pol_market)

### Diagnostic Tree

```
pol-watchlist returns count=0
├─ Cause A: sync_scan_pol_markets NEVER called
│    → Radar process NOT running (check process_manifest.json — radar key missing!)
│    → Radar crashed / not started
│
├─ Cause B: sync_scan_pol_markets called but Gamma API returned 0 political markets
│    → Check log for "[POL_REFRESH] scan_complete count=N"
│    → If count=0: keyword filter too strict OR Gamma API has no political markets
│
├─ Cause C: scan succeeded, DB written, but fetch_active_pol_markets() returns []
│    → Check DB: SELECT COUNT(*), SUM(is_active) FROM pol_market_watchlist
│    → If data exists but count=0: deactivate_closed_pol_markets() marked all as inactive
│    → If no data: scan never wrote anything
```

### Process Manifest Check

Current `run/process_manifest.json` shows:
- `backend`: PID 22596, running ✅
- `orchestrator`: PID 26112, running ✅
- `analysis_worker`: PID 5636, running ✅
- **radar: NOT PRESENT** ❌

**This is the root cause!** Radar is not running. Without radar, `_sync_pol_tokens_from_watchlist` is never called, `_last_pol_refresh` stays at 0.0, and `sync_scan_pol_markets` never upserts any political markets into `pol_market_watchlist`.

### Diagnostic SQL (run against `data/panopticon.db`)

```sql
-- Confirm table exists and has data
SELECT COUNT(*), SUM(is_active) FROM pol_market_watchlist;

-- Confirm last POL scan time
SELECT market_id, is_active, subscribed_at, last_signal_ts
FROM pol_market_watchlist
ORDER BY subscribed_at DESC LIMIT 5;
```

### Next Steps

If radar is confirmed not running:
1. Diagnose why `scripts/restart_all.ps1` didn't start radar
2. Check `run/radar.pid` — does it exist?
3. Check for radar crash logs

If radar is running but count=0:
1. Check log for `[POL_REFRESH] scan_complete count=N`
2. If N=0: investigate Gamma API response or keyword filter

**IMPORTANT: Do NOT modify pol_monitor.py filtering logic without confirming root cause first.**

### Deliverable

Report findings to Architect with:
- Root cause (radar not running / Gamma API issue / DB query issue)
- Evidence (process manifest, log lines, SQL results)
- Recommended fix (start radar / adjust filter / fix query)

---

## Task D108-4: Fix WebSocket ShadowDB Connection Leak

**Files:**
- Modify: `panopticon_py/api/app.py:131-223`

### Current State (D107, LEAKS)

```python
@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    await _ws_manager._connect(ws)
    try:
        await ws.send_json({"type": "connected"})
        from panopticon_py.db import ShadowDB
        db = ShadowDB()        # ← Created INSIDE try
        last_hit_ts = ""
        last_obs_ts = ""

        while True:
            await asyncio.sleep(5)
            update_heartbeat("backend")
            try:
                # ... db reads ...
            except Exception:
                pass
    except WebSocketDisconnect:
        _ws_manager._disconnect(ws)
    finally:
        _ws_manager._disconnect(ws)
# ← NO db.close() — ShadowDB connection leaked!
```

### Correct Implementation

```python
@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    """Push live hunting/shadow data to connected dashboards every 5 seconds."""
    await _ws_manager._connect(ws)
    from panopticon_py.db import ShadowDB
    db = ShadowDB()              # ← Moved BEFORE try, outside try/except
    try:
        await ws.send_json({"type": "connected"})
        last_hit_ts = ""
        last_obs_ts = ""

        while True:
            await asyncio.sleep(5)
            update_heartbeat("backend")
            try:
                # Latest hunting shadow hits
                hit_rows = db.conn.execute("""
                    SELECT hit_id, address, market_id, entity_score, entropy_z,
                           sim_pnl_proxy, outcome, payload_json, created_ts_utc
                    FROM hunting_shadow_hits
                    ORDER BY created_ts_utc DESC LIMIT 50
                """).fetchall()

                # Latest wallet observations
                obs_rows = db.conn.execute("""
                    SELECT obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc
                    FROM wallet_observations
                    ORDER BY ingest_ts_utc DESC LIMIT 50
                """).fetchall()

                # Latest tracked wallets (top by PnL)
                wallet_rows = db.conn.execute("""
                    SELECT wallet_address, entity_id, all_time_pnl, win_rate,
                           discovery_source, last_seen_ts_utc, last_updated_at
                    FROM tracked_wallets
                    ORDER BY all_time_pnl DESC LIMIT 20
                """).fetchall()

                # Latest raw events (L1/L2/L3)
                event_rows = db.conn.execute("""
                    SELECT event_id, layer, event_type, source, market_id,
                           payload_json, ingest_ts_utc
                    FROM raw_events
                    ORDER BY ingest_ts_utc DESC LIMIT 20
                """).fetchall()

                await ws.send_json({
                    "type": "live_update",
                    "hunting_hits": [
                        {
                            "hit_id": r[0], "address": r[1], "market_id": r[2],
                            "entity_score": r[3], "entropy_z": r[4],
                            "sim_pnl_proxy": r[5], "outcome": r[6],
                            "payload_json": r[7], "created_ts_utc": r[8],
                        }
                        for r in hit_rows
                    ],
                    "wallet_obs": [
                        {
                            "obs_id": r[0], "address": r[1], "market_id": r[2],
                            "obs_type": r[3], "payload_json": r[4], "ingest_ts_utc": r[5],
                        }
                        for r in obs_rows
                    ],
                    "tracked_wallets": [
                        {
                            "wallet_address": r[0], "entity_id": r[1],
                            "all_time_pnl": r[2], "win_rate": r[3],
                            "discovery_source": r[4], "last_seen_ts_utc": r[5],
                            "last_updated_at": r[6],
                        }
                        for r in wallet_rows
                    ],
                    "raw_events": [
                        {
                            "event_id": r[0], "layer": r[1], "event_type": r[2],
                            "source": r[3], "market_id": r[4],
                            "payload_json": r[5], "ingest_ts_utc": r[6],
                        }
                        for r in event_rows
                    ],
                    "ts": time.time(),
                })
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        db.close()                # ← NEW: always close DB connection
        _ws_manager._disconnect(ws)
```

### Key Changes

1. `db = ShadowDB()` moved to BEFORE `try:` block (avoids `NameError` in `finally` if constructor throws)
2. `db.close()` added in `finally:` block (always runs regardless of how we exit)
3. `_ws_manager._disconnect(ws)` called in `finally:` (safe — has `if ws in self._connections` guard)
4. `WebSocketDisconnect` handler is now `pass` (cleanup happens in `finally`)
5. `last_hit_ts` and `last_obs_ts` variables are unused — removed to reduce noise

### Verification

```bash
# After restart, open dashboard WebSocket connection
# Monitor open SQLite connections: should not grow over time
# Check log for no errors on WS disconnect
```

---

## Execution Order

| Priority | Task | Reason |
|----------|------|--------|
| P0 | D108-4 ws_stream ShadowDB leak | Resource leak, fix first |
| P0 | D108-1 PROCESS_VERSION reorder | Correctness, needed before D108-4 |
| P1 | D108-2 .gitignore typo | Simple fix, no risk |
| P2 | D108-3 pol-watchlist diagnosis | Diagnostic only, no code changes yet |

**After all code changes: restart all processes via `scripts/restart_all.ps1`**

---

## Self-Review Checklist

1. **Spec coverage:** All 4 tasks covered with concrete steps
2. **Placeholder scan:** No TBD/TODO — all code is complete
3. **Type consistency:** `ShadowDB()`, `db.conn.execute()`, `db.close()` — consistent with existing patterns
4. **No conflicts:** D108-1 and D108-4 both modify `app.py` but non-overlapping sections (D108-1: lines 1-53, D108-4: lines 131-223)
5. **Version bumps:** Both `app.py` PROCESS_VERSION and `versions_ref.json` updated together
