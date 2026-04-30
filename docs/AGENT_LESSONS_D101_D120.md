# Agent Lessons — D101 to D120

> Last updated: D120 (2026-05-01)
> Purpose: Coding agent rules derived from actual bugs encountered during D101–D120 sprints.

---

## Agent Hard Rules

The following rules are **mandatory** for all coding agents. Violations may result in silent failures that `python -c "import X"` cannot detect.

---

### RULE-IMPORT-1: `except Exception` swallows `NameError`

`NameError` is a subclass of `Exception`. If `json.dump()` is called inside a function body without a top-level `import json`, an `except Exception` block **silently swallows** the `NameError`. `python -c "import X"` cannot detect function-body `NameError`.

**Rule**: Every time you add a function that uses a standard library module, confirm the import exists at the **top level** of the file, not just in the function body.

---

### RULE-IMPORT-2: `python -c "import X"` only tests the import graph

| Test method | Can detect | Cannot detect |
|-------------|-----------|---------------|
| `python -c "import X"` | Top-level syntax errors, circular imports | Function-body `NameError`, runtime-path missing imports |
| Actual service startup | All startup paths | Delayed-trigger paths |

**Rule**: Handoff verification must **not** rely solely on import checks. It must include "simulate the trigger path" testing.

---

### RULE-TIME-1: Never locally re-implement `utc_now_rfc3339_ms`

Calling `datetime.now(timezone.utc)` twice in the same function creates a cross-second race.

**Rule**: The only legal source is:
```python
from panopticon_py.time_utils import utc_now_rfc3339_ms
```
Do not create local `_utc_now_rfc3339_ms()` helpers. See `run_hft_orchestrator.py:L453` (D120 fixed).

---

### RULE-SQLITE-1: All `sqlite3.Row` accesses must use named access

Positional access `r[0]` through `r[14]` silently breaks when column order changes (e.g., after a schema migration or JOIN modification).

**Rule**: Always use `dict(r)` or `r["col_name"]`. SELECT statements must include explicit `AS alias` for computed columns (e.g., `COUNT(*) AS cnt`). `sqlite3.Row` uses the alias name as the key, not the original column name.

---

### RULE-SEMAPHORE-1: `asyncio.Semaphore` must not be module-level

In Python 3.9, `asyncio.Semaphore(2)` created at module import time may be "attached to a different event loop".

**Rule**: Initialize `asyncio.Semaphore` inside the coroutine, not at module scope.

---

### RULE-SEMAPHORE-2: Semaphore wraps a single request, not an entire loop

Holding a lock across `await asyncio.sleep()` makes the Semaphore behave like a mutex (serializes all requests instead of limiting concurrency).

**Rule**: `await asyncio.sleep()` must be **outside** the semaphore-acquired block.

---

### RULE-API-1: Guard `tokens[0].get()` with `isinstance`

```python
# CORRECT
token_id = (
    tokens[0].get("token_id")
    if tokens and isinstance(tokens[0], dict)
    else None
)
```

**Rule**: Always guard list indexing with `isinstance(x, dict)` when parsing external API responses (Gamma, Polymarket CLOB). The API may return `None` or non-dict objects in unexpected shapes.

---

### RULE-REENTRY-1: All `stop()` methods must check `_running` first

Double sentinel causes `_loop` to break twice, corrupting queue state.

**Rule**: Every `stop()` method must have a reentry guard as the first executable line:
```python
def stop(self) -> None:
    if not self._running:
        return
    # ... rest of shutdown
```

---

### RULE-CONTRACT-1: Cross-class implicit method contracts must be TypedDict

`AsyncDBWriter.health()` and `AsyncDBWriterStub.health()` returning `{"running", "thread_alive", "queue_depth", "queue_unfinished"}` is an **implicit contract**. If `AsyncDBWriter.health()` gains a new field, the Stub fallback won't sync, causing dashboard inconsistency.

**Rule**: Define an explicit `TypedDict` or `dataclass` for any dict-based contract shared between classes. Example:
```python
from typing import TypedDict

class AsyncDBWriterHealth(TypedDict):
    running: bool
    thread_alive: bool
    queue_depth: int
    queue_unfinished: int
```

---

### RULE-PATH-1: Cross-process JSON snapshot must use the same env var path

The writer and reader must use identical `os.getenv("ASYNC_WRITER_HEALTH_PATH", "data/async_writer_health.json")` — same key, same default. If the writer uses a literal path and the reader uses an env var with no default, reads will silently fail with `FileNotFoundError`.

**Rule**: Define the path key as a constant at module level, export it, and use it in both writer and reader.

---

### RULE-DEAD-1: Unread assignments are dead code — delete them

```python
# Dead code:赋值后从未读取
graph_engine = HiddenLinkGraphEngine(db=db)  # never used
# Meanwhile: global _graph_engine shadows this
```

**Rule**: Before every commit, `grep -n "graph_engine" orchestrator.py` to check for unread local variables. If a variable is assigned but never read, it is dead code — delete it.

---

### RULE-CLOSURE-1: Closure functions must not be lifted to module level without injection

`_persist_writer_health()` references `db_writer` (a local variable in `main_async()`). Lifting it to module level causes `NameError` because `db_writer` is out of scope.

**Rule**: Before lifting a nested function to module level, verify it has no free variables referencing local scope. If it does, convert to dependency injection (pass the value as a parameter).

---

## Pattern Index (cross-reference)

| Bug pattern | Detection method | Affected sprints |
|-------------|-------------------|------------------|
| Missing top-level import caught by function call | `python -c "import X"` | D120 |
| Hidden `NameError` under `except Exception` | Code review | D120 |
| Positional row access breaking after schema change | Query result diff | D113–D119 |
| Cross-second race in timestamp generation | Logic review | D120 |
| Semaphore attached to wrong event loop | Python 3.9 test | D109 |
| `tokens[0].get()` without isinstance guard | Gamma API response audit | D110–D111 |
| Double sentinel on reentry | `stop()` call sequence test | D116 |
| Implicit dict contract desync | TypedDict review | D119 |
| Dead local variable shadowing global | `grep` for unread assignments | D120 |

---

*End of Agent Lessons*