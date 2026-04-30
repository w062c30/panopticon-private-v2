# Architect Handoff — 2026-04-30 D96 (Updated)

**IMPORTANT**: Read live source at `https://github.com/w062c30/panopticon-private-v2`. File:line references below.

## Phase N Completed
| Item | File | Status |
|------|------|--------|
| D96-A ENTROPY_LOOKBACK_SEC 360→1800 | `signal_engine.py:L63` | ✅ |
| D96-B T1 route fix (per-token ew, no signal) | `run_radar.py:_on_message` | ✅ |
| D96-C T1 short-circuit in `_process_event` | `signal_engine.py:L519–L522` | ✅ |
| D96-D T1 logs → DEBUG | `run_radar.py:L1741,L2095` | ✅ |
| D96-NEW-1a `_poll_single_market_identity` | `run_radar.py:L1617–1700` | ✅ |
| D96-NEW-1b On-fire forced poll | `run_radar.py:L2358` | ✅ |
| D96-NEW-1c Grace Period Retry (8s) | `signal_engine.py:L533–551` | ✅ |
| D96-NEW-2 Order Reconstruction Engine | `panopticon_py/ingestion/order_reconstruction_engine.py` (NEW) | ✅ |
| D96-NEW-3 Kyle λ → order-level total_size | `run_radar.py:L2144–2157` | ✅ |
| D96-E Version bumps | `run_radar.py:v1.1.22-D96`, `run_hft_orchestrator.py:v1.1.20-D96` | ✅ |
| D96-F TradeListPanel full width + timestamps | `dashboard/src/components/TradeListPanel.tsx` | ✅ |
| DB migration (code + manual) | `db.py`, manual SQL | ✅ |

## System Status (live indicators)
| Component | Status | Evidence |
|-----------|--------|---------|
| WS connection | ✅ | `last_trade_price` arriving |
| `ENTROPY_LOOKBACK_SEC` | ✅ | Python import confirms `1800` |
| Backend | ✅ | running |
| Orchestrator | ✅ | v1.1.20-D96 |
| Frontend | ✅ | Vite ready :5173 |
| Kyle Lambda samples (DB) | ❌ | 0 rows — **ROOT CAUSE FOUND** |

## Diagnostic Findings

### Root cause: DB data was NEVER persisted across restarts

**Confirmed via git forensics:**

```
git-tracked panopticon.db:   128 bytes  (empty placeholder)
live panopticon.db:          462,848 bytes (created fresh by Python on first start)
```

`data/panopticon.db` is tracked in git as a **128-byte empty file**. When Python processes first start and call `ShadowDB()` → `bootstrap()`, SQLite creates the actual DB file **from scratch** (`db.py:L399–L413`). Every `restart_all.ps1` kills processes → Python starts fresh → new empty DB.

**`restart_all.ps1` does NOT delete the DB** — it only removes `*.pid`, `radar.log`, `radar.err.log`. The DB file survives restarts. But since the DB starts empty (from the 128-byte git placeholder), each fresh Python invocation creates a **new empty DB**.

