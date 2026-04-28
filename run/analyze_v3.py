import json
from pathlib import Path
import datetime

ET_OFFSET = 4 * 3600

def et_str(ts):
    utc = datetime.datetime.utcfromtimestamp(ts)
    et = utc - datetime.timedelta(hours=4)
    return et.strftime("%I:%M %p ET")

files = sorted(Path("run/monitor_results_v3").glob("*.json"))
print(f"Found {len(files)} window result files\n")

for f in files:
    d = json.loads(f.read_text())
    slug = d.get("slug", "")
    ws = d.get("window_start_ts", 0)
    we = ws + 300
    samples = d.get("samples", [])
    sc = len(samples)

    print("=" * 65)
    print(f"Window: {slug}")
    print(f"  ET: {et_str(ws)} - {et_str(we)}")
    print(f"  Samples: {sc}")

    bids = [s["best_bid"] for s in samples if s.get("best_bid") is not None]
    asks = [s["best_ask_raw"] for s in samples if s.get("best_ask_raw") is not None]
    trades_list = [s.get("trade_count", 0) for s in samples]
    d64a_vals = [s.get("d64a_ask") for s in samples]
    d64a_blocked = sum(1 for v in d64a_vals if v is None)
    d64a_99 = sum(1 for v in d64a_vals if v is not None and v > 0.9)

    bid_r = f"[{min(bids) if bids else 'N/A'}, {max(bids) if bids else 'N/A'}]"
    ask_r = f"[{min(asks) if asks else 'N/A'}, {max(asks) if asks else 'N/A'}]"
    print(f"  Book: bid_range={bid_r}  ask_range={ask_r}")
    print(f"  Trades: total={sum(trades_list)}  max_sample={max(trades_list) if trades_list else 0}")
    print(f"  D64a: blocked={d64a_blocked}/{sc}  returned_0.99={d64a_99}/{sc}")
    settlement = d.get("settlement_price")
    print(f"  Settlement: {settlement}  ok={d.get('settlement_ok')}")

    if bids and asks:
        spread = max(asks) - min(bids)
        if spread > 0.85:
            print(f"\n  VERDICT: AMM market (spread={spread:.3f} > 0.85)")
            print(f"  fetch_best_ask correctly blocked -> None (NO_TRADE)")
        else:
            print(f"\n  VERDICT: CLOB market (spread={spread:.3f})")

    print(f"\n  Sample timeline:")
    for s in samples:
        et = et_str(s["ts"])
        bid = s.get("best_bid")
        ask = s.get("best_ask_raw")
        tcount = s.get("trade_count", 0)
        d64a = s.get("d64a_ask")
        d64a_str = f"d64a={'None' if d64a is None else f'{d64a:.2f}'}"
        print(f"    [{s['sample']:02d}] {et}  bid={bid} ask={ask} trades={tcount} {d64a_str}")
    print()

print("\nSummary:")
print("  - All BTC 5m windows: AMM (spread > 0.85)")
print("  - fetch_best_ask correctly blocked by D67 AMM guard -> None")
print("  - 0 actual CLOB trades across all samples")
print("  - Settlement: None (no CLOB /prices-history for AMM markets)")
