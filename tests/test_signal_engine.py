"""Tests for signal_engine._process_event() Phase 2-C-2 changes.

Key behaviors tested:
  1. execution_records written with posterior + p_adj on all paths
  2. submit_fok_order called on Gate EXECUTE/DEGRADE
  3. submit_fok_order NOT called on Gate ABORT
  4. db.update_execution_clob_result called after CLOB returns
  5. avg_entry_price read from wallet_market_positions (or 0.0 if None)

Note: GateDecision uses EXECUTE (not PASS), DEGRADE, ABORT.
MIN_ENTROPY_Z_THRESHOLD = -4.0 by default, so z must be <= -4.1 to pass the z-score check.
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from panopticon_py.execution.constants import REASON_Z_MAGNITUDE_BELOW_THRESHOLD
from panopticon_py.signal_engine import SignalEvent, _process_event
from panopticon_py.fast_gate import GateDecision


class DummySnap:
    def __init__(self):
        self.network_ping_ms = 50.0
        self.current_base_fee = 0.0015
        self.kyle_lambda = 0.000012
        self.gas_cost_estimate = 0.25
        self.api_health = "ok"
        self.l2_timeout_ms = 0.0
        self.degraded = False
        self.kelly_cap = 0.25
        self.last_update_ts = 0.0


class DummyDB:
    """In-memory record-keeper mimicking ShadowDB for unit testing."""

    def __init__(self):
        self.execution_records: list[dict] = []
        self.execution_clob_updates: list[dict] = []
        self.position_return_value = None

    def append_execution_record(self, row: dict) -> None:
        self.execution_records.append(dict(row))

    def update_execution_clob_result(self, **kwargs) -> None:
        self.execution_clob_updates.append(kwargs)

    def get_wallet_market_position(self, wallet_address: str, market_id: str):
        return self.position_return_value


def make_event(z: float = -5.5, market_id: str = "mkt-001",
               source: str = "radar", trigger_address: str = "0xabc",
               token_id: str = "tkn-001"):
    """Radar-style entropy z (signed); ``SignalEvent.z`` is ``abs(entropy_z)`` for L2 magnitude."""
    return SignalEvent(
        source=source,  # type: ignore[arg-type]
        market_id=market_id,
        token_id=token_id,
        entropy_z=z,
        trigger_address=trigger_address,
        market_tier="t3",
    )


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestProcessEventWritesPosteriorAndPAdj:
    """Task 4: test_process_event_writes_posterior_and_p_adj."""

    @pytest.mark.asyncio
    async def test_gate_execute_writes_execution_records_with_posterior_and_p_adj(self):
        """
        Gate EXECUTE → execution_records contains both posterior (Bayesian raw output)
        and p_adj (gate-adjusted probability).
        """
        db = DummyDB()
        db.position_return_value = {"avg_entry_price": 0.55}
        event = make_event(z=-5.5)

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=True, clob_order_id="clob-123", tx_hash=None,
                reason="PASS", request_id="req-123"
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.EXECUTE
            mock_gate.reason = "PASS"
            mock_gate.p_adjusted = 0.72
            mock_gate.ev_net = 1.25
            mock_snapshot.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert len(db.execution_records) == 1, f"Expected 1 record, got {len(db.execution_records)}"
        rec = db.execution_records[0]
        assert "posterior" in rec, f"Missing posterior in {rec.keys()}"
        assert "p_adj" in rec, f"Missing p_adj in {rec.keys()}"
        assert "qty" in rec, f"Missing qty in {rec.keys()}"
        assert "ev_net" in rec, f"Missing ev_net in {rec.keys()}"
        assert "avg_entry_price" in rec, f"Missing avg_entry_price in {rec.keys()}"
        assert rec["posterior"] > 0, f"posterior should be > 0, got {rec['posterior']}"
        assert rec["p_adj"] == 0.72, f"p_adj should be 0.72, got {rec['p_adj']}"
        assert rec["ev_net"] == 1.25

    @pytest.mark.asyncio
    async def test_insufficient_consensus_writes_posterior_zero(self):
        """Insufficient consensus → accepted=0, posterior=0.0, no CLOB call."""
        # Only 1 source < MIN_CONSENSUS=2 → ABORT before gate
        db = DummyDB()
        event = make_event(z=-5.5)

        with patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            await _process_event(event, db)

        assert len(db.execution_records) == 1
        rec = db.execution_records[0]
        assert rec["accepted"] == 0
        assert rec["reason"] == "INSUFFICIENT_CONSENSUS"
        assert rec["posterior"] == 0.0
        assert rec["p_adj"] == 0.0
        assert mock_clob.call_count == 0, "submit_fok_order should not be called on ABORT"


class TestProcessEventCallsSubmitFokOrderOnPass:
    """Task 4: test_process_event_calls_submit_fok_order_on_pass."""

    @pytest.mark.asyncio
    async def test_gate_degrade_calls_submit_fok_order(self):
        """Gate DEGRADE → submit_fok_order IS called."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=True, clob_order_id="clob-deg", tx_hash=None,
                reason="DRY_RUN", request_id="req-deg"
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.DEGRADE
            mock_gate.reason = "KELLY_CAP"
            mock_gate.p_adjusted = 0.65
            mock_gate.ev_net = 0.80
            mock_snapshot.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        mock_clob.assert_called_once()
        call_kwargs = mock_clob.call_args.kwargs
        assert call_kwargs["decision_id"] is not None
        assert call_kwargs["market_id"] == event.market_id
        assert call_kwargs["side"] == "BUY"
        assert call_kwargs["dry_run"] is True  # default since LIVE_TRADING not set

    @pytest.mark.asyncio
    async def test_gate_execute_calls_submit_fok_order(self):
        """Gate EXECUTE → submit_fok_order IS called."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=True, clob_order_id="clob-pass", tx_hash=None,
                reason="PASS", request_id="req-pass"
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.EXECUTE
            mock_gate.reason = "PASS"
            mock_gate.p_adjusted = 0.75
            mock_gate.ev_net = 1.5
            mock_snapshot.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert mock_clob.call_count == 1


class TestProcessEventDoesNotCallSubmitFokOrderOnAbort:
    """Task 4: test_process_event_does_not_call_submit_fok_order_on_abort."""

    @pytest.mark.asyncio
    async def test_gate_abort_does_not_call_submit_fok_order(self):
        """Gate ABORT (e.g. LOW_EV) → submit_fok_order NOT called, but record written."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.ABORT
            mock_gate.reason = "LOW_EV"
            mock_gate.p_adjusted = 0.45
            mock_gate.ev_net = -0.5
            mock_snapshot.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert mock_clob.call_count == 0
        assert len(db.execution_records) == 1
        rec = db.execution_records[0]
        assert rec["accepted"] == 0
        assert "LOW_EV" in rec["reason"]

    @pytest.mark.asyncio
    async def test_no_price_data_abort_does_not_call_submit_fok_order(self):
        """REASON_NO_PRICE_DATA → ABORT without CLOB call."""
        db = DummyDB()
        event = make_event()

        with patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine._get_current_price", return_value=None), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            await _process_event(event, db)

        assert mock_clob.call_count == 0
        assert len(db.execution_records) == 1


