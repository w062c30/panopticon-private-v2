"""
D103: T2-POL Watchlist API Router
=================================
Serves the active political market watchlist to dashboard.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from panopticon_py.db import ShadowDB
from panopticon_py.time_utils import utc_now_rfc3339_ms

router = APIRouter(prefix="/api", tags=["watchlist"])
logger = logging.getLogger(__name__)


@router.get("/pol-watchlist")
def get_pol_watchlist() -> dict:
    """
    D103: Return active T2-POL political market watchlist.
    D105: Added generated_at for data freshness visibility.
    D107: Log actual exception instead of silent failure.
    """
    db = ShadowDB()
    try:
        markets = db.fetch_active_pol_markets()
        return {
            "count": len(markets),
            "markets": markets,
            "generated_at": utc_now_rfc3339_ms(),
        }
    except Exception as exc:
        logger.warning("[POL_WATCHLIST] fetch failed: %s", exc)
        return {
            "count": 0,
            "markets": [],
            "generated_at": utc_now_rfc3339_ms(),
            "error": str(exc),
        }
    finally:
        db.close()
