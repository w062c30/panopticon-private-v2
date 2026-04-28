# Signal Engine — Zero-Latency Event-Driven Architecture

**Date:** 2026-04-23
**Author:** Panopticon Agent
**Status:** Draft — incorporates critical HFT architecture corrections

---

## 1. Problem Statement

Dashboard shows `Latest signal rejected: SKIPPED` — a stale record from a one-off manual `run_once()` execution. The three long-running processes (`discovery_loop`, `radar`, `analysis_worker`) all produce hunting data (wallet observations, shadow hits, insider scores) but none of them drive Bayesian decisions. There is no live signal path into `execution_records`.

---

## 2. Three Critical Architecture Errors (v1 Design)

| # | Error | Severity | Root Cause |
|---|---|---|---|
| 1 | `pending_entropy_signals` DB table + 5s polling | **Fatal** | In 5-minute crypto markets, MM withdraw in 50ms. DB polling means by the time signal_engine reads the signal, market has already resolved. |
| 2 | `signal_engine` writes `wallet_market_positions` | **Fatal** | Observer/Executor role confusion. `wallet_market_positions` tracks **Insider** positions. Our trades must never update Insider LIFO cost basis. |
| 3 | Single-source trigger (Polymarket only) | **High** | Polymarket entropy drop is a lagging indicator. Hyperliquid OFI shock precedes Polymarket moves by 50–200ms. |

---

## 3. Revised Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  L1: PERCEPTION LAYER                                                         │
│                                                                               │
│  Hyperliquid WS (OFI Shock) ──┐                                                │
│                                 ├──► asyncio.Queue ⚡ ZERO I/O LATENCY ──────┐ │
│  Polymarket Radar (Entropy)  ─┘                                              │ │
│                                                     │                         │ │
│                                                     ▼                         │ │
│  L2/L3: COGNITION + DECISION LAYER (signal_engine async task)                │ │
│                                                                               │ │
│    ┌──────────────────────────────────────────────────────────────────────┐  │ │
│    │  event.source ∈ {radar, ofi, db_poll_fallback}                      │  │ │
│    │                                                                        │  │ │
│    │  1. [READ O(1)] wallet_observations  — last 60s window for market     │  │ │
│    │  2. [READ O(1)] insider_score_snapshots — filter: score ≥ 0.55         │  │ │
│    │  3. [READ O(1)] wallet_market_positions — LIFO avg_entry_price        │  │ │
│    │       (READ ONLY — updated ONLY by analysis_worker, NEVER here)       │  │ │
│    │                                                                        │  │ │
│    │  4. [COMPUTE] Consensus Bayesian Update → posterior probability        │  │ │
│    │                                                                        │  │ │
│    │  5. [GATE] L4 Fast Gate — EV check with real cost basis + Kyle λ      │  │ │
│    └──────────────────────────────────────────────────────────────────────┘  │ │
│                                                     │                         │ │
│                                                     ▼                         │ │
│  L4: EXECUTION LAYER                                                           │
│                                                                               │
│  execution_engine                                                               │
│    ├─► [WRITE] execution_records  ← OUR trades only                         │
│    └─► [LIVE only] EIP-712 FOK order → Polymarket CLOB                      │
│                                                                               │
├──────────────────────────────────────────────────────────────────────────────┘
│                                                                               │
│  ASYNC BACKGROUND WORKERS (Observer side — NEVER confused with Executor)    │
│                                                                               │
│  analysis_worker                                                               │
│    └─► [LISTEN] L1 WebSocket taker fills                                     │
│        └─► [WRITE] wallet_market_positions (LIFO VWAP per fill tick)        │
│            ↳ avg_entry_price, current_position_notional                       │
│                                                                               │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Key invariants enforced:**
- `asyncio.Queue` delivers events in < 1ms (zero disk I/O)
- `signal_engine` NEVER writes `wallet_market_positions` (READ ONLY)
- `analysis_worker` NEVER writes `execution_records` (Observer, not Executor)
- Cross-exchange lead signal: Hyperliquid OFI → Polymarket consensus

---

## 4. Data Flows

### 4.1 Event Bus — `asyncio.Queue[SignalEvent]`

The zero-latency in-memory message broker. All L1感知層 producers push `SignalEvent` dataclasses; `signal_engine` consumes them instantly.

```python
@dataclass
class SignalEvent:
    source: str             # "radar" | "ofi" | "db_poll_fallback"
    market_id: str          # Polymarket market_id
    token_id: str | None
    entropy_z: float | None = None   # Polymarket entropy drop
    ofi_shock_value: float | None = None  # Hyperliquid OFI
    trigger_address: str = "system"
    trigger_ts_utc: str | None = None

    @property
    def z(self) -> float:
        if self.entropy_z is not None:
            return abs(self.entropy_z)
        if self.ofi_shock_value is not None:
            return abs(self.ofi_shock_value)
        return 0.0
```

### 4.2 Hyperliquid OFI → Polymarket Correlation Mapping

