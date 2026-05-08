# D179 — Master Diagnosis & Sprint Plan Index

> Author: 2nd Architect (independent review)
> Date: 2026-05-08
> Scope: Issues A (entropy gate), B (arb scanner), C (manifest/lifecycle), D (174 tokens)
> **No code changes in this document — coding agent reads sprints D179a..D179d for execution.**

---

## 0. Executive Summary

The four reported "long-standing" issues are **not four bugs**; they are **three real bugs and two misreadings of metrics**. Holistic root causes:

| ID | User-perceived issue | Verdict | Real cause |
|----|---------------------|---------|------------|
| A | `z_eval_ok=0`, no entropy fires | **Real bug — multi-cause** | (1) Per-token trade-tick interarrival > `window_sec`; (2) T1 path bypasses gate counters; (3) WS reconnect re-locks all per-token EWs; (4) D178 unlock has dead-lock condition (`h_history>=2` requires unlocked). |
| B | "Arb scanner silent" | **Misreading + minor visibility gap** | Arb scanner is a **separate process** (PID 15216). Its logs go to `run/arb_scanner.err.log`, not `run/orchestrator.log`. `rvf_live_snapshot.json` has no `arb` block by design. |
| C | "PID vanishes / manifest didn't converge / radar stuck at ready" | **Two distinct real bugs** | (C-1) `pol_monitor.py` `UnboundLocalError` crashes the polygon task on every boot (`_pol_ws_consecutive_failures`). (C-2) `RadarState` machine has no `DEGRADED → READY` recovery path on WS reconnect. The "PID 23932 vs 29072" PowerShell story could not be reproduced and is **not** the same bug; treat as transient. |
| D | "174 tokens stagnant for days" | **Misreading** | "174" is the live count of **active T5 sports tokens returned by Gamma API** in `arb_scanner.run()`; it reflects a stable real-world market count, not a stale cache. |

**Cross-cutting consequence:** Because A blocks `signal_queue` and B/D look like silence, the operator cannot tell that L1 (WS) is actually healthy and that the system is performing as designed in every layer except the entropy gate.

---

## 1. Hard Evidence (Files / Lines / Logs)

All claims below are backed by inspecting the **current repo and live logs**, not the previous architect's narrative.

### 1.1 Process state (live `run/process_manifest.json`)

```12:51:run/process_manifest.json
  "orchestrator": {
    "pid": 34360,
    "version": "v1.7.9-D178",
    "version_match": true,
    "status": "running"
  },
  ...
  "radar": {
    "pid": 34360,
    "version": "v1.3.4-D178",
    "version_match": true,
    "status": "degraded"
  },
  "arb_scanner": {
    "pid": 15216,
    "version": "v0.5.11-D150",
    "status": "running"
  }
```

* Orchestrator and radar share PID 34360 (correct — radar is a coroutine inside orchestrator).
* Arb scanner is **PID 15216, separate process**, healthy.
* All `version_match=true`. **No PID confusion exists in the live system.**

### 1.2 Polygon task crash (every boot)

```138:138:run/orchestrator.err.log
2026-05-08 05:12:44,713 [ERROR] orchestrator - [ORCH] polygon crashed:
  cannot access local variable '_pol_ws_consecutive_failures' where it is not associated with a value
```

Root: classic Python `UnboundLocalError`. `_pol_ws_consecutive_failures` is a module-level global at L39; at L531 inside `_wss_loop`, it is **assigned** (`= 0`) without `global`, which makes Python treat the entire name as local in that frame. When the connect attempt at L502 raises **before** L531 is reached, L547 (`_pol_ws_consecutive_failures += 1`) tries to read an unbound local.

```39:39:panopticon_py/hunting/pol_monitor.py
_pol_ws_consecutive_failures = 0
```
```531:557:panopticon_py/hunting/pol_monitor.py
                    backoff = 5.0
                    _pol_ws_consecutive_failures = 0   # ← creates local binding for the whole frame
                    ...
            except Exception as exc:
                _pol_ws_consecutive_failures += 1      # ← UnboundLocalError when L531 unreached
```

Rule violated: `AGENTS.md` "RULE-CLOSURE-1" (closure / global hoisting). This is a 1-line fix. The orchestrator monitor (L1188) only auto-restarts `signal_engine`, so polygon stays dead but orchestrator keeps running — that is why the user sees orchestrator alive yet `polygon` features dark.

### 1.3 Per-token EW state (live `data/entropy_status.json`)

