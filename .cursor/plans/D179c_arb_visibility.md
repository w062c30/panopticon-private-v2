# D179c — Arb Scanner Visibility / "174 Token" Demystification

> Priority: **P2** (no functional regression in arb scanner; this sprint exists so issues B/D never re-appear as "silence").
> Read first: `D179_master_diagnosis.md` §1.6.

---

## 1. Why this sprint exists

The user's reports B and D come from three legitimate misunderstandings:

| Symptom | Reality | Source |
|---------|---------|--------|
| "Arb logs are missing in `run/orchestrator.log`." | Arb scanner is `python -m panopticon_py.execution.arb_scanner`, started by `restart_all.ps1` as a **separate process** (PID 15216). All its logs go to `run/arb_scanner.err.log` (35 KB, healthy) and `run/arb_scanner.log` (currently 0 bytes because logging routes to stderr per uvicorn-style start). | `scripts/restart_all.ps1:111-117` and `arb_scanner.py:688-727`. |
| "Arb DB rows are 0." | The relevant table is `arb_stats`, not `paper_trades`. Existing `arb_scanner._flush_stats` (L406-431) writes a row every 60 s. Verify with `sqlite3 data/panopticon.db 'select count(*) from arb_stats'`. The `arb_opportunities` table is **only** populated when a real arb exists — sports-market arb opportunities are very rare. | `arb_scanner.py:411-426`. |
| "174 tokens stagnant." | `[ARB_FEE_SUMMARY] total=174 kept=174 excluded=0` is the live count of active T5 sports tokens returned by Gamma's `/markets?closed=false&limit=500` filtered through `_is_tier5_sports_market`. The number is stable because the active sports-market roster is stable. WS reconnect triggers a refresh (L576-592); WS has been stable since 05:12, so no refresh has occurred — correct behaviour. | `arb_scanner.err.log:184-186`, `arb_scanner.py:518-598`. |

None of this is broken, but the **observability gap** between orchestrator-side metrics (`rvf_live_snapshot.json`) and arb_scanner-side metrics (its own `arb_stats` rows) means an operator scanning `rvf_live_snapshot.json` literally cannot see arb activity. We close that gap, and we update FUNCTION_STATUS.md to make the process boundary explicit.

---

## 2. Scope

This sprint **does** the following:

1. Add an `arb` block to `data/rvf_live_snapshot.json` written by the radar's `_metrics_json_loop`. Source: read the latest row of `arb_stats` from the shared SQLite DB.
2. Document the process boundary in `FUNCTION_STATUS.md` and `panopticon_py/hunting/INDEX.md`.
3. Remove the empty `run/arb_scanner.log` file and document that arb logs live in `arb_scanner.err.log`.
4. Add a one-time startup log line to `arb_scanner.py` that explains the source of the token count, e.g. `[ARB] Subscribed token count = N (Gamma active T5 sports markets, refreshed on WS reconnect)`. Eliminates the "stagnant 174" misread.

This sprint **does NOT**:

* Make the arb scanner part of orchestrator's process tree.
* Change `_is_tier5_sports_market` filter logic.
* Route arb events into `signal_queue`.

---

## 3. Code-level plan

### 3.1 New `arb` block in RVF snapshot

`run_radar.py` `_metrics_json_loop` already writes `data/rvf_live_snapshot.json` every 5 s. Append a helper, called from the same loop:

```python
# panopticon_py/hunting/run_radar.py — near _write_entropy_snapshot
def _read_arb_snapshot(db) -> dict:
    """
    D179c: read the latest arb_stats row written by arb_scanner (separate process).
    Best-effort; never raises into the metrics_json_loop.
    """
    try:
        cur = db.conn.execute(
            "SELECT ts_utc, ws_connected, tokens_subscribed, active_tokens, "
            "       total_updates, reconnect_count, opp_count_total, opp_count_1h, "
            "       best_profit, tokens_total, tokens_kept, tokens_excluded "
            "FROM arb_stats ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return {"present": False}
        # sqlite3.Row supports both index and key access; we use index since
        # ShadowDB connects without row_factory by default for this read path.
        return {
            "present": True,
            "ts_utc": row[0],
            "ws_connected": bool(row[1]),
            "tokens_subscribed": int(row[2] or 0),
            "active_tokens": int(row[3] or 0),
            "total_updates": int(row[4] or 0),
            "reconnect_count": int(row[5] or 0),
            "opp_count_total": int(row[6] or 0),
            "opp_count_1h": int(row[7] or 0),
            "best_profit": float(row[8] or 0.0),
            "tokens_total": int(row[9] or 0),
            "tokens_kept": int(row[10] or 0),
            "tokens_excluded": int(row[11] or 0),
            "stale_seconds": _seconds_since_iso(row[0]),
        }
    except Exception as exc:
        logger.debug("[RVF][ARB_SNAPSHOT] skipped: %s", exc)
        return {"present": False, "error": str(exc)[:160]}
```

