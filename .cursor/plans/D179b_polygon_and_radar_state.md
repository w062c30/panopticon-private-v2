# D179b — Polygon Listener Crash + Radar State Recovery

> Priority: **P0** (small surface; removes a chronic boot-time crash and a misleading "degraded" label that complicate every diagnosis).
> Read first: `D179_master_diagnosis.md` §1.2 and §1.7.
> Do this **before** D179a — it's a 1-line + 3-line change that frees up debugging.

---

## 1. Bug 1 — `pol_monitor.py` UnboundLocalError on every boot

### Evidence

```138:138:run/orchestrator.err.log
2026-05-08 05:12:44,713 [ERROR] orchestrator - [ORCH] polygon crashed:
  cannot access local variable '_pol_ws_consecutive_failures' where it is not associated with a value
```

```39:39:panopticon_py/hunting/pol_monitor.py
_pol_ws_consecutive_failures = 0
```
```498:557:panopticon_py/hunting/pol_monitor.py
async def _wss_loop(self) -> None:
    backoff = 5.0
    while True:
        try:
            async with websockets.connect(...) as ws:
                ...
                backoff = 5.0
                _pol_ws_consecutive_failures = 0    # ← this line makes the WHOLE frame
                                                    #   treat _pol_ws_consecutive_failures as local
                async for raw in ws:
                    ...
        except Exception as exc:
            _pol_ws_consecutive_failures += 1       # ← UnboundLocalError if L531 not reached
            backoff = min(
                _POL_WS_BACKOFF_BASE * (2 ** (_pol_ws_consecutive_failures - 1)),
                _POL_WS_BACKOFF_MAX,
            )
            ...
```

`AGENTS.md` calls this out as RULE-CLOSURE-1. The orchestrator monitor at `run_hft_orchestrator.py:1188` only auto-restarts `signal_engine`; polygon stays dead, suppressing every Polygon-derived signal (USDC transfer trail, whale topology). It also sprays the user with a scary `[ERROR]` line that masks real problems.

### Fix

Two minimally-invasive options. Choose **option A**.

**Option A (chosen) — declare global**

```python
# panopticon_py/hunting/pol_monitor.py
async def _wss_loop(self) -> None:
    global _pol_ws_consecutive_failures
    backoff = 5.0
    while True:
        try:
            ...
            _pol_ws_consecutive_failures = 0       # now correctly mutates module-level
            ...
        except Exception as exc:
            _pol_ws_consecutive_failures += 1      # now reads module-level
            ...
```

**Option B (rejected) — make it a class attribute**

`PolygonListener._consecutive_failures: int = 0`. Cleaner OO, but it's a *behavioural* change (counter resets on listener re-instantiation, which currently doesn't happen but conceivably could). Defer.

### Logic-error checklist

| # | Trap | Mitigation |
|---|------|-----------|
| L1 | Adding `global` outside `_wss_loop` (e.g. in `run()`) silently does nothing. | Place `global` declaration **inside** `_wss_loop` as the first statement. |
| L2 | Other functions in `pol_monitor.py` may read `_pol_ws_consecutive_failures` for diagnostics. After the fix the value is now actually maintained — verify no consumer expects "always 0". | `rg _pol_ws_consecutive_failures panopticon_py` shows only the four call sites (L39, L531, L547, L549, L554). All are inside `_wss_loop`. Safe. |
| L3 | Exponential backoff at L548-549 grows without ceiling if the listener loops forever on a permanent failure. | `_POL_WS_BACKOFF_MAX` already caps at 900s. Verify the env override path isn't broken. |
| L4 | Tests for `pol_monitor` may have monkey-patched the module global to 0 between runs. | The new code mutates the same global — patches will still work. Add a fixture `monkeypatch.setattr` in the regression test if needed. |

### Test

```python
# tests/test_pol_monitor_d179b.py
import asyncio, pytest
from unittest.mock import AsyncMock, patch
from panopticon_py.hunting import pol_monitor

@pytest.mark.asyncio
async def test_d179b_pol_listener_handles_initial_connect_failure():
    """Initial websockets.connect failure must not raise UnboundLocalError."""
    listener = pol_monitor.PolygonListener(api_key="dummy", outbound=asyncio.Queue(), db_path=":memory:")

    # Force websockets.connect to raise on first call only, then cancel via _stop_event.
    fail_count = {"n": 0}

    async def _fake_connect(*args, **kwargs):
        fail_count["n"] += 1
        if fail_count["n"] == 1:
            raise OSError("simulated initial connect failure")
        # 2nd call: cancel via timeout instead of looping
        await asyncio.sleep(10.0)

    with patch("panopticon_py.hunting.pol_monitor.websockets.connect", side_effect=_fake_connect):
        with patch("panopticon_py.hunting.pol_monitor._POL_WS_BACKOFF_BASE", 0.01):
            try:
                await asyncio.wait_for(listener._wss_loop(), timeout=0.5)
            except asyncio.TimeoutError:
                pass  # acceptable — we only care that no UnboundLocalError fired
    assert fail_count["n"] >= 1
    assert pol_monitor._pol_ws_consecutive_failures >= 1
```

