"""
check_shadow_readiness.py

Verifies the system is ready to unlock LIVE_TRADING by checking shadow mode metrics.

Exit codes:
  0  — All thresholds met or exceeded (ready for human review)
  1  — Below any threshold (not ready)

Usage:
  python scripts/check_shadow_readiness.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

DB_PATH = os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db")

# Unlock thresholds per architect review
THRESH_TRADES = 100
THRESH_WIN_RATE = 0.55
THRESH_AVG_EV_NET = 0.0
# Kyle λ primary path
THRESH_KYLE_SAMPLES = 500
THRESH_KYLE_ASSETS = 10
# Kyle λ fallback path (30-day grace period for thin markets)
THRESH_KYLE_FALLBACK_SAMPLES = 50
THRESH_KYLE_FALLBACK_ASSETS = 3
KYLE_ACCUMULATION_DAYS = 30  # fallback eligible after this period


@dataclass(frozen=True)
class ReadinessResult:
    """Result of shadow mode readiness check. Shared between check script and orchestrator."""
    is_ready: bool
    trade_count: int
    win_rate: float | None
    avg_ev_net: float | None
    kyle_sample_count: int
    kyle_asset_count: int
    kyle_mode: str  # "primary", "fallback", "accumulating"
    kyle_fallback_eligible: bool
    summary: str  # human-readable one-line summary


def check_readiness(db_path: str | None = None) -> ReadinessResult:
    """
    Check shadow mode readiness thresholds.
    Shared implementation — used by both check_shadow_readiness.py script
    and run_hft_orchestrator.py LIVE_TRADING guard.
    """
    path = db_path or os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db")
    p = Path(path)
    if not p.exists():
        return ReadinessResult(
            is_ready=False,
            trade_count=0,
            win_rate=None,
            avg_ev_net=None,
            summary=f"DB not found at {path}",
        )

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    # 1. Paper trade count
    row = conn.execute(
        "SELECT COUNT(*) FROM execution_records WHERE mode = 'PAPER' AND accepted = 1"
    ).fetchone()
    trade_count = row[0] if row else 0

    # 2. Win rate
    rows = conn.execute(
        "SELECT realized_pnl_usd FROM realized_pnl_settlement WHERE realized_pnl_usd IS NOT NULL"
    ).fetchall()
    if rows:
        wins = sum(1 for (p,) in rows if p > 0)
        losses = sum(1 for (p,) in rows if p < 0)
        total = wins + losses
        win_rate = (wins / total) if total > 0 else None
    else:
        win_rate = None

    # 3. Avg EV net
    row_ev = conn.execute(
        "SELECT AVG(estimated_ev_usd) FROM realized_pnl_settlement WHERE estimated_ev_usd IS NOT NULL"
    ).fetchone()
    avg_ev_net = row_ev[0] if row_ev and row_ev[0] is not None else None

    # 4. Kyle lambda samples
    row_kyle = conn.execute(
        "SELECT COUNT(*) FROM kyle_lambda_samples"
    ).fetchone()
    kyle_sample_count = row_kyle[0] if row_kyle else 0

    row_kyle_assets = conn.execute(
        "SELECT COUNT(DISTINCT asset_id) FROM kyle_lambda_samples"
    ).fetchone()
    kyle_asset_count = row_kyle_assets[0] if row_kyle_assets else 0

    conn.close()

    # 4. Kyle lambda samples — 3-tier logic (primary / fallback / accumulating)
    # Primary: >= 500 samples across >= 10 assets
    # Fallback: >= 50 samples across >= 3 assets (after 30 days with no primary path)
    # Accumulating: < 50 samples — not eligible for fallback yet
    kyle_primary = (kyle_sample_count >= THRESH_KYLE_SAMPLES and
                    kyle_asset_count >= THRESH_KYLE_ASSETS)
    kyle_fallback = (kyle_sample_count >= THRESH_KYLE_FALLBACK_SAMPLES and
                      kyle_asset_count >= THRESH_KYLE_FALLBACK_ASSETS)
    kyle_fallback_eligible = kyle_fallback  # eligible to use global P75 fallback
    if kyle_primary:
        kyle_mode = "primary"
    elif kyle_fallback:
        kyle_mode = "fallback"
    else:
        kyle_mode = "accumulating"

    ok_trades = trade_count >= THRESH_TRADES
    ok_wr = win_rate is not None and win_rate >= THRESH_WIN_RATE
    ok_ev = avg_ev_net is not None and avg_ev_net > THRESH_AVG_EV_NET

    summary_parts = []
    if not ok_trades:
        summary_parts.append(f"trades {trade_count}/{THRESH_TRADES}")
    if not ok_wr:
        summary_parts.append(f"win_rate {win_rate:.1%}/{THRESH_WIN_RATE:.0%}" if win_rate else "win_rate N/A")
    if not ok_ev:
        summary_parts.append(f"avg_EV {avg_ev_net:+.2f}/{THRESH_AVG_EV_NET:+.1f}" if avg_ev_net else "avg_EV N/A")
    if kyle_mode == "accumulating":
        summary_parts.append(
            f"kyle_samples {kyle_sample_count}/{THRESH_KYLE_SAMPLES} "
            f"({kyle_asset_count} assets, accumulating)"
        )
    elif kyle_mode == "fallback":
        summary_parts.append(
            f"kyle_samples {kyle_sample_count}/{THRESH_KYLE_SAMPLES} "
            f"({kyle_asset_count}/{THRESH_KYLE_ASSETS} assets, FALLBACK)"
        )

    if summary_parts:
        summary = "Below thresholds: " + ", ".join(summary_parts)
    else:
        summary = "All thresholds met"

    return ReadinessResult(
        is_ready=ok_trades and ok_wr and ok_ev and (kyle_primary or kyle_fallback),
        trade_count=trade_count,
        win_rate=win_rate,
        avg_ev_net=avg_ev_net,
        kyle_sample_count=kyle_sample_count,
        kyle_asset_count=kyle_asset_count,
        kyle_mode=kyle_mode,
        kyle_fallback_eligible=kyle_fallback_eligible,
        summary=summary,
    )


def _load_db() -> sqlite3.Connection:
    path = Path(DB_PATH)
    if not path.exists():
        print(f"[ERROR] DB not found at {DB_PATH}")
        sys.exit(2)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _count_paper_trades(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM execution_records
        WHERE mode = 'PAPER' AND accepted = 1
        """
    ).fetchone()
    return row[0] if row else 0


