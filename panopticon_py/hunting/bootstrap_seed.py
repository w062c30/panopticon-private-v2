"""Cold-start: score wallets from DB + Moralis (MVP), write Top-N to Redis seed v1."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

from panopticon_py.db import ShadowDB
from panopticon_py.hunting.entity_linker import load_cex_blacklist, trace_funding_roots
from panopticon_py.hunting.four_d_classifier import classify_high_frequency_wallet
from panopticon_py.hunting.moralis_client import fetch_wallet_erc20_transfers_capped
from panopticon_py.hunting.redis_seed import RedisSeedStore
from panopticon_py.hunting.trade_aggregate import aggregate_taker_sweeps
from panopticon_py.load_env import load_repo_env
from panopticon_py.rate_limit_governor import RateLimitGovernor


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_to_synthetic_trades(wallet: str, rows: list[dict]) -> list[dict]:
    w = wallet.lower()[:42]
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        to_a = str(r.get("to_address") or r.get("to") or "").lower()
        fr_a = str(r.get("from_address") or r.get("from") or "").lower()
        dec = int(r.get("token_decimals") or 6)
        raw = r.get("value")
        try:
            val = float(raw) / (10**dec) if raw is not None else 0.0
        except (TypeError, ValueError):
            val = 0.0
        ts = r.get("block_timestamp")
        ts_ms = time.time() * 1000.0
        if isinstance(ts, (int, float)):
            tsf = float(ts)
            ts_ms = tsf * 1000.0 if tsf < 1e12 else tsf
        elif isinstance(ts, str):
            try:
                from datetime import datetime as _dt

                ts_ms = _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0
            except ValueError:
                try:
                    ts_ms = float(ts) * 1000.0
                except ValueError:
                    pass
        side = "BUY" if to_a == w else "SELL"
        out.append(
            {
                "timestamp": ts_ms,
                "taker": w,
                "side": side,
                "size": abs(val),
                "market_id": str(r.get("token_symbol") or "erc20"),
            }
        )
    return sorted(out, key=lambda x: x["timestamp"])


def _score_wallet(wallet: str, governor: RateLimitGovernor) -> tuple[float, dict]:
    rows = fetch_wallet_erc20_transfers_capped(wallet, governor=governor)
    trace = trace_funding_roots(wallet, governor=governor)
    syn = _rows_to_synthetic_trades(wallet, rows)
    parents = aggregate_taker_sweeps(syn)
    label, scores, reasons = classify_high_frequency_wallet(parents)
    base = sum(float(r.get("value") or 0) for r in rows if isinstance(r, dict)) ** 0.5 / (1.0 + len(rows) * 0.05)
    bonus = 0.0
    if label == "INSIDER_ALGO_SLICING":
        bonus = 5.0
    elif label == "POTENTIAL_INSIDER":
        bonus = 2.0
    if trace["cex_anonymized"]:
        base *= 0.35
    score = base + bonus + scores.idi * 2.0
    meta = {"label": label, "reasons": reasons, "trace": trace, "parents": len(parents)}
    return score, meta


def main() -> int:
    load_repo_env()
    ap = argparse.ArgumentParser(description="Bootstrap Redis seed whitelist v1 (MVP)")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--mirror-sqlite", action="store_true", help="Upsert watched_wallets for seed members")
    args = ap.parse_args()

    if not os.getenv("REDIS_URL", "").strip():
        print("REDIS_URL is required", flush=True)
        return 2
    if not os.getenv("MORALIS_API_KEY", "").strip():
        print("MORALIS_API_KEY is required for MVP bootstrap", flush=True)
        return 2

    db = ShadowDB()
    db.bootstrap()
    gov = RateLimitGovernor()
    _ = load_cex_blacklist()

    candidates = db.fetch_distinct_trade_wallets(120)
    raw_env = os.getenv("HUNT_BOOTSTRAP_EXTRA_WALLETS", "").strip()
    for part in raw_env.split(","):
        a = part.strip().lower()
        if a.startswith("0x") and len(a) >= 42 and a not in candidates:
            candidates.append(a[:42])

    ranked: list[tuple[str, float, dict]] = []
    for addr in candidates:
        try:
            sc, meta = _score_wallet(addr, gov)
            ranked.append((addr, sc, meta))
        except Exception as exc:
            # Log but continue so one bad wallet doesn't halt the entire bootstrap
            logging.getLogger("bootstrap_seed").warning(
                "bootstrap_wallet_skip", extra={"address": addr, "error": str(exc)}
            )
            continue

    ranked.sort(key=lambda x: x[1], reverse=True)
    top = [(a, s) for a, s, _m in ranked[: args.top]]

    store = RedisSeedStore()
    store.write_top(top, version="v1", source="bootstrap_mvp")

    if args.mirror_sqlite:
        ts = _utc()
        for addr, _s in top:
            db.upsert_watched_wallet(
                {
                    "address": addr,
                    "label": "seed_v1",
                    "source": "cold_start_v1",
                    "created_ts_utc": ts,
                    "active": 1,
                }
            )

    print(json.dumps({"ok": True, "written": len(top), "sample": top[:5]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
