"""
D124 Phase 0 — WS Format Discovery Smoke Test
Run: python run/ws_smoke_test.py

Purpose: Test all Polymarket WS subscription formats to find which
receives price_change / last_trade_price / book events.

6 format variants tested:
  F_CURRENT         — assets_ids + type=market + custom_feature_enabled (current radar)
  F_NO_S            — asset_ids (no s) + type=market + custom_feature_enabled
  F_SUBSCRIBE       — assets_ids + type=subscribe + custom_feature_enabled
  F_SUBSCRIBE_NO_S  — asset_ids + type=subscribe + custom_feature_enabled
  F_ARRAY           — array wrapper + assets_ids + type=market
  F_BARE            — asset_ids + type=market (no custom_feature_enabled)
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, ".")
from panopticon_py.load_env import load_repo_env

load_repo_env()


def load_tokens() -> tuple[str, str]:
    """Load tokens from btc_monitor_tokens.json or resolve via Gamma."""
    import urllib.request
    import urllib.parse

    # Try btc_monitor_tokens.json first (known-good tokens)
    tokens_path = pathlib.Path("run/btc_monitor_tokens.json")
    if tokens_path.exists():
        try:
            data = json.loads(tokens_path.read_text())
            for slug, market_data in data.items():
                ids = market_data.get("token_ids", [])
                if ids and len(ids) >= 2:
                    print(f"[SMOKE] Tokens from btc_monitor_tokens.json: {slug}")
                    return ids[0], ids[1]
        except Exception as e:
            print(f"[SMOKE] btc_monitor_tokens.json error: {e}")

    # Fallback: Gamma API resolve current BTC 5m window
    now_et = int(time.time()) - 4 * 3600
    ws = (now_et // 300) * 300
    ws_utc = ws + 4 * 3600
    slug = f"btc-updown-5m-{ws_utc}"
    url = f"https://gamma-api.polymarket.com/markets?{urllib.parse.urlencode({'slug': slug})}"
    print(f"[SMOKE] Gamma resolve: slug={slug}")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            markets = json.loads(r.read())
        if markets:
            m = markets[0]
            ids_raw = m.get("clobTokenIds", "[]")
            ids = json.loads(ids_raw) if isinstance(ids_raw, str) else (ids_raw or [])
            if ids:
                print(f"[SMOKE] Gamma resolved: {[str(t)[:20] for t in ids]}")
                return ids[0], (ids[1] if len(ids) > 1 else ids[0])
    except Exception as e:
        print(f"[SMOKE] Gamma error: {e}")

    return "", ""


T1, T2 = load_tokens()
if not T1:
    print("[SMOKE] FATAL: No token available. Exiting.")
    sys.exit(1)
print(f"[SMOKE] Test tokens: YES={T1[:20]}... NO={T2[:20]}...")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TIMEOUT_SECS = 30

# Interesting event types (trade/market data, not metadata)
INTERESTING = {
    "price_change", "last_trade_price", "book", "trade",
    "order_filled", "tick_size_change", "last_trade", "ticker",
}

FORMATS: list[tuple[str, object]] = [
    # F_CURRENT — current radar format
    ("F_CURRENT",
     {"assets_ids": [T1, T2], "type": "market", "custom_feature_enabled": True}),
    # F_NO_S — asset_ids (no s) key
    ("F_NO_S",
     {"asset_ids": [T1, T2], "type": "market", "custom_feature_enabled": True}),
    # F_SUBSCRIBE — type=subscribe
    ("F_SUBSCRIBE",
     {"assets_ids": [T1, T2], "type": "subscribe", "custom_feature_enabled": True}),
    # F_SUBSCRIBE_NO_S — both fixes
    ("F_SUBSCRIBE_NO_S",
     {"asset_ids": [T1, T2], "type": "subscribe", "custom_feature_enabled": True}),
    # F_ARRAY — array wrapper
    ("F_ARRAY",
     [{"assets_ids": [T1, T2], "type": "market", "custom_feature_enabled": True}]),
    # F_BARE — minimal, no custom_feature_enabled
    ("F_BARE",
     {"asset_ids": [T1], "type": "market"}),
]


async def test_format(label: str, payload: dict | list) -> dict:
    """Test one payload format."""
    try:
        import websockets
    except ImportError:
        print("[SMOKE] websockets not installed: pip install websockets")
        return {"label": label, "error": "websockets missing"}

    print(f"\n{'=' * 60}")
    print(f"[SMOKE][{label}] payload={json.dumps(payload)[:120]}")
    result: dict = {"label": label, "msgs": [], "event_types": [], "error": None}
    t0 = time.monotonic()

    try:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(json.dumps(payload))
            print(f"[SMOKE][{label}] SENT. Waiting {TIMEOUT_SECS}s...")
            deadline = t0 + TIMEOUT_SECS

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(1.0, remaining))
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    continue
                if not data:
                    continue

                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    etype = item.get("event_type") or item.get("type") or "?"
                    if etype in ("PONG", "pong", "PING"):
                        continue
                    result["msgs"].append(item)
                    if etype not in result["event_types"]:
                        result["event_types"].append(etype)
                    interesting = etype.lower() in INTERESTING
                    marker = "FIRE" if interesting else "box"
                    print(
                        f"  [{marker}] +{time.monotonic() - t0:.1f}s "
                        f"type={etype} keys={list(item.keys())[:6]}"
                    )
                    if interesting:
                        print(f"     DATA: {json.dumps(item)[:200]}")
    except Exception as e:
        result["error"] = str(e)
        print(f"[SMOKE][{label}] ERROR: {e}")

    n = len(result["msgs"])
    has_interesting = bool(set(result["event_types"]) & INTERESTING)
    if result["error"]:
        verdict = "ERR"
    elif has_interesting:
        verdict = "WINNER"
    elif n > 0:
        verdict = "msgs-no-trade"
    else:
        verdict = "zero"
    print(f"[SMOKE][{label}] {verdict} | msgs={n} types={result['event_types'] or '(none)'}")
    return result


async def main() -> None:
    all_results: list[dict] = []
    for label, payload in FORMATS:
        r = await test_format(label, payload)
        all_results.append(r)
        await asyncio.sleep(3)

    # REST verify (confirm market has activity)
    print(f"\n{'=' * 60}")
    print("[SMOKE] REST Verify")
    try:
        import urllib.request
        url = f"https://clob.polymarket.com/trades?token_id={T1}&limit=5"
        with urllib.request.urlopen(url, timeout=5) as resp:
            trades = json.loads(resp.read())
        print(f"[SMOKE][REST] last {len(trades)} trades for T1:")
        for t in (trades or [])[:3]:
            print(f"  price={t.get('price')} time={t.get('timestamp') or t.get('matchTime')}")
    except Exception as e:
        print(f"[SMOKE][REST] failed: {e}")

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    winners: list[dict] = []
    for r in all_results:
        has_trade = bool(set(r["event_types"]) & INTERESTING)
        marker = "WIN" if has_trade else "   "
        err_str = f" ERR={r['error']}" if r.get("error") else ""
        print(f"  [{marker}] {r['label']:25s} msgs={len(r['msgs']):3d} "
              f"types={r['event_types'] or '(none)'}{err_str}")
        if has_trade:
            winners.append(r)

    if winners:
        print(f"\nWINNERS ({len(winners)}): {[w['label'] for w in winners]}")
        print(f"Winning event_types: {winners[0]['event_types']}")
        winning_format = winners[0]
    else:
        print("\nNO WINNERS — all formats silent. Check REST verify above.")
        winning_format = None

    # Save results
    out_path = pathlib.Path("run/ws_format_discovery.json")
    out_data = {
        "test_ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tokens": {"T1": T1[:20], "T2": T2[:20]},
        "all_results": all_results,
        "winners": winners,
        "winning_format": winning_format,
    }
    out_path.write_text(json.dumps(out_data, indent=2, default=str))
    print(f"\n[SMOKE] Saved: {out_path}")
    print("[SMOKE] Next: check run/ws_format_discovery.json for winning format")


if __name__ == "__main__":
    asyncio.run(main())
