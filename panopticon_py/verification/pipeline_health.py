"""
panopticon_py/verification/pipeline_health.py

RVF — Regular Verification Framework
PipelineHealthCollector: non-invasive metrics collector.

Reads from existing DB tables and orchestrator log file.
Does NOT instrument the hot path (L1-L4).
Does NOT write to any table except its own pipeline_health table.

The 6 failure points measured:
  L1-IN:  trade tick arrives on WS (log parsing)
  L1-OUT: EntropyWindow fires z-score (log parsing + DB)
  L2:     SignalEvent queued (log parsing — queue.put lines)
  L3:     Bayesian update fires / EV gate (DB: hunting_shadow_hits)
  L4:     Paper trade written (DB: execution_records mode=PAPER)
  L5:     Wallet observation written (DB: wallet_observations)
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


@dataclass
class PipelineSnapshot:
    """Immutable snapshot of pipeline health at a point in time."""
    ts_utc: str
    window_minutes: int = 5

    # L1
    l1_trade_ticks_received: int = 0
    l1_trade_ticks_by_tier: dict = field(default_factory=dict)
    l1_entropy_fires: int = 0
    l1_entropy_fires_by_tier: dict = field(default_factory=dict)
    l1_kyle_samples_written: int = 0

    # L2/L3
    l2_signal_events_queued: int = 0
    l3_bayesian_updates: int = 0
    l3_gate_pass: int = 0
    l3_gate_reject: int = 0

    # L4
    l4_paper_trades: int = 0
    l4_live_trades: int = 0

    # L5
    l5_wallet_obs_written: int = 0
    l5_insider_score_updates: int = 0

    # Derived
    l1_tick_rate_per_min: float = 0.0
    pipeline_pass_rate: float = 0.0
    kyle_accumulation_rate: float = 0.0
    data_staleness_flag: int = 0
    notes: list = field(default_factory=list)

    # Internal: db_path for cross-table queries in alert phase
    _db_path: Optional[str] = field(default=None, repr=False)


class PipelineHealthCollector:
    """
    Non-invasive pipeline metrics collector.

    Reads:  kyle_lambda_samples, hunting_shadow_hits, execution_records,
            wallet_observations, insider_score_snapshots
    Parses: orchestrator log file (last 5000 lines)
    Writes: pipeline_health table (own table only)
    """

    KYLE_TARGET = 500  # target kyle samples for calibration

    def __init__(
        self,
        db_path: str,
        log_path: str,
        window_minutes: int = 5,
    ) -> None:
        self.db_path = db_path
        self.log_path = log_path
        self.window_minutes = window_minutes

    def collect(self) -> PipelineSnapshot:
        """
        Collect all metrics for the rolling window.
        Returns a PipelineSnapshot with all fields populated.
        """
        snap = PipelineSnapshot(
            ts_utc=datetime.now(timezone.utc).isoformat(),
            window_minutes=self.window_minutes,
            _db_path=self.db_path,
        )
        window_start = datetime.now(timezone.utc) - timedelta(minutes=self.window_minutes)
        ws_str = window_start.strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # ── L1: kyle_lambda_samples ─────────────────────────────────
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM kyle_lambda_samples WHERE ts_utc > ?",
                (ws_str,),
            ).fetchone()
            snap.l1_kyle_samples_written = row["cnt"] or 0

            # ── L3: hunting_shadow_hits (proxy for bayesian_updates) ─────
            row2 = conn.execute(
                "SELECT COUNT(*) as cnt FROM hunting_shadow_hits WHERE ts_utc > ?",
                (ws_str,),
            ).fetchone()
            snap.l3_bayesian_updates = row2["cnt"] or 0

            # ── L4: execution_records by mode ──────────────────────────────
            rows = conn.execute(
                "SELECT mode, COUNT(*) as cnt FROM execution_records "
                "WHERE created_ts_utc > ? GROUP BY mode",
                (ws_str,),
            ).fetchall()
            for row in rows:
                if row["mode"] == "PAPER":
                    snap.l4_paper_trades = row["cnt"] or 0
                elif row["mode"] == "LIVE":
                    snap.l4_live_trades = row["cnt"] or 0

            # ── L5: wallet_observations ─────────────────────────────────
            row3 = conn.execute(
                "SELECT COUNT(*) as cnt FROM wallet_observations WHERE observed_at_utc > ?",
                (ws_str,),
            ).fetchone()
            snap.l5_wallet_obs_written = row3["cnt"] or 0

            # ── L5: insider_score_snapshots ────────────────────────────────
            row4 = conn.execute(
                "SELECT COUNT(*) as cnt FROM insider_score_snapshots WHERE snapshot_ts_utc > ?",
                (ws_str,),
            ).fetchone()
            snap.l5_insider_score_updates = row4["cnt"] or 0

            # ── Derived: kyle accumulation rate (cumulative) ─────────────
            row5 = conn.execute(
                "SELECT COUNT(*) as cnt FROM kyle_lambda_samples"
            ).fetchone()
            total_kyle = row5["cnt"] or 0
            snap.kyle_accumulation_rate = round(
                total_kyle / self.KYLE_TARGET, 4
            )

        # ── Log parsing ──────────────────────────────────────────────────
        snap = self._parse_log_metrics(snap, window_start)

        # ── Derived metrics ─────────────────────────────────────────────
        snap.l1_tick_rate_per_min = round(
            snap.l1_trade_ticks_received / max(self.window_minutes, 1), 2
        )
        snap.pipeline_pass_rate = round(
            snap.l4_paper_trades / max(snap.l3_bayesian_updates, 1), 4
        )

        # ── Staleness flags ────────────────────────────────────────────
        if snap.l1_entropy_fires > 0 and snap.l4_paper_trades == 0:
            snap.data_staleness_flag = 1
            snap.notes.append(
                "WARN: entropy fires but 0 paper trades — "
                "check EV gate or fast_gate thresholds"
            )
        if snap.l4_live_trades > 0:
            snap.data_staleness_flag = 1
            snap.notes.append(
                f"CRITICAL: {snap.l4_live_trades} LIVE trades detected "
                "— LIVE_TRADING must be OFF (Invariant 5.1)"
            )

        return snap

    # ── Log parsing ──────────────────────────────────────────────────────

    def _parse_log_metrics(
        self,
        snap: PipelineSnapshot,
        window_start: datetime,
    ) -> PipelineSnapshot:
        """
        Parse last 5000 lines of the orchestrator log.
        Extracts: DIAG TRADE_TICK, ENTROPY_FIRE, L1_SUBSCRIPTION, queue.put lines.
        Filters to window_start onwards using log timestamp.
        """
        try:
            with open(self.log_path, "r", errors="replace") as f:
                lines = f.readlines()[-5000:]
        except (FileNotFoundError, IOError) as exc:
            snap.notes.append(f"WARN: log file unreadable ({self.log_path}): {exc}")
            return snap

        tier_ticks = {"t1": 0, "t2": 0, "t3": 0, "t5": 0}
        tier_entropy = {"t1": 0, "t2": 0, "t3": 0, "t5": 0}
        total_ticks = 0
        total_entropy = 0
        l2_queue_puts = 0

        for line in lines:
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if not ts_match:
                continue
            try:
                line_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                line_ts = line_ts.replace(tzinfo=timezone.utc)
                if line_ts < window_start:
                    continue
            except ValueError:
                continue

            # L1-IN: trade tick
            if "[DIAG][TRADE_TICK]" in line or "last_trade_price" in line:
                total_ticks += 1
                tier = self._tier_from_line(line, default="t3")
                tier_ticks[tier] += 1

            # L1-OUT: entropy fire
            if "[DIAG][ENTROPY_FIRE]" in line or "ENTROPY_FIRE" in line:
                total_entropy += 1
                tier = self._tier_from_line(line, default="t3")
                tier_entropy[tier] += 1

            # L2: queue.put
            if "queue.put" in line or "signal_queue" in line or "[SE][QUEUE]" in line:
                l2_queue_puts += 1

        snap.l1_trade_ticks_received = total_ticks
        snap.l1_trade_ticks_by_tier = tier_ticks
        snap.l1_entropy_fires = total_entropy
        snap.l1_entropy_fires_by_tier = tier_entropy
        snap.l2_signal_events_queued = l2_queue_puts
        return snap

    @staticmethod
    def _tier_from_line(line: str, default: str = "t3") -> str:
        """Detect tier from a log line's context."""
        for tier in ["t1", "t2", "t3", "t5"]:
            if f"tier={tier}" in line or f"market_tier={tier}" in line:
                return tier
        return default

    # ── DB write ────────────────────────────────────────────────────────

    def write_to_db(self, snap: PipelineSnapshot) -> None:
        """Write a snapshot to the pipeline_health table."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pipeline_health (
                    ts_utc, window_minutes,
                    l1_trade_ticks_received, l1_trade_ticks_by_tier,
                    l1_entropy_fires, l1_entropy_fires_by_tier,
                    l1_kyle_samples_written,
                    l2_signal_events_queued,
                    l3_bayesian_updates, l3_gate_pass, l3_gate_reject,
                    l4_paper_trades, l4_live_trades,
                    l5_wallet_obs_written, l5_insider_score_updates,
                    l1_tick_rate_per_min, pipeline_pass_rate,
                    kyle_accumulation_rate, data_staleness_flag, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snap.ts_utc,
                    snap.window_minutes,
                    snap.l1_trade_ticks_received,
                    json.dumps(snap.l1_trade_ticks_by_tier),
                    snap.l1_entropy_fires,
                    json.dumps(snap.l1_entropy_fires_by_tier),
                    snap.l1_kyle_samples_written,
                    snap.l2_signal_events_queued,
                    snap.l3_bayesian_updates,
                    snap.l3_gate_pass,
                    snap.l3_gate_reject,
                    snap.l4_paper_trades,
                    snap.l4_live_trades,
                    snap.l5_wallet_obs_written,
                    snap.l5_insider_score_updates,
                    snap.l1_tick_rate_per_min,
                    snap.pipeline_pass_rate,
                    snap.kyle_accumulation_rate,
                    snap.data_staleness_flag,
                    json.dumps(snap.notes),
                ),
            )
            conn.commit()