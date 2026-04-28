"""D69 Phase 0 — Official 10-minute market monitor."""
import time, json, datetime, pathlib, sqlite3, threading, sys, requests, logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
sys.path.insert(0, r"d:\Antigravity\Panopticon")
import os
os.chdir(r"d:\Antigravity\Panopticon")

from panopticon_py.ingestion.polymarket_streams import (
    ClobWebSocket, RtdsWebSocket, MarketTradePoller,
    PolyTrade, ClobTrade, CryptoPriceUpdate, fetch_wallet_history
)
from panopticon_py.ingestion.insider_detector import InsiderDetector, InsiderAlert

GAMMA  = "https://gamma-api.polymarket.com"
OUTDIR = pathlib.Path("run/monitor_results")
OUTDIR.mkdir(exist_ok=True)
DURATION = 600  # 10 minutes

# Resolve current window
now_ts   = int(time.time())
ws_cur   = (now_ts // 300) * 300
slugs    = [f"btc-updown-5m-{ws_cur}", f"btc-updown-5m-{ws_cur+300}"]

token_id = ""; condition_id = ""
for slug in slugs:
    try:
        r  = requests.get(f"{GAMMA}/markets",
                          params={"slug": slug}, timeout=5)
        ms = r.json() if r.ok else []
        if ms:
            m   = ms[0] if isinstance(ms, list) else ms
            cid = m.get("conditionId","")
            ids = m.get("clobTokenIds") or []
            if isinstance(ids, str): ids = json.loads(ids)
            if ids and cid:
                token_id = ids[0]; condition_id = cid
                print(f"Resolved: {slug}")
                print(f"  conditionId={cid[:20]}...")
                print(f"  token_id={token_id[:20]}...")
                break
    except Exception as e:
        print(f"Failed: {slug}: {e}")

# Shared state
lock          = threading.Lock()
clob_trades:  list = []
poly_trades:  list = []
btc_prices:   list = []
alerts:       list = []

# ── DB writer ──────────────────────────────────────────────────────────────
def write_trade_to_db(t: PolyTrade):
    try:
        conn = sqlite3.connect('data/panopticon.db')
        conn.execute("""
            INSERT OR IGNORE INTO wallet_activity
                (proxy_wallet, name, pseudonym, side, outcome,
                 price, size, usdc_size,
                 timestamp, transaction_hash,
                 condition_id, event_slug, asset, title, bio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            t.proxy_wallet, t.name, t.pseudonym, t.side, t.outcome,
            t.price, t.size, t.usdc_size,
            t.timestamp, t.transaction_hash,
            t.condition_id, t.event_slug, t.asset, t.title, t.bio,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        pass

def update_alert_flags(a: InsiderAlert):
    layer = 1 if "L1" in a.trigger else 2 if "L2" in a.trigger else 3
    try:
        conn = sqlite3.connect('data/panopticon.db')
        conn.execute(f"""
            UPDATE wallet_activity
            SET insider_l{layer}=1, alert_trigger=?
            WHERE transaction_hash=?
        """, (a.trigger, a.tx_hash))
        conn.commit()
        conn.close()
    except Exception:
        pass

# ── Handlers ───────────────────────────────────────────────────────────────
def on_clob(t: ClobTrade):
    with lock: clob_trades.append(t)

def on_poly(t: PolyTrade):
    with lock: poly_trades.append(t)
    write_trade_to_db(t)

def on_btc(p: CryptoPriceUpdate):
    with lock: btc_prices.append(p)

def on_alert(a: InsiderAlert):
    with lock: alerts.append(a)
    update_alert_flags(a)
    print(f"\n  ALERT [{a.trigger}] {a.name or 'anon'} ({a.proxy_wallet[:12]}...) ${a.usd_size:.0f} {a.outcome}")

# ── Start all layers ───────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"D69 Phase 0 — Official Data Collection")
print(f"Duration: {DURATION}s | Windows: {slugs[0]} + next")
print(f"Token: {token_id[:20] if token_id else 'NONE'} | Condition: {condition_id[:20] if condition_id else 'NONE'}")
print(f"{'='*65}\n")

ws_clob = None
ws_rtds = None
detector = None

if token_id:
    ws_clob = ClobWebSocket(on_trade=on_clob)
    ws_clob.subscribe(token_ids=[token_id])
    ws_clob.start()
    print("CLOB WS started")

ws_rtds = RtdsWebSocket(on_crypto_price=on_btc, symbols=["btcusdt"])
ws_rtds.start()
print("RTDS WS started")

if condition_id:
    detector = InsiderDetector(
        condition_id    = condition_id,
        on_alert        = on_alert,
        large_trade_usd = 200.0,
        rapid_window    = 180,
        rapid_count     = 3,
        high_winrate    = 0.70,
        min_usd         = 10.0,
    )
    orig = detector._poller.on_trade
    def _combined(t):
        on_poly(t); orig(t)
    detector._poller.on_trade = _combined
    detector.start()
    print("InsiderDetector started")

# ── Run ────────────────────────────────────────────────────────────────────
deadline = time.time() + DURATION
try:
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        with lock:
            nc=len(clob_trades); np_=len(poly_trades)
            nb=len(btc_prices);  na=len(alerts)
        print(f"  [{remaining:3d}s] CLOB={nc} data_trades={np_} BTC_px={nb} alerts={na}", end="\r")
        time.sleep(5)
except KeyboardInterrupt:
    print("\nInterrupted")
finally:
    if ws_clob: ws_clob.stop()
    if ws_rtds: ws_rtds.stop()
    if detector: detector.stop()

# ── Summary ────────────────────────────────────────────────────────────────
print(f"\n\n{'='*65}")
print("D69 Phase 0 — Results")
print(f"{'='*65}")

with lock:
    ptt = poly_trades[:]
    alt = alerts[:]
    clt = clob_trades[:]
    btp = btc_prices[:]

print(f"\nCLOB WS: {len(clt)} trades ({'CLOB active' if clt else 'no trades'})")
print(f"data-api: {len(ptt)} trades")

if ptt:
    wallets  = set(t.proxy_wallet for t in ptt)
    total_v  = sum(t.usdc_size for t in ptt)
    tx_cover = sum(1 for t in ptt if t.transaction_hash)
    print(f"Unique wallets: {len(wallets)}")
    print(f"Total USD vol: ${total_v:,.2f}")
    print(f"tx_hash coverage: {tx_cover}/{len(ptt)}")

    top5 = sorted(ptt, key=lambda x: -x.usdc_size)[:5]
    print(f"\nTop 5 trades:")
    for t in top5:
        nm = (t.name or t.pseudonym or 'anon')[:20]
        print(f"  ${t.usdc_size:7.2f}  {nm:<20} ({t.proxy_wallet[:12]}...) {t.side} {t.outcome} @ {t.price:.2f}")

print(f"\nInsider Alerts: {len(alt)}")
for a in alt[:5]:
    print(f"  [{a.trigger}] {a.name or 'anon'} ${a.usd_size:.0f} {a.outcome}")

if btp:
    vals = [p.value for p in btp]
    print(f"\nBTC price: ${min(vals):,.0f}–${max(vals):,.0f} ({len(btp)} ticks)")
else:
    print("\nRTDS BTC: 0 ticks")

# DB count
conn = sqlite3.connect('data/panopticon.db')
db_n = conn.execute('SELECT COUNT(*) FROM wallet_activity').fetchone()[0]
db_w = conn.execute('SELECT COUNT(DISTINCT proxy_wallet) FROM wallet_activity').fetchone()[0]
db_a = conn.execute('SELECT COUNT(*) FROM wallet_activity WHERE insider_l1=1 OR insider_l2=1 OR insider_l3=1').fetchone()[0]
conn.close()

print(f"\nwallet_activity: {db_n} rows | {db_w} wallets | {db_a} flagged")

# Save
ts  = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
out = OUTDIR / f"d69_official_{ts}.json"
out.write_text(json.dumps({
    "collection_start": "D69 official",
    "duration_s": DURATION,
    "condition_id": condition_id,
    "token_id": token_id,
    "clob_trades": len(clt),
    "poly_trades": len(ptt),
    "btc_ticks": len(btp),
    "alerts": len(alt),
    "unique_wallets": len(set(t.proxy_wallet for t in ptt)),
    "total_usd": sum(t.usdc_size for t in ptt),
    "tx_hash_coverage": sum(1 for t in ptt if t.transaction_hash) / max(len(ptt),1),
    "amm_verdict": "CLOB" if clt else "no_clob_trades",
    "wallet_activity_db": db_n,
}, indent=2))
print(f"Saved -> {out}")