"""
D103: T2-POL Watchlist API Router
=================================
Serves the active political market watchlist to dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter

from panopticon_py.db import ShadowDB

router = APIRouter(prefix="/api", tags=["watchlist"])


@router.get("/pol-watchlist")
def get_pol_watchlist() -> dict:
    """
    D103: Return active T2-POL political market watchlist.
    Used by dashboard to display monitored political markets.
    """
    db = ShadowDB()
    try:
        db.bootstrap()
        markets = db.fetch_active_pol_markets()
        return {
            "count": len(markets),
            "markets": markets,
        }
    finally:
        db.close()
