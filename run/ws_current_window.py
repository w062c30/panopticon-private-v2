"""D124 Phase 0b — Current window smoke test"""
import asyncio, json, time, sys
sys.path.insert(0, ".")
from panopticon_py.load_env import load_repo_env
load_repo_env()
import httpx, websockets

INTERESTING = {"price_change","last_trade_price","book","trade","order_filled","tick_size_change","last_trade","ticker"}

async def main():
    # Resolve current BTC 5m via httpx (has proper headers)
    now_et = int(time.time()) - 4*3600
    ws_ts = (now_et // 300) * 300
    ws_utc = ws_ts + 4*3600
    slug = f"btc-updown-5m-{ws_utc}"
    r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=5)
    m = r.json()
    if not m:
        print("No market found"); return
    ids_raw = m[0].get("clobTokenIds", "[]")
    ids = json.loads(ids_raw) if isinstance(ids_raw, str) else ids_raw
    T1, T2 = ids[0], (ids[1] if len(ids) > 1 else ids[0])
    print(f"Current BTC 5m: slug={slug} T1={T1[:20]}... T2={T2[:20]}...")
    print(f"Active={m[0].get('active')} Closed={m[0].get('closed')}")

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    T = 45  # 45 seconds

    formats = [
        ("F_CURRENT",   {"assets_ids": [T1,T2], "type": "market",     "custom_feature_enabled": True}),
        ("F_SUBSCRIBE", {"assets_ids": [T1,T2], "type": "subscribe", "custom_feature_enabled": True}),
    ]
    results = []
    for label, payload in formats:
        print(f"\n{'='*50}\n[{label}] payload={json.dumps(payload)[:100]}")
        t0 = time.monotonic()
        msgs, types = [], []
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=30) as ws_conn:
                await ws_conn.send(json.dumps(payload))
                while time.monotonic() - t0 < T:
                    try:
                        raw = await asyncio.wait_for(ws_conn.recv(), timeout=1.0)
                        data = json.loads(raw)
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if not isinstance(item, dict): continue
                            etype = item.get("event_type") or item.get("type") or "?"
                            if etype in ("PONG","pong","PING"): continue
                            msgs.append(item)
                            if etype not in types: types.append(etype)
                            marker = "FIRE" if etype.lower() in INTERESTING else "box"
                            print(f"  [{marker}] +{time.monotonic()-t0:.1f}s type={etype} keys={list(item.keys())[:6]}")
                            if etype.lower() in INTERESTING:
                                print(f"     DATA: {json.dumps(item)[:200]}")
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            print(f"  ERROR: {e}")
        has_fire = bool(set(types) & INTERESTING)
        winner = "WINNER" if has_fire else ("msgs" if msgs else "ZERO")
        print(f"  => [{winner}] msgs={len(msgs)} types={types}")
        results.append({"label": label, "msgs": len(msgs), "types": types, "winner": has_fire})
        await asyncio.sleep(3)

    print(f"\n{'='*50}\nSUMMARY")
    for r2 in results:
        print(f"  [{r2['winner']}] {r2['label']:25s} msgs={r2['msgs']} types={r2['types']}")

    import pathlib
    pathlib.Path("run/ws_current_window.json").write_text(json.dumps({
        "slug": slug, "T1": T1[:20], "T2": T2[:20],
        "active": m[0].get("active"),
        "results": results,
    }, indent=2))

asyncio.run(main())
