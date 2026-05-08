# D179a — Entropy Gate / RVF Pass-Through Fix

> Priority: **P0** (gates the entire RVF/paper-trade pipeline)
> Read first: `D179_master_diagnosis.md` §1.3, §1.4, §1.5
> Read second: AGENTS.md `Coding Agent 強制規則 D101–D120` (especially RULE-CLOSURE-1, RULE-TIME-1)

---

## 1. What's actually broken (re-stated, code-level)

Three coupled problems block `_entropy_z_eval_ok_count` from ever incrementing:

### A1. **Trade-tick-driven H sampling fails at low tick rates**

```192:227:panopticon_py/hunting/entropy_window.py
self._events.append((recv_mono, max(0.0, buy_vol), max(0.0, sell_vol)))
cutoff = recv_mono - self.window_sec
while self._events and self._events[0][0] < cutoff:
    self._events.popleft()
...
def current_entropy(self) -> float | None:
    if self._trigger_locked or len(self._events) < 2:
        return None
    ...
def record_H_sample(self, recv_mono: float) -> None:
    h = self.current_entropy()
    if h is None:
        return                # ← no-op for sparse tokens
    self._h_history.append(h)
```

For a token whose mean inter-tick gap is comparable to or larger than `window_sec` (60 s for T2/T3/T5), `_events` typically holds **1** element after the per-push cutoff. `current_entropy()` returns `None`, `record_H_sample()` is a no-op, `_h_history` never grows past 0–1, `zscore_of_latest_delta()` returns `(None, None)` forever, and `_entropy_z_eval_ok_count` stays at 0.

**This is the single largest root cause.** Live evidence: `entropy_status.json` shows 5 z_ready tokens but 36 still-locked-with-events=0; `[D75_ENTROPY_GATE]` over 30+ minutes shows `eval=1..33`, `hist_not_ready ≈ eval`, `z_eval_ok=0` (see `D179_master_diagnosis.md` §1.5).

### A2. **D178 unlock has a circular pre-condition**

```207:217:panopticon_py/hunting/entropy_window.py
if self._trigger_locked and prev_recv_mono is not None:
    gap = recv_mono - prev_recv_mono
    min_events = min(5, self._unlock_event_count)
    if (gap <= self.max_internal_gap_sec
            and len(self._events) >= min_events
            and len(self._h_history) >= 2):     # ← circular
        self._trigger_locked = False
```

For tokens that NEVER unlocked since boot, `_h_history` is empty (because `record_H_sample` requires `current_entropy()` to succeed, which requires `not _trigger_locked`). Therefore `len(self._h_history) >= 2` can never become true and D178 never fires. The 36 stuck tokens in `entropy_status.json` match this exactly.

### A3. **T1 path bypasses gate counters**

```2927:2939:panopticon_py/hunting/run_radar.py
if tier == "t1":
    ...
    t1_ew.push(recv, t1_buy, t1_sell)
    t1_ew.record_H_sample(recv)
    # D96-B: STOP — Kyle λ can still compute (mid_before already captured above)
    # but T1 does NOT go through consensus pipeline
    continue
```

T1 flow returns **before** L3092 (`_entropy_eval_total += 1`), so the heartbeat reports a misleadingly low `eval` count vs raw `last_trade_price`, and any T1 z-score never enters the gate distribution. T1 markets are the highest-frequency feed (BTC/ETH/SOL 5 m), so they SHOULD be the easiest path to a non-zero `z_eval_ok` — but currently they are entirely invisible.

---

## 2. Fix strategy (in priority order)

### Step 1 — Break the D178 circular dependency

Make D178 unlock require **either** `len(_h_history) >= 2` **or** `len(_events) >= min_events_strong` (e.g. `min(10, _unlock_event_count)`). The "strong events" branch covers fresh tokens with no preserved history. **Do not** simply drop the `_h_history` clause: it protects against unlocking on a single anomalous spike.

