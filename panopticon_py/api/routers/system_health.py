from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from panopticon_py.api.schemas import ReadinessResponse, SystemStatusResponse
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.link_resolver import backfill_unresolved_links_once
from panopticon_py.utils.process_guard import get_all_versions

router = APIRouter(prefix="/api/system_health", tags=["system_health"])


@router.get("/readiness", response_model=ReadinessResponse)
def get_readiness() -> ReadinessResponse:
    db = ShadowDB()
    try:
        db.bootstrap()
        readiness = db.fetch_readiness_metrics()
    finally:
        db.close()
    return ReadinessResponse(
        currentPaperTrades=readiness["current_paper_trades"],
        targetTrades=readiness["target_trades"],
        runningDays=readiness["running_days"],
        targetDays=readiness["target_days"],
        currentWinRate=readiness["current_win_rate"],
        isReady=readiness["is_ready"],
    )


@router.get("/status", response_model=SystemStatusResponse)
def get_status() -> SystemStatusResponse:
    db = ShadowDB()
    try:
        db.bootstrap()
        status = db.fetch_system_status()
    finally:
        db.close()

    return SystemStatusResponse(
        state=str(status["state"]),
        message=str(status["message"]),
        lastEventTs=status["last_event_ts"],
        lastDecisionId=status["last_decision_id"],
        lastExecutionReason=status["last_execution_reason"],
        lastRejectReason=status["last_reject_reason"],
    )


@router.get("/link_resolver_stats")
def get_link_resolver_stats() -> dict[str, int]:
    db = ShadowDB()
    try:
        db.bootstrap()
        resolved_now = backfill_unresolved_links_once(db, limit=20)
        stats = db.link_resolver_stats()
    finally:
        db.close()
    return {
        "mappingCount": int(stats["mapping_count"]),
        "unresolvedOpenCount": int(stats["unresolved_count"]),
        "unresolvedResolvedCount": int(stats["resolved_count"]),
        "resolvedThisCycle": int(resolved_now),
    }


@router.get("/watchdog_status")
def get_watchdog_status() -> dict[str, Any]:
    """
    D116: Return watchdog liveness + all monitored process heartbeat ages.
    Used by frontend system health dashboard for real-time process monitoring.

    Note: manifest read is fast (JSON file, no DB). 1s polling is acceptable.
    """
    manifest = get_all_versions()
    now = datetime.now(timezone.utc)
    processes: dict[str, dict[str, Any]] = {}

    for name, entry in manifest.items():
        hb_ts = entry.get("last_heartbeat_ts")
        age_sec: float | None = None
        if hb_ts:
            try:
                hb_dt = datetime.fromisoformat(hb_ts)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                age_sec = round((now - hb_dt).total_seconds(), 1)
            except (ValueError, TypeError):
                pass

        processes[name] = {
            "status": entry.get("status", "unknown"),
            "version": entry.get("version", "unknown"),
            "version_match": entry.get("version_match"),
            "heartbeat_age_sec": age_sec,
            "pid": entry.get("pid"),
        }

    return {
        "processes": processes,
        "checked_at": now.isoformat(),
    }
