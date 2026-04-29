# Project Experience Playbook

This document captures reusable engineering lessons for this project.

## Entry: Global skill install and verification workflow

### Trigger symptom
- Requested capabilities are missing or inconsistently available across coding agents.

### True root cause layer
- Tooling/environment layer: required skills not installed globally, or installed only for limited agent targets.

### Affected layers
- Local tooling
- Agent runtime behavior
- Development workflow reliability

### Fastest detection checks
1. Run `npx skills ls -g --json` and confirm required skill names are present.
2. If a skill name is ambiguous, run `npx skills find "<term>"` to identify canonical package/skill IDs.
3. Re-run `npx skills ls -g --json` after install to confirm runtime-visible availability.

### Durable prevention rules
- For capability requests, verify install state first before adding or changing project code.
- Prefer global install for shared workflows: `npx skills add <owner/repo> -g -y`.
- Treat skills as contracts for behavior; verify by observed runtime listing, not assumption.

### Regression checklist
- [ ] Required skills appear in global list output.
- [ ] Requested capability aliases map to actual installed skill names.
- [ ] Installation was done with non-interactive flags for reproducibility (`-g -y`).

## Entry: Communication language contract in agent workflows

### Trigger symptom
- Inconsistent language across user-facing outputs and internal agent coordination causes confusion and review friction.

### True root cause layer
- Policy/configuration layer: missing explicit communication contract for audience-specific language.

### Affected layers
- Agent orchestration
- CLI/UI text surfaces
- Documentation and review workflow

### Fastest detection checks
1. Verify `.cursorrules` includes explicit audience split (human/UI vs agent-to-agent).
2. Confirm user-facing responses are Traditional Chinese in actual runtime interactions.
3. Confirm internal agent coordination text remains English.

### Durable prevention rules
- Define language policy as an always-on project rule instead of relying on ad-hoc prompts.
- Treat user-visible text as contract surface and synchronize docs/rules with behavior changes.
- Re-verify language policy after rule or workflow updates.

### Regression checklist
- [ ] `.cursorrules` exists and is project-scoped.
- [ ] Human-facing communication is Traditional Chinese.
- [ ] Agent-to-agent communication is English.

---

## Entry: `.env` blank-key trap — `unify_env` does not overwrite existing empties

### Trigger symptom
- `DISCOVERY_HISTORY_MIN_OBS=''` → `ValueError: invalid literal for int() with base 10: ''`
- `DISCOVERY_PROVIDER` appears as `gamma_public` even though `.env` has `dual_track`
- Discovery loop returns all-zero candidate counts silently
- All 3 tracks report 0 data despite live API connectivity

### True root cause chain

**Layer 1 — `.env` corruption**
The `.env` file committed to the repo had 10 keys present but set to empty strings (e.g. `DISCOVERY_HISTORY_MIN_OBS=` with no value). These were the result of `unify_env.py` previously running against a partially-filled `.env`.

**Layer 2 — `unify_env.py` bug (additive-only merge)**
`unify_env.py` only *adds* keys that are missing from `.env`; it does **not** update keys that exist but are blank. So running it against an `.env` with `KEY=` (empty) reports "Added 0 missing keys" — a false negative. The file remains broken.

**Layer 3 — `ensure_shadow_mode_env()` partial fallback**
`start_shadow_hydration.py`'s `ensure_shadow_mode_env()` sets fallback values **only when an env var is absent**:
```python
if not env.get("DISCOVERY_HISTORY_MIN_OBS", "").strip():
    env["DISCOVERY_HISTORY_MIN_OBS"] = "20"
```
Because the key exists (just as `''`), `env.get("DISCOVERY_HISTORY_MIN_OBS", "")` returns `''`, the `.strip()` passes, and the fallback is **never applied**. Same for `DISCOVERY_PROVIDER` — the stale shell-session value `gamma_public` wins.

**Layer 4 — `int(os.getenv(...))` crash path**
`discovery_main_loop` calls `make_hybrid_history_fetcher_with_stats` at line 266 using `int(os.getenv("DISCOVERY_HISTORY_MIN_OBS", "20"))` directly (not the safe `_env_int()` helper). With `''` from the corrupted `.env`, this raises `ValueError` before the loop even starts.

**Layer 5 — Silent failure in discovery tracks**
After the crash path was avoided (e.g. shell reloaded), the system fell through to `gamma_public` provider (stale shell env), which has no fallback chain. Gamma API failure → mock fallback → zero candidates. Track B's leaderboard has a strict `pnl > 5000` threshold with no relaxation, returning `[]` silently.

**Layer 6 — Scrubber over-filtering**
`scrub_wallet_for_discovery` with empty history blindly passed all wallets through — but the upstream candidate feeds were all empty anyway. The combination of upstream empty feeds + non-strict scrubber meant zero wallets entered the tracking system.

### Fastest detection checks
1. Run `python scripts/unify_env.py` and verify it says "Added N missing keys" with N > 0 if any `.env` key is blank.
2. Check `python -c "import os; print({k:v for k,v in os.environ.items() if 'DISCOVERY' in k or 'LEADERBOARD' in k})"` to see actual runtime env values after `load_repo_env()`.
3. Run `python -m panopticon_py.hunting.discovery_loop --provider dual_track --run-once` with `--run-once` to get a real-time cycle report with per-track candidate counts.

