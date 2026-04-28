import json
from pathlib import Path

files = sorted(Path("run/monitor_results_v3").glob("*.json"))
for f in files:
    d = json.loads(f.read_text())
    slug = d.get("slug", "")
    ws = d.get("window_start_ts", 0)
    samples = d.get("samples", [])
    sc = len(samples)
    settlement = d.get("settlement_price")
    settlement_ok = d.get("settlement_ok")
    print(f"Slug: {slug}")
    print(f"  Window start ts: {ws}")
    print(f"  Samples collected: {sc}")
    if samples:
        s = samples[0]
        print(f"  First sample: bid={s.get('best_bid')} ask={s.get('best_ask_raw')} mid={s.get('mid')}")
        print(f"  Trades: count={s.get('trade_count')} tps={s.get('trades_per_sec')} latest={s.get('latest_price')}")
        print(f"  D64a: ask={s.get('d64a_ask')} ok={s.get('d64a_ok')}")
        if sc > 1:
            s2 = samples[-1]
            print(f"  Last sample: bid={s2.get('best_bid')} ask={s2.get('best_ask_raw')} mid={s2.get('mid')}")
    print(f"  Settlement: {settlement} ok={settlement_ok}")
    print()