---

## 2. Bug 2 — Radar `DEGRADED` is a one-way state

### Evidence

```90:97:panopticon_py/hunting/run_radar.py
_ALLOWED_TRANSITIONS = {
    ...
    RadarState.READY:    {RadarState.DEGRADED, RadarState.FAILED},
    RadarState.DEGRADED: {RadarState.SYNCING,  RadarState.FAILED},
    ...
}
```

The transition table allows `DEGRADED → SYNCING`, but the only call to `_set_radar_state(SYNCING)` is during initial boot at L3297. After the first WS disconnect:

```3358:3365:panopticon_py/hunting/run_radar.py
def _on_ws_disconnected() -> None:
    if mc:
        mc.on_ws_disconnected()
    if _radar_boot_state is not None:
        _radar_boot_state.ws_connected = False
        if _radar_boot_state.state == RadarState.READY:
            _set_radar_state(RadarState.DEGRADED)
```

The promotion-back path:

```2629:2641:panopticon_py/hunting/run_radar.py
if (
    not _radar_boot_state.first_payload_seen
    ...
):
    _radar_boot_state.first_payload_seen = True
    ...
    if _radar_boot_state.subscription_sync_ok:
        _set_radar_state(RadarState.READY)
```

is gated by `not first_payload_seen`, which becomes `True` once and never resets. Therefore once `DEGRADED`, the radar stays `DEGRADED` for the lifetime of the process. The manifest has shown `radar.status="degraded"` since 21:27:33 even though hundreds of trade ticks/min are flowing.

This is what the user reports as "stuck at ready" / "stuck at degraded". The data path is fine; only the operator-facing label is wrong, and that wrongness misroutes operator attention every time.

### Fix

Make the WS-connected callback re-promote when payloads are flowing again:

```python
# panopticon_py/hunting/run_radar.py
def _on_ws_connected() -> None:
    if mc:
        mc.on_ws_connected()
    if _radar_boot_state is not None:
        _radar_boot_state.ws_connected = True
        _radar_boot_state.last_error = None
        # D179b: allow DEGRADED → SYNCING on actual reconnect; payload arrival promotes to READY.
        if _radar_boot_state.state == RadarState.DEGRADED:
            _set_radar_state(RadarState.SYNCING)
        _dump_radar_boot_state()
```

And re-arm the payload-promotion edge so it fires whenever we leave the READY state:

```python
# inside _on_message, replace the existing first_payload_seen branch
if _radar_boot_state is not None:
    _radar_boot_state.last_payload_at = time.time()
    _radar_boot_state.entropy_window_count = len(_entropy_windows)
    if (
        item.get("event_type") in {"book", "price_change", "last_trade_price"}
        and _radar_boot_state.state in {RadarState.SYNCING, RadarState.CONNECTING, RadarState.STARTING}
    ):
        # D179b: re-arm promotion every time we leave READY
        if not _radar_boot_state.first_payload_seen:
            _radar_boot_state.first_payload_seen = True
            logger.info("[RADAR_BOOT][D179b] first_payload_seen=True ...")
        if _radar_boot_state.subscription_sync_ok:
            _set_radar_state(RadarState.READY)
    _dump_radar_boot_state()
```

The transition table already allows `SYNCING → READY` and `DEGRADED → SYNCING`, so **no change to `_ALLOWED_TRANSITIONS` is required**.

### Logic-error checklist

| # | Trap | Mitigation |
|---|------|-----------|
| L1 | Setting `state=SYNCING` may also cancel boot-timeout logic (L3504). | The boot-timeout code at L3503-3513 only fires while `state in {STARTING, CONNECTING, SYNCING}` *and* `first_payload_seen=False`. After D179b, `first_payload_seen=True` once first-ever payload arrives, so re-entering SYNCING from DEGRADED will not raise `RadarBootError`. Verify by running the boot-timeout test in §3. |
| L2 | If WS reconnects but no payload follows for 45s, the radar would now sit in SYNCING (not DEGRADED) — which is *worse* observability. | Acceptable: SYNCING is a transient state and the ws_runner loop reconnects on no-msg. If 45s no payload, `boot_timeout` would have fired during the first boot; on recovery it does not. We accept "SYNCING with stale data" as a clearer signal than "DEGRADED with fresh data". |
| L3 | `mc.on_ws_connected()` may emit duplicate metrics. | Existing semantics preserved; D179b only adds a state-machine call after `mc`. |
| L4 | Test harness fakes `_radar_boot_state` directly; new branches need coverage. | See §3 tests below. |
| L5 | The transition `DEGRADED → SYNCING` is allowed only from `_set_radar_state` with `force=False`. Double-check the call passes the same default. | Yes — `_set_radar_state(RadarState.SYNCING)` uses `force=False`. The `DEGRADED → SYNCING` row exists in `_ALLOWED_TRANSITIONS`. |