### Durable prevention rules
- **`unify_env.py` must treat blank values as missing**: change `if k not in merged` to `if k not in merged or not merged[k].strip()`. This is the single highest-leverage fix.
- **`ensure_shadow_mode_env()` should check both absence and blankness** using `if not env.get(k):` instead of `if not env.get(k, "").strip()` — or better, always prefer the `_env_int`/`_env_float` helpers that have built-in defaults.
- **`load_repo_env()` should override blanks too** when called with `override=True` (which `discovery_main_loop` can pass), since `.env` represents the canonical runtime contract.
- **Add a startup sanity check** in `discovery_main_loop`: validate required numeric env vars with `_env_int()` defaults before use, and fail fast with a clear message instead of crashing on `int('')`.
- **Discovery tracks must never return `[]` silently**: every track/fallback should log at WARN level when its candidate list is 0, so zero-data cycles are visible in logs without needing to parse runtime reports.

### Regression checklist
- [ ] `python scripts/unify_env.py` prints "Added N missing keys" with N == 0 when all `.env` keys are properly populated.
- [ ] `python -c "import os; print(os.getenv('DISCOVERY_HISTORY_MIN_OBS'))"` returns a non-empty string matching `.env.example`.
- [ ] `python -m panopticon_py.hunting.discovery_loop --provider dual_track --run-once` produces a runtime report with `track_a_count > 0` and `track_b_count > 0`.
- [ ] `fingerprint_scrubber` no longer drops all no-history wallets — wallets with candidate source signal pass through to the uncertain bucket.

---

## Entry: Signal Engine v4-FINAL rebuild — asyncio.Queue migration + DB lock deadlock

### Trigger symptom
- `ModuleNotFoundError: No module named 'panopticon_py.hft.hft_execution_gate'` during `run_hft_orchestrator.py` startup.
- `sqlite3.OperationalError: database is locked` from `analysis_worker` during concurrent hydration + orchestrator runs.
- `signal_engine` not processing events despite OFI data flowing.
- `entropy_state.events: 0` in logs despite active Polymarket WebSocket.

### True root cause chain

**Layer 1 — Stale module reference after file deletion**
`hft_execution_gate.py` was deleted per architectural ruling (Q1), but `panopticon_py/hft/__init__.py` still imported `GateDecision`, `HFTExecutionGate`, `ShockHandler` from it. Any import of the `hft` package triggered `ModuleNotFoundError`.

**Layer 2 — Overlapping subprocess management**
Both `start_shadow_hydration.py` and `run_hft_orchestrator.py` independently spawned `discovery_loop` subprocess. With both running, concurrent writes to `data/panopticon.db` from `discovery_loop` (via hydration) and `run_hft_orchestrator` itself caused SQLite lock contention.

**Layer 3 — asyncio.Queue signal source gap**
The `_run_async` in `signal_engine.py` still had a `_poll_db_fallback` branch (Q10 not applied), and the orchestrator was still spawning `signal_engine` as a subprocess instead of an asyncio task (Q11 not applied). The OFI path had no route to actually deliver events to the signal engine.

**Layer 4 — Missing OFI→Polymarket mapping**
`OFI_MARKET_MAP` did not exist initially. The orchestrator's `run_hyperliquid_ofi` needed to map Hyperliquid markets (e.g. `BTC-USD`) to Polymarket market IDs before putting events into `signal_queue`. Without the map, OFI shocks were dropped.

### Fastest detection checks
1. `python -c "from panopticon_py.hft import *" ` — confirms module import error.
2. `Get-Process python | Where-Object {$_.Path -like '*Antigravity*'}` — checks for concurrent Python processes.
3. `python -c "import sqlite3; c=sqlite3.connect('data/panopticon.db').cursor(); print(c.execute('SELECT COUNT(*) FROM execution_records').fetchone())"` — checks DB accessibility.
4. `curl http://127.0.0.1:8001/api/system_health/status` — verifies orchestrator alive.

### Durable prevention rules
- **When deleting a module, always audit `__init__.py` imports first**: orphaned imports cause cryptic failures at the call site.
- **Use a DB-based advisory lock** (`_process_locks` table + `INSERT OR REPLACE`) to enforce mutual exclusion between processes that share a SQLite DB. This is WAL-safe and works across process boundaries.
- **Designated startup roles are mutually exclusive**: `start_shadow_hydration.py` = Observer Launcher (discovery + analysis only); `run_hft_orchestrator.py` = Full real-time system. Never run both simultaneously.
- **All signal sources must route through `asyncio.Queue[SignalEvent]`**: no DB polling, no subprocess spawn for the signal engine. The orchestrator owns the queue and the SE runs as an asyncio task.
- **Maintain `OFI_MARKET_MAP` manually**: Polymarket's market structure does not allow automatic Hyperliquid→Polymarket correlation. Add entries manually when new markets are mapped.

