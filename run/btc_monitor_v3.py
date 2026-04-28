from __future__ import annotations

import json
import time
import datetime
import pathlib
import sys
import requests

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
OUTDIR = pathlib.Path("run/monitor_results_v3")
OUTDIR.mkdir(exist_ok=True)

ET_OFFSET = 4 * 3600


def current_window_start() -> int:
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


def resolve_token(slug: str) -> dict:
    for attempt in range(5):
        try:
            r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            markets = r.json()
            if markets:
                m = markets[0] if isinstance(markets, list) else markets
                ids = m.get("clobTokenIds", [])
                if isinstance(ids, str):
                    ids = json.loads(ids)
                return {"token_ids": ids or [], "condition_id": m.get("conditionId", "")}
        except Exception:
            pass
        if attempt < 4:
            time.sleep(3)
    return {"token_ids": [], "condition_id": ""}


def take_sample(token_id: str, n: int) -> dict:
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
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask_raw = float(asks[0]["price"]) if asks else None
        if best_bid is not None and best_ask_raw is not None:
            mid_val = round((best_bid + best_ask_raw) / 2, 4)
        else:
            mid_val = None
        row.update({
            "book_ok": True,
            "bid_depth": len(bids),
            "ask_depth": len(asks),
            "best_bid": best_bid,
            "best_ask_raw": best_ask_raw,
            "mid": mid_val,
        })
        print(f"    book bid={best_bid} ask={best_ask_raw} mid={mid_val} ({len(bids)}b/{len(asks)}a)")
    except Exception as e:
        row.update({"book_ok": False, "book_err": str(e)})
        print(f"    book err: {e}")

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
        latest_price = float(trades[0].get("price", 0)) if trades else None
        tps = round(len(trades) / span, 2) if span > 0 else None
        row.update({
            "trades_ok": True,
            "trade_count": len(trades),
            "trade_span_secs": round(span, 1),
            "trades_per_sec": tps,
            "latest_price": latest_price,
        })
        tps_str = f"{tps:.2f}/s" if tps else "?"
        print(f"    trades {len(trades)} in {span:.0f}s -> {tps_str} latest={latest_price}")
    except Exception as e:
        row.update({"trades_ok": False, "trade_err": str(e)})
        print(f"    trades err: {e}")

    # 3. D64a fetch_best_ask (AMM-guarded)
    try:
        sys.path.insert(0, ".")
        from panopticon_py.ingestion.clob_client import fetch_best_ask
        ask = fetch_best_ask(token_id, timeout_sec=3.0)
        row.update({"d64a_ask": ask, "d64a_ok": ask is not None})
        if ask is not None:
            print(f"    D64a fetch_best_ask = ask={ask}")
        else:
            print(f"    D64a fetch_best_ask = AMM blocked (None)")
    except Exception as e:
        row.update({"d64a_ok": False, "d64a_err": str(e)})
        print(f"    D64a err: {e}")

    return row


def run_window(window_start: int, window_num: int) -> dict:
    slug = make_slug(window_start)
    end_ts = window_end(window_start)
    now = int(time.time())

    print(f"\n{'=' * 65}")
    print(f"WINDOW {window_num}: {slug}")
    print(f"  ET window: {et_str(window_start)} - {et_str(end_ts)}")
    print(f"  Current: {now_et_str()}")
    print(f"  Switch in: {max(end_ts - now, 0)}s (at {et_str(end_ts)})")
    print(f"{'=' * 65}")

    token_info = resolve_token(slug)
    ids = token_info.get("token_ids", [])
    if not ids:
        print(f"  ERROR: No token_id -- skipping")
        return {"slug": slug, "window_start_ts": window_start,
                "window_end_ts": end_ts, "error": "no_token_id"}

    yes_token = ids[0]
    no_token = ids[1] if len(ids) > 1 else None
    print(f"  YES token: {yes_token[:24]}...")
    if no_token:
        print(f"  NO  token: {no_token[:24]}...")

    samples = []
    n = 0

    # If window already open, sample immediately
    if now >= window_start:
        n += 1
        print(f"\n  [Sample {n}] (already open -- sampling now)")
        samples.append(take_sample(yes_token, n))
    else:
        wait = window_start - now + 1
        print(f"  Waiting {wait}s for window open...")
        time.sleep(wait)
        n += 1
        print(f"  [Sample {n}] window opened -- sampling")
        samples.append(take_sample(yes_token, n))

    # Sample every 30s until window closes
    while True:
        now = int(time.time())
        if now >= end_ts:
            print(f"\n  Window closed at {et_str(end_ts)}.")
            break
        sleep_sec = min(30, end_ts - int(time.time()) - 2)
        if sleep_sec > 1:
            time.sleep(sleep_sec)
        n += 1
        remaining = max(end_ts - int(time.time()), 0)
        print(f"\n  [Sample {n}] {now_et_str()} ({remaining}s to close)")
        samples.append(take_sample(yes_token, n))

    # Post-window settlement
    print(f"\n  --- POST-WINDOW: fetch_settlement_price ---")
    time.sleep(8)
    settlement = None
    settlement_ok = False
    try:
        from panopticon_py.market_data.clob_series import fetch_settlement_price
        settlement = fetch_settlement_price(yes_token, timeout_sec=10.0)
        settlement_ok = settlement is not None
        if settlement is not None:
            print(f"  D64b settlement = {settlement}  OK")
        else:
            print(f"  D64b settlement = None (AMM or no CLOB history)")
    except Exception as e:
        print(f"  D64b err: {e}")

    result = {
        "slug": slug,
        "token_id_yes": yes_token,
        "window_start_ts": window_start,
        "window_end_ts": end_ts,
        "sample_count": n,
        "samples": samples,
        "settlement_price": settlement,
        "settlement_ok": settlement_ok,
    }
    (OUTDIR / f"{slug}.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    print("BTC 5m Monitor v3 -- immediate sampling")
    print(f"Window switch times:")
    for ts, label in [
        (1777351500, "12:45:00 AM ET (current)"),
        (1777351800, "12:50:00 AM ET (next)"),
        (1777352100, "12:55:00 AM ET"),
    ]:
        print(f"  {ts} = {et_str(ts)} = {label}")
    print()

    current_start = current_window_start()
    print(f"Current window: {make_slug(current_start)}")
    print(f"  ET now: {now_et_str()}")
    print(f"  Switch at: {et_str(window_end(current_start))} (in {max(window_end(current_start) - int(time.time()), 0)}s)")
    print()

    all_results = []
    for i in range(3):
        ws = current_start + i * 300
        result = run_window(ws, i + 1)
        all_results.append(result)

    (OUTDIR / "all_windows.json").write_text(json.dumps(all_results, indent=2))
    print(f"\n\nALL COMPLETE -- saved to {OUTDIR}/")


if __name__ == "__main__":
    main()
