from __future__ import annotations

from fastapi import APIRouter

from panopticon_py.api.schemas import ReportCurrentResponse, ReportCounts, ReportPnl, ReportQuality
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.live_trade_pnl_service import build_live_report, fetch_hybrid_trade_rows

router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("/current", response_model=ReportCurrentResponse)
def get_current_report() -> ReportCurrentResponse:
    db = ShadowDB()
    try:
        db.bootstrap()
        rows = fetch_hybrid_trade_rows(db, limit=200)
        link_stats = db.link_resolver_stats()
    finally:
        db.close()

    total_links = max(1, int(link_stats["mappingCount"]) + int(link_stats["unresolvedOpenCount"]))
    canonical_hit_rate = float(link_stats["mappingCount"]) / float(total_links)
    fallback_rate = float(link_stats["unresolvedOpenCount"]) / float(total_links)
    report = build_live_report(
        rows,
        canonical_hit_rate=canonical_hit_rate,
        fallback_rate=fallback_rate,
        unresolved_count=int(link_stats["unresolvedOpenCount"]),
    )
    return ReportCurrentResponse(
        counts=ReportCounts(**report["counts"]),
        pnl=ReportPnl(**report["pnl"]),
        quality=ReportQuality(**report["quality"]),
        findings=report["findings"],
        updatedAt=report["updatedAt"],
    )

