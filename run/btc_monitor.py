import json, time, requests, datetime, sys, pathlib

CLOB   = "https://clob.polymarket.com"
GAMMA  = "https://gamma-api.polymarket.com"
OUTDIR = pathlib.Path("run/monitor_results")
OUTDIR.mkdir(exist_ok=True)

slugs      = json.load(open("run/btc_monitor_slugs.json"))
token_data = json.load(open("run/btc_monitor_tokens.json"))

def ensure_token(slug):
    if token_data.get(slug, {}).get("token_ids"):
        return token_data[slug]["token_ids"]
    for _ in range(5):
        try:
            r = requests.get(f"{GAMMA}/markets",
                             params={"slug": slug}, timeout=5)
            m = r.json()
            if m:
                m = m[0] if isinstance(m, list) else m
                ids = m.get("clobTokenIds", [])
                if isinstance(ids, str): ids = json.loads(ids)
                if ids:
                    token_data[slug]["token_ids"] = ids
                    token_data[slug]["condition_id"] = m.get("conditionId","")
                    return ids
        except: pass
        time.sleep(3)
    return []

def take_sample(slug, token_id, n):
    ts  = int(time.time())
    row = {"sample": n, "ts": ts,
           "utc": datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")}

    # 1. Order book
    try:
        b    = requests.get(f"{CLOB}/book",
                            params={"token_id": token_id}, timeout=5).json()
        bids = b.get("bids", [])
        asks = b.get("asks", [])
        row.update({
            "book_ok":        True,
            "bid_depth":      len(bids),
            "ask_depth":      len(asks),
            "best_bid":       float(bids[0]["price"]) if bids else None,
            "best_ask_raw":   float(asks[0]["price"]) if asks else None,
            "mid":            round((float(bids[0]["price"])+float(asks[0]["price"]))/2,4)
                              if bids and asks else None,
        })
        print(f"  book  bid={row['best_bid']} ask={row['best_ask_raw']} mid={row['mid']} ({len(bids)}b/{len(asks)}a)")
    except Exception as e:
        row.update({"book_ok": False, "book_err": str(e)})
        print(f"  book error: {e}")

    # 2. Recent trades
    try:
        t = requests.get(f"{CLOB}/trades",
                         params={"token_id": token_id, "limit": 200},
                         timeout=5).json()
        trades = t if isinstance(t, list) else t.get("data", [])
        span = 0
        if len(trades) >= 2:
            def get_ts(tr):
                for k in ("timestamp","matchTime","createdAt","time"):
                    if tr.get(k): return float(tr[k])
                return 0
            newest = get_ts(trades[0])
            oldest = get_ts(trades[-1])
            span   = newest - oldest if oldest > 0 else 0
        row.update({
            "trades_ok":        True,
            "trade_count":      len(trades),
            "trade_span_secs":  round(span, 1),
            "trades_per_sec":   round(len(trades)/span, 2) if span > 0 else None,
            "latest_price":     float(trades[0].get("price",0)) if trades else None,
        })
        print(f"  trades {len(trades)} in {span:.0f}s -> {row['trades_per_sec'] or '?'}/sec latest={row['latest_price']}")
    except Exception as e:
        row.update({"trades_ok": False, "trade_err": str(e)})
        print(f"  trade error: {e}")

    # 3. D64a fetch_best_ask
    try:
        sys.path.insert(0, ".")
        from panopticon_py.ingestion.clob_client import fetch_best_ask
        ask_d64 = fetch_best_ask(token_id, timeout_sec=3.0)
        row.update({"d64a_ask": ask_d64, "d64a_ok": ask_d64 is not None})
        print(f"  D64a fetch_best_ask = {ask_d64}")
    except Exception as e:
        row.update({"d64a_ok": False, "d64a_err": str(e)})
        print(f"  D64a error: {e}")

    return row

def run_window(w):
    slug     = w["slug"]
    end_ts   = w["end_ts"]
    start_ts = w["start_ts"]
    now      = int(time.time())

    print(f"\n{'='*65}")
    print(f"WINDOW {w['window']}/3: {slug}")
    print(f"ET: {w['start_et']}-{w['end_et']}  remaining={max(end_ts-now,0)}s")
    print(f"{'='*65}")

    if now < start_ts:
        wait = start_ts - now + 1
        print(f"Waiting {wait}s for window to open...")
        time.sleep(wait)

    ids = ensure_token(slug)
    if not ids:
        print(f"No token_id for {slug} -- skipping")
        return {"slug": slug, "error": "no_token_id"}

    yes_token = ids[0]
    no_token  = ids[1] if len(ids) > 1 else None
    print(f"YES token: {yes_token[:24]}...")
    if no_token:
        print(f"NO  token: {no_token[:24]}...")

    samples = []
    n       = 0
    while True:
        now = int(time.time())
        if now >= end_ts:
            print("\nWindow closed.")
            break
        n += 1
        print(f"\n[Sample {n}] {datetime.datetime.utcnow().strftime('%H:%M:%S')} UTC  ({max(end_ts-now,0)}s remaining)")
        samples.append(take_sample(slug, yes_token, n))
        sleep = min(30, end_ts - int(time.time()) - 2)
        if sleep > 1:
            time.sleep(sleep)

    print(f"\n--- POST-WINDOW: fetch_settlement_price ---")
    time.sleep(8)
    settlement    = None
    settlement_ok = False
    try:
        from panopticon_py.market_data.clob_series import fetch_settlement_price
        settlement = fetch_settlement_price(yes_token, timeout_sec=10.0)
        settlement_ok = settlement is not None
        print(f"D64b settlement = {settlement}")
    except Exception as e:
        print(f"D64b error: {e}")

    result = {
        "slug": slug,
        "token_id_yes": yes_token,
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
        "sample_count": n,
        "samples": samples,
        "settlement_price": settlement,
        "settlement_ok": settlement_ok,
    }
    (OUTDIR / f"{slug}.json").write_text(json.dumps(result, indent=2))
    return result

all_results = [run_window(w) for w in slugs]
(OUTDIR / "all_windows.json").write_text(json.dumps(all_results, indent=2))
print("\n\nALL 3 WINDOWS COMPLETE")
