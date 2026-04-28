"""Observation-only runner: CLOB (+ optional Moralis) sampling, insider heuristics, no L4 / no TS orders."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from panopticon_py.db import AsyncDBWriter, ShadowDB
from panopticon_py.friction_state import FrictionStateWorker, GlobalFrictionState
from panopticon_py.ingestion.analysis_worker import InsiderAnalysisWorker
from panopticon_py.ingestion.clob_poller import ClobIngestionWorker
from panopticon_py.ingestion.health import paper_gate_counters, run_observation_health_checks
from panopticon_py.ingestion.moralis_poller import MoralisIngestionWorker
from panopticon_py.rate_limit_governor import RateLimitGovernor
from panopticon_py.load_env import load_repo_env


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_watch_list(writer: AsyncDBWriter) -> int:
    raw = os.getenv("WATCH_WALLET_LIST", "").strip()
    if not raw:
        return 0
    n = 0
    for part in raw.split(","):
        addr = part.strip()
        if not addr.startswith("0x") or len(addr) < 42:
            continue
        writer.submit(
            "watched_wallet",
            {
                "address": addr.lower()[:42],
                "label": None,
                "source": "env:WATCH_WALLET_LIST",
                "created_ts_utc": _utc_now(),
                "active": 1,
            },
        )
        n += 1
    return n


def main() -> int:
    load_repo_env()
    ap = argparse.ArgumentParser(description="Panopticon observation-only ingestion (no trading path)")
    ap.add_argument("--duration-sec", type=float, default=0.0, help="0 = run until Ctrl+C")
    ap.add_argument("--report-interval-sec", type=float, default=15.0)
    args = ap.parse_args()

    obs_only = os.getenv("OBSERVATION_ONLY", "1").lower() in ("1", "true", "yes")
    if not obs_only:
        print("warning: OBSERVATION_ONLY is not truthy; this entrypoint still never submits orders.", file=sys.stderr)

    if os.getenv("ENABLE_SIM_TRADING", "0").lower() in ("1", "true", "yes"):
        print("error: ENABLE_SIM_TRADING must be 0 for observation runner", file=sys.stderr)
        return 2

    if os.getenv("PANOPTICON_USE_TS_BRIDGE", "0").lower() in ("1", "true", "yes"):
        print("warning: PANOPTICON_USE_TS_BRIDGE is on; this runner does not call the bridge.", file=sys.stderr)

    db = ShadowDB()
    db.bootstrap()
    ok, issues = run_observation_health_checks(db)
    relax = os.getenv("OBSERVATION_RELAX_HEALTH", "0").lower() in ("1", "true", "yes")
    if not ok and not relax:
        print(json.dumps({"ok": False, "health_issues": issues}), flush=True)
        return 1
    if issues:
        print(json.dumps({"ok": ok, "health_issues": issues, "relaxed": relax}), flush=True)

    writer = AsyncDBWriter(db)
    governor = RateLimitGovernor()
    friction = FrictionStateWorker(GlobalFrictionState())
    clob = ClobIngestionWorker(writer, governor=governor)
    moralis = MoralisIngestionWorker(db, writer, governor=governor)
    analysis = InsiderAnalysisWorker(db, writer)

    friction.start()
    writer.start()
    _seed_watch_list(writer)
    clob.start()
    moralis.start()
    analysis.start()
    time.sleep(0.2)

    start = time.time()
    report_iv = max(1.0, float(args.report_interval_sec))
    dur = float(args.duration_sec)
    try:
        while True:
            if dur > 0 and (time.time() - start) >= dur:
                break
            counters = paper_gate_counters(db)
            line = {
                "ts_utc": _utc_now(),
                "observation_only": True,
                "paper_gate_counters": counters,
                "paper_gate_targets": {
                    "raw_l2_signals_min": 100,
                    "simulated_fills_accepted_min": 30,
                    "note": "docs/paper_to_live_gate.md section 1",
                },
                "friction_api_health": friction.state.get().api_health,
            }
            print(json.dumps(line), flush=True)
            if dur > 0 and (time.time() - start) >= dur:
                break
            time.sleep(report_iv)
    except KeyboardInterrupt:
        pass
    finally:
        analysis.stop()
        moralis.stop()
        clob.stop()
        writer.stop()
        friction.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