class TestClobUpdateWritesClobOrderId:
    """Task 4: test_clob_update_writes_clob_order_id."""

    @pytest.mark.asyncio
    async def test_clob_update_writes_clob_order_id_on_pass(self):
        """CLOB result accepted → update_execution_clob_result called with clob_order_id."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=True,
                clob_order_id="clob-xyz-789",
                tx_hash="0xabc123",
                reason="PASS",
                request_id="req-xyz",
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.EXECUTE
            mock_gate.reason = "PASS"
            mock_gate.p_adjusted = 0.75
            mock_gate.ev_net = 1.5
            mock_snapshot.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert len(db.execution_clob_updates) == 1, f"Expected 1 update, got {len(db.execution_clob_updates)}"
        upd = db.execution_clob_updates[0]
        assert upd["clob_order_id"] == "clob-xyz-789"
        assert upd["tx_hash"] == "0xabc123"
        assert upd["settlement_status"] == "pending_submit"

    @pytest.mark.asyncio
    async def test_clob_update_writes_rejected_on_reject(self):
        """CLOB result rejected → update_execution_clob_result called with settlement_status=rejected."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=False,
                clob_order_id=None,
                tx_hash=None,
                reason="CLOB_REJECT",
                request_id="req-rej",
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.EXECUTE
            mock_gate.reason = "PASS"
            mock_gate.p_adjusted = 0.72
            mock_gate.ev_net = 1.1
            mock_snapshot.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert len(db.execution_clob_updates) == 1
        upd = db.execution_clob_updates[0]
        assert upd["settlement_status"] == "rejected"
        assert upd["reason"] == "CLOB_REJECT"


