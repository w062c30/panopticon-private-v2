"""
BTC 5m Live Monitor v2 — D67 post-feedback update.

Key fix: windows are computed at RUNTIME using current UTC time, not
pre-computed once at startup. This ensures:
  1. The monitor always uses the correct current window, not a stale one
  2. ±1 window buffer is naturally included
  3. No reliance on saved slugs from a previous run

Slug formula (confirmed via Polymarket):
  window_start_utc = floor((now_utc + ET_OFFSET) / 300) * 300 - ET_OFFSET
  slug = f"btc-updown-5m-{window_start_utc}"
  where ET_OFFSET = 4 * 3600 (EDT = UTC-4 in April)

Verification:
  1777350600 -> 2026-04-28 04:30 UTC = 2026-04-28 00:30 EDT
  1777350900 -> 2026-04-28 04:35 UTC = 2026-04-28 00:35 EDT
  Both confirmed via polymarket.com/event/btc-updown-5m-*
"""
from __future__ import annotations

import json
import time
import datetime
import pathlib
import sys
import requests

CLOB   = "https://clob.polymarket.com"
GAMMA  = "https://gamma-api.polymarket.com"
OUTDIR = pathlib.Path("run/monitor_results_v2")
OUTDIR.mkdir(exist_ok=True)

ET_OFFSET = 4 * 3600  # EDT = UTC-4 in April


def current_window_start_utc() -> int:
    """Return the UTC Unix timestamp of the CURRENT 5min window start.
    Always computed at call time — never cached."""
    now_utc = int(time.time())
    # floor in ET, convert back to UTC
    return (now_utc + ET_OFFSET) // 300 * 300 - ET_OFFSET


def make_slug(window_start_utc: int) -> str:
    return f"btc-updown-5m-{window_start_utc}"


def now_et() -> str:
    """Return current ET time as HH:MM AM/PM."""
    now_utc = datetime.datetime.utcfromtimestamp(int(time.time()))
    now_et = now_utc - datetime.timedelta(hours=4)
    return now_et.strftime("%I:%M %p ET")


def utc_from_window(window_start_utc: int) -> str:
    return datetime.datetime.utcfromtimestamp(window_start_utc).strftime("%H:%M:%S UTC")


def resolve_token_for_slug(slug: str) -> dict:
    """Resolve YES/NO token_ids for a given slug via Gamma API."""
    for attempt in range(5):
        try:
            r = requests.get(
                f"{GAMMA}/markets",
                params={"slug": slug},
                timeout=5,
            )
            markets = r.json()
            if markets:
                m = markets[0] if isinstance(markets, list) else markets
                ids = m.get("clobTokenIds", [])
                if isinstance(ids, str):
                    ids = json.loads(ids)
                return {
                    "token_ids": ids or [],
                    "condition_id": m.get("conditionId", ""),
                }
        except Exception:
            pass
        if attempt < 4:
            time.sleep(3)
    return {"token_ids": [], "condition_id": ""}


def take_sample(token_id: str, n: int) -> dict:
    """Collect one sample: order book + trades + D64a fetch_best_ask."""
    ts = int(time.time())
    row = {
        "sample": n,
        "ts": ts,
        "utc": datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S"),
    }

    # 1. Order book
    try:
        b = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=5).json()
        bids = b.get("bids", [])
        asks = b.get("asks", [])
        row.update({
            "book_ok": True,
            "bid_depth": len(bids),
            "ask_depth": len(asks),
            "best_bid": float(bids[0]["price"]) if bids else None,
            "best_ask_raw": float(asks[0]["price"]) if asks else None,
            "mid": round((float(bids[0]["price"]) + float(asks[0]["price"])) / 2, 4)
                    if bids and asks else None,
        })
        print(f"  book bid={row['best_bid']} ask={row['best_ask_raw']} mid={row['mid']} ({len(bids)}b/{len(asks)}a)")
    except Exception as e:
        row.update({"book_ok": False, "book_err": str(e)})
        print(f"  book err: {e}")

    # 2. Recent trades
    try:
        t = requests.get(f"{CLOB}/trades",
                        params={"token_id": token_id, "limit": 200},
                        timeout=5).json()
        trades = t if isinstance(t, list) else t.get("data", [])
        span = 0.0
        if len(trades) >= 2:
            def get_ts(tr):
                for k in ("timestamp", "matchTime", "createdAt", "time"):
                    if tr.get(k):
                        return float(tr[k])
                return 0.0
            newest = get_ts(trades[0])
            oldest = get_ts(trades[-1])
            span = newest - oldest if oldest > 0 else 0.0
        row.update({
            "trades_ok": True,
            "trade_count": len(trades),
            "trade_span_secs": round(span, 1),
            "trades_per_sec": round(len(trades) / span, 2) if span > 0 else None,
            "latest_price": float(trades[0].get("price", 0)) if trades else None,
        })
        tps_str = f"{row['trades_per_sec']:.2f}/s" if row["trades_per_sec"] else "?"
        print(f"  trades {len(trades)} in {span:.0f}s -> {tps_str} latest={row['latest_price']}")
    except Exception as e:
        row.update({"trades_ok": False, "trade_err": str(e)})
        print(f"  trades err: {e}")

    # 3. D64a fetch_best_ask (with AMM guard)
    try:
        sys.path.insert(0, ".")
        from panopticon_py.ingestion.clob_client import fetch_best_ask
        ask = fetch_best_ask(token_id, timeout_sec=3.0)
        row.update({"d64a_ask": ask, "d64a_ok": ask is not None})
        status = f"ask={ask}" if ask else "AMM blocked (None)"
        print(f"  D64a fetch_best_ask = {status}")
    except Exception as e:
        row.update({"d64a_ok": False, "d64a_err": str(e)})
        print(f"  D64a err: {e}")

    return row


