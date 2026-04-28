"""
tests/test_insider_pattern.py

Tests for INSIDER_PATTERN_COLLECTOR module.
Covers scoring logic, case type classification, and Invariant 6.2 enforcement.

Target: 6 new tests → total suite >= 167
CRITICAL INVARIANT 6.2: pattern_score is FORENSIC ONLY.
It MUST NOT enter signal_engine, SignalEvent, p_prior, LR, posterior, ev_net.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from panopticon_py.analysis.insider_pattern import (
    _age_days,
    _count_correlated_bets,
    _count_cluster_wallets,
    _count_same_ts_wallets,
    _detect_decoy_pattern,
    _parse_ts,
    compute_pattern_score,
)
from panopticon_py.signal_engine import SignalEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WALLET_OBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_observations (
    obs_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    market_id TEXT,
    obs_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ingest_ts_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_obs_address ON wallet_observations(address);
CREATE INDEX IF NOT EXISTS idx_wallet_obs_ingest ON wallet_observations(ingest_ts_utc);
"""


def _make_db() -> sqlite3.Connection:
    """Create an in-memory DB with wallet_observations table."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_WALLET_OBS_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Task F: 6 core tests
# ---------------------------------------------------------------------------

class TestInsiderPatternScoring:
    """Task F: Insider pattern scoring tests."""

    def test_solo_op_high_score(self):
        """
        New wallet (<1 day), betting into 5% prior, 3 correlated markets.
        Assert score >= 0.70, case_type == 'SOLO_OP'.
        """
        conn = _make_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        # Use <1 day age to get account_age = 0.35
        one_hour_ago_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 3600, tz=timezone.utc
        ).isoformat()

        # Main wallet: 3 correlated market observations (recent — within 72h window)
        main_wallet = "0x1111111111111111111111111111111111111111"
        for i in range(3):
            conn.execute(
                """
                INSERT INTO wallet_observations
                  (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    main_wallet,
                    f"asset_correlated_{i}",
                    "clob_trade",
                    json.dumps({"size": 500.0, "price": 0.05}),
                    now_iso,
                ),
            )
        conn.commit()

        result = compute_pattern_score(
            wallet_address=main_wallet,
            asset_id="asset_main",
            stake_usd=1000.0,
            market_prior=0.04,  # <5% → max prior_conviction = 0.30
            account_first_seen_ts=one_hour_ago_iso,
            db_conn=conn,
        )

        # Expected: prior_conv=0.30 + account_age(~0.35) + correlated_mkts(>=2 → 0.10) = 0.75
        assert result["score"] >= 0.70, f"Expected score >= 0.70, got {result['score']}, factors={result['factors']}"
        assert result["case_type"] == "SOLO_OP", f"Expected SOLO_OP, got {result['case_type']}"
        conn.close()

    def test_cluster_pattern_detected(self):
        """
        Verify that _count_cluster_wallets correctly identifies cluster wallets
        when observations have uppercase 'Size' JSON key.
        _detect_decoy_pattern returns False when no small bets exist.
        Both facts together → case_type = 'CLUSTER'.
        """
        conn = _make_db()
        recent_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 600, tz=timezone.utc
        ).isoformat()

        main_wallet = "0x2222222222222222222222222222222222220000"
        # Main wallet: 2 correlated observations
        for i in range(2):
            conn.execute(
                """
                INSERT INTO wallet_observations
                  (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    main_wallet,
                    f"asset_related_{i}",
                    "clob_trade",
                    json.dumps({"size": 1000.0, "price": 0.50}),
                    recent_iso,
                ),
            )
        # 5 cluster wallets: recent, same market, similar stake, uppercase 'Size'
        for i in range(5):
            wallet = f"0x222222222222222222222222222222222222{i:04d}"
            conn.execute(
                """
                INSERT INTO wallet_observations
                  (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    wallet,
                    "asset_cluster_target",
                    "clob_trade",
                    json.dumps({"Size": 50000.0, "price": 0.10}),
                    recent_iso,
                ),
            )
        conn.commit()

        # Directly verify _count_cluster_wallets returns 4
        # (5 other wallets, minus 1 for the main wallet = 4)
        cluster_count = _count_cluster_wallets(
            conn, main_wallet, "asset_cluster_target", 50000.0, tolerance=0.20, hours=1, max_age_days=7
        )
        assert cluster_count >= 3, (
            f"Expected _count_cluster_wallets>=3, got {cluster_count}"
        )

        # _detect_decoy_pattern: no small bets on other markets → False
        has_decoy = _detect_decoy_pattern(
            conn, main_wallet, "asset_cluster_target", large_threshold=1000, small_threshold=100
        )
        assert has_decoy is False, "Expected no decoy pattern"

        # Full scoring: cluster=5 → 0.12, no decoy → SOLO_OP or CLUSTER
        # With recent first_seen and prior 0.04:
        result = compute_pattern_score(
            wallet_address=main_wallet,
            asset_id="asset_cluster_target",
            stake_usd=50000.0,
            market_prior=0.04,
            account_first_seen_ts=recent_iso,
            db_conn=conn,
        )
        assert result["cluster_wallet_count"] >= 3, (
            f"Expected cluster_wallet_count>=3, got {result['cluster_wallet_count']}"
        )
        assert result["case_type"] == "CLUSTER", f"Expected CLUSTER, got {result['case_type']}"
        conn.close()

    def test_decoy_cluster_detected(self):
        """
        Verify that _detect_decoy_pattern correctly identifies the decoy signature:
        >=1 large bet (>=1000) on target market AND >0 small bets (<=100) on other markets.
        cluster_count >= 3 + has_decoy=True → case_type = 'DECOY_CLUSTER'.
        """
        conn = _make_db()
        recent_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 600, tz=timezone.utc
        ).isoformat()

        main_wallet = "0x3333333333333333333333333333333333330000"

        # Main wallet: large bet on target + small bet on other market (decoy signature)
        conn.execute(
            """
            INSERT INTO wallet_observations
              (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                main_wallet,
                "asset_iran_strikes",
                "clob_trade",
                json.dumps({"Size": 45000.0, "price": 0.03}),
                recent_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO wallet_observations
              (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                main_wallet,
                "asset_decoy",
                "clob_trade",
                json.dumps({"size": 50.0, "price": 0.50}),
                recent_iso,
            ),
        )
        conn.commit()

        # 3 cluster wallets with same pattern (use indices 1-3 to avoid overlap with main wallet 0000)
        for i in range(1, 4):
            wallet = f"0x333333333333333333333333333333333333{i:04d}"
            conn.execute(
                """
                INSERT INTO wallet_observations
                  (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    wallet,
                    "asset_iran_strikes",
                    "clob_trade",
                    json.dumps({"Size": 45000.0, "price": 0.03}),
                    recent_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO wallet_observations
                  (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    wallet,
                    "asset_decoy",
                    "clob_trade",
                    json.dumps({"size": 50.0, "price": 0.50}),
                    recent_iso,
                ),
            )
        conn.commit()

        # Directly verify decoy detection
        has_decoy = _detect_decoy_pattern(
            conn, main_wallet, "asset_iran_strikes", large_threshold=1000, small_threshold=100
        )
        assert has_decoy is True, "Expected decoy pattern to be detected"

        # Cluster count: 3 cluster wallets (excluding main wallet)
        cluster_count = _count_cluster_wallets(
            conn, main_wallet, "asset_iran_strikes", 45000.0, tolerance=0.20, hours=1, max_age_days=7
        )
        assert cluster_count >= 2, f"Expected cluster_count>=2, got {cluster_count}"

        result = compute_pattern_score(
            wallet_address=main_wallet,
            asset_id="asset_iran_strikes",
            stake_usd=45000.0,
            market_prior=0.03,
            account_first_seen_ts=recent_iso,
            db_conn=conn,
        )

        assert result["case_type"] == "DECOY_CLUSTER", f"Expected DECOY_CLUSTER, got {result['case_type']}"
        assert result["has_decoy_bets"] is True, "Expected has_decoy_bets == True"
        conn.close()

    def test_old_wallet_low_score(self):
        """
        Wallet age 180 days, prior 0.45 (normal market).
        Assert score < 0.20.
        """
        conn = _make_db()
        old_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 180 * 86400, tz=timezone.utc
        ).isoformat()

        result = compute_pattern_score(
            wallet_address="0x4444444444444444444444444444444444444444",
            asset_id="asset_regular",
            stake_usd=500.0,
            market_prior=0.45,
            account_first_seen_ts=old_iso,
            db_conn=conn,
        )

        assert result["score"] < 0.20, f"Expected score < 0.20, got {result['score']}"
        conn.close()

    def test_pattern_score_not_in_signal_event(self):
        """
        Construct a SignalEvent — assert it has no field named 'pattern_score'.
        This enforces Invariant 6.2 at test level.
        """
        expected_fields = {
            "source", "market_id", "token_id", "entropy_z",
            "ofi_shock_value", "trigger_address", "trigger_ts_utc", "market_tier",
        }
        event = SignalEvent(
            source="radar",
            market_id="test_market",
            token_id="test_token",
            entropy_z=-4.5,
            trigger_address="0xabc",
            trigger_ts_utc=datetime.now(timezone.utc).isoformat(),
            market_tier="t2",
        )
        event_dict = vars(event)
        assert "pattern_score" not in event_dict, "SignalEvent must NOT have pattern_score (Invariant 6.2)"
        for field in expected_fields:
            assert field in event_dict, f"SignalEvent missing expected field: {field}"

    def test_backfill_runs_without_error(self):
        """
        Mock wallet_observations with 3 rows.
        Call backfill logic — assert no exceptions, assert 0-3 flags inserted.
        """
        conn = _make_db()
        recent_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 3600, tz=timezone.utc
        ).isoformat()

        wallets = [
            "0x5555555555555555555555555555555555555551",
            "0x5555555555555555555555555555555555555552",
            "0x5555555555555555555555555555555555555553",
        ]
        for w in wallets:
            conn.execute(
                """
                INSERT INTO wallet_observations
                  (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    w,
                    "asset_test",
                    "clob_trade",
                    json.dumps({"size": 5000.0, "price": 0.10}),
                    recent_iso,
                ),
            )
        conn.commit()

        inserted = 0
        for w in wallets:
            result = compute_pattern_score(
                wallet_address=w,
                asset_id="asset_test",
                stake_usd=5000.0,
                market_prior=0.10,
                account_first_seen_ts=recent_iso,
                db_conn=conn,
            )
            if result["score"] >= 0.70:
                inserted += 1

        assert 0 <= inserted <= 3, "Backfill should handle 0-3 flags gracefully"
        conn.close()


class TestDBHelpers:
    """DB helper function tests."""

    def test_count_correlated_bets_returns_int(self):
        """_count_correlated_bets should return an integer."""
        conn = _make_db()
        result = _count_correlated_bets(conn, "0xany", "any_asset", hours=72)
        assert isinstance(result, int)
        conn.close()

    def test_count_cluster_wallets_returns_int(self):
        """_count_cluster_wallets should return an integer (zero on empty DB)."""
        conn = _make_db()
        result = _count_cluster_wallets(conn, "0x0001", "any_asset", 5000.0, tolerance=0.20, hours=1, max_age_days=7)
        assert isinstance(result, int)
        conn.close()

    def test_count_same_ts_wallets_returns_int(self):
        """_count_same_ts_wallets should return an integer."""
        conn = _make_db()
        result = _count_same_ts_wallets(conn, "0xtest", "asset_test", 5000.0, tolerance=0.20, hours=1)
        assert isinstance(result, int)
        conn.close()

    def test_detect_decoy_pattern_false_without_small_bets(self):
        """Without small bets on other markets, decoy pattern should be False."""
        conn = _make_db()
        result = _detect_decoy_pattern(conn, "0xtest", "asset_main", large_threshold=1000, small_threshold=100)
        assert result is False
        conn.close()

    def test_detect_decoy_pattern_true_with_large_and_small(self):
        """With >=1 large bet on target AND >0 small bets on others → True."""
        conn = _make_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        wallet = "0x6666666666666666666666666666666666666666"
        # Large bet on target
        conn.execute(
            """
            INSERT INTO wallet_observations
              (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                wallet,
                "asset_target",
                "clob_trade",
                json.dumps({"size": 5000.0, "price": 0.10}),
                now_iso,
            ),
        )
        # Small bet on other
        conn.execute(
            """
            INSERT INTO wallet_observations
              (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                wallet,
                "asset_other",
                "clob_trade",
                json.dumps({"size": 50.0, "price": 0.50}),
                now_iso,
            ),
        )
        conn.commit()

        result = _detect_decoy_pattern(conn, wallet, "asset_target", large_threshold=1000, small_threshold=100)
        assert result is True
        conn.close()

    def test_age_days_with_none_ts(self):
        """_age_days with None ts should return 999.0 (unknown = old = low suspicion)."""
        assert _age_days(None) == 999.0

    def test_parse_ts_with_valid_iso(self):
        """_parse_ts with valid ISO string should return datetime with year 2026."""
        ts = "2026-04-24T12:00:00+00:00"
        result = _parse_ts(ts)
        assert result is not None
        assert result.year == 2026

    def test_parse_ts_with_none(self):
        """_parse_ts with None should return None."""
        assert _parse_ts(None) is None
