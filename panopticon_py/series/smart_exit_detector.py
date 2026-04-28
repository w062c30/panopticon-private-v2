"""
panopticon_py/series/smart_exit_detector.py

D21 Phase 3 — Smart Exit Alert.

Detects when high insider_score wallets are exiting YES positions
at high probability (>= 70%) in T2 markets.

This is the mirror of pre-catalyst accumulation:
smart money that bought at 10% is now selling at 80% —
strong signal the top is in.

In Phase 3: LOG only. No p_prior adjustment yet.
Requires 100 paper trades back-validation before architect approves接入.

Usage:
  await check_smart_exit(wallet_addr, market_id, "NO", trade_price, trade_size, db)
  # Called from analysis_worker after each wallet_observation write.
  # Does NOT block the write path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Thresholds (architect-configurable via env)
SMART_EXIT_THRESHOLD_PROB = float(
    __import__("os").getenv("SMART_EXIT_THRESHOLD_PROB", "0.70")
)
SMART_EXIT_MIN_INSIDER_SCORE = float(
    __import__("os").getenv("SMART_EXIT_MIN_INSIDER_SCORE", "0.60")
)
SMART_EXIT_MIN_POSITION_RATIO = float(
    __import__("os").getenv("SMART_EXIT_MIN_POSITION_RATIO", "0.30")
)


async def check_smart_exit(
    wallet_addr: str,
    market_id: str,
    trade_side: str,  # "NO" = selling YES
    trade_price: float,
    trade_size: float,
    db,
) -> bool:
    """
    Returns True if this trade looks like a smart exit.
    Called from analysis_worker after each wallet_observation write.
    Does NOT block the write path — always logs and returns bool.
    """
    # Only "NO" side (selling YES) or explicit "SELL_YES" qualifies
    if trade_side not in ("NO", "SELL_YES"):
        return False
    # Must be selling at >= threshold probability
    if trade_price < SMART_EXIT_THRESHOLD_PROB:
        return False

    # Must be a tracked insider
    try:
        insider_score = db.get_latest_insider_score(wallet_addr)
    except Exception:
        return False
    if not insider_score or insider_score < SMART_EXIT_MIN_INSIDER_SCORE:
        return False

    # Must have a known long position (side=YES, OPEN)
    try:
        position = db.get_wallet_series_position(wallet_addr, market_id)
    except Exception:
        return False
    if not position or position.get("side") != "YES":
        return False
    if position.get("position_status") == "CLOSED":
        return False

    # Compute what fraction of position is being sold
    position_size = position.get("total_size") or 1e-8
    sell_ratio = trade_size / position_size
    if sell_ratio < SMART_EXIT_MIN_POSITION_RATIO:
        return False

    logger.info(
        "[SMART_EXIT] wallet=%s...%s market=%s "
        "exit_prob=%.3f sell_ratio=%.1f%% insider_score=%.3f "
        "original_entry=%.3f",
        wallet_addr[:8], wallet_addr[-4:],
        market_id[:20],
        trade_price, sell_ratio * 100,
        insider_score,
        position.get("avg_entry_prob") or 0.0,
    )

    # Write violation record (LOGGED only — no trade signal yet)
    try:
        series_id = db.get_series_id_for_market(market_id)
        db.write_series_violation(
            series_id=series_id,
            violation_type="SMART_EXIT",
            wallet_address=wallet_addr,
            gap_pct=trade_price - (position.get("avg_entry_prob") or 0.0),
            action_taken="LOGGED",
        )
        db.update_wallet_series_position_exit(
            wallet_addr=wallet_addr,
            slug=market_id,
            exit_prob=trade_price,
            sell_ratio=sell_ratio,
        )
    except Exception as e:
        logger.warning("[SMART_EXIT][DB_ERROR] %s", e)

    return True