def run_window(window_start_utc: int, window_num: int) -> dict:
    """Monitor one 5-min window from now until close."""
    slug = make_slug(window_start_utc)
    end_ts = window_start_utc + 300
    now = int(time.time())

    print(f"\n{'=' * 65}")
    print(f"WINDOW {window_num}: {slug}")
    print(f"  ET window: {utc_from_window(window_start_utc)} - {utc_from_window(end_ts)}")
    print(f"  ET now:    {now_et()}  (computed at runtime)")
    print(f"  remaining: {max(end_ts - now, 0)}s")
    print(f"{'=' * 65}")

    # Wait for window to open if we're early
    if now < window_start_utc:
        wait = window_start_utc - now + 1
        print(f"  Waiting {wait}s for window open...")
        time.sleep(wait)

    # Resolve token
    token_info = resolve_token_for_slug(slug)
    ids = token_info.get("token_ids", [])
    if not ids:
        print(f"  ERROR: No token_id for {slug} — skipping")
        return {"slug": slug, "window_start_ts": window_start_utc,
                "window_end_ts": end_ts, "error": "no_token_id"}

    yes_token = ids[0]
    no_token = ids[1] if len(ids) > 1 else None
    print(f"  YES token: {yes_token[:24]}...")
    if no_token:
        print(f"  NO  token: {no_token[:24]}...")

    samples = []
    n = 0
    while True:
        now = int(time.time())
        if now >= end_ts:
            print("\n  Window closed.")
            break
        n += 1
        remaining = max(end_ts - now, 0)
        print(f"\n  [Sample {n}] {datetime.datetime.utcnow().strftime('%H:%M:%S')} UTC  ({remaining}s left)")
        samples.append(take_sample(yes_token, n))
        sleep = min(30, end_ts - int(time.time()) - 2)
        if sleep > 1:
            time.sleep(sleep)

    # Post-window: settlement price
    print(f"\n  --- POST-WINDOW: fetch_settlement_price ---")
    time.sleep(8)
    settlement = None
    settlement_ok = False
    try:
        from panopticon_py.market_data.clob_series import fetch_settlement_price
        settlement = fetch_settlement_price(yes_token, timeout_sec=10.0)
        settlement_ok = settlement is not None
        print(f"  D64b settlement = {settlement}  {'OK' if settlement_ok else 'None (AMM — expected)'}")
    except Exception as e:
        print(f"  D64b err: {e}")

    result = {
        "slug": slug,
        "token_id_yes": yes_token,
        "window_start_ts": window_start_utc,
        "window_end_ts": end_ts,
        "sample_count": n,
        "samples": samples,
        "settlement_price": settlement,
        "settlement_ok": settlement_ok,
    }
    (OUTDIR / f"{slug}.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    print("BTC 5m Live Monitor v2 — real-time window computation")
    print(f"ET offset: +{ET_OFFSET//3600}h (EDT = UTC-{ET_OFFSET//3600})")
    print(f"Slug formula: btc-updown-5m-{{window_start_utc}}")
    print(f"Window compute: floor((now_utc + {ET_OFFSET}) / 300) * 300 - {ET_OFFSET}")
    print()

    # Always compute current window at THIS moment
    current_start = current_window_start_utc()
    print(f"Current window: {make_slug(current_start)}")
    print(f"  = {utc_from_window(current_start)} UTC")
    print(f"  = {datetime.datetime.utcfromtimestamp(current_start + ET_OFFSET).strftime('%I:%M %p ET')} ET")
    print()

    # Monitor current window + next 2 windows (±1 buffer)
    all_results = []
    for i in range(3):
        window_start = current_start + i * 300
        result = run_window(window_start, i + 1)
        all_results.append(result)

    (OUTDIR / "all_windows.json").write_text(json.dumps(all_results, indent=2))
    print("\n\nALL WINDOWS COMPLETE")
    print(f"Results saved to: {OUTDIR}/")


if __name__ == "__main__":
    main()
