"""
D68 Phase 0: 10-minute live monitor on BTC 5m current window.
Uses the new three-layer architecture (D68a).

Key principle: slug calculation is done RIGHT BEFORE each monitoring cycle,
not frozen at startup. The loop continuously realigns to current time.
"""

import os
import time
import json
import datetime
import pathlib
import sqlite3
import logging
import sys
import threading
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)

proj = r"d:\Antigravity\Panopticon"
sys.path.insert(0, proj)
os.chdir(proj)

from panopticon_py.ingestion.polymarket_streams import (
    ClobWebSocket, RtdsWebSocket, MarketTradePoller,
    PolyTrade, ClobTrade, CryptoPriceUpdate
)
from panopticon_py.ingestion.insider_detector import (
    InsiderDetector, InsiderAlert
)

GAMMA    = "https://gamma-api.polymarket.com"
OUTDIR   = pathlib.Path("run/monitor_results")
OUTDIR.mkdir(exist_ok=True)
DURATION = 600   # 10 minutes
ET_OFFSET = 4 * 3600  # EDT = UTC-4


# ── Slug helpers (called fresh each cycle) ────────────────────────────────

def current_window_start() -> int:
    """Window start in UTC seconds, aligned to 5-min ET boundary."""
    return (int(time.time()) + ET_OFFSET) // 300 * 300 - ET_OFFSET


def make_slug(ws: int) -> str:
    return f"btc-updown-5m-{ws}"


def window_end(ws: int) -> int:
    return ws + 300


def et_str(ts: int) -> str:
    utc = datetime.datetime.utcfromtimestamp(ts)
    et = utc - datetime.timedelta(hours=4)
    return et.strftime("%I:%M:%S %p ET")


def now_et_str() -> str:
    now = int(time.time())
    utc = datetime.datetime.utcfromtimestamp(now)
    et = utc - datetime.timedelta(hours=4)
    return f"{et.strftime('%I:%M:%S %p ET')} (UTC {utc.strftime('%H:%M:%S')})"


# ── Shared results store ───────────────────────────────────────────────────
lock          = threading.Lock()
clob_trades:  list[ClobTrade]         = []
poly_trades:  list[PolyTrade]         = []
btc_prices:   list[CryptoPriceUpdate] = []
alerts:       list[InsiderAlert]      = []

# ── Callbacks ────────────────────────────────────────────────────────────────

def on_clob_trade(t: ClobTrade):
    with lock: clob_trades.append(t)