```python
# Hyperliquid OFI shock is in BTC-USD; map to Polymarket via correlation_edges
def _map_ofi_to_polymarket(market_id: str, db: ShadowDB) -> str:
    row = db.conn.execute(
        """
        SELECT market_a, market_b FROM correlation_edges
        WHERE (market_a = ? OR market_b = ?) AND window_sec = 60
        ORDER BY updated_ts_utc DESC LIMIT 1
        """, (market_id, market_id)
    ).fetchone()
    if row:
        a, b = row[0], row[1]
        return b if a == market_id else a
    return market_id  # fallback
```

### 4.3 Queue → Consensus Bayesian Update

```python
async def _run_async(queue: asyncio.Queue[SignalEvent], db: ShadowDB) -> None:
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            # DEGRADED MODE: fallback DB poll (last resort, max 5s latency)
            fallback = _poll_db_fallback(db)
            if fallback:
                await _process_event(fallback, db)
            continue
        await _process_event(event, db)
```

Inside `_process_event`:

```
1. Z-score threshold: |event.z| < MIN_ENTROPY_Z_THRESHOLD → SKIP
2. OFI source → map to Polymarket via correlation_edges
3. Collect insider sources (score ≥ 0.55) from wallet_observations (last 60s)
4. Consensus check: len(sources) < MIN_CONSENSUS_SOURCES → INSUFFICIENT_CONSENSUS
5. Bayesian posterior = geometric_mean_LR(sources)
6. READ wallet_market_positions for avg_entry_price (LIFO, READ ONLY)
7. Fetch real-time price from Gamma API
8. L4 Fast Gate
9. WRITE execution_records (our trades ONLY)
```

### 4.4 L4 Fast Gate with LIFO Cost Basis

```python
signal_input = FastSignalInput(
    p_prior=posterior,
    quote_price=current_price,        # Gamma API real-time
    payout=1.0,
    capital_in=current_price * order_size_usd,
    order_size=order_size_usd,
    avg_entry_price=avg_entry,        # LIFO from wallet_market_positions (READ ONLY)
    delta_t_ms=150.0,
    gamma=0.001,
    slippage_tolerance=0.009,
    min_ev_threshold=0.0,
    daily_opp_cost=0.0008,
    days_to_resolution=3.0,
    bid_ask_imbalance=0.0,
)
gate = fast_execution_gate(signal_input, snapshot)
```

EV formula (corrected — no double-counting):

```
ev_net = p_adj * payout * qty - capital_in - avg_entry * qty - taker_fee - gas - slippage
```

When `avg_entry = 0` (no existing position): degrades to original flat-payout formula.

---

## 5. Observer/Executor Separation (Critical Invariant)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  OBSERVER SIDE  (analysis_worker — WebSocket listener)                      │
│                                                                             │
│  On each taker fill tick from L1 WebSocket:                                 │
│    wallet_market_positions += fill (BUY: +qty, SELL: -qty)                  │
│    avg_entry_price = LIFO_VWAP(update)                                      │
│    wallet_observations += raw tick                                          │
│    insider_score_snapshots += new score                                     │
│                                                                             │
│  SIGNAL ENGINE NEVER TOUCHES THESE TABLES (READ ONLY)                       │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  EXECUTOR SIDE  (signal_engine — decision actor)                            │
│                                                                             │
│  On consensus + L4 gate BUY decision:                                        │
│    execution_records += our_trade (accepted=1, reason=PASS)                 │
│                                                                             │
│  SIGNAL ENGINE NEVER TOUCHES wallet_market_positions (FORBIDDEN)             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. DB Tables

### 6.1 `pending_entropy_signals` — DEGRADED FALLBACK ONLY

**Not the primary path.** This table is a degraded fallback for when the asyncio.Queue is unavailable (e.g., process restart). Normal operation uses zero-I/O queue delivery.

```sql
CREATE TABLE IF NOT EXISTS pending_entropy_signals (
    signal_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT,
    entropy_z REAL NOT NULL,
    sim_pnl_proxy REAL,
    trigger_address TEXT NOT NULL,
    trigger_ts_utc TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT,
    created_ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(signal_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_entropy_signals_unconsumed
    ON pending_entropy_signals(market_id, consumed_at)
    WHERE consumed_at IS NULL;
```

### 6.2 `wallet_market_positions` — Insider LIFO Tracking (Observer Side)

Maintained **exclusively** by `analysis_worker`. `signal_engine` reads only.

```sql
CREATE TABLE IF NOT EXISTS wallet_market_positions (
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    current_position_notional REAL NOT NULL DEFAULT 0.0,
    avg_entry_price REAL NOT NULL DEFAULT 0.0,
    last_updated_ts_utc TEXT NOT NULL,
    PRIMARY KEY (wallet_address, market_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_market_positions_wallet
    ON wallet_market_positions(wallet_address);

CREATE INDEX IF NOT EXISTS idx_wallet_market_positions_market
    ON wallet_market_positions(market_id);
```

