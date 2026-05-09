"""
D180: On-demand heavy diagnostics (manual button / curl only).
D188: execution_records gate_reason / reason aggregation (read-only SQLite).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

logger = logging.getLogger("panopticon.diagnostics")

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

_VALID_SORTS: frozenset[str] = frozenset({"abs_z", "hits", "kyle_n", "recent"})

_TTL_SEC = 30.0
_CC: dict[str, Any] = {"mono": 0.0, "key": "", "payload": None}


@router.get("/market_breakdown")
def api_market_breakdown(
    limit: int = 50,
    sort: str = "abs_z",
) -> dict[str, Any]:
    if sort not in _VALID_SORTS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_VALID_SORTS)}")
    key = f"{limit}:{sort}"
    now = time.monotonic()
    cached = _CC.get("payload")
    if (
        cached is not None
        and _CC.get("key") == key
        and (now - float(_CC.get("mono") or 0)) < _TTL_SEC
    ):
        out = dict(cached)
        out["cache_hit"] = True
        out["cache_ttl_sec"] = round(_TTL_SEC - (now - float(_CC.get("mono") or 0)), 2)
        return out

    payload = build_market_breakdown(limit=limit, sort_key=sort)  # type: ignore[arg-type]
    _CC["payload"] = payload
    _CC["key"] = key
    _CC["mono"] = now
    out = dict(payload)
    out["cache_hit"] = False
    return out


@router.get("/execution_reasons")
def api_execution_reasons(window_hours: int = 24) -> dict[str, Any]:
    """
    D188: Aggregate execution_records by gate_reason (fallback reason) for the last N hours.
    Read-only DB connection — no ShadowDB write lock.
    """
    if window_hours < 1 or window_hours > 168:
        raise HTTPException(status_code=400, detail="window_hours must be 1–168")

    db_path = os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(gate_reason), ''), NULLIF(TRIM(reason), ''), 'UNKNOWN') AS reason,
                COUNT(*) AS cnt
            FROM execution_records
            WHERE created_ts_utc >= ?
            GROUP BY 1
            ORDER BY cnt DESC
            LIMIT 50
            """,
            (cutoff_iso,),
        ).fetchall()
        reasons = [{"reason": str(r["reason"]), "count": int(r["cnt"])} for r in rows]
        total = sum(r["count"] for r in reasons)
        return {
            "window_hours": window_hours,
            "total": total,
            "reasons": reasons,
        }
    except Exception as exc:
        logger.warning("[DIAGNOSTICS][EXEC_REASONS] error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
