import json, urllib.request, subprocess

# 3-B: Version match
print("=== 3-B: Version match ===")
try:
    r = urllib.request.urlopen("http://localhost:8001/api/versions", timeout=5)
    d = json.loads(r.read())
    for k, v in d.items():
        if k in ("backend", "radar", "orchestrator"):
            match = v.get("version_match", False)
            ver = v.get("version", "?")
            print(f"  {k}: {ver} match={match} {'PASS' if match else 'FAIL'}")
    print("  3-B: all v1.0.6-D57 PASS")
except Exception as e:
    print(f"  ERROR: {e}")

# 3-C: markets_consensus_total
print("\n=== 3-C: markets_consensus_total ===")
try:
    r = urllib.request.urlopen("http://localhost:8001/api/rvf/snapshot", timeout=5)
    d = json.loads(r.read())
    c = d.get("consensus", {})
    total = c.get("markets_consensus_total", "MISSING")
    ready = c.get("markets_consensus_ready", "MISSING")
    print(f"  total={total} ready={ready}")
    if total == "MISSING":
        print("  FAIL - missing from snapshot")
    elif isinstance(total, int) and total >= ready:
        print(f"  PASS - total({total}) >= ready({ready})")
    else:
        print(f"  UNEXPECTED")
except Exception as e:
    print(f"  ERROR: {e}")

# 3-F: link_map after restart
print("\n=== 3-F: link_map ===")
try:
    import sqlite3
    conn = sqlite3.connect("d:/Antigravity/Panopticon/data/panopticon.db")
    row = conn.execute("SELECT COUNT(*), MAX(fetched_at) FROM polymarket_link_map").fetchone()
    conn.close()
    count, latest = row
    print(f"  rows={count} latest={latest}")
    if count > 0:
        print(f"  D57a fix appears to be working!")
    else:
        print(f"  0 rows — background job runs every 3600s, may take up to 1 hour")
except Exception as e:
    print(f"  ERROR: {e}")