class TestAvgEntryPriceFromWalletPosition:
    """
    D64 Q1 ruling: avg_entry_price = CLOB /book asks[0].price (best_ask).
    The wallet_market_positions avg_entry_price is only used as prev_avg_entry
    for the NO_PRICE_DATA path (when best_ask returns None → NO_TRADE).
    """

    @pytest.mark.asyncio
    async def test_avg_entry_price_from_best_ask_not_wallet(self):
        """
        D64 Q1: avg_entry_price = best_ask (CLOB), not wallet_market_positions.
        wallet_market_positions only feeds prev_avg_entry (used on NO_PRICE_DATA path).
        """
        db = DummyDB()
        db.position_return_value = {"avg_entry_price": 0.42}  # wallet has 0.42
        event = make_event()

        captured_input = None

        def capture_gate(inp, snapshot):
            nonlocal captured_input
            captured_input = inp
            mock_result = MagicMock()
            mock_result.decision = GateDecision.ABORT
            mock_result.reason = "LOW_EV"
            mock_result.p_adjusted = 0.5
            mock_result.ev_net = -0.1
            return mock_result

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.fast_execution_gate", side_effect=capture_gate) as mock_gate_fn:

            mock_snapshot.return_value = DummySnap()
            await _process_event(event, db)

        assert captured_input is not None, "fast_execution_gate was never called"
        # D64 Q1: avg_entry_price = best_ask (conftest mock returns 0.50), NOT wallet position
        assert captured_input.avg_entry_price == 0.50, \
            f"Expected avg_entry_price=0.50 from best_ask, got {captured_input.avg_entry_price}"

    @pytest.mark.asyncio
    async def test_avg_entry_price_no_trade_when_best_ask_unavailable(self):
        """
        D64 Q1: If best_ask returns None → NO_TRADE (do not fall back to 0.5).
        In this case prev_avg_entry from wallet_market_positions is NOT used.
        """
        db = DummyDB()
        db.position_return_value = {"avg_entry_price": 0.42}
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snapshot, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.fetch_best_ask", return_value=None), \
             patch("panopticon_py.signal_engine.fast_execution_gate") as mock_gate:

            mock_snapshot.return_value = DummySnap()
            await _process_event(event, db)

        # NO_TRADE: execution record written with accepted=0, reason=NO_PRICE_DATA
        assert len(db.execution_records) == 1
        rec = db.execution_records[0]
        assert rec["accepted"] == 0
        assert rec["gate_reason"] == "NO_PRICE_DATA"
        # fast_execution_gate should NOT be called when best_ask is None
        mock_gate.assert_not_called()


# ── Z-Score Regression Tests (D8) ────────────────────────────────────────────
#
# Bug: original code was "z < abs(MIN_ENTROPY_Z_THRESHOLD)"
# which silently dropped ALL negative z values (z=-5.5 < 4.0 = True always).
# Fixed: "abs(z) < abs(MIN_ENTROPY_Z_THRESHOLD)"
#   |z| < 4.0 → skip (weak signal)
#   |z| >= 4.0 → continue (strong signal)

