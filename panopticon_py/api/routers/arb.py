"""
D150-1: Arb Scanner API router.
Provides /api/arb/health and /api/arb/stats endpoints.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from panopticon_py.db import ShadowDB
from panopticon_py.time_utils import utc_now_rfc3339_ms

router = APIRouter(prefix="/api/arb", tags=["arb"])


def _manifest_entry(name: str) -> dict[str, Any]:
    """Read manifest entry for a process. Returns empty dict if absent."""
    try:
        manifest_path = Path("run/process_manifest.json")
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return manifest.get(name, {})
    except Exception:
        pass
    return {}


def _heartbeat_age(entry: dict[str, Any]) -> float | None:
    """Compute heartbeat age in seconds, or None if no timestamp."""
    hb_ts = entry.get("last_heartbeat_ts")
    if not hb_ts:
        return None
    try:
        hb_dt = datetime.fromisoformat(hb_ts)
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - hb_dt).total_seconds()
    except (ValueError, TypeError):
        return None


@router.get("/health")
def arb_health() -> dict[str, Any]:
    """
    D148-3: Fast arb_scanner health check — manifest-based, zero arb_scanner overhead.
    Replaces the inline endpoint in app.py to consolidate all /api/arb/* routes here.
    """
    entry = _manifest_entry("arb_scanner")
    age_sec = _heartbeat_age(entry)
    pid = entry.get("pid")

    # D146-P1-1: Use is_process_alive() — Windows-safe (GetExitCodeProcess)
    pid_alive = False
    if pid:
        from panopticon_py.utils.process_guard import is_process_alive
        pid_alive = is_process_alive(int(pid))

    try:
        db = ShadowDB()
        latest_row = db.conn.execute(
            "SELECT reconnect_count FROM arb_stats ORDER BY ts_utc DESC LIMIT 1"
        ).fetchone()
        reconnect_count = int(latest_row["reconnect_count"]) if latest_row else 0
        db.close()
    except Exception:
        reconnect_count = 0

    return {
        "pid": pid,
        "pid_alive": pid_alive,
        "status": entry.get("status", "unknown"),
        "version": entry.get("version"),
        "heartbeat_age_s": round(age_sec, 1) if age_sec is not None else None,
        "heartbeat_stale": (age_sec or 9999) > 300,
        "heartbeat_bootstrapping": age_sec is None and pid_alive,
        "crash_reason": entry.get("crash_reason"),
        # D149-3: reconnect_warning flags excessive WS reconnects
        # D150-1: reconnect_critical flags sustained instability
        "reconnect_warning": reconnect_count > 3,
        "reconnect_critical": reconnect_count > 10,
        "reconnect_count": reconnect_count,
        "ts": utc_now_rfc3339_ms(),
    }


@router.get("/stats")
def arb_stats(limit: int = 60) -> JSONResponse:
    """
    D148-3: Return recent arb_stats rows from DB.
    limit=60 gives the last ~1h of 60s-granularity data.
    """
    try:
        db = ShadowDB()
        rows = db.conn.execute(
            "SELECT * FROM arb_stats ORDER BY ts_utc DESC LIMIT ?",
            (min(limit, 1440),),
        ).fetchall()
        db.close()
        return JSONResponse({
            "stats": [dict(r) for r in rows],
            "count": len(rows),
            "ts": utc_now_rfc3339_ms(),
        })
    except Exception as e:
        return JSONResponse(
            {"stats": [], "error": str(e), "ts": utc_now_rfc3339_ms()},
            status_code=500,
        )