`data/entropy_status.json` (last write 21:33:04 UTC):

* `total: 46` (only 46 tokens have ever received a tick → `_get_or_create_ew`),
* `z_ready_count: 5`,
* `locked_count: 36`,
* 5 tokens are mid-warmup (`h_hist<5`, `events>=1`, not locked).

So D178's "gap-safe unlock" is **partially working**: 10 tokens (5 ready + 5 mid) escaped the lock; 36 stayed locked because their post-flush tick rate fell below `len(_events) >= 5` *and* `len(_h_history) >= 2` (the deadlock — a fresh-flushed token has `_h_history` preserved per D154, but tokens that NEVER unlocked have `_h_history==0`).

### 1.4 The "H Hist: 1144" / "Buffer Events: 0" misreading

```3593:3665:panopticon_py/hunting/run_radar.py
ew_states = [ew.state_dict() for ew in _entropy_windows.values()]
total_events = sum(s["events"] for s in ew_states)       # ← unused below
any_locked   = any(s["trigger_locked"] for s in ew_states)
total_h_hist = sum(s["h_hist"] for s in ew_states)        # ← THIS is "H Hist"
state = {"events": total_events, "trigger_locked": any_locked, "h_hist": total_h_hist}
...
logger.info(
    "[RADAR %s] Buffer Events: %s, Trigger Locked: %s, H Hist: %s",
    os.getpid(),
    len(recent),                                          # ← THIS is "Buffer Events"
    state.get("trigger_locked"),
    state.get("h_hist"),
)
```

* **`Buffer Events: 0`** is `len(recent)`, the deque of fired-only entropy events (only appends when entropy fires — `recent.append(msg)` at L3154). Until any entropy event fires, this is 0 by design. **It is *not* the per-token EW deque size.**
* **`H Hist`** is the **sum across all tokens'** `_h_history`. So `H Hist: 1144` does not mean any single token has 1144 H samples; the 200-cap-per-token (`while len(self._h_history) > 200: popleft`) means one busy token can contribute up to 200 alone.

**The previous architect's diagnosis ("Buffer Events: 0 ⇒ EW _events is empty") was wrong.** Buffer Events being 0 is normal and tells us nothing about EW state.

### 1.5 The actual entropy-gate symptom

`run/orchestrator.log` shows ~17 cycles of `D75_ENTROPY_GATE` since D178 deploy. Pattern:

```7931:7931:run/orchestrator.log
05:16:38,769 [D75_ENTROPY_GATE] event_type_60s={last_trade_price:381,book:1171,price_change:17316,other:994}
                                gate_60s={eval:31,locked:16,hist_not_ready:15,z_eval_ok:0,z_below_thr:0,fired:0}
```

```38621:38621:run/orchestrator.log
05:34:19,975 [D75_ENTROPY_GATE] event_type_60s={last_trade_price:369,book:943,price_change:21904,other:1273}
                                gate_60s={eval:33,locked:0,hist_not_ready:33,z_eval_ok:0,z_below_thr:0,fired:0}
```

Two facts:

1. **`eval` is wildly smaller than `last_trade_price` count** (e.g. `eval=33` vs `last_trade_price=369`). This is because **T1 markets bypass the eval counter** — see L2939:
   ```2939:2939:panopticon_py/hunting/run_radar.py
   continue   # T1 has its own EW path; never increments _entropy_eval_total
   ```
   T1 (BTC/ETH/SOL 5m) tokens dominate the WS feed. The remaining ~30 evals/min are the few T2/T3/T5 ticks where `trade_size>0` and trade_side is BUY/SELL.

2. **Of those 30 evals, most are `hist_not_ready`** (`z` is None and not locked). Reason: the per-token interarrival of `last_trade_price` events on T2/T3 markets (5–60 s typical) is comparable to or longer than `window_sec` (60 s for T2/T3). After `popleft` cutoff, `_events` often holds only 1 element, so `current_entropy()` returns `None`, so `record_H_sample()` is a no-op, so `len(_h_history) < min_history_for_z` (5) forever.

This is the real bug behind A.

### 1.6 Arb scanner is alive

```184:186:run/arb_scanner.err.log
2026-05-08 05:12:46,824 [INFO] __main__ - [ARB_FEE_SUMMARY] total=174 kept=174 excluded=0
2026-05-08 05:12:46,824 [INFO] __main__ - [ARB_INIT] token_ids=174 sample=[...] all_valid_format=True
2026-05-08 05:12:47,344 [INFO] __main__ - [ARB] WS connected, subscribed to 174 token_ids
```

