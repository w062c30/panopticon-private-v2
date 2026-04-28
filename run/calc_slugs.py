import time, datetime, json, pathlib

now_ts = int(time.time())
windows = []
for i in range(3):
    start_ts = (now_ts // 300) * 300 + i * 300
    end_ts   = start_ts + 300
    slug     = f"btc-updown-5m-{start_ts}"
    start_utc = datetime.datetime.utcfromtimestamp(start_ts)
    end_utc   = datetime.datetime.utcfromtimestamp(end_ts)
    start_et  = start_utc - datetime.timedelta(hours=4)
    end_et    = end_utc   - datetime.timedelta(hours=4)
    remaining = end_ts - now_ts if i == 0 else 300
    windows.append({
        "window": i+1,
        "slug": slug,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "start_et": start_et.strftime("%I:%M"),
        "end_et": end_et.strftime("%I:%M %p"),
        "remaining_secs": remaining,
    })
    print(f"Window {i+1}: {slug}")
    print(f"  ET: {start_et.strftime('%I:%M')}-{end_et.strftime('%I:%M %p')}  remaining={remaining}s")

pathlib.Path("run").mkdir(exist_ok=True)
json.dump(windows, open("run/btc_monitor_slugs.json","w"), indent=2)

anchor_ts = 1777318500
anchor_check = datetime.datetime.utcfromtimestamp(anchor_ts)
print()
print(f"Verify anchor 1777318500 -> {anchor_check} UTC")
