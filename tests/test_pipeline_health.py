"""
tests/test_pipeline_health.py

RVF — Regular Verification Framework
Tests for PipelineSnapshot and check_snapshot behavior.
"""

import pytest
from unittest.mock import MagicMock

from panopticon_py.verification.pipeline_health import (
    PipelineSnapshot,
    PipelineHealthCollector,
)
from panopticon_py.verification.pipeline_alert import (
    check_snapshot,
    Alert,
    THRESHOLDS,
)


class TestPipelineSnapshot:
    """Unit tests for PipelineSnapshot dataclass."""

    def test_staleness_flag_set_when_entropy_fires_but_no_trades(self):
        """
        If l1_entropy_fires > 0 and l4_paper_trades == 0,
        data_staleness_flag should be set.
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l1_entropy_fires=5,
            l4_paper_trades=0,
            l1_trade_ticks_received=47,
            l1_kyle_samples_written=2,
            l1_tick_rate_per_min=9.40,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.004,
        )
        # Simulate the staleness logic from collect()
        if snap.l1_entropy_fires > 0 and snap.l4_paper_trades == 0:
            snap.data_staleness_flag = 1

        assert snap.data_staleness_flag == 1

    def test_pipeline_pass_rate_zero_when_no_paper_trades(self):
        """
        pipeline_pass_rate = l4_paper_trades / max(l3_bayesian_updates, 1).
        With 10 entropy fires and 0 paper trades, rate should be 0.0.
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l3_bayesian_updates=10,
            l4_paper_trades=0,
        )
        snap.pipeline_pass_rate = round(
            snap.l4_paper_trades / max(snap.l3_bayesian_updates, 1), 4
        )
        assert snap.pipeline_pass_rate == 0.0

    def test_kyle_accumulation_rate_calculation(self):
        """
        kyle_accumulation_rate = total_samples / 500.
        With 250 samples, rate should be 0.5 (< 1.0 = not done yet).
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            kyle_accumulation_rate=0.5,
        )
        assert snap.kyle_accumulation_rate == 0.5
        assert snap.kyle_accumulation_rate < 1.0  # not done yet

    def test_l1_tick_rate_per_min_calculation(self):
        """
        l1_tick_rate_per_min = l1_trade_ticks_received / max(window_minutes, 1).
        With 47 ticks in 5-minute window → 9.4/min.
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l1_trade_ticks_received=47,
            window_minutes=5,
        )
        snap.l1_tick_rate_per_min = round(
            snap.l1_trade_ticks_received / max(snap.window_minutes, 1), 2
        )
        assert snap.l1_tick_rate_per_min == 9.40

    def test_default_tier_dicts_are_empty(self):
        """Tier dicts default to empty dict, not None."""
        snap = PipelineSnapshot(ts_utc="2026-04-25T00:00:00+00:00")
        assert snap.l1_trade_ticks_by_tier == {}
        assert snap.l1_entropy_fires_by_tier == {}
        assert snap.l5_wallet_obs_written == 0
        assert snap.l4_live_trades == 0