* "174" is the **live Gamma API output**, not a hard-coded seed. (`fetch_t5_token_ids` at `arb_scanner.py:123` filters via `_is_tier5_sports_market`.)
* On every reconnect the scanner re-fetches (`arb_scanner.py:576-592`); WS has been stable since 05:12:47, so the count has not refreshed (correct behaviour).
* `panopticon.db` table `arb_stats` is the scanner's persistence target (`arb_scanner.py:411-426`); orchestrator never reads it. That is why `rvf_live_snapshot.json` and `orchestrator.log` show nothing arb-related.

### 1.7 Radar state-machine dead-end

```90:97:panopticon_py/hunting/run_radar.py
_ALLOWED_TRANSITIONS = {
    RadarState.STARTING:   {RadarState.CONNECTING, RadarState.FAILED},
    RadarState.CONNECTING: {RadarState.SYNCING,    RadarState.FAILED},
    RadarState.SYNCING:    {RadarState.READY,      RadarState.DEGRADED, RadarState.FAILED},
    RadarState.READY:      {RadarState.DEGRADED,   RadarState.FAILED},
    RadarState.DEGRADED:   {RadarState.SYNCING,    RadarState.FAILED},
    RadarState.FAILED:     {RadarState.STARTING},
}
```

Only call site that promotes to READY is in `_on_message`:

```2629:2641:panopticon_py/hunting/run_radar.py
if (
    not _radar_boot_state.first_payload_seen
    and (item.get("event_type") in {"book", "price_change", "last_trade_price"})
):
    _radar_boot_state.first_payload_seen = True
    ...
    if _radar_boot_state.subscription_sync_ok:
        _set_radar_state(RadarState.READY)
```

**`first_payload_seen` is one-shot.** After the first ever payload, this branch never fires. WS disconnect → `DEGRADED`, but reconnect can never go back to `READY`. That is why the manifest reports `radar.status="degraded"` despite hundreds of trade-ticks/min flowing in. **Operator-visible "stuck at degraded" is a labelling bug**, not a data-flow bug.

The user's report "radar stuck at `ready`" likely conflates the orchestrator status (`running`) with the radar status (`ready` would actually be the success state, not a stuck state). With the dead-end above, after one disconnect the manifest shows `degraded`, and that is the actual symptom you see today.

---

## 2. Cross-cutting Failure Mode Map

```
┌─────────────────────────────────────────────────────────────────────┐
│  WS feed (HEALTHY)                                                  │
│   ├─► T1 (BTC/ETH/SOL 5m): per-token EW push → record_H_sample      │
│   │   ⚠ T1 bypasses _entropy_eval_total counter                      │
│   ├─► T2/T3/T5: per-token EW push                                    │
│   │   ⚠ Low tick-rate ⇒ _events deque ≤1 most of the time            │
│   │   ⚠ current_entropy()=None ⇒ _h_history stays <5                 │
│   │   ⚠ Periodic WS disconnect ⇒ mark_reconnect ⇒ 36 tokens locked   │
│   └─► record_H_sample → _h_history (per token, sum=1144 in heartbeat)│
│                                                                     │
│  zscore_of_latest_delta() returns None → _entropy_z_eval_ok_count=0 │
│   ⇒ no signal_queue.put() ⇒ no paper_trades                          │
│                                                                     │
│  Side channels                                                      │
│   ├─► Polygon task crashes on every boot (UnboundLocalError)        │
│   ├─► Radar status latches DEGRADED after first WS disconnect       │
│   ├─► Arb scanner runs in its own process, logs/DB invisible to     │
│   │   orchestrator's snapshot                                       │
│   └─► 174 ≡ live Gamma "active T5 sports" count (not a cache)        │
└─────────────────────────────────────────────────────────────────────┘
```

The primary chain to fix is the entropy gate (A). Once `entropy_fires_60s ≥ 1`, paper_trade rows will materialise and the rest of the RVF pipeline becomes observable.

---

## 3. Sprint Index