In `_metrics_json_loop`, splice the result into the persisted JSON via `mc.persist_json` extension. Use the existing `mc.update_arb_snapshot(...)` if it exists; otherwise add a minimal dict-merge in the same loop:

```python
arb_snap = _read_arb_snapshot(db)
mc.update_arb_snapshot(arb_snap)   # or pass through to persist_json
```

If `MetricsCollector` lacks `update_arb_snapshot`, add it as a thin TypedDict-backed setter (per AGENTS.md RULE-CONTRACT-1):

```python
# panopticon_py/metrics_collector.py (or wherever MetricsCollector lives)
from typing import TypedDict, NotRequired

class ArbSnapshot(TypedDict, total=False):
    present: bool
    ts_utc: str
    ws_connected: bool
    tokens_subscribed: int
    active_tokens: int
    total_updates: int
    reconnect_count: int
    opp_count_total: int
    opp_count_1h: int
    best_profit: float
    tokens_total: int
    tokens_kept: int
    tokens_excluded: int
    stale_seconds: float
    error: NotRequired[str]

def update_arb_snapshot(self, snap: ArbSnapshot) -> None:
    self._arb_snap = dict(snap)

# inside persist_json, after building the existing payload:
payload["arb"] = getattr(self, "_arb_snap", {"present": False})
```

`stale_seconds` lets the operator see at a glance whether the arb scanner is alive: > 120 s means investigate.

### 3.2 Self-explaining startup log in arb_scanner

```python
# panopticon_py/execution/arb_scanner.py inside run() after [ARB_INIT]
logger.info(
    "[ARB_TOKENS] subscribed=%d source=Gamma /markets?closed=false&limit=500 "
    "filter=_is_tier5_sports_market refresh_on=ws_reconnect (~each disconnect). "
    "Stable count is expected when WS is stable.",
    len(current_token_ids),
)
```

This single log entry resolves the user's "174 stagnant" question for any future operator.

### 3.3 FUNCTION_STATUS / INDEX entries

Add to `FUNCTION_STATUS.md` under a new section, **explicit about process boundary**:

```markdown
## panopticon_py/execution/arb_scanner.py  (separate process — NOT inside orchestrator)

| Function | Status | Reason | Since |
|---------|--------|--------|-------|
| `ArbScanner.run()` | ✅ ACTIVE | Standalone process started by `scripts/restart_all.ps1:Start-ArbScanner`. Logs to `run/arb_scanner.err.log`. PID listed in `run/process_manifest.json` under `arb_scanner`. | D135 |
| `ArbScanner._connect_and_listen()` | ✅ ACTIVE | WS subscription manager; refreshes T5 sports token list on every reconnect via `fetch_t5_token_ids`. Stable token count (e.g. 174) is the live Gamma active-T5 count, not a stale cache. | D141 |
| `ArbScanner._flush_stats()` | ⏰ BACKGROUND_60S | Persists `arb_stats` rows. Read by orchestrator's `_read_arb_snapshot` (D179c). | D148 |
| `_is_tier5_sports_market()` | ✅ ACTIVE | T5 filter mirrored from `run_radar.py`. Edits MUST be mirrored on both sides. | D118 |
```

### 3.4 Empty log file cleanup

`run/arb_scanner.log` is 0 bytes because Python's stdlib logging routes `__main__` to stderr through `logging.basicConfig`. Two options:

**Option A (chosen):** Leave the empty file in place; document it. The PowerShell `Start-ArbScanner` redirects both streams, so deleting the file would just be re-created on next start.

**Option B (rejected):** Make arb_scanner ALSO log to stdout. Increases coupling; deferred.

Add to `EXPERIENCE_PLAYBOOK.md`:

```markdown
## EXP — D179c — Empty `run/arb_scanner.log`
arb_scanner uses `logging.basicConfig(... stream=sys.stderr)`. PowerShell's
`-RedirectStandardOutput run/arb_scanner.log` therefore captures nothing.
The file exists only because the OS allocates it. Real logs are in
`run/arb_scanner.err.log` (`-RedirectStandardError`).
```

---

## 4. Logic-error checklist