### Regression checklist
- [ ] `python run_hft_orchestrator.py` starts without `ModuleNotFoundError`.
- [ ] `python scripts/start_shadow_hydration.py` runs alone without DB lock errors.
- [ ] Only one of the two launcher scripts runs at any given time (enforced by advisory lock).
- [ ] OFI shocks appear in `signal_queue` and are acknowledged by signal engine logs.
- [ ] `execution_records` shows new entries with `mode='PAPER'` and `source` populated.
- [ ] `entropy_state.events` increments after Polymarket entropy events.

---

## Entry: Relic table FK trap — orphaned FOREIGN KEY on internal-UUID columns

### Trigger symptom
- `FOREIGN KEY constraint failed` every N seconds in signal_engine logs
- `execution_records` rows never written — `accepted` count stuck at 0
- L2/L3/L4 pipeline appears "alive" (no process crash) but produces zero output
- Error is swallowed by `try/except` — system looks healthy in PID checks

### True root cause chain

**Layer 1 — Relic table not fully decommissioned**
`strategy_decisions` was marked as a relic table and never written to,
but `db.py` still declared `FOREIGN KEY(decision_id) REFERENCES strategy_decisions(decision_id)`
on `execution_records`. The schema contract was broken at table creation time.

**Layer 2 — Internal UUID as FK parent**
`signal_engine.py` generates a fresh `uuid4()` as `decision_id` on every event.
It never writes to `strategy_decisions` first. The FK check always fails because
the parent row does not exist.

**Layer 3 — Silent failure masking**
`append_execution_record()` raises `IntegrityError` which is caught upstream.
The orchestrator does not crash or restart — it silently discards every write.
From the outside, the system looks healthy: PID alive, WS events arriving, Kyle λ growing.
Only a direct DB row count reveals the blockage.

**Layer 4 — Agent prompt did not audit FK chain**
Prompts correctly flagged `strategy_decisions` as a relic table (do not drop, do not write),
but failed to cross-check whether any *other* table still held a FK reference to it.
The constraint survived multiple schema migrations because SQLite does not enforce FK
by default — it only triggers at write time when `PRAGMA foreign_keys = ON`.

### Fastest detection checks
1. `sqlite3 panopticon.db ".schema execution_records"` — look for `REFERENCES strategy_decisions`
2. `grep -n "FOREIGN KEY\|strategy_decisions" panopticon_py/db.py`
3. `sqlite3 panopticon.db "PRAGMA foreign_key_list('execution_records');"` — lists all FK constraints
4. `ls -t logs/orchestrator_*.log | head -1 | xargs grep -c "FOREIGN KEY\|IntegrityError"` — count silent failures

### Durable prevention rules
- **When retiring a table, always audit FK reverse dependencies**:
  `grep -rn "REFERENCES <table_name>" panopticon_py/` before marking any table as relic.
- **Internal UUIDs must never be FK children**: if a column is generated at runtime
  (uuid4, snowflake ID), it cannot reference an external parent table.
  Remove the FK or make the parent table always-written first.
- **SQLite FK enforcement is opt-in**: `PRAGMA foreign_keys = OFF` by default.
  Add `PRAGMA foreign_keys = ON` at DB connection time in `db.py` so violations surface
  immediately during development, not silently in production.
- **Schema change = add to regression checklist**: any `ALTER TABLE` or `CREATE TABLE`
  change should be followed by a round-trip insert test confirming no FK violations.
- **Agent prompts must include FK audit step** when schema migrations happen.

### SQLite schema rebuild pattern (no DROP CONSTRAINT support)
```sql
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
  ALTER TABLE execution_records RENAME TO execution_records_bak;
  CREATE TABLE execution_records ( ... ); -- without FK line
  INSERT INTO execution_records SELECT * FROM execution_records_bak;
  DROP TABLE execution_records_bak;
COMMIT;
PRAGMA foreign_keys = ON;
```

### Regression checklist
- [ ] `sqlite3 panopticon.db "PRAGMA foreign_key_list('execution_records');"` returns empty.
- [ ] `db.py` CREATE TABLE for `execution_records` contains no `REFERENCES strategy_decisions`.
- [ ] Direct insert test with fresh UUID `decision_id` (no prior `strategy_decisions` write) succeeds.
- [ ] `grep -c "FOREIGN KEY\|IntegrityError" <latest_log>` returns 0 after restart.

---

## Entry: Cross-layer timing mismatch — slow cadence source vs narrow lookback window

### Trigger symptom
- Market overlap confirmed (whale wallets + entropy_drop on same market_id) ✅
- `execution_records accepted=1` still 0 despite overlap ❌
- `_collect_insider_sources()` returns 0 sources even though wallet_observations has rows
- DB query outside the pipeline shows wallets exist; query inside pipeline returns empty

### True root cause chain

**Layer 1 — Two independent cadences, one shared time window**
`whale_scanner` injects wallet_observations every 300s (WHALE_SCAN_INTERVAL_SEC).
`signal_engine._collect_insider_sources()` queries wallet_observations with a
`WHERE ingest_ts_utc >= NOW() - ENTROPY_LOOKBACK_SEC` window (default: 60s).
When entropy fires, the most recent whale injection is typically 60–300s old.
Result: 0 wallets found within the 60s window → INSUFFICIENT_CONSENSUS always.