---

## 3. Test plan (TDD)

```python
# tests/test_radar_state_d179b.py
from panopticon_py.hunting import run_radar
from panopticon_py.hunting.run_radar import RadarState, RadarBootState

def _fresh_state(state):
    run_radar._radar_boot_state = RadarBootState(boot_id="t", state=state)
    run_radar._radar_boot_state.subscription_sync_ok = True
    run_radar._radar_boot_state.first_payload_seen = True

def test_d179b_degraded_ws_reconnect_transitions_to_syncing():
    _fresh_state(RadarState.DEGRADED)
    # Mimic the body of _on_ws_connected (post D179b)
    if run_radar._radar_boot_state.state == RadarState.DEGRADED:
        run_radar._set_radar_state(RadarState.SYNCING)
    assert run_radar._radar_boot_state.state == RadarState.SYNCING

def test_d179b_payload_after_reconnect_promotes_to_ready():
    _fresh_state(RadarState.SYNCING)
    # In _on_message, with subscription_sync_ok already True, payload should promote
    assert run_radar._radar_boot_state.subscription_sync_ok
    if run_radar._radar_boot_state.state in {RadarState.SYNCING, RadarState.CONNECTING}:
        run_radar._set_radar_state(RadarState.READY)
    assert run_radar._radar_boot_state.state == RadarState.READY

def test_d179b_invalid_transition_logged_not_raised():
    """Guard: STARTING -> READY directly should still be rejected."""
    _fresh_state(RadarState.STARTING)
    run_radar._set_radar_state(RadarState.READY)  # not in allowed transitions
    assert run_radar._radar_boot_state.state == RadarState.STARTING
```

The third test protects against accidentally widening the allowed transitions during this sprint.

---

## 4. Step-by-step execution plan

1. Add the failing tests above (`tests/test_pol_monitor_d179b.py`, `tests/test_radar_state_d179b.py`). Run `pytest -k d179b` → all fail.
2. Apply Bug 1 fix (`global` statement). Re-run `pytest tests/test_pol_monitor_d179b.py -x` → green.
3. Apply Bug 2 fixes (two small edits in `run_radar.py`). Re-run `pytest tests/test_radar_state_d179b.py -x` → green.
4. Run full suite `pytest -x` → ≥95 green.
5. Bump versions:
   * `pol_monitor.py`: `_PROCESS_VERSION = "v1.0.1-D179b"`
   * `run_radar.py`: `PROCESS_VERSION = "v1.3.5-D179a"` if combined, otherwise `"v1.3.4-D179b"`
   * `run_hft_orchestrator.py`: `PROCESS_VERSION = "v1.7.10-D179b"`
   * Update `run/versions_ref.json` in same commit.
6. `scripts/restart_all.ps1`. Wait 5 minutes (this sprint does NOT need 30 min soak).
7. Validate (§5).
8. Commit & push to GitHub. Write architect handoff.

---

## 5. Sprint acceptance criteria

* `pytest -k d179b` all green.
* After restart:
  * `run/orchestrator.err.log` contains **zero** `polygon crashed` lines.
  * `[POL_LISTENER]` log shows `WSS subscribed sub_id=...` and steady-state behaviour.
  * `data/process_manifest.json` `radar.status` flips from `degraded` back to `ready` within 30 seconds of any WS reconnect (verify by killing the WS upstream connectivity briefly OR by tail-grepping `[RADAR_BOOT] state=ready` after a reconnect log).
  * No new `WARNING [RADAR_BOOT] invalid transition ignored` entries.

---

## 6. Files touched

| File | Change | Lines |
|------|--------|-------|
| `panopticon_py/hunting/pol_monitor.py` | add `global _pol_ws_consecutive_failures` inside `_wss_loop`; bump `_PROCESS_VERSION` | L498 area, L36 |
| `panopticon_py/hunting/run_radar.py` | `_on_ws_connected` adds DEGRADED→SYNCING; `_on_message` first-payload branch re-armed | L3350-L3356, L2629-L2641 |
| `run/versions_ref.json` | bump pol_monitor library, radar, orchestrator | bottom |
| `tests/test_pol_monitor_d179b.py` | new | new |
| `tests/test_radar_state_d179b.py` | new | new |
| `panopticon_py/hunting/INDEX.md` | optional: mark `_on_ws_connected` ✅ ACTIVE-D179b | append |

No DB schema changes. No contract changes.