| # | Trap | Mitigation |
|---|------|-----------|
| L1 | Reading `arb_stats` from radar's connection while arb_scanner writes — risk of `database is locked`. | ShadowDB sets `journal_mode=WAL` and `busy_timeout=5000`; the read is `LIMIT 1` and inside an existing best-effort try/except (caller swallows `Exception`). Confirmed safe. |
| L2 | `arb_stats` table may not exist on a fresh DB. | `arb_scanner._flush_stats` writes via `INSERT INTO arb_stats ...`; the table is created in arb_scanner's bootstrap (verify in `panopticon_py/db.py` and confirm migration is idempotent). If it doesn't exist, `_read_arb_snapshot` returns `{"present": False}` from the OperationalError path. **Do not** add the table from radar — keep ownership with arb_scanner. |
| L3 | If arb_scanner is not running, the snapshot stays "stale". | Surface `stale_seconds` so dashboards alert. Watchdog already restarts arb_scanner on death. |
| L4 | Polling DB every 5 s adds I/O on a hot path. | One row read with index on `id DESC`; <1 ms. Add an explicit index `CREATE INDEX IF NOT EXISTS idx_arb_stats_id ON arb_stats(id DESC)` if profiling shows otherwise (PRIMARY KEY already serves this). |
| L5 | Test for the new `arb` snapshot field can flake when `arb_stats` is empty. | Test must seed one row before running. Use a minimal pytest fixture that inserts a known-good row. |
| L6 | `MetricsCollector.persist_json` writes a fixed schema; adding a new top-level key may break a downstream consumer. | Backend `/api/rvf/live` returns the JSON as-is; adding a key is non-breaking. Confirm no consumer pins the JSON shape via `pydantic.BaseModel` with `extra='forbid'`. |
| L7 | Hard-coding column ordering in `_read_arb_snapshot` violates AGENTS.md RULE-SQLITE-1 ("use named access"). | Use `db.conn.row_factory = sqlite3.Row` ad-hoc inside the helper, or use `dict(zip(["ts_utc",...], row))`. Pick named-zip to avoid affecting the connection's global row_factory. |

---

## 5. Test plan

```python
# tests/test_arb_snapshot_d179c.py
import sqlite3, pathlib, pytest
from panopticon_py.hunting import run_radar
from panopticon_py.db import ShadowDB

@pytest.fixture
def db_with_arb_stats(tmp_path):
    p = tmp_path / "panopticon.db"
    db = ShadowDB(path=str(p))
    db.bootstrap()
    db.conn.execute(
        """CREATE TABLE IF NOT EXISTS arb_stats(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT, ws_connected INTEGER, tokens_subscribed INTEGER,
            active_tokens INTEGER, total_updates INTEGER, reconnect_count INTEGER,
            opp_count_total INTEGER, opp_count_1h INTEGER, best_profit REAL,
            tokens_total INTEGER, tokens_kept INTEGER, tokens_excluded INTEGER
        )"""
    )
    db.conn.execute(
        "INSERT INTO arb_stats VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-08T05:35:00.000Z", 1, 174, 23, 1500, 0, 0, 0, 0.0, 174, 174, 0),
    )
    db.conn.commit()
    yield db
    db.close()

def test_d179c_arb_snapshot_present(db_with_arb_stats):
    snap = run_radar._read_arb_snapshot(db_with_arb_stats)
    assert snap["present"] is True
    assert snap["tokens_subscribed"] == 174
    assert snap["ws_connected"] is True
    assert "stale_seconds" in snap

def test_d179c_arb_snapshot_absent(tmp_path):
    p = tmp_path / "panopticon.db"
    db = ShadowDB(path=str(p))
    db.bootstrap()
    snap = run_radar._read_arb_snapshot(db)
    assert snap["present"] is False
```

---

## 6. Step-by-step

1. Verify `arb_stats` schema exists and is created by arb_scanner bootstrap. Add `CREATE TABLE IF NOT EXISTS` to arb_scanner if missing (only if absent today).
2. Add failing tests above.
3. Implement `_read_arb_snapshot` and `MetricsCollector.update_arb_snapshot` (TypedDict + setter).
4. Wire into `_metrics_json_loop`.
5. Add `[ARB_TOKENS]` self-explaining log in `arb_scanner.run()`.
6. Update `FUNCTION_STATUS.md` and `EXPERIENCE_PLAYBOOK.md`.
7. Bump versions:
   * `arb_scanner` → `v0.5.12-D179c`
   * `radar` → next D179 patch
   * Update `run/versions_ref.json`.
8. `restart_all.ps1`. Confirm:
   * `data/rvf_live_snapshot.json` shows `"arb": {"present": true, "tokens_subscribed": 174, ...}`.
   * `[ARB_TOKENS] subscribed=174 source=Gamma ...` in `run/arb_scanner.err.log`.
9. `pytest -k d179c` green; full suite ≥95.

---

## 7. Acceptance criteria

* `data/rvf_live_snapshot.json` contains an `arb` block with `present: true`, `tokens_subscribed > 0`, `stale_seconds < 120`.
* `FUNCTION_STATUS.md` has the new arb scanner section.
* No regression in `pytest -x`.
* Operator dashboard (if any) renders the new fields without 500-ing.

---

## 8. Files touched

| File | Change |
|------|--------|
| `panopticon_py/hunting/run_radar.py` | new `_read_arb_snapshot`; call from `_metrics_json_loop` |
| `panopticon_py/metrics_collector.py` (or equivalent) | `update_arb_snapshot` + `ArbSnapshot` TypedDict |
| `panopticon_py/execution/arb_scanner.py` | one-line `[ARB_TOKENS]` info log |
| `FUNCTION_STATUS.md` | arb_scanner section |
| `EXPERIENCE_PLAYBOOK.md` | empty arb_scanner.log explanation |
| `run/versions_ref.json` | version bumps |
| `tests/test_arb_snapshot_d179c.py` | new |