**Layer 2 — Structural gap, not a probability problem**
This is not "sometimes the timing lines up" — the whale scan cadence (300s) is
structurally longer than the lookback window (60s). They can never overlap unless
an entropy event fires within 60s of a whale scan completing. In practice: never.

**Layer 3 — Overlap metric was misleading**
A DB query `SELECT ... WHERE obs_type='clob_trade'` without a time filter showed
overlap — because it included *all historical* whale wallets. The pipeline only
queries a narrow recent window. Raw DB counts and pipeline query counts diverge.

**Layer 4 — Agent prompt anticipated but did not preempt**
The D44 prompt included a timing check step, but as a diagnostic rather than
a proactive design constraint. A lookback window check should be part of every
new data-source integration checklist.

### Fastest detection checks
1. `grep -n "ENTROPY_LOOKBACK_SEC" panopticon_py/signal_engine.py` — find default value
2. Compare with `WHALE_SCAN_INTERVAL_SEC` in `whale_scanner.py` — if lookback < cadence: broken
3. Direct query with explicit time filter matching pipeline window:
   ```sql
   SELECT COUNT(*) FROM wallet_observations
   WHERE obs_type='clob_trade'
     AND ingest_ts_utc >= datetime('now', '-60 seconds', 'utc');
   ```
   If this returns 0 while unfiltered query returns rows: timing mismatch confirmed.
4. Check log timestamps: `[WHALE][OBS_INJECT]` timestamp vs nearest `entropy_drop` timestamp.
   Gap > ENTROPY_LOOKBACK_SEC = timing mismatch.

### Durable prevention rules
- **Lookback window must be ≥ source cadence × 1.2 buffer**:
  `ENTROPY_LOOKBACK_SEC ≥ WHALE_SCAN_INTERVAL_SEC × 1.2`
  Document this invariant in `PANOPTICON_CORE_LOGIC.md`.
- **Always test pipeline queries with time filter, not raw counts**:
  When verifying data flow, reproduce the exact WHERE clause the pipeline uses.
  Raw `COUNT(*)` without time filter is misleading for cadence-dependent sources.
- **Cadence/window pair is a trading logic decision, not infrastructure**:
  Never change `ENTROPY_LOOKBACK_SEC` or `WHALE_SCAN_INTERVAL_SEC` without
  architect approval. Document as paired constraint.
- **New data source integration checklist must include**:
  - What is the write cadence of this source?
  - What is the read window of the consumer?
  - Is write cadence < read window? If not: broken by design.

### Fix pattern
```python
# signal_engine.py — change default only, keep env override
ENTROPY_LOOKBACK_SEC = int(os.getenv("ENTROPY_LOOKBACK_SEC", "360"))
# Rule: default = WHALE_SCAN_INTERVAL_SEC × 1.2 = 300 × 1.2 = 360
```

### Regression checklist
- [ ] `ENTROPY_LOOKBACK_SEC` default ≥ `WHALE_SCAN_INTERVAL_SEC × 1.2`.
- [ ] Pipeline query with time filter returns rows during active whale scan cycle.
- [ ] `execution_records accepted=1` count grows after restart.
- [ ] Both values documented as paired constraint in `PANOPTICON_CORE_LOGIC.md`.

---

## Entry: Market coverage structural mismatch — signal sources targeting disjoint markets

### Trigger symptom
- Both signal sources healthy individually (whale wallets written, entropy events fired)
- `execution_records` stuck at INSUFFICIENT_CONSENSUS for hours
- No obvious error in logs — pipeline runs silently with 0 output
- DB shows whale wallets for T2 markets, entropy_drops for T1/T3/T5 markets

### True root cause chain

**Layer 1 — Independent market selection logic**
`whale_scanner` sampled only from `_t2_raw_markets` (geopolitical markets from Gamma API).
`run_radar` subscribed to all tiers (T1 BTC/ETH, T2 geopolitical, T3, T5 sports)
and generated entropy_drop events across all of them.
`MIN_CONSENSUS_SOURCES=2` requires BOTH sources to observe the SAME market.
With disjoint market coverage: mathematically impossible to reach consensus.

**Layer 2 — T1 market structure prevents T1 consensus**
BTC/ETH 5-minute up/down markets (T1) are bot-dominated.
50 trades typically yield only 1-3 distinct wallets (automated market makers).
Even if whale_scanner covered T1, distinct wallet count rarely reaches MIN_CONSENSUS_SOURCES=2.
T1 was never the right consensus target — T2 geopolitical markets are.

**Layer 3 — "Wait for natural overlap" is not a strategy**
The architectural review initially considered waiting for natural market overlap (Option A).
This was incorrect: the mismatch was structural (different APIs, different market lists),
not probabilistic. Natural overlap rate was 0/4 over 4 hours of observation.

**Layer 4 — Monitoring visibility absent**
`execution_records` had no `market_id` column. Could not determine *which* markets
were generating INSUFFICIENT_CONSENSUS. The absence of this column delayed diagnosis
by multiple sessions.

### Fastest detection checks
1. Check whale_scanner market source:
   `grep -n "_t2_raw_markets\|scan_once\|_fetch_markets" panopticon_py/hunting/whale_scanner.py`