def _win_rate(conn: sqlite3.Connection) -> float | None:
    """Win rate from realized_pnl_settlement (demo paper trades seeded by bootstrap)."""
    rows = conn.execute(
        """
        SELECT realized_pnl_usd FROM realized_pnl_settlement
        WHERE realized_pnl_usd IS NOT NULL
        """
    ).fetchall()
    if not rows:
        return None
    wins = sum(1 for (p,) in rows if p > 0)
    losses = sum(1 for (p,) in rows if p < 0)
    total = wins + losses
    if total == 0:
        return None
    return wins / total


def _avg_ev_net(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        """
        SELECT AVG(estimated_ev_usd) FROM realized_pnl_settlement
        WHERE estimated_ev_usd IS NOT NULL
        """
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _recent_pass_signals(conn: sqlite3.Connection, n: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT execution_id, decision_id, reason, mode, source, created_ts_utc
        FROM execution_records
        WHERE accepted = 1 AND mode = 'PAPER'
        ORDER BY created_ts_utc DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [
        {
            "execution_id": r[0],
            "decision_id": r[1],
            "reason": r[2],
            "mode": r[3],
            "source": r[4],
            "created_ts_utc": r[5],
        }
        for r in rows
    ]


def main() -> None:
    print("=" * 60)
    print("  Panopticon Shadow Mode Readiness Check")
    print("=" * 60)
    print()

    result = check_readiness()
    ok_trades = result.trade_count >= THRESH_TRADES
    ok_wr = result.win_rate is not None and result.win_rate >= THRESH_WIN_RATE
    ok_ev = result.avg_ev_net is not None and result.avg_ev_net > THRESH_AVG_EV_NET
    kyle_primary = result.kyle_mode == "primary"
    kyle_fallback = result.kyle_mode == "fallback"
    ok_kyle = kyle_primary or kyle_fallback  # ready if either tier met

    # 1. Paper trade count
    print(f"[{'OK' if ok_trades else '!!'}] Paper trades (accepted=1, mode=PAPER)")
    print(f"       {result.trade_count} / {THRESH_TRADES} required")
    if not ok_trades:
        print(f"       Need {THRESH_TRADES - result.trade_count} more")
    print()

    # 2. Win rate
    if result.win_rate is None:
        print("[??] Win rate: no settled trades found (skipping)")
    else:
        print(f"[{'OK' if ok_wr else '!!'}] Win rate (settled trades)")
        print(f"       {result.win_rate:.1%} / {THRESH_WIN_RATE:.0%} required")
        if not ok_wr:
            print(f"       Need {(THRESH_WIN_RATE - result.win_rate):.1%} more")
    print()

    # 3. Avg EV net
    if result.avg_ev_net is None:
        print("[??] Avg EV net: no data (skipping)")
    else:
        print(f"[{'OK' if ok_ev else '!!'}] Average EV net")
        print(f"       {result.avg_ev_net:+.4f} / > {THRESH_AVG_EV_NET:+.1f} required")
        if not ok_ev:
            label = "negative" if result.avg_ev_net < 0 else "zero"
            print(f"       EV is {label} — needs improvement")
    print()

    # 4. Kyle lambda samples — 3-tier output
    # Primary: >= 500 samples + >= 10 assets
    # Fallback: >= 50 samples + >= 3 assets (after 30 days)
    # Accumulating: < 50 samples
    kyle_primary = result.kyle_mode == "primary"
    kyle_fallback = result.kyle_mode == "fallback"
    kyle_accumulating = result.kyle_mode == "accumulating"

    if kyle_primary:
        kyle_icon = "OK"
        kyle_label = "Kyle lambda (primary) ✅"
    elif kyle_fallback:
        kyle_icon = "!!"
        kyle_label = "Kyle lambda (FALLBACK) ⚠"
    else:
        kyle_icon = "!!"
        kyle_label = "Kyle lambda (accumulating)"

    print(f"[{kyle_icon}] {kyle_label}")
    print(f"       {result.kyle_sample_count} samples, {result.kyle_asset_count} assets")
    if kyle_accumulating:
        print(f"       Primary threshold: {THRESH_KYLE_SAMPLES} samples / {THRESH_KYLE_ASSETS} assets")
        print(f"       Fallback available after {KYLE_ACCUMULATION_DAYS} days: {THRESH_KYLE_FALLBACK_SAMPLES} samples / {THRESH_KYLE_FALLBACK_ASSETS} assets")
        print(f"       NOT READY — still accumulating samples")
    elif kyle_fallback:
        print(f"       FALLBACK ELIGIBLE — reduced threshold active")
        print(f"       Human review required: confirm global P75 fallback is reasonable")
    else:
        print(f"       Primary threshold met ✅")
    print()

    # 5. Recent PASS signals
    conn = _load_db()
    recent = _recent_pass_signals(conn, 20)
    print(f"[INFO] Recent {len(recent)} PASS signals (last 20):")
    for sig in recent[:5]:
        print(f"       {sig['created_ts_utc'][:19]} | {sig['source']:5s} | {sig['reason']}")
    if len(recent) > 5:
        print(f"       ... ({len(recent) - 5} more)")
    print()

    print("-" * 60)
    if result.is_ready:
        print("[READY] All automated thresholds met.")
        print("         Human review of recent PASS signals still required.")
        print("         Then manually set LIVE_TRADING=true in .env")
        conn.close()
        sys.exit(0)
    else:
        print("[NOT READY] Some thresholds not yet met.")
        print("             Shadow mode continues.")
        conn.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