def on_poly_trade(t: PolyTrade):
    with lock: poly_trades.append(t)
    try:
        conn = sqlite3.connect(r"d:\Antigravity\Panopticon\data\panopticon.db")
        conn.execute("""
            INSERT OR IGNORE INTO wallet_activity
                (proxy_wallet, name, pseudonym, side, outcome, price,
                 size, usdc_size, timestamp, transaction_hash,
                 condition_id, event_slug, asset, title, bio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            t.proxy_wallet, t.name, t.pseudonym, t.side, t.outcome,
            t.price, t.size, t.usdc_size, t.timestamp,
            t.transaction_hash, t.condition_id, t.event_slug,
            t.asset, t.title, t.bio,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

def on_btc_price(p: CryptoPriceUpdate):
    with lock: btc_prices.append(p)

def on_alert(a: InsiderAlert):
    with lock: alerts.append(a)
    try:
        layer = 1 if "L1" in a.trigger else 2 if "L2" in a.trigger else 3
        conn = sqlite3.connect(r"d:\Antigravity\Panopticon\data\panopticon.db")
        conn.execute(f"""
            UPDATE wallet_activity
            SET insider_l{layer}=1, alert_trigger=?
            WHERE transaction_hash=?
        """, (a.trigger, a.tx_hash))
        conn.commit()
        conn.close()
    except Exception:
        pass


def resolve_condition(slugs: list[str]) -> tuple[str, str]:
    """Resolve token_id + conditionId for first available slug. Returns ("", "") if none."""
    for slug in slugs:
        try:
            r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            mks = r.json() if r.ok else []
            if not mks:
                continue
            m = mks[0] if isinstance(mks, list) else mks
            condition_id = m.get("conditionId", "") or ""
            ids = m.get("clobTokenIds") or []
            if isinstance(ids, str):
                ids = json.loads(ids)
            if ids and condition_id:
                return ids[0], condition_id
        except Exception:
            continue
    return "", ""


# ── Persistent WebSocket + Poller state ────────────────────────────────────
clob_ws  = None
rtds_ws  = None
detector = None
cur_token_id = ""
cur_condition_id = ""


def start_layers(token_id: str, condition_id: str):
    """Start all three layers. Called each time we get fresh token/condition."""
    global clob_ws, rtds_ws, detector, cur_token_id, cur_condition_id
    cur_token_id = token_id
    cur_condition_id = condition_id

    # Layer 1: CLOB WS
    clob_ws = ClobWebSocket(on_trade=on_clob_trade)
    clob_ws.subscribe(token_ids=[token_id])
    clob_ws.start()
    print(f"CLOB WS started for {token_id[:20]}...")

    # Layer 2: RTDS WS
    rtds_ws = RtdsWebSocket(on_crypto_price=on_btc_price, symbols=["btcusdt"])
    rtds_ws.start()
    print("RTDS WS started (BTC price)")

    # Layer 3: InsiderDetector
    detector = InsiderDetector(
        condition_id    = condition_id,
        on_alert        = on_alert,
        on_trade        = on_poly_trade,
        large_trade_usd = 200.0,
    )
    detector.start()
    print(f"InsiderDetector started for {condition_id[:20]}...")


def stop_layers():
    global clob_ws, rtds_ws, detector
    if clob_ws:
        clob_ws.stop()
        clob_ws = None
    if rtds_ws:
        rtds_ws.stop()
        rtds_ws = None
    if detector:
        detector.stop()
        detector = None


# ── Main monitoring loop ────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"D68 Phase 0 Monitor — 10 minutes, dynamic slug realignment")
print(f"{'='*65}\n")

start_time = time.time()
deadline   = start_time + DURATION

# Bootstrap: calculate slug RIGHT NOW before loop
ws_cur  = current_window_start()
ws_nxt  = ws_cur + 300
slug_cur = make_slug(ws_cur)
slug_nxt = make_slug(ws_nxt)
now_str  = now_et_str()

print(f"[BOOTSTRAP] at {now_str}")
print(f"  Current window: {slug_cur} ({et_str(ws_cur)} - {et_str(window_end(ws_cur))})")
print(f"  Next window:    {slug_nxt} ({et_str(ws_nxt)} - {et_str(window_end(ws_nxt))})")
print()

# Resolve condition — do this RIGHT BEFORE starting
token_id, condition_id = resolve_condition([slug_cur, slug_nxt])
if not condition_id:
    print("WARNING: Could not resolve conditionId — using global feed fallback")
    condition_id = ""
    token_id = ""
else:
    print(f"Resolved: token_id={token_id[:20]}... condition_id={condition_id[:20]}...")

start_layers(token_id, condition_id)

last_slug_recheck = 0
print(f"\nMonitoring for {DURATION}s ({DURATION//60} min)...\n")

try:
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        now = int(time.time())

        # ── Dynamic slug realignment every 60s ─────────────────────────
        if now - last_slug_recheck >= 60:
            last_slug_recheck = now
            ws_now  = current_window_start()
            ws_now_nxt = ws_now + 300
            slug_now  = make_slug(ws_now)
            slug_now_nxt = make_slug(ws_now_nxt)

            # Check if current window has changed (we've rolled into a new window)
            if slug_now != slug_cur:
                print(f"\n[REFRESH] Window changed: {slug_cur} -> {slug_now}")
                print(f"  Old window: {slug_cur}")
                print(f"  New window: {slug_now}")
                print(f"  Next:       {slug_now_nxt}")

                # Stop old layers
                stop_layers()

                # Re-resolve for new window
                token_id, condition_id = resolve_condition([slug_now, slug_now_nxt])
                if condition_id:
                    start_layers(token_id, condition_id)
                    ws_cur = ws_now
                    ws_nxt = ws_now_nxt
                    slug_cur = slug_now
                    slug_nxt = slug_now_nxt
                else:
                    print("WARNING: Could not resolve new window — continuing with old")

        # ── Status report ────────────────────────────────────────────────
        with lock:
            nc  = len(clob_trades)
            np_ = len(poly_trades)
            nb  = len(btc_prices)
            na  = len(alerts)
        print(
            f"  [{remaining:3d}s] CLOB={nc} trades={np_} BTC_px={nb} alerts={na} "
            f"| current={slug_cur}",
            end="\r"
        )
        time.sleep(5)

except KeyboardInterrupt:
    print("\nInterrupted")
finally:
    stop_layers()

# ── Summary ────────────────────────────────────────────────────────────────
print(f"\n\n{'='*65}")
print("PHASE 0 RESULTS")
print(f"{'='*65}")

print(f"\nLayer 1 — CLOB WebSocket:")
print(f"  Trades: {len(clob_trades)}")
if not clob_trades:
    print("  -> 0 trades: AMM confirmed (bid/ask spread ~0.98)")

print(f"\nLayer 2 — RTDS BTC Price:")
if btc_prices:
    vals = [p.value for p in btc_prices]
    print(f"  Updates: {len(btc_prices)}")
    print(f"  BTC: ${min(vals):,.0f} - ${max(vals):,.0f}")
else:
    print("  No updates")

print(f"\nLayer 3 — data-api Trades (with identity):")
print(f"  Trades captured: {len(poly_trades)}")
if poly_trades:
    wallets  = set(t.proxy_wallet for t in poly_trades)
    total_v  = sum(t.usdc_size for t in poly_trades)
    tx_cover = sum(1 for t in poly_trades if t.transaction_hash)
    print(f"  Unique wallets:  {len(wallets)}")
    print(f"  Total volume:    ${total_v:.2f}")
    print(f"  tx_hash coverage:{tx_cover}/{len(poly_trades)}")
    sample = sorted(poly_trades, key=lambda x: -x.usdc_size)[:3]
    print(f"  Top 3 by size:")
    for t in sample:
        print(f"    ${t.usdc_size:.0f}  {t.name or t.pseudonym or 'anon'} "
              f"({t.proxy_wallet[:12]}...) "
              f"{t.side} {t.outcome} @ {t.price:.2f}")
else:
    print("  No trades captured")

print(f"\nInsider Alerts: {len(alerts)}")
for a in alerts:
    print(f"  [{a.trigger}] {a.name} ({a.proxy_wallet[:12]}...) "
          f"${a.usd_size:.0f} {a.outcome}")

# Save JSON
output = {
    "slug":               slug_cur,
    "condition_id":       condition_id,
    "duration_s":         DURATION,
    "actual_duration_s":   int(time.time() - start_time),
    "clob_trade_count":    len(clob_trades),
    "poly_trade_count":    len(poly_trades),
    "btc_price_count":     len(btc_prices),
    "alert_count":         len(alerts),
    "unique_wallets":      len(set(t.proxy_wallet for t in poly_trades)),
    "total_volume_usd":    sum(t.usdc_size for t in poly_trades),
    "amm_confirmed":       len(clob_trades) == 0,
    "alerts": [
        {"trigger": a.trigger, "wallet": a.proxy_wallet,
         "name": a.name, "usd": a.usd_size, "outcome": a.outcome}
        for a in alerts
    ],
    "sample_trades": [
        {"wallet": t.proxy_wallet, "name": t.name,
         "side": t.side, "outcome": t.outcome,
         "price": t.price, "usd": t.usdc_size, "tx": t.transaction_hash}
        for t in sorted(poly_trades, key=lambda x: -x.usdc_size)[:10]
    ],
}
ts  = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
out = OUTDIR / f"d68_phase0_{ts}.json"
out.write_text(json.dumps(output, indent=2))
print(f"\nSaved -> {out}")

conn = sqlite3.connect(r"d:\Antigravity\Panopticon\data\panopticon.db")
db_count = conn.execute("SELECT COUNT(*) FROM wallet_activity").fetchone()[0]
conn.close()
print(f"wallet_activity DB rows: {db_count}")
