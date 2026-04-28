# Agent Brief: Panopticon Signal Engine Architecture Rebuild

**Created for:** New agent tasked with rebuilding the Panopticon Signal Engine
**Context:** This brief was generated from a full conversation session. Read everything here before writing any code.
**Status:** Awaiting your decisions on 6 review questions, then execute the rebuild

---

## 1. What is Panopticon?

Panopticon is a **crypto/hyperliquid/polymarket predictive trading system** that hunts "smart money" wallets (insider-tracked entities) using multiple sensing tracks, then makes Bayesian consensus decisions and executes trades through L4 gates.

The core principle: Follow smart money wallets with high insider scores; use entropy drops and OFI shocks as triggers; never let a trade through without consensus and EV check.

Key files to understand the system philosophy:
- `d:\Antigravity\Panopticon\PANOPTICON_CORE_LOGIC.md`
- `d:\Antigravity\Panopticon\docs\superpowers\specs\2026-04-23-signal-engine-design.md`

---

## 2. What Happened in This Conversation

### Phase 1: Bug Fixing (COMPLETED)
The conversation started by fixing broken data collection:
- `.env` had blank values causing `ValueError: invalid literal for int() with base 10: ''`
- Fixed `scripts/unify_env.py` to overwrite existing blank keys
- Enhanced Track A (Gamma CLOB takers) and Track B (Leaderboard whales) with multi-tier fallback
- Improved `fingerprint_scrubber.py` to retain wallets with initial signals even with sparse history

### Phase 2: SKIPPED Signal Investigation (COMPLETED)
Dashboard showed `Latest signal rejected: SKIPPED`. Root cause:
- `main_loop.py` was a one-shot mock bootstrap script with hardcoded data
- No live signal path existed into `execution_records`
- Architecture gap: radar/discovery/analysis all produced data but nothing drove Bayesian decisions

### Phase 3: Signal Engine Design (COMPLETED, but then REVISED)
A SPEC was written (`docs/superpowers/specs/2026-04-23-signal-engine-design.md`) proposing:
1. `pending_entropy_signals` DB table written by radar, polled every 5s by signal_engine subprocess
2. `wallet_market_positions` table for LIFO tracking
3. Consensus Bayesian decision pipeline
4. L4 Fast Gate with `avg_entry_price`

### Phase 4: Implementation (COMPLETED — then rolled back for revision)
The agent implemented the SPEC. Created/modified 7 files:
- `panopticon_py/signal_engine.py` (NEW)
- `panopticon_py/db.py` (added tables + methods)
- `panopticon_py/fast_gate.py` (added `avg_entry_price`)
- `panopticon_py/hunting/run_radar.py` (writes `pending_entropy_signals`)
- `panopticon_py/ingestion/analysis_worker.py` (LIFO updates)
- `scripts/start_shadow_hydration.py` + `run_hft_orchestrator.py` (spawn SE)
- `.env.example` (new env vars)

All tests passed (LIFO, FastGate EV formula, execution_records write).

### Phase 5: User's 3 Fatal Architecture Corrections (CRITICAL — READ THIS)
After seeing the SPEC, the user identified **3 fatal bugs** in the proposed design:

---

#### FATAL BUG 1: DB Table Polling = Death in 5-Min Crypto Markets

**Problem:** Using `pending_entropy_signals` DB table + 5s polling is "Web 2.0 CRUD thinking, Web3 HFT poison." In Polymarket's 5-minute markets, market makers withdraw in **50ms**. By the time signal_engine reads from disk, the market has already resolved.

**Correct approach:** Zero-latency **asyncio.Queue** in-memory message broker. No disk I/O on the hot path.

---

#### FATAL BUG 2: Observer/Executor Role Confusion

**Problem:** `signal_engine` was writing `wallet_market_positions`. This violates separation of concerns:
- `wallet_market_positions` tracks **Insider positions** (Observer side)
- `signal_engine` is the **Executor** — its trades must NEVER update Insider's LIFO cost basis