```python
# entropy_window.py push() — replace the existing D178 block
if self._trigger_locked and prev_recv_mono is not None:
    gap = recv_mono - prev_recv_mono
    min_events_soft = min(5, self._unlock_event_count)
    min_events_strong = min(10, self._unlock_event_count)
    if gap <= self.max_internal_gap_sec and (
        # path-A: warm history preserved by D154 across reconnect
        (len(self._events) >= min_events_soft and len(self._h_history) >= 2)
        # path-B (D179a): fresh tokens with no history yet
        or (len(self._events) >= min_events_strong)
    ):
        self._trigger_locked = False
        _logger.info(
            "[EW][D179a] unlocked gap_safe gap=%.1f events=%d h_hist=%d strong=%s",
            gap, len(self._events), len(self._h_history),
            len(self._events) >= min_events_strong,
        )
```

This is the *minimum* code change for `entropy_fires_60s >= 1` once Step 2 also lands. Without Step 2, low-rate tokens never reach `min_events_strong=10`.

### Step 2 — Decouple H-sampling from per-tick rate (the central fix)

Add a **periodic timer** that calls `record_H_sample` for every per-token EW once per `H_SAMPLE_PERIOD_SEC` (default `5.0`). This replaces "H sample = 1 per trade tick" with "H sample = 1 per fixed cadence per token", which is the standard way Shannon entropy is consumed in HFT.

Place the timer in `run_radar.py` `_live_ticks_unlocked` as a sibling task to the WS reader, so it fires regardless of WS message arrival. Use `time.monotonic()` for cadence (consistent with `recv_mono`).

```python
# run_radar.py — alongside existing tasks in _live_ticks_unlocked
async def _h_sample_tick_loop() -> None:
    period = float(os.getenv("HUNT_H_SAMPLE_PERIOD_SEC", "5.0"))
    if period < 1.0 or period > 60.0:
        raise ValueError(f"HUNT_H_SAMPLE_PERIOD_SEC={period} invalid (1..60)")
    while not _close_event.is_set():
        recv = time.monotonic()
        # Snapshot keys to avoid "dict changed during iteration"
        for token_id in list(_entropy_windows.keys()):
            ew = _entropy_windows.get(token_id)
            if ew is None:
                continue
            ew.record_H_sample(recv)
        await asyncio.sleep(period)

h_sample_task = asyncio.create_task(_h_sample_tick_loop(), name="h_sample_tick")
```

Why a *fixed* cadence works:

* When tokens are quiet, `current_entropy()` still uses the rolling sums of whatever ticks fell into `[recv-window_sec, recv]`; it returns `None` only if `len(_events) < 2`.
* So Step 2 alone does **not** rescue ultra-quiet tokens; it rescues the tokens that have ≥2 ticks in the window but happen to be sampled at the wrong tick boundary. That covers the bulk of T2/T3 once they un-lock.
* Truly silent tokens stay silent — that's correct.

> **Why not simply lower `current_entropy`'s threshold to `len>=1`?**
> Shannon entropy of a single observation is degenerate (always `log 2` if buy or sell exclusively, 0 otherwise). It would inject artefactual H deltas at every isolated tick and make the z-score distribution noisy. Reject this option (the previously-proposed "accumulator-bucket" rewrite); see §3.

### Step 3 — Account T1 in the gate counters

Refactor the T1 branch so the gate counters update for T1 too, *without* enabling the consensus/signal-emit path (which is intentionally disabled for T1):

```python
# run_radar.py — inside the t1 branch, replace `continue` with helper, then continue
if tier == "t1":
    ...
    t1_ew.push(recv, t1_buy, t1_sell)
    t1_ew.record_H_sample(recv)
    # D179a: count T1 in gate diagnostics so heartbeat eval matches reality
    _d_diag, z_diag = t1_ew.zscore_of_latest_delta()
    state_diag = t1_ew.state_dict()
    _entropy_eval_total += 1
    if z_diag is None:
        if state_diag.get("trigger_locked"):
            _entropy_locked_count += 1
        else:
            _entropy_hist_not_ready_count += 1
    else:
        _entropy_z_eval_ok_count += 1
        _entropy_z_samples.append(float(z_diag))
        if z_diag < get_z_threshold():
            _entropy_z_below_threshold_count += 1
    continue  # T1 still does NOT go through consensus / signal_queue
```

This restores observability without changing T1's "no-emit" rule.

