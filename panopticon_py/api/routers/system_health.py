from __future__ import annotations

from fastapi import APIRouter

from panopticon_py.api.schemas import ReadinessResponse, SystemStatusResponse
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.link_resolver import backfill_unresolved_links_once

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