2. Check WS subscription scope:
   `grep -n "_token_tier_map\|_tier" panopticon_py/hunting/run_radar.py | head -10`
3. Cross-check overlap in DB:
   ```sql
   SELECT ct.market_id, COUNT(DISTINCT ct.address) as whale_wallets,
          COUNT(DISTINCT ed.obs_id) as entropy_events
   FROM wallet_observations ct
   JOIN wallet_observations ed ON ct.market_id = ed.market_id
     AND ed.obs_type = 'entropy_drop'
   WHERE ct.obs_type = 'clob_trade'
   GROUP BY ct.market_id HAVING whale_wallets >= 2;
   ```
   If 0 rows: structural mismatch confirmed.
4. Add `market_id` to `execution_records` early — enables rapid overlap diagnosis.

### Durable prevention rules
- **All signal sources must share a common market registry**:
  Any source contributing to consensus must observe the same market universe.
  Use a shared module-level registry (`register_active_markets()`) updated by
  the WS subscription refresh cycle.
- **Add `market_id` to `execution_records` from day one**:
  This column costs nothing and is the fastest way to diagnose consensus failures.
  Absence delays diagnosis by sessions.
- **New signal source integration checklist**:
  - What market universe does this source observe?
  - Does it match the WS subscription list exactly?
  - If not: add `register_active_markets()` bridge before enabling consensus.
- **T1 BTC/ETH markets are structurally wrong for wallet-based consensus**:
  Bot-dominated markets have 1-3 distinct wallets per 50 trades.
  Consensus threshold of 2 is unreachable without bot wallets (which have no insider score).
  Document T1 exclusion from consensus in `PANOPTICON_CORE_LOGIC.md`.

### Regression checklist
- [ ] `whale_scanner._active_market_registry` is populated after WS subscription refresh.
- [ ] DB cross-query above returns ≥ 1 row with `whale_wallets >= 2` after one scan cycle.
- [ ] `execution_records` has `market_id` column populated for new rows.
- [ ] `PANOPTICON_CORE_LOGIC.md` documents T1 exclusion from wallet consensus.

---

## Entry: Test count regression as silent indicator of code quality debt

### Trigger symptom
- Test suite passes but count *decreases* after adding new features (307 → 303)
- No explicit test failures reported — CI appears green
- New functions added but test count drops net negative

### True root cause chain
**Layer 1 — Tests deleted or overwritten during refactor**
When implementing D42/D43, existing tests were silently removed or overwritten
rather than appended to. A decrease in test count after feature addition is
always a red flag: features add tests, never remove them.

**Layer 2 — No baseline count enforcement in agent prompt**
Agent prompts stated "307 baseline" but did not include an explicit assertion:
`pytest -q | tail -1 | grep "307 passed"` before and after changes.
The agent self-reported "303 passed, no new failures" without flagging the regression.

### Durable prevention rules
- **Test count is a one-way ratchet**: it can only increase or stay flat after feature work.
  Any decrease = regression, regardless of whether remaining tests pass.
- **Encode exact count in agent prompt**:
  `pytest -q | tail -1` must show `≥ N passed` where N = pre-session baseline.
- **After each PR/session, record new baseline** in the handoff explicitly.
- **`git diff HEAD~1 -- tests/` before and after** any session touching test files.

### Regression checklist
- [ ] `pytest -q | tail -1` shows count ≥ pre-session baseline.
- [ ] `git diff HEAD~1 -- tests/ | grep "^-.*def test_"` returns empty (no deleted tests).
- [ ] New functions each have ≥ 2 new tests (happy path + edge case) in the diff.

---

## Entry: Autonomous agent prompt design — what works and what creates blind spots

### Trigger symptom
- Agent completes tasks correctly but misses structural issues that require cross-file audit
- Agent escalates correctly but prompt lacked preemptive checks for known failure modes
- Agent collects data correctly but interprets raw DB counts without time-filter context

### What worked well

1. **Ranked root cause list (min 3, max 8)** — prevented single-hypothesis tunnel vision.
   Agents consistently found the real cause when forced to list alternatives.

2. **Raw data accumulator as session-independent artifact** — `logs/raw_data_accumulator.log`
   survived process restarts and provided trend data across sessions. High value.

3. **Escalation rules with specific triggers** — explicit "STOP if X" conditions prevented
   agents from modifying trading logic autonomously. Zero trading logic violations across
   all sessions.

4. **PRIME DIRECTIVES section** — having `0.1`–`0.8` numbered and non-negotiable meant
   agents never attempted to set `LIVE_TRADING=true` or modify consensus thresholds.

5. **Handoff format standardization** — consistent handoff structure made cross-session
   continuity reliable. Architect could resume from any handoff without re-reading logs.

6. **"Research before coding" step (GitHub/docs lookup)** — reduced incorrect fix attempts.
   Agents found correct API patterns before writing code.

### What created blind spots

1. **FK audit not included in schema change checklist**
   Prompts said "relic table — do not touch" but never said "audit all REFERENCES to it."
   Fix: add `grep -rn "REFERENCES <table>" panopticon_py/` to every schema change step.