### Step 4 — Lower `min_history_for_z` floor cautiously

`config.get_min_history_for_z()` defaults to 5. After Step 2 lands the per-token cadence is ~5 s, so 5 H samples = 25 s of warm-up. That is acceptable. **Do not lower below 5**; below that the stdev tail used in z-score becomes meaningless. Leave the env var as the documented knob.

### Step 5 — Reset bookkeeping when WS reconnects flush all EWs

`_reconnect_all_entropy_windows` (L934-940) clears every token's `_events` and re-locks them. Combined with the H-sample timer (Step 2) those tokens recover automatically. **No change needed**, but add a single info log so the operator can correlate WS reconnects with the eval-rate dip.

```python
# run_radar.py L934 — add log
def _reconnect_all_entropy_windows() -> None:
    n = len(_entropy_windows)
    for ew_obj in _entropy_windows.values():
        ew_obj.mark_reconnect(reason="ws_disconnect")
    logger.info("[EW][D179a] WS reconnect flushed %d per-token windows", n)
```

---

## 3. Why we **reject** the originally-proposed bucket-accumulator rewrite

The handoff document proposes `_buy_acc` / `_sell_acc` accumulators so that `current_entropy()` works at `len(_events) >= 1`. Reasons to reject:

1. **Statistical noise.** A single tick produces `H = 0` (one side dominant). Any fresh tick on the other side yields `H = 1`. The first `delta_H` after warm-up is therefore `±1`; with a near-zero `sigma` from the pre-warmup tail, z-score explodes to ±50 (the existing clamp). Every isolated trade would fire spurious entropy.
2. **Loss of "no decision" semantic.** `None` is currently the gate's "insufficient data" signal. Forcing a numeric H eliminates `hist_not_ready` and merges it with real signals — the post-fix gate diagnostic loses information.
3. **Higher-risk patch.** Touches both data structure (deque ↔ accumulator) and call paths; far bigger surface than Step 2's external timer.

The H-sample timer (Step 2) achieves the same goal — consistent H samples — without breaking the statistical contract.

---

## 4. Logic-error checklist (anti-bugs catalog)

The coding agent **must** verify each item before opening the PR. These are the foreseeable bugs given the change surface.

| # | Trap | Mitigation |
|---|------|-----------|
| L1 | `_entropy_windows` is mutated from the WS coroutine; Step 2 timer iterates it. `dict changed during iteration` raises during `_get_or_create_ew`. | Always iterate over `list(_entropy_windows.keys())`; never `.items()` on the live dict. |
| L2 | The H-timer keeps every per-token EW alive forever even after the token leaves the subscription set. | Step 2 only calls `record_H_sample`; it does not allocate new EWs. Stale EWs are bounded because `record_H_sample` no-ops on insufficient data. Add a 24 h TTL cleanup in a *separate* sprint if memory becomes an issue. |
| L3 | A second `record_H_sample` call from the timer can append two H samples in the *same* `time.monotonic()` instant if a tick arrives concurrently, biasing the rolling deque. | `record_H_sample` is idempotent: it appends `current_entropy()`'s value. Two calls within the same window only differ if `_events` changed; the 200-cap deque tolerates this. |
| L4 | Step 3 introduces `_entropy_eval_total += 1` for T1 — but that variable is the **`nonlocal`** captured by `_on_message`, not module-level. | All five counters are declared `nonlocal` at L2612-2614; Step 3 must remain inside `_on_message` so the closure captures them. Do **not** move the T1 branch out of `_on_message`. |
| L5 | The H-timer task is created in `_live_ticks_unlocked` but never cancelled on shutdown — orphan task. | Add `h_sample_task` to the `for task in [...]` cancellation list in `run_hft_orchestrator.py:1224`. |
| L6 | `HUNT_H_SAMPLE_PERIOD_SEC` set too low (e.g. 0.1) burns CPU, set too high (e.g. 120) starves the gate. | Validate `1.0 <= period <= 60.0` at construction; raise `ValueError` else (mirrors `_unlock_max_gap_sec` validation at L110). |
| L7 | The new `[EW][D179a]` log floods at `INFO` for hundreds of tokens. | First-unlock log only — guard with a per-token flag or use `logger.debug` for the timer-driven path; reserve `INFO` for explicit unlock events. |
| L8 | `mark_radar_boot_released()` (L210) is now a no-op stub that confuses readers. | Out of scope for D179a; do not touch. |
| L9 | If Step 2 timer crashes, the WS reader continues but H samples freeze silently. | Wrap loop body in try/except + `logger.exception`; the timer must self-restart on transient errors. |
| L10 | A new test using `time.monotonic()` is hard to mock. | Inject the time source: `_h_sample_tick_loop(now_fn=time.monotonic)` so tests pass `now_fn=lambda: deterministic_clock.next()`. |
| L11 | T1 branch fires `_entropy_z_eval_ok_count` increments without going through the entropy fire path; downstream operators may misread "z_eval_ok=N" as "fired N signals". | The existing `fired` counter (`_ws_entropy_fire_count`) stays as the truth-of-trades. Document the split in the heartbeat log message. |
| L12 | `record_H_sample` mutates `_h_history` from the H-timer thread; if `zscore_of_latest_delta` runs concurrently, list copy at L241 sees an inconsistent view. | Both run in the same asyncio loop (single-threaded), no real race. Confirm by **not** running the timer in a thread; `asyncio.create_task` is sufficient. |

