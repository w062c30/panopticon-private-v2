# Data Collection Plan — LIVE Trading Unlock

> Last updated: D120 (2026-05-01)
> Purpose: Structured monitoring and threshold-based unlock criteria for LIVE_TRADING.

---

## LIVE Unlock Threshold

All conditions must be **GREEN** before LIVE trading is enabled:

```
Shadow trades ≥ 50
Win rate ≥ 55%
Average EV net > 0
T5 signal pass rate > 40%  (via /api/t5-coverage)
```

Reference: `scripts/check_shadow_readiness.py`

---

## Phase 1: System Stability Verification (Days 1–3 after D120)

**Service startup:**
```bash
# Terminal 1 — orchestrator (WAL mode + 30s busy_timeout)
python run_hft_orchestrator.py

# Terminal 2 — backend (read-only, port 8000)
uvicorn panopticon_py.api.app:app --host 0.0.0.0 --port 8000

# Browser
http://localhost:8000/dashboard/
```

**Every 10 minutes — confirm:**
```bash
# Writer health: expect running=true, stale=false, queue_depth < 50
curl http://localhost:8000/api/async-writer-health

# All processes alive + version match
curl http://localhost:8000/api/versions
```

**Phase 1 Pass Criteria:**
- 72 consecutive hours with no `[ERROR]` crash
- `data/async_writer_health.json` updated every 30s
- `pol_market_watchlist` has ≥ 5 rows with `is_active=1`
- All `version_match: true` in `/api/versions`

---

## Phase 2: Signal Accumulation (Days 4–14)

| Metric | Daily target | 14-day cumulative |
|--------|-------------|-------------------|
| Shadow hits | ≥ 10 | ≥ 140 |
| T5 signals fired | ≥ 5 | ≥ 70 |
| T2-POL signals | ≥ 2 | ≥ 28 |
| Wallet observations | ≥ 50 | ≥ 700 |

**Daily snapshot SQL (run at 23:55 daily):**
```sql
SELECT date('now') as snapshot_date,
  (SELECT COUNT(*) FROM hunting_shadow_hits) as total_hits,
  (SELECT COUNT(*) FROM wallet_observations) as total_obs,
  (SELECT COUNT(*) FROM pol_market_watchlist WHERE is_active=1) as pol_active,
  (SELECT COUNT(*) FROM wallet_market_positions
   WHERE outcome IS NOT NULL) as resolved_trades;
```

**Phase 2 concurrent sprint: D121**
- Priority target: Debt-1 (`_on_insider_alert` bare `sqlite3.connect`)
- Secondary: Debt-2 TypedDict for `AsyncDBWriterHealth`

---

## Phase 3: Win Rate Validation + LIVE Unlock Assessment (Days 15–30)

**Unlock decision process:**
1. `python scripts/check_shadow_readiness.py` → all GREEN
2. Architect review of `PANOPTICON_CORE_LOGIC.md` Invariant 1.4 compliance
3. T5 pass rate (`/api/t5-coverage`) > 40%
4. Debt-1 and Debt-2 resolved or risk accepted with sign-off
5. Operator manually sets `LIVE_TRADING=1`

---

## Milestone Timeline

```
2026-05-01  D120 stable — Phase 1 starts
2026-05-04  Phase 1 pass (72h no crash)
2026-05-05  Phase 2 starts + D121 sprint begins
2026-05-14  Phase 2 mid-point check (hits > 70)
2026-05-18  Phase 2 ends — Phase 3 begins
2026-05-31  LIVE_TRADING unlock assessment meeting
```

---

## Monitoring Dashboard Endpoints

| Endpoint | Purpose | Pass condition |
|----------|---------|----------------|
| `GET /api/async-writer-health` | Writer queue health | `running=true, stale=false, queue_depth < 50` |
| `GET /api/versions` | Process version alignment | all `version_match: true`, all `status: "running"` |
| `GET /api/pol-watchlist` | POL market activity | ≥ 5 rows with `is_active=1` |
| `GET /api/t5-coverage` | T5 signal pass rate | pass rate > 40% |
| `GET /api/link_resolver_stats` | Link resolution health | `resolved_count / mapping_count` ratio stable |

---

*End of Data Collection Plan*