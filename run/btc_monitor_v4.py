"""
BTC 5m Monitor v4 -- Single Continuous Market Logic

Key principles:
1. BTC 5m is ONE market (all slugs are the same market, just different resolution windows)
2. Monitor starts IMMEDIATELY when program is online
3. At any moment, monitor current 5-min window + next 5-min window
4. Each window is monitored for 10 min total: 5 min before open + 5 min active
5. When window closes, drop it and add the next new window

Example at 12:47 AM ET:
  - window_A (current): 12:45-12:50 AM ET  → being monitored
  - window_B (next):   12:50-12:55 AM ET  → being monitored
  - at 12:50, window_A ends, window_C (12:55-01:00) starts being monitored
"""

from __future__ import annotations

import json
import time
import datetime
import pathlib
import sys
import requests

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
OUTDIR = pathlib.Path("run/monitor_results_v4")
OUTDIR.mkdir(exist_ok=True)

ET_OFFSET = 4 * 3600  # EDT = UTC-4


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


def take_sample(token_id: str, sample_n: int, window_slug: str) -> dict:
    ts = int(time.time())
    row = {
        "sample": sample_n,
        "ts": ts,
        "utc": datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S"),
        "window_slug": window_slug,
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
            "spread": round(best_ask_raw - best_bid, 4) if best_bid is not None and best_ask_raw is not None else None,
        })
        print(f"    [{sample_n}] bid={best_bid} ask={best_ask_raw} mid={mid_val} ({len(bids)}b/{len(asks)}a)")
    except Exception as e:
        row.update({"book_ok": False, "book_err": str(e)})
        print(f"    [{sample_n}] book err: {e}")

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
        print(f"    [{sample_n}] trades {len(trades)} in {span:.0f}s -> {tps_str} latest={latest_price}")
    except Exception as e:
        row.update({"trades_ok": False, "trade_err": str(e)})
        print(f"    [{sample_n}] trades err: {e}")

    # 3. D64a fetch_best_ask (AMM-guarded)
    try:
        sys.path.insert(0, ".")
        from panopticon_py.ingestion.clob_client import fetch_best_ask
        ask = fetch_best_ask(token_id, timeout_sec=3.0)
        row.update({"d64a_ask": ask, "d64a_ok": True})
        if ask is not None:
            print(f"    [{sample_n}] D64a fetch_best_ask = {ask}")
        else:
            print(f"    [{sample_n}] D64a fetch_best_ask = AMM blocked (None)")
    except Exception as e:
        row.update({"d64a_ok": False, "d64a_err": str(e)})
        print(f"    [{sample_n}] D64a err: {e}")

    return row


def resolve_token_cached(slug: str, cache: dict) -> str | None:
    """Resolve token_id for slug, reusing cache to avoid redundant Gamma API calls."""
    if slug in cache:
        return cache[slug]
    info = resolve_token(slug)
    ids = info.get("token_ids", [])
    if ids:
        cache[slug] = ids[0]
        return ids[0]
    return None


