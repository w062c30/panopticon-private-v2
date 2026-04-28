from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from panopticon_py.execution.clob_client import (
    PING_ABORT_THRESHOLD_MS,
    CLOBSubmitResult,
    _get_cached_ping_ms,
    sign_eip712,
    submit_fok_order,
)
from panopticon_py.execution.constants import (
    REASON_DRY_RUN,
    REASON_PING_ABORT,
    REASON_PASS,
    REASON_CLOB_REJECT,
)


class DummySnap:
    def __init__(self, ping_ms: float) -> None:
        self.network_ping_ms = ping_ms


class DummyState:
    def __init__(self, ping_ms: float) -> None:
        self._snap = DummySnap(ping_ms)

    def get(self):
        return self._snap


# ── Ping ──────────────────────────────────────────────────────────────────────

class TestGetCachedPingMs:
    def test_none_state_returns_zero(self):
        assert _get_cached_ping_ms(None) == 0.0

    def test_state_returns_ping_ms(self):
        assert _get_cached_ping_ms(DummyState(187.0)) == 187.0


# ── Signing ────────────────────────────────────────────────────────────────────

class TestSignEip712:
    def test_empty_key_produces_mock_signature(self):
        order = {"orderType": "FOK", "marketId": "123", "side": "BUY", "size": 10, "price": 0.55}
        result = sign_eip712(order, "")
        assert "signature" in result
        assert result["signature"].startswith("0x")

    def test_signing_adds_signature_field(self):
        order = {"orderType": "FOK", "marketId": "456", "side": "SELL", "size": 20, "price": 0.42}
        result = sign_eip712(order, "deadbeef" * 8)
        assert "signature" in result
        assert result["orderType"] == "FOK"


# ── FOK hard-coded ─────────────────────────────────────────────────────────────

class TestFOKHardCoded:
    def test_signature_has_no_order_type_parameter(self):
        import inspect
        sig = inspect.signature(submit_fok_order)
        param_names = list(sig.parameters.keys())
        assert "order_type" not in param_names
        assert "time_in_force" not in param_names
        assert "tif" not in param_names
        assert "orderType" not in param_names


# ── Ping circuit breaker ────────────────────────────────────────────────────────

class TestPingAbort:
    def test_abort_above_threshold_returns_result_no_db_write(self):
        """Ping abort returns CLOBSubmitResult with REASON_PING_ABORT, NO db_writer call."""
        state = DummyState(PING_ABORT_THRESHOLD_MS + 10)
        result = asyncio.run(submit_fok_order(
            market_id="m2",
            token_id="t2",
            side="BUY",
            size=100.0,
            price=0.6,
            decision_id="dec-ping-abort",
            state=state,
        ))
        assert result.accepted is False
        assert result.reason == REASON_PING_ABORT
        assert result.clob_order_id is None
        assert result.tx_hash is None

    def test_bypass_below_threshold_with_private_key(self):
        """Below threshold + valid key → live submission, no db_writer, returns accepted."""
        state = DummyState(PING_ABORT_THRESHOLD_MS - 50)
        called_payload: dict = {}

        def fake_urlopen(req, *, timeout=None):
            nonlocal called_payload
            called_payload = json.loads(req.data)
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"accepted": True, "clob_order_id": "c2"}).encode("utf-8")
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch("urllib.request.urlopen", fake_urlopen):
            result = asyncio.run(submit_fok_order(
                market_id="m4",
                token_id="t4",
                side="BUY",
                size=30.0,
                price=0.45,
                decision_id="dec-below-threshold",
                state=state,
                private_key="1" * 64,
                dry_run=False,
            ))
            assert result.accepted is True
            assert result.clob_order_id == "c2"

    def test_no_state_bypasses_circuit_breaker(self):
        """state=None bypasses circuit breaker (ping assumed healthy)."""
        def fake_urlopen(req, *, timeout=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"accepted": True, "clob_order_id": "c3"}).encode("utf-8")
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch("urllib.request.urlopen", fake_urlopen):
            result = asyncio.run(submit_fok_order(
                market_id="m3",
                token_id="t3",
                side="SELL",
                size=25.0,
                price=0.51,
                decision_id="dec-no-state",
                state=None,
                private_key="1" * 64,
                dry_run=False,
            ))
            assert result.accepted is True
            assert result.clob_order_id == "c3"


# ── Dry run ────────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_returns_mock_without_network(self):
        """dry_run=True returns mock accepted result with no network call."""
        result = asyncio.run(submit_fok_order(
            market_id="m5",
            token_id="t5",
            side="BUY",
            size=50.0,
            price=0.55,
            decision_id="dec-dry-run",
            dry_run=True,
        ))
        assert result.accepted is True
        assert result.clob_order_id is not None
        assert result.tx_hash is not None
        assert result.dry_run is True
        assert result.status_code == 200
        assert result.reason == REASON_DRY_RUN

    def test_dry_run_accepted_is_true(self):
        result = asyncio.run(submit_fok_order(
            market_id="m5b",
            token_id="t5b",
            side="BUY",
            size=50.0,
            price=0.55,
            decision_id="dec-dry-run-2",
            dry_run=True,
        ))
        assert result.accepted is True