The rule: `wallet_market_positions` is **READ-ONLY** in signal_engine. Only `analysis_worker` writes to it.

Also: `signal_engine` was writing `paper_trades` — also forbidden.

---

#### FATAL BUG 3: Missing Cross-Exchange Lead Signal

**Problem:** Polymarket entropy drop alone is a **lagging indicator**. Hyperliquid OFI (BTC-USD) shock precedes Polymarket moves by 50–200ms. Single-source triggers miss the lead signal.

**Correct approach:** Hyperliquid OFI → asyncio.Queue → signal_engine consensus → execution. Cross-exchange correlation mapping via `correlation_edges` table.

---

## 3. The Correct Final Architecture

```
L1: PERCEPTION LAYER (zero latency event bus)

Hyperliquid WS (BTC-USD OFI) ──┐
                                  ├──► asyncio.Queue[SignalEvent] ⚡ ZERO DISK I/O
Polymarket Radar (entropy)  ─────┘
                                  │
                                  ▼
L2/L3: signal_engine (_run_async task)
  READ wallet_observations (last 60s, O(1))
  READ insider_score_snapshots (score >= 0.55, O(1))
  READ wallet_market_positions (LIFO avg_entry, READ ONLY!)
  OFI shock: map market_id via correlation_edges
  Consensus Bayesian Update
  L4 Fast Gate (ev_net = p*qty - cap_in - avg_entry*qty - fees)
                                  │
                                  ▼
L4: execution_records (WRITE — our trades ONLY)
    paper_trades: FORBIDDEN in signal_engine
    wallet_market_positions: FORBIDDEN in signal_engine

ASYNC BACKGROUND WORKERS (Observer — strictly isolated)

analysis_worker (every 25s, threading.Thread)
  for each clob_trade observation:
    upsert_wallet_market_positions (LIFO) ← ONLY this writes
    rank_insider() → insider_score_snapshots
```

---

## 4. Current vs Final State

### Current (BROKEN — in codebase right now)

- `run_radar.py`: writes `db.append_pending_entropy_signal()` → DB table (Disk I/O!)
- `signal_engine.py`: subprocess, polls DB every 5s, writes `wallet_market_positions` and `paper_trades` (VIOLATION)
- `hft_execution_gate.py`: `ShockHandler` completely bypasses signal_engine consensus (GATE VIOLATION)
- `HyperliquidOFIEngine(on_shock=ShockHandler)` → independent gate → no Bayesian consensus

### Final (CORRECT — what you must implement)

- `run_radar.py`: `await queue.put(SignalEvent(source="radar", ...))` — no DB write for signal path
- `run_hft_orchestrator.py`: creates `signal_queue: asyncio.Queue[SignalEvent]`; `on_shock` callback does `await signal_queue.put(...)`
- `signal_engine.py`: async task `_run_async(queue, db)` — reads queue, never writes `wallet_market_positions`
- `hft_execution_gate.py`: **DELETED** — OFI unified through signal_engine consensus
- `analysis_worker.py`: **UNCHANGED** — already correct (only writer to `wallet_market_positions`)
- `start_shadow_hydration.py`: remove SE subprocess (already integrated into orchestrator)

---

## 5. Full Component Inventory

### All 6 Tracks in the System

| Track | Type | Entry File | Current State | Role |
|---|---|---|---|---|
| T1 Discovery Loop | subprocess | `discovery_loop.py` | ✓ OK | Finds smart money wallets via Gamma + Leaderboard |
| T2 Polymarket Radar | asyncio task | `run_radar.py` | Needs rewrite | WS monitoring + entropy detection |
| T3 Hyperliquid OFI | asyncio task | `hyperliquid_ws_client.py` | Needs refactor | BTC-USD OFI shock detection |
| T4 Graph Linker | asyncio task | `graph_linker.py` | ✓ OK | HFT wallet clustering |
| T5 Analysis Worker | threading.Thread subprocess | `analysis_worker.py` | ✓ OK (unchanged) | Insider scoring + LIFO position tracking |
| T6 Signal Engine | subprocess | `signal_engine.py` | Needs rewrite | Consensus Bayesian decisions |

