"""
panopticon_py/verification/rvf_runner.py

RVF Runner — Regular Verification Framework entry point.

Usage (standalone):
    PANOPTICON_SHADOW=1 python -m panopticon_py.verification.rvf_runner \\
        --db data/panopticon.db \\
        --log logs/orchestrator_latest.log \\
        --interval 300

Or with auto-log-discovery:
    PANOPTICON_SHADOW=1 python -m panopticon_py.verification.rvf_runner \\
        --db data/panopticon.db \\
        --interval 300

RVF is NOT started by default.
Activate via PANOPTICON_RVF=1 environment variable.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from .pipeline_health import PipelineHealthCollector
from .pipeline_alert import check_snapshot

logger = logging.getLogger(__name__)

HEADER = """
╔══════════════════════════════════════════════════════════════════╗
║         PANOPTICON — Pipeline Verification Framework           ║
║         RVF v1.0 | Shadow Mode | 5-min rolling window         ║
╚══════════════════════════════════════════════════════════════════╝
"""


def _get_latest_log_path(logs_dir: str = "logs") -> Optional[str]:
    """
    Find the most recently modified orchestrator log file.
    Returns None if no log files found.
    """
    pattern = os.path.join(logs_dir, "orchestrator_*.log")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    # Sort by modification time (newest first)
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def _render_table(snap, alerts: list) -> str:
    """Render a PipelineSnapshot + alerts as a text table."""
    mode_str = "SHADOW" if os.getenv("PANOPTICON_SHADOW") == "1" else "PRODUCTION"
    icon_map = {"INFO": "ℹ", "WARN": "⚠", "CRITICAL": "🚨", "?": "?"}

    rows = [
        f"\n[RVF] {snap.ts_utc}  mode={mode_str}  window={snap.window_minutes}min",
        "-" * 68,
        (
            f"  L1  trade_ticks       : {snap.l1_trade_ticks_received:>6}  "
            f"({snap.l1_tick_rate_per_min:.2f}/min)  "
            f"by_tier={json.dumps(snap.l1_trade_ticks_by_tier)}"
        ),
        (
            f"  L1  entropy_fires     : {snap.l1_entropy_fires:>6}  "
            f"by_tier={json.dumps(snap.l1_entropy_fires_by_tier)}"
        ),
        (
            f"  L1  kyle_samples(5m) : {snap.l1_kyle_samples_written:>6}  "
            f"cumul={snap.kyle_accumulation_rate * 100:.1f}%  (target:500)"
        ),
        f"  L2  queue_puts        : {snap.l2_signal_events_queued:>6}",
        (
            f"  L3  bayesian_updates : {snap.l3_bayesian_updates:>6}  "
            f"gate_pass={snap.l3_gate_pass}  "
            f"gate_reject={snap.l3_gate_reject}"
        ),
        (
            f"  L4  paper_trades     : {snap.l4_paper_trades:>6}  "
            f"live_trades={snap.l4_live_trades}"
            + ("  ⚠ LIVE ACTIVE" if snap.l4_live_trades else "")
        ),
        (
            f"  L5  wallet_obs       : {snap.l5_wallet_obs_written:>6}  "
            f"insider_updates={snap.l5_insider_score_updates}"
        ),
        f"  pipeline_pass_rate    : {snap.pipeline_pass_rate:.2%}",
        "-" * 68,
    ]

    for alert in alerts:
        icon = icon_map.get(alert.severity, "?")
        rows.append(f"  {icon} [{alert.severity}][{alert.layer}] {alert.message}")

    rows.append("")
    return "\n".join(rows)


async def run_rvf_loop(
    db_path: str,
    log_path: Optional[str],
    interval_sec: int = 300,
    shadow_mode: bool = True,
) -> None:
    """
    Main RVF collection loop.

    Args:
        db_path:   Path to panopticon.db
        log_path:  Path to orchestrator log. Auto-discovered if None.
        interval_sec: Collection interval (default 300s = 5 min)
        shadow_mode: If True use shadow thresholds, else production
    """
    window_min = interval_sec // 60
    collector = PipelineHealthCollector(
        db_path=db_path,
        log_path=log_path or "",
        window_minutes=window_min,
    )
    # Auto-discover log if not provided
    if not collector.log_path or not os.path.exists(collector.log_path):
        discovered = _get_latest_log_path()
        if discovered:
            collector.log_path = discovered
            print(f"[RVF] auto-discovered log: {collector.log_path}")
        else:
            print("[RVF] WARNING: no orchestrator log found — L1 log metrics will be empty")

    print(HEADER)
    mode_label = "SHADOW" if shadow_mode else "PRODUCTION"
    print(f"[RVF] starting {mode_label} mode, interval={interval_sec}s, log={collector.log_path}")

    while True:
        try:
            snap = collector.collect()
            mode = "shadow" if shadow_mode else "production"
            alerts = check_snapshot(snap, mode=mode)
            output = _render_table(snap, alerts)
            print(output)
            collector.write_to_db(snap)
            logger.info("[RVF] snapshot written: ticks=%d fires=%d paper=%d",
                        snap.l1_trade_ticks_received,
                        snap.l1_entropy_fires,
                        snap.l4_paper_trades)
        except Exception as exc:
            print(f"[RVF][ERROR] collection failed: {exc}")
            logger.warning("[RVF] collection error: %s", exc)
        await asyncio.sleep(interval_sec)


# ── CLI entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Panopticon RVF — Regular Verification Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to panopticon.db",
    )
    parser.add_argument(
        "--log",
        default=None,
        help=(
            "Path to orchestrator log file. "
            "If omitted, auto-discovers the newest logs/orchestrator_*.log"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Collection interval in seconds (default: 300 = 5 min)",
    )
    args = parser.parse_args()

    shadow_mode = os.getenv("PANOPTICON_SHADOW") == "1"
    asyncio.run(
        run_rvf_loop(
            db_path=args.db,
            log_path=args.log,
            interval_sec=args.interval,
            shadow_mode=shadow_mode,
        )
    )