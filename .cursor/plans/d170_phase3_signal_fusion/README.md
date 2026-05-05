# D170 — Phase 3 L4 Signal Fusion (PATH-A + PATH-B Merge)

> **Sprint**: D170 | **Predecessor**: D169 (PolygonListener + Wallet Engine basics) | **Successor**: D171 (Insider Score precision)
> **Duration target**: 3 calendar days | **Risk**: MEDIUM (touches signal_engine.py, isolated logic) | **STATUS: BLOCKED ON D169 + AQ-7 (new — see below)**

---

## ARCHITECT BLOCK — DO NOT START WITHOUT RULING

**AQ-7 (new question raised by D170 design)**:

PATH-A produces entropy-based ALERT (`asset_id`, direction inferred from `z` sign, confidence from `|z|`). PATH-B (D169 → D171) produces wallet-based ALERT (`wallet_address`, `market_id`, side, size, confidence). The two ALERTs share `market_id` but otherwise have orthogonal schemas.

**Question**: How does L4 fusion compute "same direction"?
- **Option A**: PATH-A direction = sign(`z`); PATH-B direction = wallet's net side (BUY/SELL). Both must map to the same outcome on the market (YES/NO or numerical direction).
- **Option B**: Trust PATH-B as primary; PATH-A only boosts confidence when present.
- **Option C**: Train a fusion classifier (out of D170 scope; D171 backlog).

**Default**: Option A (most explicit). Architect must rule.

---

## Sprint goal

Add L4 fusion logic to `panopticon_py/signal_engine.py` so two independent ALERT sources (entropy gate + insider wallet) combine before firing:

| Rule | Behavior |
|---|---|
| Same direction within 5 min | confidence × 1.5; one fire (deduped) |
| Opposite direction within 5 min | skip both (uncertain) |
| Single-source ALERT | normal threshold (`MIN_ENTROPY_Z_THRESHOLD = -4.0` for PATH-A; PATH-B has its own threshold from D171) |
| Same source repeating within 5 min | dedup |

D170 ships **with PATH-B as a stub**. PATH-B real ALERT generation lands in D171. D170's signal fusion is therefore:
- Live for PATH-A.
- Stubbed for PATH-B (always returns no alert until D171).
- Verified end-to-end with synthetic PATH-B ALERTs in unit tests.

---

## Critical risks

### CR-1: `Invariant 5.1` — signal_engine MUST NOT touch `paper_trades` or `wallet_market_positions`

`panopticon_py/signal_engine.py` line 11 docstring states this. L4 fusion logic only writes to `execution_records`. Caller (orchestrator) routes execution to paper/real path.

### CR-2: 5-min dedup window — clock source

Use `time.monotonic()` for the dedup window (per AGENTS.md time contract — runtime durations use monotonic, not UTC). The TTL cache from P2-T2 is reusable.

### CR-3: AGENTS.md "Committee Shadow Experiment isolation"

If `committee_score` or `disagreement_index` already exists in `signal_engine.py`, this fusion logic is **ADDITIONAL** to that, not a replacement. The shadow experiment must remain isolated; D170 fusion belongs to baseline path.

---

## Task ordering

Single task (P3-T1) — small enough to not split. Composer 2 backup; primary Minimax M2.7.

---

## D170 exit criteria

- [ ] `signal_engine.py` accepts both PATH-A and PATH-B ALERT inputs.
- [ ] Fusion rules implemented per table above.
- [ ] Unit tests cover: same-direction boost, opposite-direction skip, single-source pass-through, dedup.
- [ ] Soak (1 hour, dry-run + organic): ≥ 5 organic PATH-A fires; PATH-B stub does not interfere.
- [ ] No regressions in D167–D169 tests.
- [ ] Versions: `signal_engine.py` MINOR bump; `run_hft_orchestrator.py` PATCH if wiring changes.

---

## Files modified by D170

| File | Tasks | Change scope |
|---|---|---|
| `panopticon_py/signal_engine.py` | P3-T1 | Add `L4Fuser` class; extend fire path. |
| `tests/test_d170_signal_fusion.py` | P3-T1 | New unit tests. |
| `run_hft_orchestrator.py` | P3-T1 wiring | Wire PATH-A and (stubbed) PATH-B ALERT inputs into fuser. |
| `run/versions_ref.json` | all | Sync. |

---

## Architect deferrals

| Code | Question | Default | Action |
|---|---|---|---|
| AQ-7 | Direction mapping (PATH-A vs PATH-B) | Option A | Architect must rule. |
| NQ-1 | Confidence boost factor (default 1.5×) | 1.5 | Confirm. |
| NQ-3 | PATH-B FOLLOW_THRESHOLD value | Defer to D171 | Stub uses high threshold (no fires from PATH-B in D170). |
