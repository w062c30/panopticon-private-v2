"""D72 Phase 0 Detailed Report — produce data for reports/d72_signal_pipeline_report.md"""
import json
import sqlite3
from datetime import datetime, timezone

DB = "data/panopticon.db"
now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
print(f"\n=== D72 Detailed Diagnostic === {now_utc} UTC\n")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# ── 1. Top wallets by USD volume (from wallet_observations, T1 only) ──────────
print("=" * 60)
print("TOP 50 WALLETS BY USD VOLUME (wallet_observations, T1 only)")
print("=" * 60)
cur.execute("""
    SELECT
        wo.address,
        SUM(json_extract(wo.payload_json, '$.size') * json_extract(wo.payload_json, '$.price')) AS usd_volume,
        COUNT(*) AS trade_count,
        json_extract(wo.payload_json, '$.side') AS side,
        json_extract(wo.payload_json, '$.price') AS price
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1'
      AND wo.obs_type = 'clob_trade'
      AND json_extract(wo.payload_json, '$.size') IS NOT NULL
      AND json_extract(wo.payload_json, '$.price') IS NOT NULL
    GROUP BY wo.address
    ORDER BY usd_volume DESC
    LIMIT 50
""")
rows = cur.fetchall()
print(f"\n{'Rank':<5} {'Wallet':<20} {'USD':>12} {'Trades':>6} {'Side':<6} {'Price':>6}")
print("-" * 60)
for i, (addr, usd, cnt, side, price) in enumerate(rows, 1):
    wallet_short = f"{addr[:16]}..." if addr else "(unknown)"
    print(f"{i:<5} {wallet_short:<20} ${usd:>10.4f} {cnt:>6} {str(side):<6} {float(price or 0):>6.4f}" if price else f"{i:<5} {wallet_short:<20} ${usd:>10.4f} {cnt:>6}")

# ── 2. Side / Outcome distribution ───────────────────────────────────────────
print("\n" + "=" * 60)
print("SIDE DISTRIBUTION (wallet_observations T1)")
print("=" * 60)
cur.execute("""
    SELECT json_extract(payload_json, '$.side') AS side, COUNT(*) AS cnt
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
    GROUP BY side
    ORDER BY cnt DESC
""")
for row in cur.fetchall():
    print(f"  {row[0] or '(null)'}: {row[1]}")

print("\n" + "=" * 60)
print("OUTCOME DISTRIBUTION (wallet_observations T1)")
print("=" * 60)
cur.execute("""
    SELECT json_extract(payload_json, '$.outcome') AS outcome, COUNT(*) AS cnt
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
    GROUP BY outcome
    ORDER BY cnt DESC
""")
for row in cur.fetchall():
    print(f"  {row[0] or '(null)'}: {row[1]}")

# ── 3. Price distribution ──────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("PRICE DISTRIBUTION (wallet_observations T1)")
print("=" * 60)
cur.execute("""
    SELECT
        CASE
            WHEN CAST(json_extract(payload_json, '$.price') AS REAL) < 0.20 THEN '< 0.20'
            WHEN CAST(json_extract(payload_json, '$.price') AS REAL) BETWEEN 0.20 AND 0.40 THEN '0.20–0.40'
            WHEN CAST(json_extract(payload_json, '$.price') AS REAL) BETWEEN 0.40 AND 0.60 THEN '0.40–0.60'
            WHEN CAST(json_extract(payload_json, '$.price') AS REAL) BETWEEN 0.60 AND 0.80 THEN '0.60–0.80'
            ELSE '>= 0.80'
        END AS price_bucket,
        COUNT(*) AS cnt
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
    GROUP BY price_bucket
    ORDER BY price_bucket
""")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

# ── 4. USD size distribution ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("USD SIZE DISTRIBUTION (wallet_observations T1)")
print("=" * 60)
cur.execute("""
    SELECT
        CASE
            WHEN CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL) < 10 THEN '< $10'
            WHEN CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL) BETWEEN 10 AND 50 THEN '$10–$50'
            WHEN CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL) BETWEEN 50 AND 100 THEN '$50–$100'
            WHEN CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL) BETWEEN 100 AND 500 THEN '$100–$500'
            ELSE '>= $500'
        END AS usd_bucket,
        COUNT(*) AS cnt
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
    GROUP BY usd_bucket
    ORDER BY usd_bucket
""")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

# ── 5. T1 token breakdown ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("PER-TOKEN BREAKDOWN (wallet_observations T1)")
print("=" * 60)
cur.execute("""
    SELECT
        pm.token_id,
        pm.slug,
        COUNT(*) AS obs_count,
        COUNT(DISTINCT wo.address) AS unique_wallets,
        SUM(CAST(json_extract(wo.payload_json, '$.size') AS REAL) * CAST(json_extract(wo.payload_json, '$.price') AS REAL)) AS total_usd
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
    GROUP BY pm.token_id, pm.slug
    ORDER BY obs_count DESC
    LIMIT 20
""")
print(f"\n{'Token ID':<20} {'Slug':<25} {'Obs':>6} {'Wallets':>7} {'USD':>10}")
print("-" * 70)
for row in cur.fetchall():
    token_short = f"{row[0][:16]}..." if row[0] else "(unknown)"
    slug_short = row[1][:22] if row[1] else "(unknown)"
    print(f"{token_short:<20} {slug_short:<25} {row[2]:>6} {row[3]:>7} ${row[4] or 0:>9.2f}")

