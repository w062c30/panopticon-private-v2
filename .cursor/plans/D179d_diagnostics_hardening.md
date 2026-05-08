# D179d — Diagnostics & Heartbeat Hardening

> Priority: **P1** (no functional change to the trade path; prevents the next ambiguous symptom from devouring an architect-day).
> Read first: `D179_master_diagnosis.md` §1.4 — every misreading in this epic came from misnamed metrics.
> Run AFTER D179a + D179b (so the fixes themselves can be observed).

---

## 1. What this sprint fixes

The previous architect's hypothesis was wrong because three log lines/metric names actively mislead:

1. `[RADAR ...] Buffer Events: %s` is `len(recent)`, the deque of fired events. It is *not* the per-token EW deque size. (`run_radar.py:3662`.)
2. `H Hist: %s` is the **sum** across all per-token `_h_history` lengths. (`run_radar.py:3596, 3660-3665`.)
3. `[D75_ENTROPY_GATE] eval=N` excludes T1 markets. (`run_radar.py:2939`.) This will be partially fixed by D179a, but the metric still benefits from a per-tier breakdown.

Plus the script-level `Wait-ManifestConverge` race is opaque — it should log *why* convergence failed (file-locked? PID mismatch? JSON parse error?). Today the operator just sees `WARN [orchestrator] manifest did not converge`, with no actionable detail.

---

## 2. Specific changes

### 2.1 Rename + augment the radar heartbeat

```python
# run_radar.py — replace the existing heartbeat log around L3659-3665
# Compute per-tier breakdown
tier_counts = {"t1": 0, "t2": 0, "t3": 0, "t5": 0, "other": 0}
tier_h_hist_sum = {"t1": 0, "t2": 0, "t3": 0, "t5": 0, "other": 0}
tier_z_ready = {"t1": 0, "t2": 0, "t3": 0, "t5": 0, "other": 0}
for tok, ew in _entropy_windows.items():
    t = (ew.tier or "other").lower()
    bucket = t if t in tier_counts else "other"
    tier_counts[bucket] += 1
    tier_h_hist_sum[bucket] += len(ew._h_history)
    if (not ew._trigger_locked) and len(ew._h_history) >= ew.min_history_for_z:
        tier_z_ready[bucket] += 1

logger.info(
    "[RADAR_HB][D179d] pid=%s fired_events_recent=%d any_locked=%s "
    "ew_count_total=%d "
    "per_tier_count={t1:%d,t2:%d,t3:%d,t5:%d,other:%d} "
    "per_tier_h_hist_sum={t1:%d,t2:%d,t3:%d,t5:%d,other:%d} "
    "per_tier_z_ready={t1:%d,t2:%d,t3:%d,t5:%d,other:%d}",
    os.getpid(),
    len(recent),                                  # explicitly named "fired"
    state.get("trigger_locked"),
    sum(tier_counts.values()),
    *tier_counts.values(),
    *tier_h_hist_sum.values(),
    *tier_z_ready.values(),
)
```

Drop the legacy `Buffer Events: 0, Trigger Locked: ..., H Hist: ...` line (or keep both for one sprint as a deprecation cushion). Document the new line in `EXPERIENCE_PLAYBOOK.md`.

### 2.2 Per-tier eval split in `D75_ENTROPY_GATE`

Add four `nonlocal` counters in `_on_message` (post-D179a):

```python
nonlocal _entropy_eval_by_tier   # dict[str, int]
nonlocal _entropy_z_eval_ok_by_tier
```

Increment in the same place `_entropy_eval_total` is bumped. In the 60 s heartbeat block (~L3635), append:

```python
logger.info(
    "[D75_ENTROPY_GATE_TIER] eval_by_tier=%s z_ok_by_tier=%s",
    dict(_entropy_eval_by_tier),
    dict(_entropy_z_eval_ok_by_tier),
)
```

This single line tells the operator instantly whether T1 is firing the gate or whether all action is in T2/T3.

### 2.3 PowerShell manifest-converge tracing

In `scripts/restart_all.ps1` `Wait-ManifestConverge` (L49-72), emit a single-line reason on failure:

```powershell
Write-Warning ("  [MANIFEST_TRACE] svc={0} expected_pid={1} last_manifest_pid={2} alive={3} parse_ok={4}" -f `
    $ServiceName, $ExpectedPid, ($entry.pid 2>$null), $alive, $parseOk)
```

Track:

* `parseOk`: whether `Get-Content | ConvertFrom-Json` succeeded at least once during the wait.
* `last_manifest_pid`: the PID we *did* see in the file (could be 0, stale, or correct PID but non-alive).
* `alive`: result of `Get-CimInstance -Filter "ProcessId=$ExpectedPid"`.

This makes the user's reported "manifest did not converge" line self-debugging.

### 2.4 Surface boot-state into the heartbeat log too

The boot-state file `data/radar_boot_state.json` is the source of truth for `RadarState` transitions but rarely read live. Add a one-liner each minute mirroring `state`, `ws_connected`, `first_payload_seen`, `subscription_sync_ok`. (`run_radar.py` heartbeat block.)

```python
if _radar_boot_state is not None:
    logger.info(
        "[RADAR_BOOT_HB][D179d] state=%s ws_connected=%s first_payload_seen=%s "
        "subscription_sync_ok=%s last_payload_age_s=%.1f",
        _radar_boot_state.state.value,
        _radar_boot_state.ws_connected,
        _radar_boot_state.first_payload_seen,
        _radar_boot_state.subscription_sync_ok,
        time.time() - (_radar_boot_state.last_payload_at or time.time()),
    )
