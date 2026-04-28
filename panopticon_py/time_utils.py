from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_rfc3339_ms() -> str:
    """Canonical internal timestamp: UTC RFC3339 with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_external_ts_to_utc(value: Any) -> str:
    """
    Normalize external timestamp values to canonical internal UTC string.

    Accepts:
    - epoch milliseconds (int/float/string)
    - epoch seconds (int/float/string)
    - ISO/RFC3339 strings
    - empty/invalid values (falls back to current UTC time)
    """
    if value is None:
        return utc_now_rfc3339_ms()

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return utc_now_rfc3339_ms()
        if raw.isdigit():
            value = int(raw)
        else:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            except ValueError:
                return utc_now_rfc3339_ms()

    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except Exception:
            return utc_now_rfc3339_ms()

    return utc_now_rfc3339_ms()
