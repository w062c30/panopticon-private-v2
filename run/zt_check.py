import json, pathlib

p = pathlib.Path("run/monitor_results/all_windows.json")
if not p.exists():
    print("No monitor results")
else:
    r = json.load(open(p))
    for w in r:
        slug = w.get("slug","?")[-10:]
        n = w.get("sample_count",0)
        s_ok = w.get("settlement_ok")
        d64a = sum(1 for s in w.get("samples",[]) if s.get("d64a_ok"))
        tps = [s.get("trades_per_sec") for s in w.get("samples",[]) if s.get("trades_per_sec")]
        avg = sum(tps)/len(tps) if tps else 0
        settle_str = "PASS" if s_ok else "NONE"
        print(f"[{slug}] samples={n} d64a={d64a}/{n} tps={avg:.1f}/s settle={settle_str}")