### Reader/Writer Matrix

```
WRITERS:
  discovery_loop      → discovered_entities, tracked_wallets, wallet_funding_roots, wallet_observations (clob_trade), hunting_shadow_hits
  run_radar           → wallet_observations (entropy_drop), hunting_shadow_hits, pending_entropy_signals ⚠ (needs removal)
  analysis_worker     → wallet_market_positions ✓ (ONLY correct writer), insider_score_snapshots
  graph_linker        → correlation_edges
  signal_engine       → execution_records ✓, paper_trades ✗ (FORBIDDEN), wallet_market_positions ✗ (FORBIDDEN)
  hft_execution_gate  → none (Phase 2 direct submission)

READERS:
  signal_engine      ← pending_entropy_signals (polling ⚠), wallet_observations, insider_score_snapshots, wallet_market_positions (READ ONLY)
  analysis_worker    ← wallet_observations (polling every 25s)
  graph_linker       ← wallet_observations, wallet_funding_roots
```

### Two Independent L4 Gates (CURRENT — BROKEN)

| | `fast_gate.py` | `hft_execution_gate.py` |
|---|---|---|
| Trigger | signal_engine consensus | Hyperliquid OFI shock |
| Bayesian consensus | ✓ Yes | ✗ No |
| LIFO avg_entry | ✓ Yes | ✗ No |
| Writes execution_records | ✓ signal_engine | ✗ No |
| Gate logic | O(1) EV check | O(1) Kyle lambda |

**Decision needed:** Should `hft_execution_gate.py` be deleted (unified through SE) or kept as a fast-path?

---

## 6. The 6 Review Questions (ANSWERS NEEDED)

The previous agent created a review document. You need to answer these 6 questions before finalizing the rewrite:

**Q1: hft_execution_gate.py — Delete or Keep?**
`ShockHandler` bypasses signal_engine consensus. Delete it and route all OFI shocks through SE? Or is "OFI = fast path, Polymarket = slow consensus" a valid separation?

**Q2: paper_trades — Where should it be written?**
If signal_engine can't write `paper_trades`, where does paper simulation output go? A new `paper_positions` table? Or keep it out of scope entirely?

**Q3: start_shadow_hydration.py — Keep or Deprecate?**
If SE is integrated into `run_hft_orchestrator.py` as an async task, does `start_shadow_hydration.py` still need a standalone SE subprocess? Or is it fully superseded?

**Q4: OFI → Polymarket Correlation Mapping**
`correlation_edges` table maps Hyperliquid BTC-USD to Polymarket markets. Does it have data yet? Is a static hardcoded mapping acceptable for now?

**Q5: fast_gate vs hft_execution_gate Parameter Alignment**
If both exist, which parameters take precedence? Or do they serve fundamentally different purposes (consensus path vs flash path)?

**Q6: analysis_worker — Sync or Async?**
`analysis_worker` writes `wallet_market_positions` directly via `self.db` (synchronous), not through `AsyncDBWriter`. Intentional (ensures immediate consistency)? Or should it use the async queue?

---

## 7. Files to Modify (Your Checklist)

### A. `run_hft_orchestrator.py`
- Create `signal_queue: asyncio.Queue[SignalEvent]` at top of `main_async()`
- Refactor `on_shock` callback to: `await signal_queue.put(SignalEvent(source="ofi", market_id=shock.market_id, ofi_shock_value=shock.ofi_value, ...))`
- Change `run_polymarket_radar()` to accept a `queue_put_callback` parameter
- Add `signal_engine_task = asyncio.create_task(_run_signal_engine(signal_queue, db))`
- Remove SE subprocess spawn (already done in previous session — verify)