class TestZScoreRegression:
    """D8: z-score filtering regression tests — ensure strong signals reach the gate."""

    @pytest.mark.asyncio
    async def test_strong_negative_z_is_not_filtered(self):
        """z=-5.5, |z|=5.5 >= 4.0 → signal reaches gate (not filtered)."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event(z=-5.5)

        reached_gate = False

        def gate_capture(inp, snapshot):
            nonlocal reached_gate
            reached_gate = True
            mock_result = MagicMock()
            mock_result.decision = GateDecision.ABORT
            mock_result.reason = "LOW_EV"
            mock_result.p_adjusted = 0.5
            mock_result.ev_net = -0.1
            return mock_result

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snap, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.fast_execution_gate", side_effect=gate_capture):
            mock_snap.return_value = DummySnap()
            await _process_event(event, db)

        assert reached_gate, "z=-5.5 is a strong signal (|z|=5.5 >= 4.0) and must NOT be filtered"

    @pytest.mark.asyncio
    async def test_weak_negative_z_is_filtered(self):
        """z=-2.0, |z|=2.0 < 4.0 → signal is filtered (early return, no gate call)."""
        db = DummyDB()
        event = make_event(z=-2.0)

        reached_gate = False

        def gate_capture(inp, snapshot):
            nonlocal reached_gate
            reached_gate = True
            return MagicMock(decision=GateDecision.ABORT, reason="LOW_EV",
                            p_adjusted=0.5, ev_net=-0.1)

        with patch("panopticon_py.signal_engine._build_friction_snapshot"), \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.fast_execution_gate", side_effect=gate_capture):
            await _process_event(event, db)

        assert not reached_gate, "z=-2.0 is a weak signal (|z|=2.0 < 4.0) and must be filtered"
        # D158-4: magnitude reject persists execution_records (was silent)
        assert len(db.execution_records) == 1
        assert db.execution_records[0]["accepted"] == 0
        assert db.execution_records[0]["reason"] == REASON_Z_MAGNITUDE_BELOW_THRESHOLD

    @pytest.mark.asyncio
    async def test_strong_positive_z_is_not_filtered(self):
        """z=+5.5, |z|=5.5 >= 4.0 → signal reaches gate (not filtered)."""
        db = DummyDB()
        db.position_return_value = None
        event = make_event(z=+5.5)

        reached_gate = False

        def gate_capture(inp, snapshot):
            nonlocal reached_gate
            reached_gate = True
            mock_result = MagicMock()
            mock_result.decision = GateDecision.ABORT
            mock_result.reason = "LOW_EV"
            mock_result.p_adjusted = 0.5
            mock_result.ev_net = -0.1
            return mock_result

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snap, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.fast_execution_gate", side_effect=gate_capture):
            mock_snap.return_value = DummySnap()
            await _process_event(event, db)

        assert reached_gate, "z=+5.5 is a strong signal (|z|=5.5 >= 4.0) and must NOT be filtered"

    @pytest.mark.asyncio
    async def test_z_exactly_at_threshold_is_not_filtered(self):
        """
        z=-4.0, |z|=4.0 is NOT < 4.0 → signal is NOT filtered (continues).
        The threshold uses strict <, so exactly at |z|=4.0 passes through.
        """
        db = DummyDB()
        db.position_return_value = None
        event = make_event(z=-4.0)

        reached_gate = False

        def gate_capture(inp, snapshot):
            nonlocal reached_gate
            reached_gate = True
            mock_result = MagicMock()
            mock_result.decision = GateDecision.ABORT
            mock_result.reason = "LOW_EV"
            mock_result.p_adjusted = 0.5
            mock_result.ev_net = -0.1
            return mock_result

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snap, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.fast_execution_gate", side_effect=gate_capture):
            mock_snap.return_value = DummySnap()
            await _process_event(event, db)

        # |z| = 4.0 < 4.0 is False → not filtered
        assert reached_gate, "z=-4.0 (|z|=4.0, NOT < 4.0) must NOT be filtered"


class TestExecutionIdUnifiedWithDecisionId:
    """execution_id == decision_id (Option A): UPDATE must find the same record."""

    @pytest.mark.asyncio
    async def test_update_matches_inserted_record(self):
        """
        INSERT execution_id=X, UPDATE WHERE execution_id=X → same record.
        Validates Option A: execution_id = decision_id.
        """
        db = DummyDB()
        db.position_return_value = None
        event = make_event()

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snap, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=True, clob_order_id="clob-update-test", tx_hash="0xtxupdate",
                reason="PASS", request_id="req-update"
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.EXECUTE
            mock_gate.reason = "PASS"
            mock_gate.p_adjusted = 0.75
            mock_gate.ev_net = 1.5
            mock_snap.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert len(db.execution_records) == 1
        assert len(db.execution_clob_updates) == 1

        rec = db.execution_records[0]
        upd = db.execution_clob_updates[0]

        # execution_id == decision_id (Option A)
        assert rec["execution_id"] == rec["decision_id"], \
            "Option A: execution_id must equal decision_id"

        # UPDATE uses same execution_id → should match
        assert upd["execution_id"] == rec["execution_id"], \
            "UPDATE WHERE execution_id must match the INSERT execution_id"


class TestExecutionRecordIncludesMarketId:
    """D43: market_id column must be written in all execution_record paths."""

    @pytest.mark.asyncio
    async def test_insufficient_consensus_writes_market_id(self):
        """
        Path: INSUFFICIENT_CONSENSUS → execution_record must contain market_id.
        """
        db = DummyDB()
        event = make_event(market_id="mkt-insufficient-test", z=-5.5)

        with patch(
            "panopticon_py.signal_engine._collect_insider_sources",
            return_value=[0.6]  # only 1 source, below MIN_CONSENSUS_SOURCES=2
        ):
            await _process_event(event, db)

        assert len(db.execution_records) == 1
        rec = db.execution_records[0]
        assert "market_id" in rec, "execution_record must have market_id key"
        assert rec["market_id"] == "mkt-insufficient-test"

    @pytest.mark.asyncio
    async def test_gate_execute_writes_market_id(self):
        """
        Path: Gate EXECUTE → execution_record must contain market_id.
        """
        db = DummyDB()
        db.position_return_value = {"avg_entry_price": 0.55}
        event = make_event(market_id="mkt-gate-execute", z=-5.5)

        with patch("panopticon_py.signal_engine._build_friction_snapshot") as mock_snap, \
             patch("panopticon_py.signal_engine._get_current_price", return_value=0.50), \
             patch("panopticon_py.signal_engine._collect_insider_sources", return_value=[0.6, 0.7]), \
             patch("panopticon_py.signal_engine.submit_fok_order", new_callable=AsyncMock) as mock_clob:

            mock_clob.return_value = MagicMock(
                accepted=True, clob_order_id="clob-123", tx_hash=None,
                reason="PASS", request_id="req-123"
            )
            mock_gate = MagicMock()
            mock_gate.decision = GateDecision.EXECUTE
            mock_gate.reason = "PASS"
            mock_gate.p_adjusted = 0.72
            mock_gate.ev_net = 1.25
            mock_snap.return_value = DummySnap()

            with patch("panopticon_py.signal_engine.fast_execution_gate", return_value=mock_gate):
                await _process_event(event, db)

        assert len(db.execution_records) == 1
        rec = db.execution_records[0]
        assert "market_id" in rec
        assert rec["market_id"] == "mkt-gate-execute"


class TestEntropyLookbackDefault:
    """D45b/D96: ENTROPY_LOOKBACK_SEC default covers whale / data-api cadence (not 60)."""

    def test_entropy_lookback_default_is_1800(self):
        """D96: default 1800s unless ENTROPY_LOOKBACK_SEC set at import (was 360 in D45b)."""
        import os
        saved = os.environ.pop("ENTROPY_LOOKBACK_SEC", None)
        try:
            from panopticon_py.signal_engine import ENTROPY_LOOKBACK_SEC as val
            assert val == 1800, f"Expected default 1800 (D96), got {val}"
            assert val >= 300, "Lookback must cover at least one whale scan cycle"
        finally:
            if saved is not None:
                os.environ["ENTROPY_LOOKBACK_SEC"] = saved

    def test_entropy_lookback_uses_os_getenv(self):
        """ENTROPY_LOOKBACK_SEC is read from os.getenv, not hardcoded."""
        import os
        import importlib
        os.environ["ENTROPY_LOOKBACK_SEC"] = "500"
        try:
            import panopticon_py.signal_engine as se_mod
            importlib.reload(se_mod)
            assert se_mod.ENTROPY_LOOKBACK_SEC == 500, (
                f"Env override failed: got {se_mod.ENTROPY_LOOKBACK_SEC}"
            )
        finally:
            os.environ.pop("ENTROPY_LOOKBACK_SEC", None)
            importlib.reload(se_mod)


# ── D161-3: Tests for _effective_insider_threshold and _db_execute_async_retry ──

class TestEffectiveInsiderThreshold:
    """D160-3: effective threshold differs between paper (0.30) and live (0.55) modes."""

    def test_paper_mode_returns_paper_threshold(self, monkeypatch):
        """Paper mode: effective threshold = PAPER_INSIDER_SCORE_THRESHOLD (default 0.30)."""
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.delenv("PAPER_INSIDER_SCORE_THRESHOLD", raising=False)
        import importlib
        import panopticon_py.signal_engine as se_mod
        importlib.reload(se_mod)
        assert se_mod._effective_insider_threshold() == pytest.approx(0.30)

    def test_live_mode_returns_live_threshold(self, monkeypatch):
        """Live mode: effective threshold = INSIDER_SCORE_THRESHOLD (default 0.55)."""
        monkeypatch.setenv("LIVE_TRADING", "true")
        monkeypatch.setenv("INSIDER_SCORE_THRESHOLD", "0.55")
        import importlib
        import panopticon_py.signal_engine as se_mod
        importlib.reload(se_mod)
        try:
            assert se_mod._effective_insider_threshold() == pytest.approx(0.55)
        finally:
            monkeypatch.delenv("LIVE_TRADING", raising=False)
            importlib.reload(se_mod)

    def test_paper_threshold_floor_enforced_at_import(self, monkeypatch):
        """PAPER_INSIDER_SCORE_THRESHOLD < 0.15 raises ValueError at module import."""
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.setenv("PAPER_INSIDER_SCORE_THRESHOLD", "0.10")
        import importlib
        import panopticon_py.signal_engine as se_mod
        with pytest.raises(ValueError, match="0.15"):
            importlib.reload(se_mod)
        # Restore before next test
        monkeypatch.setenv("PAPER_INSIDER_SCORE_THRESHOLD", "0.30")
        importlib.reload(se_mod)

    def test_custom_paper_threshold_respected(self, monkeypatch):
        """Custom PAPER_INSIDER_SCORE_THRESHOLD is respected in paper mode."""
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.setenv("PAPER_INSIDER_SCORE_THRESHOLD", "0.25")
        import importlib
        import panopticon_py.signal_engine as se_mod
        importlib.reload(se_mod)
        try:
            assert se_mod._effective_insider_threshold() == pytest.approx(0.25)
        finally:
            monkeypatch.delenv("PAPER_INSIDER_SCORE_THRESHOLD", raising=False)
            importlib.reload(se_mod)


class TestDbExecuteAsyncRetry:
    """D160-2 / D161-1: async-safe SQLite lock retry — event-loop safe (not time.sleep)."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt_no_retry(self):
        """If conn.execute succeeds on first try, no sleep is called."""
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_conn.execute.return_value = mock_result

        from panopticon_py.signal_engine import _db_execute_async_retry
        with patch("panopticon_py.signal_engine.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await _db_execute_async_retry(mock_conn, "SELECT 1 FROM t", ())
            assert result is mock_result
            assert mock_conn.execute.call_count == 1
            mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_on_locked_then_succeeds(self):
        """Locked OperationalError triggers retry with backoff; succeeds on second attempt."""
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_conn.execute.side_effect = [
            sqlite3.OperationalError("database is locked"),
            mock_result,  # succeeds on 2nd attempt
        ]
        from panopticon_py.signal_engine import _db_execute_async_retry
        with patch("panopticon_py.signal_engine.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await _db_execute_async_retry(
                mock_conn, "SELECT 1 FROM t", (), max_retries=3, sleep_s=0.001,
            )
            assert result is mock_result
            assert mock_conn.execute.call_count == 2
            mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_immediately_on_non_lock_error(self):
        """Non-lock OperationalError (e.g. no such table) raises immediately — no retry."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table: t")

        from panopticon_py.signal_engine import _db_execute_async_retry
        with patch("panopticon_py.signal_engine.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                await _db_execute_async_retry(
                    mock_conn, "SELECT 1 FROM t", (), max_retries=3, sleep_s=0.001,
                )
            assert mock_conn.execute.call_count == 1  # no retry on non-lock
            mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exhausted(self):
        """All attempts return locked → raises after max_retries, not silent drop."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")

        from panopticon_py.signal_engine import _db_execute_async_retry
        with patch("panopticon_py.signal_engine.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await _db_execute_async_retry(
                    mock_conn, "SELECT 1 FROM t", (), max_retries=3, sleep_s=0.001,
                )
            assert mock_conn.execute.call_count == 3  # exactly 3 attempts
            assert mock_sleep.call_count == 2  # sleep between attempt 1→2 and 2→3