"""
scripts/rvf_query.py

RVF Manual Query Tool — investigate pipeline health from the DB.

Usage:
    python scripts/rvf_query.py --db data/panopticon.db --hours 24

All queries are READ-ONLY — no writes to DB.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone, timedelta


def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _print(msg: str) -> None:
    print(msg)


def query_a_kyle_accumulation(db_path: str, hours: int = 24) -> None:
    """Kyle accumulation trajectory — last N hours, hourly samples."""
    _print(f"\n{'='*60}")
    _print(f"A. Kyle Lambda Accumulation Trajectory (last {hours}h)")
    _print(f"{'='*60}")
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%d %H:00', ts_utc) as hour,
                COUNT(*) as samples,
                AVG(lambda_obs) as avg_lambda,
                MIN(ts_utc) as first_sample,
                MAX(ts_utc) as last_sample
            FROM kyle_lambda_samples
            WHERE ts_utc > datetime('now', ?)
            GROUP BY hour
            ORDER BY hour
        """, (f"-{hours} hours",)).fetchall()

    if not rows:
        _print("  No kyle_lambda_samples in this window.")
        return

    total = sum(r["samples"] for r in rows)
    _print(f"  Total samples: {total}  |  Hours: {len(rows)}")
    _print(f"  {'hour':<20} {'samples':>8} {'avg_lambda':>12}")
    _print(f"  {'-'*20} {'-'*8} {'-'*12}")
    for r in rows:
        _print(f"  {r['hour']:<20} {r['samples']:>8} {r['avg_lambda']:>12.6f}")


def query_b_pipeline_timeline(db_path: str, limit: int = 24) -> None:
    """Pipeline health snapshots — last N windows."""
    _print(f"\n{'='*60}")
    _print(f"B. Pipeline Health Timeline (last {limit} snapshots)")
    _print(f"{'='*60}")
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT
                ts_utc,
                window_minutes,
                l1_trade_ticks_received,
                l1_entropy_fires,
                l3_bayesian_updates,
                l4_paper_trades,
                l4_live_trades,
                kyle_accumulation_rate,
                data_staleness_flag,
                notes
            FROM pipeline_health
            ORDER BY ts_utc DESC
            LIMIT ?
        """, (limit,)).fetchall()

    if not rows:
        _print("  No pipeline_health snapshots yet.")
        _print("  Run RVF (PANOPTICON_RVF=1) to collect snapshots.")
        return

    _print(f"  {'ts_utc':<28} {'ticks':>6} {'fires':>6} {'bayes':>6} "
           f"{'paper':>6} {'live':>5} {'kyle%':>7} {'stale':>5}")
    _print(f"  {'-'*28} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*5} {'-'*7} {'-'*5}")
    for r in rows:
        _print(
            f"  {r['ts_utc']:<28} "
            f"{r['l1_trade_ticks_received'] or 0:>6} "
            f"{r['l1_entropy_fires'] or 0:>6} "
            f"{r['l3_bayesian_updates'] or 0:>6} "
            f"{r['l4_paper_trades'] or 0:>6} "
            f"{r['l4_live_trades'] or 0:>5} "
            f"{(r['kyle_accumulation_rate'] or 0) * 100:>6.1f}% "
            f"{r['data_staleness_flag'] or 0:>5}"
        )


def query_c_paper_trade_by_tier(db_path: str, hours: int = 24) -> None:
    """Paper trade performance by tier — last N hours."""
    _print(f"\n{'='*60}")
    _print(f"C. Paper Trade Performance by Tier (last {hours}h)")
    _print(f"{'='*60}")
    with _conn(db_path) as conn:
        rows = conn.execute("""
            SELECT
                er.market_tier,
                COUNT(*) as trades,
                AVG(er.ev_net) as avg_ev,
                AVG(er.kelly_fraction) as avg_kelly,
                MIN(er.created_ts_utc) as first_trade,
                MAX(er.created_ts_utc) as last_trade
            FROM execution_records er
            WHERE er.created_ts_utc > datetime('now', ?)
              AND er.mode = 'PAPER'
            GROUP BY er.market_tier
            ORDER BY trades DESC
        """, (f"-{hours} hours",)).fetchall()

    if not rows:
        _print("  No PAPER execution_records in this window.")
        return

    _print(f"  {'tier':<6} {'trades':>7} {'avg_ev':>10} {'avg_kelly':>10} "
           f"{'first':<28} {'last':<28}")
    _print(f"  {'-'*6} {'-'*7} {'-'*10} {'-'*10} {'-'*28} {'-'*28}")
    for r in rows:
        _print(
            f"  {(r['market_tier'] or 'unknown'):<6} "
            f"{r['trades']:>7} "
            f"{r['avg_ev'] or 0.0:>10.4f} "
            f"{r['avg_kelly'] or 0.0:>10.4f} "
            f"{r['first_trade'] or '':<28} "
            f"{r['last_trade'] or '':<28}"
        )


def query_d_entropy_fire_latency(db_path: str, hours: int = 6) -> None:
    """Entropy fire vs paper trade — per-minute timeline."""
    _print(f"\n{'='*60}")
    _print(f"D. Entropy Fire vs Paper Trade Latency (last {hours}h)")
    _print(f"{'='*60}")
    with _conn(db_path) as conn:
        # hunting_shadow_hits = entropy fire proxy
        # LEFT JOIN with execution_records to compare
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%d %H:%M', sh.ts_utc) as minute,
                COUNT(sh.id) as entropy_fires,
                COUNT(er.id) as paper_trades
            FROM hunting_shadow_hits sh
            LEFT JOIN execution_records er ON
                strftime('%Y-%m-%d %H:%M', er.created_ts_utc) =
                strftime('%Y-%m-%d %H:%M', sh.ts_utc)
                AND er.mode = 'PAPER'
            WHERE sh.ts_utc > datetime('now', ?)
            GROUP BY minute
            ORDER BY minute
        """, (f"-{hours} hours",)).fetchall()

    if not rows:
        _print("  No hunting_shadow_hits in this window.")
        return

    _print(f"  {'minute':<20} {'entropy_fires':>14} {'paper_trades':>14} {'pass_rate':>10}")
    _print(f"  {'-'*20} {'-'*14} {'-'*14} {'-'*10}")
    for r in rows:
        fires = r["entropy_fires"] or 0
        trades = r["paper_trades"] or 0
        rate = (trades / max(fires, 1)) if fires else 0.0
        _print(
            f"  {r['minute']:<20} {fires:>14} {trades:>14} {rate:>9.1%}"
        )