2. **Raw DB counts without time filter used as pipeline proxy**
   Prompts asked agents to check row counts to verify data flow, but pipeline uses
   time-filtered queries. Raw counts always look healthy even when pipeline sees nothing.
   Fix: always provide the exact SQL the pipeline uses, not a simplified version.

3. **Timing invariant not encoded as design rule**
   `ENTROPY_LOOKBACK_SEC ≥ source_cadence × 1.2` was discovered empirically.
   It should have been a pre-integration checklist item.
   Fix: add cadence/lookback compatibility check to "new signal source" integration template.

4. **Market coverage overlap not verified at integration time**
   D39/D40 were implemented correctly in isolation, but the market universe mismatch
   between whale_scanner and run_radar was not caught until D41 monitoring.
   Fix: add explicit cross-source market_id overlap query to every new signal source checklist.

5. **Test count decrease not treated as blocker**
   307→303 regression was reported by agent as "303 passed, no new failures."
   Prompt said "do not regress" but did not encode a hard numeric assertion.
   Fix: `pytest -q | tail -1 | grep -E "[0-9]+ passed"` with explicit count check.

### Durable prevention rules
- Encode all discovered invariants as numbered PRIME DIRECTIVES, not prose.
- Every schema change prompt must include a FK reverse-audit step.
- Every new data source prompt must include: cadence, lookback window, market universe overlap.
- Pipeline verification queries must use the exact WHERE clause the pipeline uses.
- Test baseline must be a hard numeric assertion, not a qualitative "no failures" check.

### Regression checklist
- [ ] FK reverse audit included in any schema migration prompt.
- [ ] Pipeline verification SQL uses time-filtered queries matching actual code.
- [ ] Cadence/lookback invariant checked for every new data source.
- [ ] Market universe overlap query included in every new signal source integration.
- [ ] Test count assertion is numeric and explicit in agent prompt.

## Entry: RVF L5 Consensus Wallet Display — SQL Column Name vs Runtime Column Discovery

### Trigger symptom
- Frontend dashboard shows "L5 共識錢包" all zeros (qualifying_wallets=0, path_b_promoted=0) despite DB having 5000+ qualifying records.
- After adding `sync_consensus_from_db()` to heartbeat, metrics still 0.
- JSON snapshot (`data/rvf_live_snapshot.json`) shows all zeros in `consensus` section.

### True root cause layers

**Layer 1: Wrong column names in SQL queries**
- `wallet_observations` table uses `address` not `wallet` for wallet address column.
- `discovered_entities` uses `address` not `wallet`.
- `wallet_observations` uses `ingest_ts_utc` not `observed_at_utc` for timestamp.
- `discovered_entities` has no `slug` column (no markets table to JOIN on for slug resolution).

**Layer 2: Metrics not synced to JSON at startup or on 5s cadence**
- `_sync_metrics_baseline()` does NOT call `sync_consensus_from_db()` — consensus fields only update on 60s heartbeat.
- `_metrics_json_loop()` does NOT call `sync_consensus_from_db()` — JSON snapshot misses consensus data until heartbeat passes.
- Even when heartbeat fires, if first heartbeat hasn't fired yet, frontend sees all zeros.

**Layer 3: Process restart not working (CWD issue)**
- `Start-Process python -ArgumentList "..."` without `-WorkingDirectory` causes Python to run in temp dir — `ModuleNotFoundError: No module named 'panopticon_py'` silently.
- Solution: use `subprocess.Popen([sys.executable, "..."], cwd=script_dir)` with explicit cwd, or use `cmd /c "cd /d <dir> && ..."`.

**Layer 4: Frontend WS-only startup delay**
- `RvfMetricsPanel` only connected to WebSocket; on first load, WS needs time to establish + receive first message.
- Solution: `useEffect` on mount calls REST `/api/rvf/snapshot` immediately for instant first render.

### Affected layers
- `panopticon_py/metrics/metrics_collector.py` — SQL column names
- `panopticon_py/hunting/run_radar.py` — baseline sync and JSON loop cadence
- `dashboard/src/components/RvfMetricsPanel.tsx` — initial data fetch
- Process startup scripts

### Fastest detection checks

1. **Check actual DB schema before writing SQL**:
```python
import sqlite3
conn = sqlite3.connect('data/panopticon.db')
cols = conn.execute('PRAGMA table_info(table_name)').fetchall()
for c in cols: print(f'  {c[1]} {c[2]}')
```
NEVER assume column names — always verify with `PRAGMA table_info` or `PRAGMA foreign_key_list`.

2. **Check JSON snapshot before assuming code works**:
```python
import json
with open('data/rvf_live_snapshot.json') as f:
    d = json.load(f)
print(d.get('consensus', {}))
print('written_at:', d.get('_written_at'))
```
If `consensus` is all zeros, the sync function isn't populating it.

3. **Check file modification time to verify service is running**:
```powershell
Get-Item data/rvf_live_snapshot.json | Select-Object Name, LastWriteTime
# Compare to current time — if stale, radar is not running
```

4. **Test SQL in isolation before debugging metrics flow**:
```python
# Run the exact same query that sync_consensus_from_db uses
conn.execute("""
    SELECT COUNT(*) FROM discovered_entities
    WHERE insider_score >= 0.55
""").fetchone()[0]  # Should return 5000+ not 0
```

