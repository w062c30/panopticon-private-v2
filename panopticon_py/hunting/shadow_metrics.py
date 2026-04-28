"""Shadow-mode metrics: win rate from hunting_shadow_hits (no auto-live)."""

from __future__ import annotations

import os

from panopticon_py.db import ShadowDB


def shadow_win_rate(db: ShadowDB, *, min_rows: int | None = None) -> float | None:
    m = int(min_rows if min_rows is not None else os.getenv("HUNT_SHADOW_MIN_OUTCOMES", "5"))
    return db.hunting_shadow_win_rate(min_rows=m)


def shadow_unlock_hint_allowed(db: ShadowDB) -> bool:
    """
    Returns True if rolling win rate exceeds threshold — **hint only**; never enables live trading.
    """
    thr = float(os.getenv("HUNT_SHADOW_WIN_RATE_HINT", "0.75"))
    wr = shadow_win_rate(db)
    if wr is None:
        return False
    return wr >= thr and os.getenv("HUNT_SHADOW_UNLOCK_HINT", "0").lower() in ("1", "true", "yes")
