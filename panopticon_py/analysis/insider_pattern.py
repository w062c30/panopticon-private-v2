"""
Panopticon — INSIDER_PATTERN_COLLECTOR Module
Date: 2026-04-24
Authority: PANOPTICON_CORE_LOGIC.md v2.1
Ground truth: Van Dyke/Maduro (DOJ 2026-04-23) + Iran Ceasefire (2026-04-07)
              + Iran Airstrikes (2026-02-28)

CRITICAL INVARIANT 6.2 (PANOPTICON_CORE_LOGIC.md):
  pattern_score is FORENSIC ONLY.
  It MUST NOT enter signal_engine, SignalEvent, p_prior, LR, posterior, ev_net.
  This module is for human review and pattern library construction only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _amount_from_payload(payload_json: str | dict) -> float:
    """
    Extract trade amount (size/usd) from a wallet_observation payload.
    Handles both 'size' (Data API) and 'Size' (WS uppercase) keys.
    """
    if isinstance(payload_json, dict):
        d = payload_json
    else:
        try:
            d = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            return 0.0
    # Try lowercase first (Data API convention), then uppercase (WS convention)
    for key in ("size", "Size", "amount", "Amount"):
        v = d.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(first_seen_ts: str | None) -> float:
    """Return wallet age in days at time of first observation."""
    if not first_seen_ts:
        return 999.0  # unknown age → treat as old (low suspicion)
    parsed = _parse_ts(first_seen_ts)
    if parsed is None:
        return 999.0
    delta = _utcnow() - parsed
    return max(0.0, delta.total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Pattern scoring
# ---------------------------------------------------------------------------


def compute_pattern_score(
    wallet_address: str,
    asset_id: str,
    stake_usd: float,
    market_prior: float,
    account_first_seen_ts: str | None,
    db_conn: Any,  # ShadowDB connection
) -> dict[str, Any]:
    """
    Compute insider-pattern score for a single wallet bet.

    Returns
    -------
    {
        "score": float,               # 0.0 - 1.0
        "case_type": str,             # 'SOLO_OP' | 'CLUSTER' | 'DECOY_CLUSTER'
        "factors": dict,             # breakdown of each factor contribution
        "correlated_mkts": int,
        "cluster_wallet_count": int,
        "same_ts_wallets": int,
        "has_decoy_bets": bool
    }

    Scoring is forensic-only per Invariant 6.2.
    It must NEVER be passed to signal_engine, SignalEvent, p_prior, LR, posterior, ev_net.
    """
    score = 0.0
    factors: dict[str, Any] = {}

    # ── Factor 1: Account age ──────────────────────────────────────────────
    age_days_val = _age_days(account_first_seen_ts)
    if age_days_val < 0.02:  # < 30 minutes
        factors["account_age"] = 0.40
        score += 0.40
    elif age_days_val < 1:  # < 1 day
        factors["account_age"] = 0.35
        score += 0.35
    elif age_days_val < 7:  # < 1 week
        factors["account_age"] = 0.25
        score += 0.25
    elif age_days_val < 30:  # < 1 month
        factors["account_age"] = 0.10
        score += 0.10
    else:
        factors["account_age"] = 0.0

    # ── Factor 2: Market prior conviction ─────────────────────────────────
    if market_prior < 0.05:
        factors["prior_conviction"] = 0.30
        score += 0.30
    elif market_prior < 0.10:
        factors["prior_conviction"] = 0.22
        score += 0.22
    elif market_prior < 0.20:
        factors["prior_conviction"] = 0.14
        score += 0.14
    elif market_prior < 0.35:
        factors["prior_conviction"] = 0.07
        score += 0.07
    else:
        factors["prior_conviction"] = 0.0

    # ── Factor 3: Correlated market cluster ───────────────────────────────
    correlated = _count_correlated_bets(db_conn, wallet_address, asset_id, hours=72)
    if correlated >= 4:
        factors["correlated_mkts"] = 0.15
        score += 0.15
    elif correlated >= 2:
        factors["correlated_mkts"] = 0.10
        score += 0.10
    else:
        factors["correlated_mkts"] = 0.0

    # ── Factor 4: Coordinated wallet cluster ──────────────────────────────
    cluster_count = _count_cluster_wallets(
        db_conn, wallet_address, asset_id, stake_usd, tolerance=0.20, hours=1, max_age_days=7
    )
    if cluster_count >= 10:
        factors["cluster"] = 0.20
        score += 0.20
    elif cluster_count >= 3:
        factors["cluster"] = 0.12
        score += 0.12
    elif cluster_count >= 1:
        factors["cluster"] = 0.05
        score += 0.05
    else:
        factors["cluster"] = 0.0

    # ── Factor 5: Decoy bet signature ─────────────────────────────────────
    has_decoy = _detect_decoy_pattern(
        db_conn, wallet_address, asset_id, large_threshold=1000, small_threshold=100
    )
    factors["decoy_bets"] = 0.10 if has_decoy else 0.0
    score += factors["decoy_bets"]

    # ── Case type classification ─────────────────────────────────────────
    if cluster_count >= 3 and has_decoy:
        case_type = "DECOY_CLUSTER"
    elif cluster_count >= 3:
        case_type = "CLUSTER"
    else:
        case_type = "SOLO_OP"

    # ── same_ts_wallets (computed as part of cluster detection) ──────────
    same_ts_wallets = _count_same_ts_wallets(
        db_conn, wallet_address, asset_id, stake_usd, tolerance=0.20, hours=1
    )

    return {
        "score": min(score, 1.0),
        "case_type": case_type,
        "factors": factors,
        "correlated_mkts": correlated,
        "cluster_wallet_count": cluster_count,
        "same_ts_wallets": same_ts_wallets,
        "has_decoy_bets": has_decoy,
    }


# ---------------------------------------------------------------------------
# DB query helpers (used internally by compute_pattern_score)
# ---------------------------------------------------------------------------


def _count_correlated_bets(
    db_conn: Any, wallet_address: str, asset_id: str, hours: int = 72
) -> int:
    """
    Count distinct OTHER market_ids this wallet bet on in the same 72h window.
    Proxy for "did this wallet bet across multiple markets in same event cluster?"
    """
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        row = db_conn.execute(
            """
            SELECT COUNT(DISTINCT market_id)
            FROM wallet_observations
            WHERE address = ?
              AND market_id != ?
              AND ingest_ts_utc > ?
            """,
            (wallet_address.lower(), asset_id, cutoff_iso),
        ).fetchone()
        return int(row[0] if row and row[0] is not None else 0)
    except Exception:
        return 0


def _get_amount_from_obs(db_conn: Any, wallet_address: str, asset_id: str) -> float | None:
    """Extract amount (USD) from most recent wallet_observation for a wallet+asset."""
    try:
        row = db_conn.execute(
            """
            SELECT payload_json FROM wallet_observations
            WHERE address = ? AND market_id = ?
            ORDER BY ingest_ts_utc DESC LIMIT 1
            """,
            (wallet_address.lower(), asset_id),
        ).fetchone()
        if not row or not row[0]:
            return None
        return _amount_from_payload(row[0])
    except Exception:
        return None


def _count_cluster_wallets(
    db_conn: Any,
    wallet_address: str,
    asset_id: str,
    stake_usd: float,
    tolerance: float = 0.20,
    hours: int = 1,
    max_age_days: int = 7,
) -> int:
    """
    Count OTHER wallets (not this one) with similar stake size (±tolerance),
    same asset, same time window, all new (<max_age_days old).
    Uses _amount_from_payload to handle both 'size' and 'Size' keys.
    """
    try:
        cutoff_ts = datetime.now(timezone.utc).timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
        lo = stake_usd * (1 - tolerance)
        hi = stake_usd * (1 + tolerance)

        rows = db_conn.execute(
            """
            SELECT wo.address, wo.payload_json
            FROM wallet_observations wo
            JOIN (
                SELECT address, MIN(ingest_ts_utc) as first_seen
                FROM wallet_observations GROUP BY address
            ) age ON wo.address = age.address
            WHERE wo.market_id = ?
              AND wo.ingest_ts_utc > ?
              AND (julianday('now') - julianday(age.first_seen)) <= ?
            """,
            (asset_id, cutoff_iso, max_age_days),
        ).fetchall()

        count = 0
        for addr, payload in rows:
            if addr.lower() == wallet_address.lower():
                continue
            amt = _amount_from_payload(payload)
            if lo <= amt <= hi:
                count += 1
        return count
    except Exception:
        return 0


def _count_same_ts_wallets(
    db_conn: Any,
    wallet_address: str,
    asset_id: str,
    stake_usd: float,
    tolerance: float = 0.20,
    hours: int = 1,
) -> int:
    """
    Count wallets that placed bets within ±60 seconds of this wallet's most recent bet,
    with similar stake size, for the same market.
    """
    try:
        rows = db_conn.execute(
            """
            SELECT ingest_ts_utc FROM wallet_observations
            WHERE address = ? AND market_id = ?
            ORDER BY ingest_ts_utc DESC LIMIT 1
            """,
            (wallet_address.lower(), asset_id),
        ).fetchall()
        if not rows:
            return 0
        ref_ts_str = rows[0][0]
        ref_ts = _parse_ts(ref_ts_str)
        if ref_ts is None:
            return 0

        lo = stake_usd * (1 - tolerance)
        hi = stake_usd * (1 + tolerance)
        delta_start = ref_ts.timestamp() - 60
        delta_end = ref_ts.timestamp() + 60
        start_iso = datetime.fromtimestamp(delta_start, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(delta_end, tz=timezone.utc).isoformat()

        rows2 = db_conn.execute(
            """
            SELECT wo.address, wo.payload_json
            FROM wallet_observations wo
            WHERE wo.market_id = ?
              AND wo.ingest_ts_utc BETWEEN ? AND ?
              AND wo.address != ?
            """,
            (asset_id, start_iso, end_iso, wallet_address.lower()),
        ).fetchall()

        count = 0
        for addr, payload in rows2:
            amt = _amount_from_payload(payload)
            if lo <= amt <= hi:
                count += 1
        return count
    except Exception:
        return 0


def _detect_decoy_pattern(
    db_conn: Any,
    wallet_address: str,
    asset_id: str,
    large_threshold: float = 1000,
    small_threshold: float = 100,
) -> bool:
    """
    Detect decoy bet pattern:
    - Wallet has >=1 large bet (>= large_threshold USD) on target market
    - Wallet also has >0 small bets (<= small_threshold USD) on OTHER markets
    Returns True for decoy signature per Case C (Iran Airstrikes cluster).
    """
    try:
        wallet_lower = wallet_address.lower()

        # Fetch all observations for this wallet on the target market
        target_rows = db_conn.execute(
            """
            SELECT payload_json FROM wallet_observations
            WHERE address = ? AND market_id = ?
            """,
            (wallet_lower, asset_id),
        ).fetchall()

        has_large = False
        for (payload,) in target_rows:
            amt = _amount_from_payload(payload)
            if amt >= large_threshold:
                has_large = True
                break

        # Fetch all observations for this wallet on OTHER markets
        other_rows = db_conn.execute(
            """
            SELECT payload_json FROM wallet_observations
            WHERE address = ? AND market_id != ?
            """,
            (wallet_lower, asset_id),
        ).fetchall()

        has_small = False
        for (payload,) in other_rows:
            amt = _amount_from_payload(payload)
            if 0 < amt <= small_threshold:
                has_small = True
                break

        return bool(has_large and has_small)
    except Exception:
        return False