def main():
    print("=" * 65)
    print("BTC 5m Monitor v4 -- Single Continuous Market")
    print("=" * 65)
    print(f"Started: {now_et_str()}")
    print("Principle: BTC 5m is ONE market. Monitor current + next window always.")
    print()

    # Show upcoming window switch times
    print("Upcoming window switches:")
    for i in range(6):
        ws = current_window_start() + i * 300
        print(f"  {make_slug(ws)}  {et_str(ws)} - {et_str(ws + 300)}")
    print()

    # State: which windows are currently being monitored
    # Each window is a dict: {ws, slug, token_id, samples[], started}
    active_windows: list[dict] = []

    # Token cache to avoid redundant Gamma API calls
    token_cache: dict[str, str] = {}

    sample_counter = 0
    total_samples = 0

    # Phase 1: Bootstrap -- figure out where we are in time
    now = int(time.time())
    now_ws = current_window_start()
    next_ws = now_ws + 300

    print(f"[BOOTSTRAP] now={now} now_ws={now_ws} next_ws={next_ws}")
    print(f"  Current window: {make_slug(now_ws)} ({et_str(now_ws)} - {et_str(now_ws+300)})")

    # Start monitoring the TWO upcoming windows:
    #   - window_A: current window (may already be open or about to open)
    #   - window_B: next window (starts 5 min after current)
    bootstrap_slugs = [make_slug(now_ws), make_slug(next_ws)]
    for slug in bootstrap_slugs:
        ws = int(slug.split("-")[-1])
        token_id = resolve_token_cached(slug, token_cache)
        active_windows.append({
            "ws": ws,
            "slug": slug,
            "token_id": token_id,
            "samples": [],
            "started": False,
            "ended": False,
        })
        print(f"  Tracking: {slug} (ws={ws}) token={token_id}")

    # Phase 2: Continuous monitoring loop
    # Every 30s: collect sample from each active window, check for window changes
    print()
    print("=" * 65)
    print("STARTING CONTINUOUS MONITORING LOOP")
    print("=" * 65)

    while True:
        now = int(time.time())
        now_ws = current_window_start()

        # ── Add new window if next window is approaching ──
        # Always keep 2 windows: current + next
        # next window = (current_window_start + 300)
        current_ws = now_ws
        next_ws = now_ws + 300

        active_slugs = {w["slug"] for w in active_windows}
        needed_slugs = {make_slug(current_ws), make_slug(next_ws)}

        for slug in needed_slugs - active_slugs:
            ws = int(slug.split("-")[-1])
            token_id = resolve_token_cached(slug, token_cache)
            active_windows.append({
                "ws": ws,
                "slug": slug,
                "token_id": token_id,
                "samples": [],
                "started": False,
                "ended": False,
            })
            print(f"\n[NEW WINDOW] now={now_et_str()} tracking {slug}")

        # ── Remove windows that are already 5 min past their end ──
        # (Keep for 5 min after end, in case settlement data is still arriving)
        for w in active_windows:
            if not w["ended"] and now > w["ws"] + 600:  # 10 min after window start = 5 min after end
                w["ended"] = True
                print(f"\n[DROP WINDOW] {w['slug']} has ended (now > ws+600)")

        # ── Collect sample from each active window ──
        for w in active_windows:
            if w["ended"] and now > w["ws"] + 600:
                continue  # Already dropped
            if now < w["ws"]:
                continue  # Window not yet open, wait

            # Mark as started
            if not w["started"]:
                w["started"] = True
                print(f"\n[WINDOW OPEN] {w['slug']} now open at {now_et_str()}")

            # Collect sample if we have a token
            if w["token_id"]:
                sample_counter += 1
                total_samples += 1
                print(f"\n[Sample {sample_counter}] {now_et_str()} from {w['slug']}")
                sample = take_sample(w["token_id"], sample_counter, w["slug"])
                w["samples"].append(sample)
            else:
                print(f"\n[Sample {sample_counter}] {w['slug']} -- no token_id yet")

        # ── Print status every 30s ──
        print(f"\n  -- status {now_et_str()} --")
        for w in active_windows:
            status = "OPEN" if now >= w["ws"] else "WAIT"
            ended_tag = " (ENDED)" if w["ended"] else ""
            print(f"    {w['slug']}: {status}, {len(w['samples'])} samples{ended_tag}")
        print()

        # ── Sleep 30 seconds ──
        time.sleep(30)

        # ── Safety: if we have collected 60+ samples total, break for analysis ──
        if total_samples >= 120:  # ~60 min of data (2 windows × 1 sample/30s × 60 min)
            print("\n[STOPPING] Reached target sample count. Saving results.")
            break


    # ── Save results ──
    print("\n" + "=" * 65)
    print("SAVING RESULTS")
    print("=" * 65)

    all_windows_data = []
    for w in active_windows:
        result = {
            "slug": w["slug"],
            "window_start_ts": w["ws"],
            "window_end_ts": w["ws"] + 300,
            "sample_count": len(w["samples"]),
            "samples": w["samples"],
        }
        (OUTDIR / f"{w['slug']}.json").write_text(json.dumps(result, indent=2))
        all_windows_data.append(result)
        print(f"  {w['slug']}: {len(w['samples'])} samples saved")

    (OUTDIR / "all_windows.json").write_text(json.dumps(all_windows_data, indent=2))
    print(f"\nAll results saved to {OUTDIR}/")


if __name__ == "__main__":
    main()