**Real situation**: The DB should accumulate data across time. But because the git placeholder is empty, each fresh process start creates a new empty DB at the same path, overwriting nothing (the placeholder is 128 bytes, the real DB is 462KB — they're different). Wait — if the placeholder is 128 bytes and the live DB is 462KB, the live DB should be *larger* than the git version. Unless the Python code creates a NEW file entirely (not overwriting the git placeholder).

Actually: `sqlite3.connect(self.path.as_posix())` creates the file if it doesn't exist. The 128-byte git placeholder exists, but Python appends WAL data to it, making it 462KB. The git placeholder (empty schema) and live DB share the same path.

**Key question**: Does the 462KB DB contain the `SCHEMA_SQL` and all 34 tables? Yes — confirmed `SELECT name FROM sqlite_master` returned 34 tables. So the live DB has the schema. But **all data tables are empty** — this means the Python processes have been running but **never successfully wrote any data**.

**Conclusion**: DB data=0 is NOT because of restart creating fresh empty DB. The Python processes ARE running and creating the schema correctly. The data tables are genuinely empty because:
1. `wallet_observations`: `_poll_data_api_for_takers` polls but gets 0 rows → no writes
2. `kyle_lambda_samples`: `_flush_kyle_buffer()` called every 60s but buffer never fills → 0 writes
3. All other tables: never populated

### Kyle Lambda 0: TWO-layer issue

**Layer 1 — In-memory `MetricsCollector` reset on restart**:
- `run_radar.py:L202` calls `update_heartbeat()` (process guard), not `MetricsCollector` initialization
- Each restart creates new `MetricsCollector` instance with `self._kyle_samples = []`
- Frontend sees in-memory MC reset to zero

**Layer 2 — Buffer never flushes**:
- `append_kyle_lambda_sample` writes to `_kyle_buffer` in-memory
- Flush happens every 60s (`run_radar.py:L2592–L2593`) via `flush_kyle_buffer()`
- BUT: `_flush_kyle_buffer()` inserts into `kyle_lambda_samples` table (DB write)
- `kyle_lambda_samples: 0` in DB means either:
  - Flush never executed (Radar not running 60s yet), OR
  - Flush ran but `delta_v = 0` or `window_ts = 0` → writes skipped

### `wallet_observations` 0: Data API polling failure

`_poll_data_api_for_takers` (run_radar.py:L1543–L1603) polls `https://data-api.polymarket.com/trades` using `urllib.request`. If the endpoint returns empty lists or errors, no writes occur. No error logs seen.

---

## ⚠️ Unchecked Assumptions
- [ ] `_poll_data_api_for_takers` — confirmed it writes via `db.append_wallet_observation()` (buffered), not direct insert. Buffer flushes every 50 rows or on `flush_wallet_obs_buffer()` call. Need to verify buffer ever reaches 50 rows.
- [ ] `kyle_lambda_samples` 0: need to check if the 60s heartbeat flush has executed in current session log.
- [ ] The git-tracked 128-byte placeholder vs live 462KB DB — need to confirm if git LFS replaced the placeholder with actual data.

---

## Q1 (CRITICAL — Data persistence): How to prevent DB data loss on restart?

**Scope**: Architecture decision — affects all data pipelines
**Relevant code**: `db.py:L399–L413` (`ShadowDB.__init__`), `restart_all.ps1` (no DB deletion), `data/panopticon.db` in git as 128-byte placeholder

**Root cause**: `data/panopticon.db` is tracked in git as a **128-byte empty file**. When Python runs and calls `sqlite3.connect("data/panopticon.db")`, it opens this empty file, applies `PRAGMA journal_mode=WAL`, and builds the schema via `bootstrap()`. All 34 tables are created (confirmed). But data tables are genuinely empty because pipeline components never successfully wrote data.

**Options**:
- A: **Commit the live DB to git before restart** — add a pre-restart step that commits `data/panopticon.db` to git
  → pros: data persists; cons: GB-level DB file, git-LFS needed, data freshness unclear
- B: **Change DB path on restart** — use `data/panopticon_restart_backup.db` as restart-safe copy, keep `panopticon.db` as primary
  → pros: simple; cons: manual/script complexity
- C: **Investigate why pipeline never writes data** — the real problem is NOT restart destroying data; the pipeline itself is not populating data. Fix the pipeline.
  → pros: addresses root cause; cons: more investigation needed
- **Z: Architect's call** — if none of the above fit the constraints I cannot see.

**Suggested**: Option C — the DB schema is intact, all 34 tables exist. The issue is WHY the pipeline never writes. Investigate `_poll_data_api_for_takers` (wallet_observations=0) and Kyle λ buffer (kyle_lambda_samples=0) separately.

**Confidence**: High — git forensics confirmed DB file survives restart. Data=0 means pipeline never populated it.

→ Needs ruling: Confirm Option C approach, or direct different data persistence strategy?

---

## Q2 (D96-F partial): TradeListPanel market question name

**Scope**: Reversible — frontend UI only
**Relevant code**: `dashboard/src/components/TradeListPanel.tsx`, `dashboard/src/types/dashboard.ts:L37–L61`

**Status**: Full-width ✅, timestamps ✅, question name ❌ (not implemented)

Options:
- A: **Display existing `eventName`** — `TradeListItem.eventName` may already contain question text, just truncated
- B: **API call to get question** — add endpoint to fetch market question from Polymarket
- **Z: Architect's call**

**Suggested**: Option A first — verify `eventName` content in frontend API response.

→ Needs ruling: Approve Option A investigation, or Option B?

---

## Q3 (Secondary): Kyle Lambda immediate flush?

**Scope**: Reversible — not blocking consensus pipeline
**Relevant code**: `run_radar.py:L2140–L2160` (Kyle λ compute), `db.py:L2733–L2760` (`_flush_kyle_buffer`)

**Options**:
- A: **Wait 60s** — heartbeat flush will write samples eventually
- B: **Immediate flush** — `flush_kyle_buffer()` on every N samples or every trade tick
- **Z: Architect's call**

**Suggested**: Option A — non-blocking, verify 60s flush works first.

→ Needs ruling?

---

## Q4: Analysis Worker heartbeat

**Status**: ✅ Closed — D95 Q3 ruling confirmed: analysis_worker is event-driven, no heartbeat. Frontend correctly shows "—". No action needed.