| Sprint | File | Priority | Goal |
|--------|------|----------|------|
| D179a | `D179a_entropy_gate.md` | **P0** | Drive `z_eval_ok ≥ 1` and `entropy_fires_60s ≥ 1` within one observation window. |
| D179b | `D179b_polygon_and_radar_state.md` | **P0** | Stop polygon crash on boot; allow radar to recover from `DEGRADED → READY`. |
| D179c | `D179c_arb_visibility.md` | P2 | Surface arb scanner stats into `rvf_live_snapshot.json` and clarify FUNCTION_STATUS so issue B/D never re-appears as "silence". |
| D179d | `D179d_diagnostics_hardening.md` | P1 | Add observability so the next ambiguous symptom is self-diagnosable (per-token H, per-tier eval split, true buffer events, manifest convergence trace). |

> Execute D179b first (5 min, 1-line fix removes one chronic source of confusion) → then D179a (multi-step, biggest payoff) → then D179d (diagnostics) → then D179c (visibility).

---

## 4. Constraints (apply to ALL sprints)

These constraints are derived from `AGENTS.md` and the failure modes observed above. Coding agent **must** keep them in scope:

1. **No live trading change.** `LIVE_TRADING=false`; all D179 work runs in the existing paper/shadow mode. Do not touch order routing.
2. **Contract preservation.** No changes to `shared/contracts/panopticon-event.schema.json` or any DB writer column ordering. Only add columns at the end if needed.
3. **Process restart protocol.** After every code change to `panopticon_py/**/*.py` or entry-point scripts, the agent **must** run `scripts/restart_all.ps1` before validating logs. (`AGENTS.md` PROCESS RESTART section.)
4. **Version bumping protocol.** For each entry point you touch: `radar` `v1.3.5-D179` / orchestrator `v1.7.10-D179` etc. Update `run/versions_ref.json` in the **same commit**. (`AGENTS.md` RULE-VER-*.)
5. **Singleton & path env vars.** Do not bypass `acquire_singleton()` or hard-code paths; reuse `os.getenv("...","default")` consistent with both writer and reader. (`AGENTS.md` RULE-PATH-1.)
6. **Time contract.** Use `panopticon_py.time_utils.utc_now_rfc3339_ms()`; **never** introduce a local `_utc_now_rfc3339_ms()`. (`AGENTS.md` RULE-TIME-1.)
7. **Closure / global hoisting (RULE-CLOSURE-1).** Any rebinding inside a function/method that mirrors a module-level name **must** be declared `global` (or `nonlocal`) — the polygon bug is the canonical example.
8. **No Graphify regression.** Do not import any path under `graphify-out/`, `GRAPH_REPORT.md`, or graph.json into a signal/risk/execution path.
9. **Tests-first.** Every D179 sprint adds a failing regression test BEFORE the fix; the fix is acceptable only when the test goes green and the existing 95-test suite still passes.
10. **One sprint = one PR/commit.** Do not bundle D179a..D179d. Each sprint must be independently revertable.

---

## 5. Acceptance criteria (entire D179 epic)

A 30-minute soak after all four sprints land must show ALL of:

* `[D75_HEARTBEAT] ... entropy_fires_60s >= 1` at least once.
* `[D75_ENTROPY_GATE] ... gate_60s={... z_eval_ok>=1 ...}` strictly increasing across cycles.
* `data/entropy_status.json` `z_ready_count >= 8` and `locked_count` < `total/2`.
* `data/rvf_live_snapshot.json`:
  * `queue.processed_60s >= 1`
  * new `arb` block populated with `tokens_subscribed`, `total_updates`, `opp_count_total`.
* `run/orchestrator.err.log` contains **zero** `polygon crashed` lines after restart.
* `run/process_manifest.json` `radar.status` toggles `ready`/`degraded` correctly with WS state (not latched after first disconnect).
* SQLite `paper_trades` table `COUNT(*)` strictly increases over the soak window.
* Regression test count ≥ 95 passes; new D179 tests all pass.

---

## 6. Anti-goals (explicit non-changes)

To avoid the previous "fix everything in one go" risk:

* Do **not** lower the z-threshold below `-4.0`. Threshold tuning is deferred to a separate NQ-4 sprint until the gate produces a real `z_dist_60s` distribution.
* Do **not** remove the D165 / D178 unlock paths. They cover legitimate disconnect-recovery cases. We will *complement* them with a deadlock breaker.
* Do **not** modify `signal_engine.decide()` EV/Kelly maths. The empty signal stream is the input problem; outputs are out of scope here.
* Do **not** restructure `_live_ticks` into separate WS/heartbeat tasks. That is a bigger refactor; we use minimal patches to the existing single-loop design.
* Do **not** add MM-anonymisation rules. Out of scope.