### Durable prevention rules
- **Always `PRAGMA table_info` before writing SQL in new code.** Do not assume column names from context or similar tables.
- **Test SQL in isolation first.** Copy-paste the query into a standalone script, run it, verify results before integrating into `sync_*` function.
- **Call sync functions on startup AND on every JSON write cadence.** Do not rely on heartbeat alone — JSON loop must sync on every write.
- **Use `subprocess.Popen` with explicit `cwd=` for all background Python processes.** Do not rely on PowerShell's current directory persisting across shell calls.
- **Frontends must fetch REST on mount for critical panels.** Do not wait for WS to connect for initial render — WS is for updates, REST is for immediate data.

### Regression checklist
- [ ] New `sync_*` functions tested against actual DB schema before integration.
- [ ] JSON snapshot verified to contain correct data after new metrics added.
- [ ] Services verified running by checking file modification time (not just process list).
- [ ] Frontend panels show data on first render without waiting for WS.

---

## Entry: Cursor Write Tool 檔案腐敗 — Windows StrReplace 累積問題

### Trigger Symptom
- 測試套件中某個測試持續失敗，但錯誤訊息與「確認修復」的內容不符
- `StrReplace` 操作成功但檔案內容未如預期改變
- 檔案出現重複的類別定義（同一 `class` 出現兩次）
- pytest 載入的是腐敗版本（last class definition wins）

### Root Cause Chain

**Layer 1 — Cursor Write tool 在 Windows 的 append 行為**
Cursor 的 `StrReplace` 工具在對已存在的檔案執行修改時，在某些情况下會 append 而非 overwrite。
連續多次 StrReplace 後，檔案內容持續累積而非替換。

**Layer 2 — 重複類別定義**
`tests/test_rvf_metrics.py` 在 D59-D62 期間連續執行多次 StrReplace 後：
- 第一個 `class TestD56ConsensusTotal`（正確版本）出現在檔案前段
- 腐敗的第二個（缺少 `primary_tag`）出現在檔案尾部
- Python 的 class 定義是最後一個取勝（last definition wins）
- pytest 編譯時載入腐敗的第二個類別

**Layer 3 — `__pycache__` 删除無效**
刪除 `__pycache__` 和 `.pyc` 文件只能清除 Python bytecode 緩存。
如果原始碼檔案本身已腐敗，pytest 每次重新編譯時讀取的仍是錯誤的原始碼。

### Detection

```python
# 檢查檔案類別數量
import ast
with open('tests/test_rvf_metrics.py') as f:
    tree = ast.parse(f.read())
classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
print(classes)
# 如果預期 1 個，發現 2 個 → 重複類別問題

# 驗證 AST parse
python3 -c "import ast; ast.parse(open('PATH').read()); print('OK')"
# SyntaxError → 檔案腐敗，立即刪除重建
```

### Prevention Rules

- **RULE-FILE-1**: 每個 sprint 對同一測試檔案執行不超過 2 次 StrReplace。第 2 次之後改為完整刪除重建。
- **RULE-FILE-2**: 每次 StrReplace 後執行 AST parse 驗證。SyntaxError → 刪除重建。
- **RULE-FILE-3**: 每次 StrReplace 後執行類別計數確認。大於預期 → 刪除重建。
- **RULE-FILE-4**: 當測試在「確認修復」後仍然失敗，先檢查 `grep -n "class Test" FILE` 確認類別數量。
- **RULE-FILE-5**: `__pycache__` 删除無效於原始碼腐敗。解決方案：刪除腐敗的 `.py` 檔案，完整重建。

### Phantom Fix Pattern

當 bug 在 sprint N 確認修復後重新出現：
1. `grep "primary_tag" FILE` — 修復存在？
2. `grep "class Test*" FILE` — 發現多個類別？
3. 如果修復存在但類別重複 → 刪除重建（不要再 StrReplace）

---

## Entry: pytest Port 衝突與 Test Count Regression

### Port 衝突 (RULE-TEST-1, RULE-TEST-2)

**Trigger**: pytest 報告 `WinError 10061` 或 `Connection refused`，backend 在 pytest 期間崩潰。

**Solution**: pytest 前先停止 backend，隔離需要 live backend 的 integration tests：
```bash
pytest -q --ignore=tests/test_api.py --ignore=tests/test_api2.py --ignore=tests/test_api3.py
```

### Test Count Regression (RULE-TEST-4, RULE-TEST-5)

測試總數是單向閂（one-way ratchet）：功能工作後只能增加或不變，任何減少 = 迴歸。

```bash
pytest -q | tail -1  # 必須 ≥ 前次 session 的 baseline
```

---

## Entry: AMM vs CLOB Market Detection (D67)

### Trigger Symptom
- `avg_entry_price=0.0` appearing in paper trades despite D64a wiring
- `fetch_settlement_price()` returning `None` for markets that appear liquid
- NO_TRADE triggered on every signal for certain market types

### True Root Cause Chain
1. Polymarket hosts two distinct market types:
   - **CLOB markets**: Order-book based, real trades, tight spreads (0.01-0.05)
   - **AMM markets**: Fixed pricing (bid=0.01/ask=0.99), zero actual trades, wide spreads (0.85+)
