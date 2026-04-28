# Panopticon Time Contract

## Goal
Establish one internal time format across the project while preserving external API semantics.

## Internal Canonical Format
- Any persisted UTC field named like `*_ts_utc`, `created_ts_utc`, `updated_ts_utc`, `ingest_ts_utc` must use:
  - **RFC3339 UTC text with millisecond precision**
  - Example: `2026-04-25T11:03:14.527Z`
- In code, use `panopticon_py.time_utils.utc_now_rfc3339_ms()` for new timestamps.

## External API Timestamp Handling (Do Not Blindly Unify)
- External payload timestamps must follow source docs first (Polymarket/Gamma/Data/CLOB WS).
- For Polymarket specifically:
  - WS/Data fields like `timestamp` may be epoch milliseconds.
  - Do **not** assume ISO for external payload fields.
- Rule:
  1. Preserve raw external timestamp in payload/log context when needed.
  2. Normalize to internal canonical UTC only when writing DB/internal contract fields.
- Use `panopticon_py.time_utils.normalize_external_ts_to_utc(...)` for normalization.

## Duration/Latency Clocks
- Runtime durations, TTLs, retry intervals must use monotonic clocks (`time.monotonic()`), not wall-clock UTC.
- Never persist monotonic values as UTC timestamps.

## Query Safety
- Time-window SQL queries (`datetime('now','-X minutes')`) depend on canonical UTC text.
- Epoch-ms strings in `*_ts_utc` fields are invalid for these queries and must be normalized before insert.

## Agent Reminder
- Before changing timestamp formats in any external integration path:
  - Check relevant API docs/skills first.
  - Avoid schema-wide blind replacement.
  - Record any source-specific exceptions in handoff docs.