def query_e_t1_activity(db_path: str, limit: int = 10) -> None:
    """T1 market activity — kyle samples by asset_id (top N)."""
    _print(f"\n{'='*60}")
    _print(f"E. T1 Market Activity (top {limit} by sample count)")
    _print(f"{'='*60}")
    with _conn(db_path) as conn:
        rows = conn.execute(f"""
            SELECT
                kls.asset_id,
                COUNT(*) as samples,
                MIN(kls.ts_utc) as first_sample,
                MAX(kls.ts_utc) as last_sample,
                AVG(kls.lambda_obs) as avg_lambda,
                COUNT(DISTINCT strftime('%Y-%m-%d %H', kls.ts_utc)) as active_hours
            FROM kyle_lambda_samples kls
            GROUP BY kls.asset_id
            ORDER BY samples DESC
            LIMIT {limit}
        """).fetchall()

    if not rows:
        _print("  No kyle_lambda_samples in DB yet.")
        return

    total_rows = conn.execute("SELECT COUNT(*) as cnt FROM kyle_lambda_samples").fetchone()
    _print(f"  Total kyle samples in DB: {total_rows['cnt']}")
    _print(f"  {'asset_id':<50} {'samples':>8} {'avg_lambda':>12} "
           f"{'hours':>6} {'first':<28} {'last':<28}")
    _print(f"  {'-'*50} {'-'*8} {'-'*12} {'-'*6} {'-'*28} {'-'*28}")
    for r in rows:
        _print(
            f"  {str(r['asset_id'] or '')[:50]:<50} "
            f"{r['samples']:>8} "
            f"{r['avg_lambda'] or 0.0:>12.6f} "
            f"{r['active_hours'] or 0:>6} "
            f"{r['first_sample'] or '':<28} "
            f"{r['last_sample'] or '':<28}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RVF Manual Query Tool — Panopticon pipeline investigation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default=os.getenv("PANOPTICON_DB", "data/panopticon.db"),
        help="Path to panopticon.db (default: PANOPTICON_DB env or data/panopticon.db)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Window for time-bounded queries in hours (default: 24)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Row limit for top-N queries (default: 10)",
    )
    parser.add_argument(
        "--query",
        choices=["a", "b", "c", "d", "e", "all"],
        default="all",
        help="Which query to run (default: all)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found at {args.db}")
        return

    print(f"\nRVF Query Tool | DB: {args.db} | Hours: {args.hours}")

    if args.query in ("a", "all"):
        query_a_kyle_accumulation(args.db, args.hours)
    if args.query in ("b", "all"):
        query_b_pipeline_timeline(args.db, args.limit)
    if args.query in ("c", "all"):
        query_c_paper_trade_by_tier(args.db, args.hours)
    if args.query in ("d", "all"):
        query_d_entropy_fire_latency(args.db, args.hours)
    if args.query in ("e", "all"):
        query_e_t1_activity(args.db, args.limit)

    print(f"\n{'='*60}")
    print("RVF Query complete.")


if __name__ == "__main__":
    main()