### B. `run_radar.py`
- `_live_ticks()` signature: add `signal_queue: asyncio.Queue | None = None` parameter
- Inside `should_fire_negative_entropy()` block: replace `db.append_pending_entropy_signal(...)` with `await signal_queue.put(SignalEvent(source="radar", market_id=..., token_id=..., entropy_z=..., trigger_address=..., trigger_ts_utc=...))` when queue available
- Keep DB writes for `wallet_observations` and `hunting_shadow_hits` (not the signal path)
- Fallback: if `signal_queue is None`, write to DB (backward compat)

### C. `signal_engine.py`
- Keep `SignalEvent` dataclass
- Keep all helper functions (`_utc`, `_consensus_bayesian_update`, `_get_current_price`, `_get_insider_score`, `_collect_insider_sources`, `_map_ofi_to_polymarket`)
- Keep `_poll_db_fallback()` as degraded fallback only
- `main()`: creates queue and calls `_run_async(queue, db)` via `asyncio.run()`
- Expose `_run_async(queue, db)` for orchestrator to call directly as async task
- **REMOVE**: all `db.append_paper_trade()` calls
- **REMOVE**: all `db.upsert_wallet_market_position_lifo()` calls
- **KEEP**: `db.append_execution_records()` call

### D. `hft_execution_gate.py`
**Decision needed**: Delete entirely OR keep as fast-path (see Q1)

If keeping: ensure it does NOT duplicate what signal_engine does.

### E. `analysis_worker.py`
**DO NOT MODIFY** — already correct as Observer (only writer to `wallet_market_positions`)

### F. `start_shadow_hydration.py`
**Decision needed** (see Q3): If orchestrator fully supersedes it, remove SE subprocess from here.

---

## 8. Key Existing Code to Reference

### `signal_engine.py` (already has the correct structure):
- `SignalEvent` dataclass with `source`, `market_id`, `token_id`, `entropy_z`, `ofi_shock_value`
- `_run_async(queue, db)` async loop
- `_process_event(event, db)` with all steps
- `_poll_db_fallback(db)` for degraded mode
- `_map_ofi_to_polymarket(market_id, db)` correlation mapping

### `analysis_worker.py` (already correct — DO NOT TOUCH):
```python
# This is the ONLY correct writer of wallet_market_positions:
for o in obs:
    if o.get("obs_type") == "clob_trade":
        payload = o.get("payload", {})
        side = payload.get("side", "")
        price = payload.get("price")
        size = payload.get("size")
        if side in ("BUY", "SELL") and price is not None and size is not None:
            self.db.upsert_wallet_market_position_lifo(
                wallet_address=o["address"].lower(),
                market_id=o["market_id"],
                fill_price=float(price),
                fill_qty=float(size),
                side=side,
                updated_ts_utc=o["ingest_ts_utc"],
            )
```

### `run_hft_orchestrator.py` (current structure — needs queue integration):
```python
async def on_shock(shock: UnderlyingShock) -> None:
    logger.info("[SHOCK] hl_epoch_ms=%s ofi=%.3f ...", ...)
    # THIS IS WHERE signal_queue.put() needs to go
    takers = []
    if takers:
        graph_engine.ingest_shock_takers(shock.hl_epoch_ms, takers)
```

### `run_radar.py` (entropy fire block — needs queue.put):
```python
if ew.should_fire_negative_entropy():
    # Current: db.append_pending_entropy_signal(...)
    # Needs to be: await signal_queue.put(SignalEvent(source="radar", ...))
```

---

## 9. Environment Variables for Signal Engine

```bash
MIN_CONSENSUS_SOURCES=2        # Minimum insider entities before BUY
INSIDER_SCORE_THRESHOLD=0.55    # Minimum score to count as consensus
ENTROPY_LOOKBACK_SEC=60       # Lookback window for wallet_observations
MIN_ENTROPY_Z_THRESHOLD=-4.0  # Only process signals with |z| >= this
```

---

## 10. LIFO Accounting Rules (for analysis_worker — already implemented)

- **BUY**: `current_position_notional += fill_qty`; `avg_entry_price = VWAP(new_fill)`
- **SELL**: `current_position_notional -= fill_qty` (LIFO: reduce from most recent BUY first); `avg_entry_price UNCHANGED` for remaining position
- **If `current_position_notional = 0`**: reset `avg_entry_price = 0`
- **If SELL with no position**: do nothing

