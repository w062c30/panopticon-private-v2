import json, requests, time, sys

slugs   = json.load(open("run/btc_monitor_slugs.json"))
results = {}

for w in slugs:
    slug = w["slug"]
    sys.stdout.write(f"Resolving {slug} ... ")
    sys.stdout.flush()
    token_ids = []
    condition_id = ""
    for attempt in range(3):
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug}, timeout=5
            )
            markets = r.json()
            if markets:
                m = markets[0] if isinstance(markets, list) else markets
                ids = m.get("clobTokenIds", [])
                if isinstance(ids, str):
                    ids = json.loads(ids)
                condition_id = m.get("conditionId", "")
                token_ids = ids
                break
        except Exception as e:
            pass
        if not token_ids and attempt < 2:
            time.sleep(2)

    results[slug] = {
        "token_ids": token_ids,
        "condition_id": condition_id,
    }
    if token_ids:
        print(f"YES={token_ids[0][:16]}... NO={token_ids[1][:16]}...")
    else:
        print(f"not found yet (future window)")

    time.sleep(0.5)

json.dump(results, open("run/btc_monitor_tokens.json","w"), indent=2)
print("Saved -> run/btc_monitor_tokens.json")