---

## 5. Test plan (TDD)

Add the following to `tests/test_entropy_window_d179a.py` and `tests/test_run_radar_d179a.py`. These must **fail** against current code; then become green after Steps 1–3.

```python
# tests/test_entropy_window_d179a.py
def test_d179a_strong_events_unlocks_without_h_history():
    """Fresh token (no _h_history) must unlock when _events >= min_events_strong."""
    ew = EntropyWindow(tier="t2", window_sec=60.0)
    ew._trigger_locked = True            # mimic post-flush state
    base = 1000.0
    for i in range(10):                  # 10 ticks, 5s apart, all alternating side
        side_buy = 1.0 if i % 2 == 0 else 0.0
        side_sell = 0.0 if i % 2 == 0 else 1.0
        ew.push(base + i * 5.0, side_buy, side_sell)
    assert ew._trigger_locked is False, "D179a: 10 events should unlock fresh token"

def test_d179a_warm_history_unlock_path_preserved():
    """D178 path-A still works when _h_history is preserved and 5 events arrive."""
    ew = EntropyWindow(tier="t2", window_sec=60.0)
    ew._h_history.extend([0.5, 0.6])     # simulate D154 preservation
    ew._trigger_locked = True
    base = 1000.0
    for i in range(5):
        ew.push(base + i * 1.0, 1.0, 0.0)
    assert ew._trigger_locked is False

def test_d179a_isolated_tick_does_not_unlock():
    """A single tick must not unlock — guards against L1 statistical noise."""
    ew = EntropyWindow(tier="t2", window_sec=60.0)
    ew._trigger_locked = True
    ew.push(1000.0, 1.0, 0.0)
    assert ew._trigger_locked is True, "Single tick must not unlock"
```

```python
# tests/test_run_radar_d179a.py — outline (use existing pytest async fixtures)
async def test_h_sample_tick_loop_records_samples_for_quiet_token(monkeypatch):
    """Even when no new tick arrives, the timer must call record_H_sample."""
    from panopticon_py.hunting import run_radar as rr
    rr._entropy_windows.clear()
    ew = rr.EntropyWindow(tier="t2", window_sec=60.0)
    rr._entropy_windows["abc"] = ew
    # Pre-seed two events so current_entropy is finite
    ew.push(1000.0, 1.0, 0.0)
    ew.push(1001.0, 0.0, 1.0)
    # Start the timer for one tick
    monkeypatch.setenv("HUNT_H_SAMPLE_PERIOD_SEC", "0.05")
    task = asyncio.create_task(rr._h_sample_tick_loop())
    await asyncio.sleep(0.2)
    rr._close_event.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(ew._h_history) >= 2, "timer must record H samples between ticks"

async def test_t1_path_increments_eval_counter():
    """T1 ticks must contribute to _entropy_eval_total (gate observability)."""
    # Use the existing _on_message harness or a focused stub that calls the T1 branch.
    ...
```