2. BTC Up/Down 5m (`btc-updown-5m-*`) is an AMM market — confirmed via live monitoring
3. `GET /book` returns bids/asks for AMM too (AMM quotes), but `GET /trades` returns `[]`
4. D64a's `fetch_best_ask()` was returning 0.99 (AMM ask) which is a valid price, but AMM markets have no real entry

### Detection: Spread = best_ask - best_bid
```
spread > 0.85 → AMM market (skip)
spread ≤ 0.85 → possible CLOB (verify with /trades count)
```

### Durable Prevention Rules

**RULE-MARKET-1: AMM Detection**
```
AMM_SPREAD_THRESHOLD = 0.85
is_amm_market(best_bid, best_ask) → (best_ask - best_bid) > 0.85
```
Any market with spread > 0.85 must be treated as AMM. `fetch_best_ask()` must return `None` for AMM markets (triggering NO_TRADE).

**RULE-MARKET-2: CLOB Validation**
Real CLOB markets have: tight spread (0.01-0.05), trade history via `GET /trades`, and settlement prices via `GET /prices-history`.
If `GET /trades` returns `[]` BUT spread < 0.20 → thin CLOB market → accept (do not block).

**RULE-MARKET-3: Settlement on AMM Markets**
AMM markets have no `/prices-history` endpoint. `fetch_settlement_price()` will return `None`. This is expected and correct — do NOT estimate or fall back to 0.5.

**RULE-MARKET-4: Monitor Before Wiring**
Before assuming a market type, run a 5-minute live monitor:
1. Call `GET /book` → check spread
2. Call `GET /trades` → check trade count
3. If spread > 0.85 AND trades = 0 → AMM → skip
4. If spread < 0.20 → CLOB → proceed

**RULE-MARKET-5: Entry Price for AMM is Always None**
Even if `fetch_best_ask` returns a price for AMM (e.g., 0.99), the AMM guard must block it. AMM prices are not real entry prices.

### Regression Checklist
- [ ] `is_amm_market(0.01, 0.99)` returns `True`
- [ ] `is_amm_market(0.42, 0.44)` returns `False`
- [ ] `fetch_best_ask("BTC_5M_TOKEN")` returns `None` (AMM blocked)
- [ ] `fetch_best_ask("REAL_CLOB_TOKEN")` returns the ask price
- [ ] BTC 5m `GET /trades` returns `[]` (zero trades confirmed)


## EXP-D81-001: Python 三層 scope 模型（_live_ticks 模式）
**症狀**: UnboundLocalError 或 "can't be global" / "no binding for nonlocal" — 且每次 attempt 報錯的變數名稱不同（「游走」現象）
**根本原因**: Python 在編譯時期靜態決定 scope。只要函式體內有任何一個對變數 X 的賦值（`X = ...`），Python 就把 X 標記為整個函式的 local，包括賦值行之前的所有引用。四種常見破壞模式：
1. 把 local 初始化移到模組層級 → nonlocal 找不到 enclosing binding
2. 加上型別標注的模組層級宣告（`x: int = 0`）再用 `global x` → "annotated name can't be global"
3. 同一變數同時出現在 nonlocal 和 global 宣告 → 衝突
4. 刪除 _live_ticks 內的 local 初始化，只保留模組層級 → _on_message 的 nonlocal 失效
**正確的三層結構**（以 run_radar.py 為例）：
- 層 1（模組層級）：heartbeat 相關純模組變數（`_last_ws_diag_log_ts` 等），用 `global` 宣告存取
- 層 2（`_live_ticks` local）：所有 accumulator（`_evt_count`, `_entropy_eval_total` 等），在函式開頭初始化
- 層 3（`_on_message` nonlocal）：透過 `nonlocal` 讀寫層 2 的變數
**規則**：不得把層 2 的變數移到模組層級。不得同時使用 global + nonlocal 指向同一變數。
**驗證**：`python -c "import py_compile; py_compile.compile('run_radar.py')"` — 應無輸出
**D81 修復日期**: 2026-04-29

## EXP-D80-001: f-string 嵌套 {} 語法錯誤
**症狀**: `SyntaxError: f-string: single '}' is not allowed`
**根本原因**: Python f-string 不允許在佔位符 `{}` 內直接嵌套含 `{}` 的條件表達式或 dict literal
**修復**: 預先把所有格式化值存入變數，再用字串拼接（`"prefix:" + var + ","` 模式）
**D80 修復日期**: 2026-04-29

## EXP-D80-002: ShadowDB 缺少 execute() delegation
**症狀**: `'ShadowDB' object has no attribute 'execute'`
**根本原因**: 呼叫方 (`run_insider_monitor`) 使用 `db.execute(sql, params)` 但 `ShadowDB` 只暴露 `self.conn`
**修復**: 在 `ShadowDB` 加 `def execute(self, sql, parameters=()): return self.conn.execute(sql, parameters)`
**規則**: 新增 ShadowDB 呼叫方時，先確認 ShadowDB 是否已暴露對應方法
**D80 修復日期**: 2026-04-29


