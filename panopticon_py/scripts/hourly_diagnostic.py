"""
panopticon_py/scripts/hourly_diagnostic.py
Hourly diagnostic report — run via: python -m panopticon_py.scripts.hourly_diagnostic
"""
import sqlite3, sys, os
from datetime import datetime, timezone

DB_PATH = os.getenv("PANOPTICON_DB", "data/panopticon.db")

def qry(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()

def main():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")

    now_utc = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*60}")
    print(f"Hourly Diagnostic — {now_utc}")
    print(f"{'='*60}")

    # Kyle lambda samples
    rows = qry(conn, "SELECT COUNT(*) FROM kyle_lambda_samples")
    kyle_total = rows[0][0]
    print(f"\n[kyle_lambda_samples] total={kyle_total}")
    if kyle_total > 0:
        rows = qry(conn, "SELECT asset_id, COUNT(*) as n FROM kyle_lambda_samples GROUP BY asset_id ORDER BY n DESC LIMIT 5")
        for r in rows:
            print(f"  asset_id={r[0][:20]}... n={r[1]}")
        p75 = qry(conn, "SELECT PERCENTILE(75) WITHIN GROUP (lambda_obs) FROM kyle_lambda_samples")
        print(f"  P75 lambda={p75}")
    else:
        print("  [EMPTY] no lambda samples yet")

    # execution_records (last 24h)
    print(f"\n[execution_records] (last 24h)")
    rows = qry(conn, """
        SELECT reason, accepted, COUNT(*) as n, MIN(created_ts_utc), MAX(created_ts_utc)
        FROM execution_records
        WHERE created_ts_utc > datetime('now', '-24 hours')
        GROUP BY reason, accepted
        ORDER BY n DESC LIMIT 10
    """)
    if not rows:
        print("  [EMPTY] no execution_records in 24h")
    else:
        for r in rows:
            print(f"  {r[2]} rows | {r[0]} | accepted={r[1]} | {r[3][:19]} → {r[4][:19] if r[4] else 'N/A'}")

    # wallet_observations (last 24h)
    rows = qry(conn, """
        SELECT COUNT(*) FROM wallet_observations
        WHERE ingest_ts_utc > datetime('now', '-24 hours')
    """)
    print(f"\n[wallet_observations] last 24h = {rows[0][0]}")

    # pending_entropy_signals
    rows = qry(conn, "SELECT COUNT(*) FROM pending_entropy_signals")
    print(f"[pending_entropy_signals] total = {rows[0][0]}")

    # hunting_shadow_hits (last 24h)
    rows = qry(conn, """
        SELECT COUNT(*) FROM hunting_shadow_hits
        WHERE created_ts_utc > datetime('now', '-24 hours')
    """)
    print(f"[hunting_shadow_hits] last 24h = {rows[0][0]}")

    # insider_score_snapshots
    rows = qry(conn, "SELECT COUNT(*) FROM insider_score_snapshots")
    print(f"[insider_score_snapshots] total = {rows[0][0]}")

    # tracked_wallets
    rows = qry(conn, "SELECT COUNT(*) FROM tracked_wallets")
    print(f"[tracked_wallets] total = {rows[0][0]}")

    # ── Task 6: T1 subscription health check ────────────────────────────
    # Kyle lambda samples in last 5 min — proxy for T1 subscription liveness.
    # T1 markets fire last_trade_price every ~25-30s → expect >= 1 lambda / 5min if subscribed.
    rows = qry(conn, """
        SELECT COUNT(*) FROM kyle_lambda_samples
        WHERE ts_utc > datetime('now', '-5 minutes')
    """)
    kyle_5min = rows[0][0]
    if kyle_5min == 0:
        print("\n[DIAG][T1_SUBSCRIPTION_EMPTY] "
              "No kyle_lambda_samples in last 5 min — "
              "check _refresh_tier1_tokens() and Gamma API filter. "
              "kyle_lambda_samples will remain 0.")
    else:
        print(f"\n[DIAG][T1_SUBSCRIPTION_OK] kyle_lambda_samples(5min)={kyle_5min}")

    print(f"\n{'='*60}")
    print(f"Next run in 1 hour. To run now: python -m panopticon_py.scripts.hourly_diagnostic")
    print(f"{'='*60}\n")
    conn.close()

if __name__ == "__main__":
    main()