VWAP formula: `P_new = (P_old * Q_old + P_fill * Q_fill) / (Q_old + Q_fill)`

---

## 11. The `SignalEvent` Contract

```python
from dataclasses import dataclass

@dataclass
class SignalEvent:
    source: str              # "radar" | "ofi" | "db_poll_fallback"
    market_id: str          # Polymarket market_id
    token_id: str | None     # CLOB token_id
    entropy_z: float | None = None   # Polymarket entropy drop
    ofi_shock_value: float | None = None  # Hyperliquid OFI value
    trigger_address: str = "system"
    trigger_ts_utc: str | None = None

    @property
    def z(self) -> float:
        """Canonical z-score for threshold check."""
        if self.entropy_z is not None:
            return abs(self.entropy_z)
        if self.ofi_shock_value is not None:
            return abs(self.ofi_shock_value)
        return 0.0
```

---

## 12. Verification Plan (after rewrite)

After each file is modified, verify:

1. **`run_radar.py`**: No `db.append_pending_entropy_signal()` calls remain (replaced by `queue.put`)
2. **`signal_engine.py`**: No `db.append_paper_trade()` or `db.upsert_wallet_market_position_lifo()` calls remain
3. **`hft_execution_gate.py`**: Either deleted entirely OR confirmed as strictly fast-path with no consensus logic
4. **`run_hft_orchestrator.py`**: `on_shock` callback does `await signal_queue.put(...)` AND `run_signal_engine` is an async task (not subprocess)
5. **All 6 review questions**: Answered and incorporated

---

## 13. Files Referenced in This Brief

| File | Path | Status |
|---|---|---|
| PANOPTICON_CORE_LOGIC | `d:\Antigravity\Panopticon\PANOPTICON_CORE_LOGIC.md` | Read for invariants |
| Signal Engine SPEC (old) | `d:\Antigravity\Panopticon\docs\superpowers\specs\2026-04-23-signal-engine-design.md` | Superseded by revised SPEC |
| Architecture Review Doc | `d:\Antigravity\Panopticon\docs\superpowers\reviews\2026-04-23-signal-engine-architecture-review.md` | Full review with diagrams |
| This brief | `d:\Antigravity\Panopticon\docs\superpowers\reviews\...` | You're reading it |
| DB module | `d:\Antigravity\Panopticon\panopticon_py\db.py` | Has tables + methods needed |
| Fast Gate | `d:\Antigravity\Panopticon\panopticon_py\fast_gate.py` | L4 gate with avg_entry_price |
| Signal Engine | `d:\Antigravity\Panopticon\panopticon_py\signal_engine.py` | Needs rewrite |
| Radar | `d:\Antigravity\Panopticon\panopticon_py\hunting\run_radar.py` | Needs queue integration |
| Analysis Worker | `d:\Antigravity\Panopticon\panopticon_py\ingestion\analysis_worker.py` | DO NOT TOUCH |
| Orchestrator | `d:\Antigravity\Panopticon\run_hft_orchestrator.py` | Needs queue + SE task |
| HFT Execution Gate | `d:\Antigravity\Panopticon\panopticon_py\hft\hft_execution_gate.py` | Decision: delete or keep |
| start_shadow_hydration | `d:\Antigravity\Panopticon\scripts\start_shadow_hydration.py` | Remove SE subprocess? |

---

## 14. First Step

**Answer the 6 review questions (Section 6)**, then proceed file by file:
1. `run_hft_orchestrator.py` — add queue + wire OFI
2. `run_radar.py` — add queue.put on entropy fire
3. `signal_engine.py` — clean up, expose `_run_async` for orchestrator
4. `hft_execution_gate.py` — delete (pending Q1 answer)
5. `start_shadow_hydration.py` — remove SE subprocess (pending Q3 answer)
6. Verify everything

---

_End of brief. All context from the conversation session has been captured here._
