"""Panopticon insight report generator - run every 2 hours via scheduler or manually."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from panopticon_py.load_env import load_repo_env

logger = logging.getLogger(__name__)


def get_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "panopticon.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_age(ts: str) -> str:
    """Return human-readable age of a UTC timestamp."""
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - then
        total_sec = delta.total_seconds()
        if total_sec < 60:
            return f"{total_sec:.0f}s ago"
        elif total_sec < 3600:
            return f"{total_sec/60:.1f}m ago"
        elif total_sec < 86400:
            return f"{total_sec/3600:.1f}h ago"
        else:
            return f"{total_sec/86400:.1f}d ago"
    except Exception:
        return ts


def load_repo_env_if_needed() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_repo_env()


def generate_insight_report(conn: sqlite3.Connection) -> dict:
    """Generate structured insight report from DB."""
    cur = conn.cursor()
    report: dict = {
        "generated_utc": utc_now(),
        "data_collection": {},
        "wallet_breakdown": {},
        "shadow_hits": {},
        "entity_insights": {},
        "api_health": {},
    }

    # ── Wallet Counts ───────────────────────────────────────────────────────
    try:
        cur.execute("SELECT COUNT(*) FROM tracked_wallets")
        report["data_collection"]["total_tracked_wallets"] = cur.fetchone()[0]
    except Exception:
        report["data_collection"]["total_tracked_wallets"] = 0

    try:
        cur.execute("SELECT COUNT(*) FROM discovered_entities")
        report["data_collection"]["total_discovered_entities"] = cur.fetchone()[0]
    except Exception:
        report["data_collection"]["total_discovered_entities"] = 0

    try:
        cur.execute("SELECT COUNT(*) FROM hunting_shadow_hits")
        report["data_collection"]["total_shadow_hits"] = cur.fetchone()[0]
    except Exception:
        report["data_collection"]["total_shadow_hits"] = 0

    try:
        cur.execute("SELECT COUNT(*) FROM raw_events")
        report["data_collection"]["total_raw_events"] = cur.fetchone()[0]
    except Exception:
        report["data_collection"]["total_raw_events"] = 0

    try:
        cur.execute("SELECT COUNT(*) FROM paper_trades")
        report["data_collection"]["total_paper_trades"] = cur.fetchone()[0]
    except Exception:
        report["data_collection"]["total_paper_trades"] = 0

    # ── Wallet Breakdown by Source ────────────────────────────────────────────
    try:
        cur.execute("SELECT discovery_source, COUNT(*) FROM tracked_wallets GROUP BY discovery_source")
        by_source = {row[0]: row[1] for row in cur.fetchall()}
        report["wallet_breakdown"]["by_source"] = by_source
        report["wallet_breakdown"]["track_a_ratio"] = round(
            by_source.get("TRACK_A_CLOB_TAKER", 0) / max(1, sum(by_source.values())), 3
        )
    except Exception:
        pass

    # ── Shadow Hits Analysis ──────────────────────────────────────────────────
    try:
        cur.execute("SELECT COUNT(*) FROM hunting_shadow_hits")
        hits_count = cur.fetchone()[0]
        report["shadow_hits"]["total"] = hits_count

        if hits_count > 0:
            cur.execute("SELECT MIN(entropy_z), MAX(entropy_z), AVG(entropy_z) FROM hunting_shadow_hits")
            row = cur.fetchone()
            report["shadow_hits"]["entropy_z_stats"] = {
                "min": round(row[0], 4) if row[0] is not None else None,
                "max": round(row[1], 4) if row[1] is not None else None,
                "avg": round(row[2], 4) if row[2] is not None else None,
            }

            # Most recent hits
            cur.execute(
                "SELECT address, entropy_z, sim_pnl_proxy, created_ts_utc "
                "FROM hunting_shadow_hits ORDER BY created_ts_utc DESC LIMIT 5"
            )
            recent = [
                {
                    "address": r[0][:20] + "...",
                    "entropy_z": round(r[1], 4) if r[1] is not None else None,
                    "sim_pnl_proxy": round(r[2], 6) if r[2] is not None else None,
                    "age": utc_age(r[3]),
                }
                for r in cur.fetchall()
            ]
            report["shadow_hits"]["recent"] = recent
    except Exception:
        pass

    # ── Tracked Wallet Top Wallets ───────────────────────────────────────────
    try:
        cur.execute(
            "SELECT wallet_address, discovery_source, all_time_pnl, win_rate, history_sample_size, last_seen_ts_utc "
            "FROM tracked_wallets ORDER BY all_time_pnl DESC LIMIT 10"
        )
        top_wallets = [
            {
                "address": r[0][:20] + "...",
                "source": r[1],
                "pnl": round(r[2], 2) if r[2] is not None else None,
                "win_rate": round(r[3], 3) if r[3] is not None else None,
                "sample_size": r[4],
                "last_seen": utc_age(r[5]),
            }
            for r in cur.fetchall()
        ]
        report["data_collection"]["top_wallets_by_pnl"] = top_wallets
    except Exception:
        pass

    # ── Discovered Entities ──────────────────────────────────────────────────
    try:
        cur.execute("PRAGMA table_info(discovered_entities)")
        cols = [r[1] for r in cur.fetchall()]
        if "wallet_address" in cols:
            order_col = "wallet_address"
        elif "address" in cols:
            order_col = "address"
        else:
            order_col = cols[0] if cols else "ROWID"

        cur.execute(f"SELECT * FROM discovered_entities ORDER BY trust_score DESC LIMIT 10")
        rows = cur.fetchall()
        report["entity_insights"]["top_by_trust_score"] = [
            dict(zip([c.lower() for c in cols], r)) for r in rows
        ]
    except Exception:
        pass

    # ── API Health ───────────────────────────────────────────────────────────
    try:
        cur.execute("SELECT COUNT(*) FROM _health_probe")
        report["api_health"]["health_probes"] = cur.fetchone()[0]
    except Exception:
        pass

    try:
        cur.execute("SELECT COUNT(*) FROM polymarket_link_unresolved")
        report["api_health"]["unresolved_links"] = cur.fetchone()[0]
    except Exception:
        pass

    return report


def print_report(report: dict) -> None:
    """Pretty-print the insight report."""
    print()
    print("=" * 70)
    print("  PANOPTICON SHADOW MODE - BIANNUAL INSIGHT REPORT")
    print(f"  Generated: {report['generated_utc']}")
    print("=" * 70)
    print()

    dc = report.get("data_collection", {})
    print("  [DATA COLLECTION]")
    print(f"    Tracked wallets     : {dc.get('total_tracked_wallets', 'N/A'):>6}")
    print(f"    Discovered entities : {dc.get('total_discovered_entities', 'N/A'):>6}")
    print(f"    Shadow hits        : {dc.get('total_shadow_hits', 'N/A'):>6}")
    print(f"    Raw events         : {dc.get('total_raw_events', 'N/A'):>6}")
    print(f"    Paper trades       : {dc.get('total_paper_trades', 'N/A'):>6}")
    print()

    bw = report.get("wallet_breakdown", {})
    print("  [WALLET BREAKDOWN]")
    by_source = bw.get("by_source", {})
    for src, cnt in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"    {src:<30}: {cnt:>5}")
    print(f"    Track A ratio (Taker/Wallet) : {bw.get('track_a_ratio', 0):.1%}")
    print()

    sh = report.get("shadow_hits", {})
    print("  [SHADOW HITS]")
    print(f"    Total hits : {sh.get('total', 0)}")
    stats = sh.get("entropy_z_stats", {})
    if stats:
        print(f"    Entropy Z - min: {stats.get('min', 'N/A'):>10}  max: {stats.get('max', 'N/A'):>10}  avg: {stats.get('avg', 'N/A'):>10}")
    print()
    if sh.get("recent"):
        print("    Recent hits:")
        for h in sh["recent"]:
            print(f"      {h['address']}  z={h['entropy_z']}  pnl={h['sim_pnl_proxy']}  {h['age']}")
    print()

    tw = dc.get("top_wallets_by_pnl", [])
    if tw:
        print("  [TOP WALLETS BY PNL]")
        for w in tw[:5]:
            print(f"    {w['address']}  src={w['source']}  pnl=${w['pnl']}  wr={w['win_rate']}  n={w['sample_size']}  {w['last_seen']}")
        print()

    ah = report.get("api_health", {})
    print("  [API HEALTH]")
    print(f"    Unresolved links : {ah.get('unresolved_links', 'N/A')}")
    print()

    print("=" * 70)
    print("  SYSTEM STATUS: Shadow Mode Active | Collecting | No Live Trading")
    print("=" * 70)
    print()


def main() -> int:
    load_repo_env_if_needed()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    ap = argparse.ArgumentParser(description="Generate Panopticon insight report")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    ap.add_argument("--output", "-o", help="Write to file instead of stdout")
    args = ap.parse_args()

    db_path = get_db_path()
    if not db_path.exists():
        print(f"[ERROR] DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        report = generate_insight_report(conn)
    finally:
        conn.close()

    output = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}")
    elif args.json:
        print(output)
    else:
        print_report(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