Run order:

1. `pytest tests/test_entropy_window_d179a.py -x`  ← unit
2. `pytest tests/test_run_radar_d179a.py -x`        ← integration
3. `pytest -x` (full 95+ suite)

---

## 6. Step-by-step execution plan

> Each numbered item is one commit's worth of work. Verify the test plan before *and* after the code change.

1. **(no code yet)** Add the three failing tests above. Run `pytest -k d179a` and confirm they fail with the messages "fresh token should unlock" / "timer must record" / "T1 must increment". Commit: `D179a: failing tests for entropy gate fix`.
2. **Patch `entropy_window.py`** with the Step 1 change above. Bump the file's existing module docstring D178 reference to D179a. Re-run unit tests: only `test_d179a_strong_events_unlocks_without_h_history` and `test_d179a_isolated_tick_does_not_unlock` should be green; `test_d179a_warm_history_unlock_path_preserved` should already be green since it goes through path-A.
3. **Patch `run_radar.py`** for Step 3 (T1 gate accounting). Re-run integration test → green.
4. **Patch `run_radar.py`** for Step 2 (H-sample timer). Add task creation, append to shutdown list. Add `HUNT_H_SAMPLE_PERIOD_SEC` env var to `config.py` if a default constant is introduced. Re-run integration test → green.
5. **Patch `run_radar.py:934`** for Step 5 (single info log on flush).
6. **Bump versions.** `radar` → `v1.3.5-D179a`; `orchestrator` only if you also picked up D179b. Update `run/versions_ref.json` in the same commit.
7. Run full test suite: `pytest -x`. Confirm ≥95 pass.
8. Run `scripts/restart_all.ps1` (per AGENTS.md). Wait 30 minutes.
9. Validate against §7 acceptance.
10. Write `temp_architect_handoffs/2026-MM-DD_D179a_completion.md` per `AGENTS.md` "Architect Handoff" template. Push to GitHub.

---

## 7. Sprint acceptance criteria

* `pytest -k d179a` all green; `pytest -x` green ≥ 95.
* After 30-minute soak post-restart:
  * `[D75_HEARTBEAT] entropy_fires_60s >= 1` at least once in `run/orchestrator.log`.
  * `[D75_ENTROPY_GATE]` shows `z_eval_ok > 0` for **every** 60 s window after the first 2 minutes.
  * `data/entropy_status.json` `z_ready_count >= 8`.
  * `[EW][D179a] unlocked gap_safe ... strong=True` appears for at least one fresh token.
  * `[EW][D179a] WS reconnect flushed N per-token windows` appears at least once after a real disconnect.
  * `[DIAG][ENTROPY_FIRE] z=...` appears in the log.
* Paper-trade DB row count strictly increases over the soak window:
  ```
  sqlite3 data/panopticon.db 'select count(*) from paper_trades'
  ```

If any criterion fails, STOP and write an architect handoff per protocol; do not bundle a Step 6 "tweaking thresholds" commit.

---

## 8. Files touched

| File | Change | Lines (approx) |
|------|--------|---------------|
| `panopticon_py/hunting/entropy_window.py` | D179a unlock branch + module docstring | L11-L19, L207-L217 |
| `panopticon_py/hunting/run_radar.py` | T1 gate accounting; H-sample timer task; flush log | L934-L940, L2927-L2939, after L2613, near L3672 |
| `run_hft_orchestrator.py` | append `h_sample_task` to shutdown cancel list | L1224-L1228 |
| `config.py` | optional `HUNT_H_SAMPLE_PERIOD_SEC` default if you want a constant | tail |
| `run/versions_ref.json` | radar → v1.3.5-D179a | radar entry |
| `tests/test_entropy_window_d179a.py` | new | new file |
| `tests/test_run_radar_d179a.py` | new | new file |
| `panopticon_py/hunting/INDEX.md` | mark `_h_sample_tick_loop` ✅ ACTIVE | append |

No other files. **Do not** touch `signal_engine.py`, `consensus_radar.py`, `arb_scanner.py`, or `discovery_loop.py`.