# ── 6. Summary metrics ──────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("SUMMARY METRICS")
print("=" * 60)
cur.execute("""
    SELECT
        COUNT(*) AS total_obs,
        COUNT(DISTINCT address) AS unique_wallets,
        SUM(CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL)) AS total_usd,
        AVG(CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL)) AS avg_usd,
        MIN(CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL)) AS min_usd,
        MAX(CAST(json_extract(payload_json, '$.size') AS REAL) * CAST(json_extract(payload_json, '$.price') AS REAL)) AS max_usd,
        AVG(CAST(json_extract(payload_json, '$.price') AS REAL)) AS avg_price,
        MIN(CAST(json_extract(payload_json, '$.price') AS REAL)) AS min_price,
        MAX(CAST(json_extract(payload_json, '$.price') AS REAL)) AS max_price
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
""")
r = cur.fetchone()
print(f"\n  Total T1 observations : {r[0]}")
print(f"  Unique wallets        : {r[1]}")
print(f"  Total USD volume      : ${r[2] or 0:.2f}" if r[2] else "  Total USD volume      : $0.00")
print(f"  Avg USD per trade     : ${r[3] or 0:.4f}" if r[3] else "  Avg USD per trade     : $0.0000")
print(f"  Min USD per trade     : ${r[4] or 0:.4f}" if r[4] else "  Min USD per trade     : $0.0000")
print(f"  Max USD per trade     : ${r[5] or 0:.4f}" if r[5] else "  Max USD per trade     : $0.0000")
print(f"  Avg price             : {r[6] or 0:.4f}" if r[6] else "  Avg price             : 0.0000")
print(f"  Price min             : {r[7] or 0:.4f}" if r[7] else "  Price min             : 0.0000")
print(f"  Price max             : {r[8] or 0:.4f}" if r[8] else "  Price max             : 0.0000")

# ── 7. Whale scanner injection check ───────────────────────────────────────────
print("\n" + "=" * 60)
print("WHALE SCANNER INJECTION STATUS")
print("=" * 60)
cur.execute("""
    SELECT COUNT(DISTINCT address)
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1'
      AND wo.obs_type = 'clob_trade'
      AND json_extract(wo.payload_json, '$.source') = 'whale_scanner'
""")
whale_injected = cur.fetchone()[0] or 0
print(f"\n  T1 wallets from whale_scanner : {whale_injected}")

cur.execute("""
    SELECT COUNT(DISTINCT address)
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1'
      AND wo.obs_type = 'clob_trade'
""")
t1_wallets = cur.fetchone()[0] or 0
print(f"  Total T1 wallets           : {t1_wallets}")
print(f"  Whale injection coverage  : {whale_injected/t1_wallets*100:.1f}%" if t1_wallets > 0 else "  Whale injection coverage  : N/A")

# ── 8. execution_records gate reason ──────────────────────────────────────────
print("\n" + "=" * 60)
print("EXECUTION_RECORDS GATE REASON (last 30 min)")
print("=" * 60)
cur.execute("""
    SELECT gate_reason, COUNT(*) AS cnt
    FROM execution_records
    WHERE created_ts_utc > datetime('now', '-30 minutes')
    GROUP BY gate_reason
    ORDER BY cnt DESC
    LIMIT 10
""")
for row in cur.fetchall():
    pct = (row[1] / 43 * 100) if row[1] else 0
    print(f"  {row[0] or '(null)':<40}: {row[1]:>4}  ({pct:.1f}%)")

# ── 9. discovered_entities T1 ─────────────────────────────────────────────────
print("\n" + "=" * 60)
print("DISCOVERED ENTITIES — insider_score distribution")
print("=" * 60)
cur.execute("""
    SELECT
        CASE
            WHEN insider_score >= 0.80 THEN '0.80+ (high confidence)'
            WHEN insider_score >= 0.65 THEN '0.65–0.80 (elevated)'
            WHEN insider_score >= 0.55 THEN '0.55–0.65 (threshold)'
            WHEN insider_score >= 0.40 THEN '0.40–0.55 (moderate)'
            ELSE '< 0.40 (low)'
        END AS score_bucket,
        COUNT(*) AS cnt
    FROM discovered_entities
    WHERE insider_score IS NOT NULL AND insider_score > 0
    GROUP BY score_bucket
    ORDER BY score_bucket DESC
""")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

# ── 10. insider_score_snapshots recent ─────────────────────────────────────────
print("\n" + "=" * 60)
print("INSIDER_SCORE_SNAPSHOTS — all time and recent")
print("=" * 60)
cur.execute("SELECT COUNT(*) FROM insider_score_snapshots")
total_iss = cur.fetchone()[0] or 0
print(f"\n  Total insider_score_snapshots : {total_iss}")

cur.execute("""
    SELECT COUNT(*) FROM insider_score_snapshots
    WHERE ingest_ts_utc > datetime('now', '-60 minutes')
""")
recent_iss = cur.fetchone()[0] or 0
print(f"  Last 60 min                  : {recent_iss}")

cur.execute("""
    SELECT COUNT(*) FROM insider_score_snapshots
    WHERE ingest_ts_utc > datetime('now', '-5 minutes')
""")
last5_iss = cur.fetchone()[0] or 0
print(f"  Last 5 min                   : {last5_iss}")

conn.close()
print("\n=== Diagnostic Complete ===")
