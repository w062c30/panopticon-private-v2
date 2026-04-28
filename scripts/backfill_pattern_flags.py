"""
scripts/backfill_pattern_flags.py

Retroactively scan wallet_observations for Van Dyke-type insider patterns.
Expected to surface the Iran Feb-28 cluster and April-07 ceasefire cluster
if those wallets appear in our DB.

Usage:
    python scripts/backfill_pattern_flags.py --hours 720 --min-score 0.60

Logic:
  1. SELECT DISTINCT wallet_address FROM wallet_observations
     WHERE observed_ts_utc > (utcnow() - hours)
  2. For each wallet + each asset_id they bet on:
     - Get market_prior from execution_records or fallback to 0.50
     - Call compute_pattern_score()
     - If score >= min_score: insert_insider_pattern_flag()
  3. Print markdown table: top 20 wallets sorted by max pattern_score DESC
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, ".")
from panopticon_py.analysis.insider_pattern import compute_pattern_score
from panopticon_py.db import ShadowDB

logger = logging.getLogger(__name__)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def run_backfill(hours: int = 720, min_score: float = 0.60) -> int:
    """
    Backfill insider pattern flags over the given lookback window.
    Returns the number of flags inserted.
    """
    db = ShadowDB()
    db.bootstrap()

    cutoff_ts = datetime.now(timezone.utc).timestamp() - hours * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()

    logger.info("[BACKFILL] Scanning wallet_observations since %s", cutoff_iso)

    # Get all distinct wallet + market combinations in the window
    rows = db.conn.execute(
        """
        SELECT DISTINCT wo.address, wo.market_id, wo.ingest_ts_utc
        FROM wallet_observations wo
        WHERE wo.ingest_ts_utc > ?
        ORDER BY wo.ingest_ts_utc DESC
        """,
        (cutoff_iso,),
    ).fetchall()

    logger.info("[BACKFILL] Found %d distinct wallet-market pairs", len(rows))

    inserted = 0
    results: list[dict] = []

    for address, market_id, ingest_ts in rows:
        if not address or not market_id:
            continue

        # Get the most recent observation for stake estimation
        obs_row = db.conn.execute(
            """
            SELECT payload_json FROM wallet_observations
            WHERE address = ? AND market_id = ?
            ORDER BY ingest_ts_utc DESC LIMIT 1
            """,
            (address, market_id),
        ).fetchone()

        stake_usd = 0.0
        if obs_row and obs_row[0]:
            try:
                import json

                payload = json.loads(obs_row[0])
                stake_usd = float(payload.get("size") or payload.get("amount") or 0)
            except Exception:
                pass

        # Get wallet first seen
        first_seen = db.get_wallet_first_seen(address) or ingest_ts

        # Market prior: try to get from execution_records, else 0.50
        market_prior = 0.50
        try:
            er_row = db.conn.execute(
                """
                SELECT p_adj FROM execution_records
                WHERE market_id = ? OR token_id = ?
                ORDER BY created_ts_utc DESC LIMIT 1
                """,
                (market_id, market_id),
            ).fetchone()
            if er_row and er_row[0] is not None:
                market_prior = float(er_row[0])
        except Exception:
            pass

        try:
            result = compute_pattern_score(
                wallet_address=address,
                asset_id=market_id,
                stake_usd=stake_usd,
                market_prior=market_prior,
                account_first_seen_ts=first_seen,
                db_conn=db.conn,
            )
        except Exception:
            continue

        if result["score"] >= min_score:
            try:
                db.insert_insider_pattern_flag(
                    wallet_address=address,
                    asset_id=market_id,
                    detected_ts_utc=ingest_ts if ingest_ts else _utc(),
                    case_type=result["case_type"],
                    account_age_days=result["factors"].get("account_age", 0.0),
                    prior_at_bet=result["factors"].get("prior_conviction", 0.0),
                    stake_usd=stake_usd,
                    correlated_mkts=result["correlated_mkts"],
                    cluster_wallet_count=result["cluster_wallet_count"],
                    same_ts_wallets=result["same_ts_wallets"],
                    has_decoy_bets=int(result["has_decoy_bets"]),
                    pattern_score=result["score"],
                    flag_reason=f"BACKFILL_H{hours}_MIN{min_score}",
                )
                inserted += 1
            except Exception as e:
                logger.warning("[BACKFILL] insert failed for %s: %s", address[:20], e)

        results.append({
            "wallet": address[:20],
            "asset": market_id[:20],
            "score": result["score"],
            "case_type": result["case_type"],
            "age_days": result["factors"].get("account_age", 0.0),
            "prior_at_bet": result["factors"].get("prior_conviction", 0.0),
            "cluster_count": result["cluster_wallet_count"],
            "has_decoy": result["has_decoy_bets"],
        })

    # Sort by score descending and print top 20
    results.sort(key=lambda x: x["score"], reverse=True)
    top20 = results[:20]

    print(f"\n## INSIDER PATTERN BACKFILL RESULTS (lookback={hours}h, min_score={min_score})")
    print(f"Inserted: {inserted} flags | Scanned: {len(results)} wallet-market pairs\n")
    print("| wallet (trunc) | asset (trunc) | score | case_type | age_days | prior | cluster | has_decoy |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in top20:
        print(
            f"| `{r['wallet']}` | `{r['asset']}` | {r['score']:.3f} | {r['case_type']} "
            f"| {r['age_days']:.2f} | {r['prior_at_bet']:.3f} | {r['cluster_count']} | {r['has_decoy']} |"
        )

    return inserted


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Backfill insider pattern flags from wallet_observations")
    ap.add_argument("--hours", type=int, default=720, help="Lookback window in hours (default: 720 = 30 days)")
    ap.add_argument("--min-score", type=float, default=0.60, help="Minimum pattern score to flag (default: 0.60)")
    args = ap.parse_args()
    return 0 if run_backfill(hours=args.hours, min_score=args.min_score) >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
