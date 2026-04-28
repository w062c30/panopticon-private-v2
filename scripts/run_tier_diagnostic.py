"""
scripts/run_tier_diagnostic.py
T1 slug verification + T2/T5 Gamma diagnostic.

Usage: python scripts/run_tier_diagnostic.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
import time
from collections import Counter

import httpx

sys.path.insert(0, ".")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# ─── T1 SLUG PREFIXES ────────────────────────────────────────────────────────

T1_PREFIXES = [
    "btc-updown-5m",
    "eth-updown-5m",
    "sol-updown-5m",
    "ethereum-updown-5m",
    "solana-updown-5m",
    "bitcoin-updown-5m",
    "ethereum-up-or-down-5m",
    "solana-up-or-down-5m",
]


def compute_t1_windows(n_windows: int = 5) -> list[int]:
    """Compute Unix timestamps for current + next N 5-min windows."""
    now_ts = int(time.time())
    current = (now_ts // 300) * 300
    return [current + (i * 300) for i in range(n_windows)]


# ─── T1 DIAGNOSTIC ──────────────────────────────────────────────────────────

async def diagnose_t1() -> None:
    """T1 slug verification — confirmed BTC/ETH/SOL patterns."""
    async with httpx.AsyncClient(timeout=15) as client:
        windows = compute_t1_windows(5)
        print(f"=== T1 Slug Lookup — {len(windows)} windows x {len(T1_PREFIXES)} prefixes ===\n")
        print(f"Window timestamps (Unix): {windows}")

        found = []
        for prefix in T1_PREFIXES:
            for ts in windows:
                slug = f"{prefix}-{ts}"
                try:
                    r = await client.get(GAMMA_MARKETS_URL, params={"slug": slug})
                    data = r.json()
                    markets = data if isinstance(data, list) else ([data] if isinstance(data, dict) and data else [])

                    for m in markets:
                        if not m or not isinstance(m, dict):
                            continue
                        tids_raw = m.get("clobTokenIds") or []
                        if isinstance(tids_raw, str):
                            try:
                                tids = json.loads(tids_raw)
                            except Exception:
                                tids = []
                        elif isinstance(tids_raw, list):
                            tids = tids_raw
                        else:
                            tids = []

                        print(f"\n[FOUND] slug={slug}")
                        print(f"  question: {str(m.get('question') or '')[:70]}")
                        print(f"  endDate:  {m.get('endDate')}")
                        print(f"  volume24hr: {m.get('volume24hr')}")
                        print(f"  clobTokenIds ({len(tids)} tokens): {str(tids)[:80]}")
                        found.append((slug, m))
                except Exception as e:
                    print(f"[ERROR] slug={slug}: {e}")

        print(f"\n\n=== T1 SUMMARY ===")
        print(f"Total slugs tested: {len(T1_PREFIXES) * len(windows)}")
        print(f"Markets found: {len(found)}")
        for slug, m in found:
            print(f"  {slug}")


# ─── T2/T5 DIAGNOSTIC ───────────────────────────────────────────────────────

async def diagnose_t2_t5() -> None:
    """Full T2/T5 Gamma listing diagnostic."""
    async with httpx.AsyncClient(timeout=15) as c:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # ── T2-A: raw active markets, limit=200 (no date filter) ──────────────
        print("\n" + "=" * 70)
        print("=== T2-A: raw active markets, limit=200 ===")
        r = await c.get(GAMMA_MARKETS_URL, params={
            "active": "true", "closed": "false", "limit": 200,
        })
        all_mkts = r.json() if isinstance(r.json(), list) else []
        print(f"Total returned: {len(all_mkts)}")

        # Sample end dates to understand distribution
        buckets = {"<1d": 0, "1-3d": 0, "3-30d": 0, ">30d": 0, "no_date": 0}
        for m in all_mkts:
            raw = m.get("endDate") or m.get("endDateIso") or ""
            if not raw:
                buckets["no_date"] += 1
                continue
            try:
                end_str = str(raw)
                if "T" not in end_str:
                    end_dt = datetime.datetime.fromisoformat(end_str).replace(tzinfo=datetime.timezone.utc)
                else:
                    end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                delta_days = (end_dt - now_utc).total_seconds() / 86400
                if delta_days < 1:
                    buckets["<1d"] += 1
                elif delta_days < 3:
                    buckets["1-3d"] += 1
                elif delta_days < 30:
                    buckets["3-30d"] += 1
                else:
                    buckets[">30d"] += 1
            except Exception:
                buckets["no_date"] += 1
        print(f"endDate distribution: {buckets}")

        # ── T2: Collect 3-30d candidates ─────────────────────────────────────
        t2_candidates = []
        for m in all_mkts:
            raw = m.get("endDate") or m.get("endDateIso") or ""
            try:
                end_str = str(raw)
                if "T" not in end_str:
                    end_dt = datetime.datetime.fromisoformat(end_str).replace(tzinfo=datetime.timezone.utc)
                else:
                    end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                delta_days = (end_dt - now_utc).total_seconds() / 86400
                if 3 <= delta_days <= 30:
                    t2_candidates.append(m)
            except Exception:
                pass

        print(f"\nMarkets in 3-30d window: {len(t2_candidates)}")
        for m in t2_candidates[:5]:
            print(json.dumps({
                "slug": m.get("slug"),
                "question": m.get("question"),
                "endDate": m.get("endDate"),
                "volume24hr": m.get("volume24hr"),
                "volumeNum": m.get("volumeNum"),
                "category": m.get("groupItemTitle") or m.get("category"),
                "active": m.get("active"),
                "closed": m.get("closed"),
                "resolved": m.get("resolved"),
                "bestBid": m.get("bestBid"),
                "best_bid": m.get("best_bid"),
            }, indent=2))

        # ── T2-B: category breakdown ───────────────────────────────────────────
        print("\n=== T2-B: category (groupItemTitle) breakdown ===")
        cats = Counter(
            str(m.get("groupItemTitle") or m.get("category") or "NONE")
            for m in all_mkts
        )
        for cat, cnt in cats.most_common(15):
            print(f"  {cat}: {cnt}")

        # ── T2-C: Simulate _is_tier2_market() failures on t2_candidates ──────
        print("\n=== T2-C: simulate _is_tier2_market() on 3-30d candidates ===")
        for m in t2_candidates[:10]:
            slug = str(m.get("slug") or "").lower()
            resolved = bool(m.get("resolved"))
            closed = bool(m.get("closed"))
            best_bid_raw = m.get("bestBid") or m.get("best_bid") or "0.5"
            try:
                best_bid = float(best_bid_raw)
            except Exception:
                best_bid = 0.5
            category_raw = str(m.get("groupItemTitle") or m.get("category") or "").lower()
            is_sports = "sport" in category_raw
            is_algo = any(kw in slug for kw in ["updown", "up-or-down", "5m", "15m", "1h"])
            vol = float(m.get("volume24hr") or m.get("volumeNum") or 0)
            reject_reason = []
            if resolved:
                reject_reason.append("resolved=True")
            if closed:
                reject_reason.append("closed=True")
            if best_bid >= 0.99:
                reject_reason.append(f"bestBid={best_bid:.3f}>=0.99")
            if best_bid <= 0.01:
                reject_reason.append(f"bestBid={best_bid:.3f}<=0.01")
            if is_sports:
                reject_reason.append("sports")
            if is_algo:
                reject_reason.append("algo_kw_in_slug")
            if vol < 5000:
                reject_reason.append(f"vol={vol:.0f}<5000")
            status = "PASS" if not reject_reason else "REJECT: " + ",".join(reject_reason)
            print(f"  {m.get('slug', '?')[:55]} | vol={vol:>10,.0f} | bestBid={best_bid:.3f} | {status}")

        # ── T5-A: sports markets via tag= ─────────────────────────────────────
        print("\n\n=== T5-A: sports markets exploration via tag= ===")
        for cat_query in ["sports", "Soccer", "NBA", "Tennis"]:
            r2 = await c.get(GAMMA_MARKETS_URL, params={
                "active": "true", "closed": "false",
                "limit": 50, "tag": cat_query,
            })
            data = r2.json() if isinstance(r2.json(), list) else []
            print(f"\ntag={cat_query}: {len(data)} markets")
            for m in data[:2]:
                print(json.dumps({
                    "slug": m.get("slug"),
                    "question": m.get("question"),
                    "endDate": m.get("endDate"),
                    "active": m.get("active"),
                    "volume24hr": m.get("volume24hr"),
                    "category": m.get("groupItemTitle") or m.get("category"),
                    "tag": m.get("tags"),
                }, indent=2))

        # ── T5-B: /events with tag=soccer/nba ────────────────────────────────
        print("\n=== T5-B: /events with tag=soccer/nba/sports ===")
        for tag in ["soccer", "nba", "sports"]:
            r3 = await c.get(GAMMA_EVENTS_URL, params={
                "active": "true", "closed": "false",
                "limit": 30, "tag": tag,
            })
            data = r3.json() if isinstance(r3.json(), list) else []
            print(f"\n/events tag={tag}: {len(data)} events")
            for e in data[:2]:
                mkts = e.get("markets") or []
                print(json.dumps({
                    "event_slug": e.get("slug"),
                    "title": e.get("title"),
                    "market_count": len(mkts),
                    "first_market_endDate": mkts[0].get("endDate") if mkts else None,
                    "first_market_active": mkts[0].get("active") if mkts else None,
                    "tags": e.get("tags"),
                }, indent=2))


async def main() -> None:
    await diagnose_t1()
    await diagnose_t2_t5()


if __name__ == "__main__":
    asyncio.run(main())