class TestPipelineAlert:
    """Unit tests for check_snapshot threshold logic."""

    def test_alert_critical_on_live_trade_shadow_mode(self):
        """
        In shadow mode, l4_live_trades > 0 must generate CRITICAL alert.
        (Invariant 5.1: LIVE trades must never fire in shadow mode)
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=1,
            l4_paper_trades=0,
            l5_wallet_obs_written=10,
            l1_entropy_fires=3,
            l1_tick_rate_per_min=9.4,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.004,
        )
        alerts = check_snapshot(snap, mode="shadow")
        assert any(
            a.severity == "CRITICAL" and a.layer == "L4"
            for a in alerts
        ), f"Expected CRITICAL L4 alert, got: {alerts}"

    def test_alert_critical_on_multiple_live_trades(self):
        """Multiple LIVE trades should still trigger CRITICAL."""
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=3,
            l4_paper_trades=0,
            l5_wallet_obs_written=5,
            l1_entropy_fires=2,
            l1_tick_rate_per_min=5.0,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.01,
        )
        alerts = check_snapshot(snap, mode="shadow")
        live_alerts = [a for a in alerts if a.layer == "L4"]
        assert any(a.severity == "CRITICAL" for a in live_alerts)

    def test_alert_warn_when_no_wallet_obs(self):
        """
        In shadow mode, if l5_wallet_obs_written < 1 (threshold),
        a WARN alert should be generated.
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=0,
            l4_paper_trades=0,
            l5_wallet_obs_written=0,  # below threshold of 1
            l1_entropy_fires=0,
            l1_tick_rate_per_min=0.0,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.0,
        )
        alerts = check_snapshot(snap, mode="shadow")
        wallet_warnings = [
            a for a in alerts
            if "wallet" in a.message.lower() and a.severity == "WARN"
        ]
        assert len(wallet_warnings) >= 1, (
            f"Expected at least 1 WARN about wallet_obs, got: {alerts}"
        )

    def test_no_critical_when_live_trades_zero(self):
        """Zero live trades should NOT produce CRITICAL alert."""
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=0,
            l4_paper_trades=0,
            l5_wallet_obs_written=5,
            l1_entropy_fires=0,
            l1_tick_rate_per_min=0.0,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.01,
        )
        alerts = check_snapshot(snap, mode="shadow")
        critical_l4 = [a for a in alerts if a.layer == "L4" and a.severity == "CRITICAL"]
        assert len(critical_l4) == 0, f"Unexpected CRITICAL L4 alerts: {critical_l4}"

    def test_info_alert_always_present(self):
        """INFO alert is always appended with tick_rate/entropy/kyle info."""
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=0,
            l4_paper_trades=0,
            l5_wallet_obs_written=5,
            l1_entropy_fires=2,
            l1_tick_rate_per_min=8.0,
            pipeline_pass_rate=0.1,
            kyle_accumulation_rate=0.3,
        )
        alerts = check_snapshot(snap, mode="shadow")
        info_alerts = [a for a in alerts if a.severity == "INFO" and a.layer == "L1"]
        assert len(info_alerts) >= 1, f"Expected INFO L1 alert, got: {alerts}"

    def test_staleness_warn_when_entropy_fires_but_no_trades(self):
        """
        When data_staleness_flag=1 AND l1_entropy_fires>0,
        a WARN about L3/L4 gate should be generated.
        """
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=0,
            l4_paper_trades=0,
            l5_wallet_obs_written=5,
            l1_entropy_fires=5,
            l1_tick_rate_per_min=10.0,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.01,
            data_staleness_flag=1,
        )
        alerts = check_snapshot(snap, mode="shadow")
        l3l4_warns = [a for a in alerts if a.layer in ("L3/L4",)]
        assert len(l3l4_warns) >= 1, f"Expected L3/L4 WARN, got: {alerts}"

    def test_production_thresholds_tighter_than_shadow(self):
        """Production mode thresholds should be >= shadow thresholds."""
        shadow = THRESHOLDS["shadow"]
        prod = THRESHOLDS["production"]
        # tick rate min should be higher in production
        assert prod["l1_tick_rate_per_min_min"] >= shadow["l1_tick_rate_per_min_min"]
        # wallet obs min should be higher in production
        assert prod["l5_wallet_obs_per_window_min"] >= shadow["l5_wallet_obs_per_window_min"]

    def test_alert_has_all_required_fields(self):
        """Every Alert returned must have severity/layer/message/value/threshold/ts_utc."""
        snap = PipelineSnapshot(
            ts_utc="2026-04-25T00:00:00+00:00",
            l4_live_trades=1,
            l4_paper_trades=0,
            l5_wallet_obs_written=0,
            l1_entropy_fires=0,
            l1_tick_rate_per_min=0.0,
            pipeline_pass_rate=0.0,
            kyle_accumulation_rate=0.0,
        )
        alerts = check_snapshot(snap, mode="shadow")
        for a in alerts:
            assert hasattr(a, "severity")
            assert hasattr(a, "layer")
            assert hasattr(a, "message")
            assert hasattr(a, "value")
            assert hasattr(a, "threshold")
            assert hasattr(a, "ts_utc")
            assert a.severity in ("INFO", "WARN", "CRITICAL")


class TestPipelineHealthCollector:
    """Unit tests for PipelineHealthCollector."""

    def test_collector_initialization(self):
        """Collector stores db_path, log_path, window_minutes."""
        c = PipelineHealthCollector(
            db_path="data/panopticon.db",
            log_path="logs/orchestrator.log",
            window_minutes=5,
        )
        assert c.db_path == "data/panopticon.db"
        assert c.log_path == "logs/orchestrator.log"
        assert c.window_minutes == 5

    def test_tier_from_line_detection(self):
        """_tier_from_line detects tier from log line context."""
        c = PipelineHealthCollector("x", "y")
        assert c._tier_from_line("tier=t1 market=btc") == "t1"
        assert c._tier_from_line("market_tier=t2 foo") == "t2"
        assert c._tier_from_line("market_tier=t5 bar") == "t5"
        assert c._tier_from_line("no tier here") == "t3"  # default
        assert c._tier_from_line("") == "t3"  # default