**LIFO Accounting (per PANOPTICON_CORE_LOGIC Invariant 4.3):**

VWAP update on BUY:
$$P_{new} = \frac{P_{old} \cdot Q_{old} + P_{fill} \cdot Q_{fill}}{Q_{old} + Q_{fill}}$$

LIFO reduction on SELL: reduce from most recent BUY fills first. `avg_entry_price` does **not** change on partial SELL (only remaining position retains original VWAP).

---

## 7. Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `MIN_CONSENSUS_SOURCES` | `2` | Minimum independent entities before BUY allowed |
| `INSIDER_SCORE_THRESHOLD` | `0.55` | Minimum insider/trust score to count as consensus source |
| `ENTROPY_LOOKBACK_SEC` | `60` | Time window to look back for wallet observations |
| `MIN_ENTROPY_Z_THRESHOLD` | `-4.0` | Only process signals with \|z\| >= this |

> **Removed:** `SIGNAL_ENGINE_INTERVAL_SEC` — replaced by asyncio queue timeout (5s fallback only).

---

## 8. Files to Create / Modify

### Create
- `panopticon_py/signal_engine.py` — async event-driven subprocess

### Modify
- `panopticon_py/db.py` — add `pending_entropy_signals` + `wallet_market_positions` tables + methods
- `panopticon_py/fast_gate.py` — add `avg_entry_price` to `FastSignalInput`, compute EV with real cost basis
- `panopticon_py/hunting/run_radar.py` — push `SignalEvent` to `asyncio.Queue` on entropy drop
- `panopticon_py/ingestion/analysis_worker.py` — maintain `wallet_market_positions` LIFO on each fill tick
- `panopticon_py/run_hft_orchestrator.py` — spawn `signal_engine` as async task (not subprocess)
- `.env.example` — add new env vars

### Orchestrator Changes (run_hft_orchestrator.py)

`signal_engine` runs as an **async task** inside the orchestrator (sharing the same event loop), not as a spawned subprocess. The orchestrator creates the `asyncio.Queue[SignalEvent]` and passes it to both the OFI engine callback and the signal_engine task.

```python
# In run_hft_orchestrator.py:
signal_queue: asyncio.Queue[SignalEvent] = asyncio.Queue()

# Wire OFI on_shock → queue
async def on_shock(shock: UnderlyingShock) -> None:
    await signal_queue.put(SignalEvent(
        source="ofi",
        market_id=shock.market_id,   # Hyperliquid market (mapped via correlation_edges)
        token_id=None,
        ofi_shock_value=shock.ofi_value,
        trigger_address="hyperliquid",
        trigger_ts_utc=datetime.now(timezone.utc).isoformat(),
    ))

# Wire radar entropy → queue (via shared queue reference)
# Radar modified to accept queue.put callback

# Run signal_engine async task
signal_task = asyncio.create_task(_run_signal_engine(signal_queue, db), name="signal_engine")
```

---

## 9. Invariants Preserved

| Invariant | How Ensured |
|---|---|
| Zero I/O latency | `asyncio.Queue` event bus; DB only in 5s degraded fallback |
| Observer/Executor separation | `wallet_market_positions` WRITE reserved for `analysis_worker`; `execution_records` WRITE reserved for `signal_engine` |
| Cross-exchange lead signal | Hyperliquid OFI → Polymarket correlation mapping before consensus |
| L1.1 Trade-conditioned entropy | Radar pushes `SignalEvent` only on `should_fire_negative_entropy()`, not on quote ticks |
| L2.4 Cache-first DAL | Signal engine reads `wallet_observations` before Moralis |
| L3.1 Consensus Bayesian | LR from geometric mean of independent source scores |
| L3.2 Kelly guardrail | L4 gate enforces `kelly_fraction <= gate.kelly_cap` |
| L4.1 Ghost liquidity filter | Fast gate `slippage_tolerance` check |
| L4.3 Strict EV gate | `ev_net <= 0 → ABORT` in fast gate |
| L4.4 No market orders | FOK hardcoded; Polymarket submit deferred to Phase 2 |
| L4.5 LIFO position accounting | `analysis_worker` updates `wallet_market_positions` via LIFO on every fill tick |
| L5.1 Go-live readiness | `LIVE_TRADING=false` enforced at subprocess spawn |

---

## 10. Open Questions / Deferred

1. **Redis Pub/Sub for distributed deployment**: Currently `asyncio.Queue` (single-process). For multi-machine OFI + Radar + SE deployment, replace with Redis Pub/Sub. Local dev is single-process safe.

2. **Polymarket CLOB order submission**: `submit_clob_order()` not yet implemented — requires `clob_client.py` integration with `CLOB_KEY` and EIP-712 signing. Phase 2.

3. **OFI → Polymarket correlation hydration**: `correlation_edges` table must be populated by the graph_linker before OFI triggers are useful. The OFI shock must have a correlated Polymarket market to map to.