```

### 2.5 `run/process_manifest.json` self-consistency assertion

Add a tiny periodic check inside orchestrator's main loop (the same `while True: await asyncio.sleep(5)` block, conditional on a 60 s sub-interval) that re-reads its own manifest entry and warns if `pid != os.getpid()` or `version != PROCESS_VERSION`. This catches the kind of "phantom PID" the user reported (the symptom from issue C narrative); it is observation-only and changes nothing about behaviour.

```python
# run_hft_orchestrator.py near the existing 5s loop
async def _self_check_manifest():
    while not _close_event.is_set():
        await asyncio.sleep(60.0)
        try:
            with open("run/process_manifest.json", "r", encoding="utf-8") as f:
                m = json.load(f)
            entry = m.get("orchestrator") or {}
            if int(entry.get("pid") or 0) != os.getpid():
                logger.warning(
                    "[ORCH_SELFCHECK] manifest pid=%s != actual %d (possible double-start)",
                    entry.get("pid"), os.getpid(),
                )
            if entry.get("version") != PROCESS_VERSION:
                logger.warning(
                    "[ORCH_SELFCHECK] manifest version=%s != actual %s (stale write?)",
                    entry.get("version"), PROCESS_VERSION,
                )
        except Exception as exc:
            logger.debug("[ORCH_SELFCHECK] skipped: %s", exc)
```

Hook this as another asyncio task in `main_async()`.

---

## 3. Logic-error checklist

| # | Trap | Mitigation |
|---|------|-----------|
| L1 | Per-tier counters reset on every 60 s tick — but if two heartbeats race, counts get wiped mid-window. | They live in `_on_message`'s closure (single asyncio task); no race. |
| L2 | Heartbeat now logs ~5 lines/min with nested dict reprs; log file growth doubles. | Acceptable: log is rotated by external infra; the gain in operability is large. |
| L3 | `_entropy_eval_by_tier` defaultdict left growing if new tier strings appear. | Restrict to `t1/t2/t3/t5/other` like the existing `_evt_count`. |
| L4 | PowerShell `$entry.pid 2>$null` swallows real errors. | Use `try { $manifestPid = [int]$entry.pid } catch { $manifestPid = -1 }` so the diagnostic line shows -1 vs 0 vs the actual PID. |
| L5 | `_self_check_manifest` opens the file with no lock — concurrent write may yield a half-flushed JSON. | Use `try/except json.JSONDecodeError`; log `debug` and continue. |
| L6 | Adding `_self_check_manifest` adds another task to cancel on shutdown. | Append to the existing cancel list at L1224-1228. |

---

## 4. Test plan

```python
# tests/test_radar_heartbeat_d179d.py
def test_per_tier_counts_match_population():
    from panopticon_py.hunting import run_radar as rr
    rr._entropy_windows.clear()
    for i, t in enumerate(["t1", "t1", "t2", "t3", "t5"]):
        ew = rr.EntropyWindow(tier=t)
        ew._h_history.extend([0.5, 0.6, 0.7, 0.8, 0.9])  # > min_history
        ew._trigger_locked = False
        rr._entropy_windows[f"k{i}"] = ew
    # call the helper that builds the per-tier dict (extract into private helper)
    counts = rr._build_tier_breakdown()
    assert counts["count"] == {"t1": 2, "t2": 1, "t3": 1, "t5": 1, "other": 0}
    assert counts["z_ready"]["t1"] == 2
```

(Refactor the inline computation into a `_build_tier_breakdown()` helper to make it testable.)

---

## 5. Step-by-step

1. Refactor inline tier breakdown into helper `_build_tier_breakdown()`.
2. Add failing tests; run.
3. Replace the radar heartbeat log lines.
4. Add per-tier counters + `[D75_ENTROPY_GATE_TIER]` log.
5. Add `[RADAR_BOOT_HB][D179d]`.
6. Modify `Wait-ManifestConverge` to emit `[MANIFEST_TRACE]` on failure.
7. Add `_self_check_manifest` task in orchestrator + shutdown cancel.
8. Bump versions + `run/versions_ref.json`.
9. Run tests; restart; observe new heartbeat shape.

---

## 6. Acceptance criteria

* New heartbeat lines appear in `run/orchestrator.log` and the deprecated "Buffer Events" line is either gone or annotated as deprecated.
* `[D75_ENTROPY_GATE_TIER]` shows non-zero `eval_by_tier["t1"]` (post-D179a).
* `Wait-ManifestConverge` failures (if any) emit a `[MANIFEST_TRACE]` line with the parse / pid / alive triplet.
* Tests in `tests/test_radar_heartbeat_d179d.py` green.

---

## 7. Files touched

| File | Change |
|------|--------|
| `panopticon_py/hunting/run_radar.py` | new helper `_build_tier_breakdown`, replace heartbeat log, add tier counters, add boot-state heartbeat |
| `run_hft_orchestrator.py` | `_self_check_manifest` task |
| `scripts/restart_all.ps1` | `[MANIFEST_TRACE]` line in `Wait-ManifestConverge` |
| `EXPERIENCE_PLAYBOOK.md` | document the renamed metrics |
| `tests/test_radar_heartbeat_d179d.py` | new |
| `run/versions_ref.json` | version bumps |

No DB schema, no contract, no signal-engine impact.
