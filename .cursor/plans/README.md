# D179 Sprint Plans — Index for the Coding Agent

> Author: 2nd Architect (independent review)
> Status: **PLAN ONLY — coding agent executes one sprint per commit/PR**
> Read order is sequential.

| Order | File | Priority | What it does |
|------:|------|----------|--------------|
| 0 | [D179_master_diagnosis.md](D179_master_diagnosis.md) | — | Required reading. Hard evidence (file:line + log line numbers) for every claim, plus cross-cutting constraints. |
| 1 | [D179b_polygon_and_radar_state.md](D179b_polygon_and_radar_state.md) | **P0** | 2 small bug-fixes: `pol_monitor.py` UnboundLocalError; radar `DEGRADED → READY` recovery. ~5 min implementation, 5 min soak. Removes a chronic crash that confuses every later diagnosis. |
| 2 | [D179a_entropy_gate.md](D179a_entropy_gate.md) | **P0** | The actual `z_eval_ok=0` fix. Three coupled changes: D178-deadlock break, periodic H-sample timer, T1 gate accounting. 30-min soak validates. |
| 3 | [D179d_diagnostics_hardening.md](D179d_diagnostics_hardening.md) | P1 | Renames the misleading `Buffer Events` / `H Hist` heartbeat line; adds per-tier breakdown; manifest-converge tracing. Prevents the next "0 z_eval_ok" misread. |
| 4 | [D179c_arb_visibility.md](D179c_arb_visibility.md) | P2 | Surfaces arb_scanner stats into `rvf_live_snapshot.json`; adds self-explaining `[ARB_TOKENS]` log; documents process boundary in `FUNCTION_STATUS.md`. Closes issues B and D for good. |

## Hard rules for every sprint

1. Read `AGENTS.md` first; the rules in *Coding Agent 強制規則 D101–D120* (RULE-CLOSURE-1, RULE-TIME-1, RULE-VER-*, RULE-PATH-1) are non-negotiable.
2. **TDD.** Every sprint has a "failing test first" step — do not skip.
3. **One sprint = one commit (or one PR).** Revert-friendly.
4. **Restart after each sprint.** Use `scripts/restart_all.ps1`. Don't validate against a stale process.
5. **Architect handoff after each sprint.** Use the template in `AGENTS.md`. Push to GitHub before writing the handoff (the architect reads live code from `https://github.com/w062c30/panopticon-private-v2`).
6. **No threshold tuning in this epic.** NQ-4 (z-threshold) stays deferred until A produces a real `z_dist_60s` distribution.
7. **No live trading toggles.** `LIVE_TRADING=false` stays.

## Cumulative acceptance criteria after all four sprints

See `D179_master_diagnosis.md` §5. Verbatim, all of:

* `entropy_fires_60s >= 1` at least once in `run/orchestrator.log`.
* `z_eval_ok > 0` strictly increasing across `[D75_ENTROPY_GATE]` cycles.
* `z_ready_count >= 8` and `locked_count < total/2` in `data/entropy_status.json`.
* `queue.processed_60s >= 1` and a populated `arb` block in `data/rvf_live_snapshot.json`.
* `run/orchestrator.err.log` contains zero `polygon crashed` lines after restart.
* `run/process_manifest.json` `radar.status` toggles `ready/degraded` correctly with WS state.
* `paper_trades` row count strictly increases over the soak window.
* Regression test count ≥ 95 passes; new D179 tests all pass.