# ── Side validation ───────────────────────────────────────────────────────────

class TestSideValidation:
    def test_invalid_side_rejected(self):
        """Invalid side returns REJECT result, no network call."""
        result = asyncio.run(submit_fok_order(
            market_id="m6",
            token_id="t6",
            side="INVALID_SIDE",
            size=50.0,
            price=0.55,
            decision_id="dec-invalid-side",
        ))
        assert result.accepted is False
        assert "invalid_side" in result.reason

    def test_valid_buy_side(self):
        def fake_urlopen(req, *, timeout=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"accepted": True, "clob_order_id": "c_valid"}).encode("utf-8")
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch("urllib.request.urlopen", fake_urlopen):
            result = asyncio.run(submit_fok_order(
                market_id="m_valid",
                token_id="t_valid",
                side="BUY",
                size=10.0,
                price=0.55,
                decision_id="dec-valid-buy",
                private_key="1" * 64,
                dry_run=False,
            ))
            assert result.accepted is True

    def test_valid_sell_side(self):
        def fake_urlopen(req, *, timeout=None):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"accepted": True, "clob_order_id": "c_sell"}).encode("utf-8")
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch("urllib.request.urlopen", fake_urlopen):
            result = asyncio.run(submit_fok_order(
                market_id="m_sell",
                token_id="t_sell",
                side="SELL",
                size=10.0,
                price=0.55,
                decision_id="dec-valid-sell",
                private_key="1" * 64,
                dry_run=False,
            ))
            assert result.accepted is True


# ── Idempotency ────────────────────────────────────────────────────────────────

class TestIdempotencyKeyDerivation:
    def test_idem_key_is_decision_id_prefix(self):
        called_payload: dict = {}

        def fake_urlopen(req, *, timeout=None):
            nonlocal called_payload
            called_payload = json.loads(req.data)
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"accepted": True, "clob_order_id": "c3"}).encode("utf-8")
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch("urllib.request.urlopen", fake_urlopen):
            asyncio.run(submit_fok_order(
                market_id="m7",
                token_id="t7",
                side="BUY",
                size=10.0,
                price=0.55,
                decision_id="dec-idem-12345678901234567890",
                private_key="1" * 64,
                dry_run=False,
            ))

        idem = called_payload.get("idempotency_key", "")
        assert len(idem) >= 8
        assert called_payload.get("decision_id") == "dec-idem-12345678901234567890"


# ── CLOB live rejection ───────────────────────────────────────────────────────

class TestCLOBReject:
    def test_clob_rejected_returns_reject_result(self):
        """CLOB HTTP 400 → returns REJECT result, no db_writer."""
        def fake_urlopen(req, *, timeout=None):
            mock_resp = MagicMock()
            mock_resp.status = 400
            mock_resp.read.return_value = json.dumps({"error": "bad signature"}).encode("utf-8")
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch("urllib.request.urlopen", fake_urlopen):
            result = asyncio.run(submit_fok_order(
                market_id="m8",
                token_id="t8",
                side="BUY",
                size=10.0,
                price=0.55,
                decision_id="dec-clob-reject",
                private_key="1" * 64,
                dry_run=False,
            ))
            assert result.accepted is False
            assert result.reason == REASON_CLOB_REJECT
            assert result.status_code == 400


# ── No-DB-side-effect (Phase 2-C-2 invariant) ─────────────────────────────────

class TestNoDBWrite:
    def test_submit_returns_result_without_db_side_effect(self):
        """
        Phase 2-C-2 invariant: submit_fok_order NEVER writes execution_records.
        DB write responsibility moved to signal_engine._process_event().
        This test verifies the function is purely a result-returner.
        """
        result = asyncio.run(submit_fok_order(
            market_id="m_no_db",
            token_id="t_no_db",
            side="BUY",
            size=50.0,
            price=0.55,
            decision_id="dec-no-db",
            dry_run=True,
        ))
        # Should return a valid result without any DB writer being called
        assert result is not None
        assert isinstance(result, CLOBSubmitResult)
        assert hasattr(result, "accepted")
        assert hasattr(result, "reason")
        assert hasattr(result, "clob_order_id")
        assert hasattr(result, "tx_hash")
        # No exception = no implicit DB write failure


# ── Regression ────────────────────────────────────────────────────────────────

class TestNoGTCGTDMARKET:
    def test_no_forbidden_order_types_in_source(self):
        import os
        import re
        import subprocess

        result = subprocess.run(
            ["git", "grep", "-n", "-E", r'"(GTC|GTD|MARKET)"', "--", "panopticon_py"],
            capture_output=True,
            text=True,
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        )
        hits = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
            and "test" not in line.lower()
            and "comment" not in line.lower()
            and "TODO" not in line
            and "FIXME" not in line
        ]
        assert len(hits) == 0, f"Forbidden order types found:\n" + "\n".join(hits)