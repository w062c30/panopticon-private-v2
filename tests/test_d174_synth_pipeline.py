from __future__ import annotations

import json
import sqlite3

import pytest

from panopticon_py.hunting.four_d_classifier import (
    FourDScores,
    classify_high_frequency_wallet,
    compute_insider_score,
    load_fingerprint_from_watchlist,
    scores_from_parents,
)
from panopticon_py.hunting.trade_aggregate import ParentTrade

SYNTH_FINGERPRINTS = {
    "happy_full": {
        "size_entropy": 0.72,
        "timing_entropy": 0.55,
        "concentration": {"max": 0.88, "top3": 0.95},
        "funding_source": 0.40,
        "insufficient_data": False,
    },
    "missing_funding": {
        "size_entropy": 0.65,
        "concentration": {"max": 0.70},
        "insufficient_data": False,
    },
    "conc_float": {
        "size_entropy": 0.50,
        "concentration": 0.80,
        "funding_source": 0.10,
        "insufficient_data": False,
    },
    "low_confidence": {
        "size_entropy": 0.90,
        "concentration": {"max": 0.95},
        "funding_source": 0.80,
        "insufficient_data": True,
    },
    "corrupt_json": "NOT_VALID_JSON{{{",
    "null_row": None,
}


def make_parents(scenario: str) -> list[ParentTrade]:
    base_ts = 1_700_000_000_000
    if scenario == "insider_slicing":
        ts_offsets = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10000]
        return [
            ParentTrade(
                "0xABC",
                1,
                5000,
                base_ts + ts_offsets[i],
                base_ts + ts_offsets[i] + 10,
                3,
                "MKT-1",
            )
            for i in range(len(ts_offsets))
        ]
    if scenario == "market_maker":
        out = []
        for i in range(10):
            side = 1 if i % 2 == 0 else -1
            out.append(ParentTrade("0xDEF", side, 1000, base_ts + i * 200, base_ts + i * 200 + 5, 2, "MKT-2"))
        return out
    if scenario == "empty":
        return []
    if scenario == "single":
        return [ParentTrade("0xGHI", 1, 9000, base_ts, base_ts + 100, 1, "MKT-3")]
    if scenario == "uniform_gaps":
        return [
            ParentTrade("0xJKL", 1, 2000, base_ts + i * 1000, base_ts + i * 1000 + 10, 2, "MKT-4")
            for i in range(10)
        ]
    return []


@pytest.fixture
def synth_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE wallet_watchlist (
            wallet_address TEXT PRIMARY KEY,
            score_components_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE insider_score_inference_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT,
            inference_payload TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class TestCP1WalletNormalization:
    def test_uppercase_wallet_finds_row(self, synth_db):
        wallet = "0xabcdef1234567890abcdef1234567890abcdef12"
        fp = SYNTH_FINGERPRINTS["happy_full"]
        synth_db.execute(
            "INSERT INTO wallet_watchlist VALUES (?, ?)",
            (wallet, json.dumps(fp)),
        )
        synth_db.commit()

        result = load_fingerprint_from_watchlist(wallet.upper(), synth_db)
        assert result is not None
        assert result["size_entropy"] == pytest.approx(0.72, abs=1e-6)

    def test_wallet_truncated_to_42(self, synth_db):
        long_wallet = "0x" + "a" * 100
        result = load_fingerprint_from_watchlist(long_wallet, synth_db)
        assert result is None


class TestCP2JsonParse:
    def test_corrupt_json_returns_none(self, synth_db):
        wallet = "0xbadwallet"
        synth_db.execute(
            "INSERT INTO wallet_watchlist VALUES (?, ?)",
            (wallet, SYNTH_FINGERPRINTS["corrupt_json"]),
        )
        synth_db.commit()
        result = load_fingerprint_from_watchlist(wallet, synth_db)
        assert result is None

    def test_null_json_returns_none(self, synth_db):
        wallet = "0xnullwallet"
        synth_db.execute("INSERT INTO wallet_watchlist VALUES (?, ?)", (wallet, None))
        synth_db.commit()
        result = load_fingerprint_from_watchlist(wallet, synth_db)
        assert result is None


class TestCP4InsufficientDataDiscount:
    def test_score_halved_when_insufficient(self):
        s_good = FourDScores(
            idi=0.9,
            burst=0.9,
            taker_ratio=0.9,
            size_entropy=0.9,
            concentration=0.9,
            insufficient_data=False,
        )
        s_bad = FourDScores(
            idi=0.9,
            burst=0.9,
            taker_ratio=0.9,
            size_entropy=0.9,
            concentration=0.9,
            insufficient_data=True,
        )
        score_good = compute_insider_score(s_good)
        score_bad = compute_insider_score(s_bad)
        assert score_bad == pytest.approx(score_good * 0.5, abs=1e-6)


class TestCP5EmptyParents:
    def test_empty_parents_no_crash(self):
        fp = SYNTH_FINGERPRINTS["happy_full"]
        s = scores_from_parents([], fingerprint=fp)
        assert s.idi == 0.0
        assert s.burst == 0.0
        assert s.size_entropy == pytest.approx(0.72, abs=1e-6)

    def test_empty_parents_no_fingerprint(self):
        s = scores_from_parents([], fingerprint=None)
        assert s.idi == 0.0
        assert s.size_entropy == 0.0
        assert s.insufficient_data is False


class TestCP6ZeroWeights:
    def test_all_zero_weights_no_divide_by_zero(self, monkeypatch):
        for key in [
            "INSIDER_W_IDI",
            "INSIDER_W_BURST",
            "INSIDER_W_TAKER",
            "INSIDER_W_SIZE_ENT",
            "INSIDER_W_CONC",
            "INSIDER_W_FUND",
        ]:
            monkeypatch.setenv(key, "0")

        s = FourDScores(
            idi=0.8,
            burst=0.7,
            taker_ratio=0.9,
            size_entropy=0.6,
            concentration=0.5,
            insufficient_data=False,
        )
        score = compute_insider_score(s)
        assert 0.0 <= score <= 1.0


class TestCP7ConcentrationSchemaDrift:
    def test_concentration_as_plain_float(self):
        fp = SYNTH_FINGERPRINTS["conc_float"]
        s = scores_from_parents(make_parents("insider_slicing"), fingerprint=fp)
        assert isinstance(s.concentration, float)
        assert 0.0 <= s.concentration <= 1.0


class TestCP8EnvVarThreshold:
    def test_invalid_idi_high_env_fallbacks_without_crash(self, monkeypatch):
        monkeypatch.setenv("HUNT_IDI_HIGH", "abc")
        parents = make_parents("insider_slicing")
        label, _, _ = classify_high_frequency_wallet(parents)
        assert label in ("INSIDER_ALGO_SLICING", "POTENTIAL_INSIDER", "MARKET_MAKER_NOISE", "UNCERTAIN_NOISE")


class TestEndToEndHappyPath:
    def test_insider_slicing_full_fingerprint(self, synth_db):
        wallet = "0xe2ewalletinsider0000000000000000000000001"
        fp = SYNTH_FINGERPRINTS["happy_full"]
        synth_db.execute(
            "INSERT INTO wallet_watchlist VALUES (?, ?)",
            (wallet, json.dumps(fp)),
        )
        synth_db.commit()

        loaded_fp = load_fingerprint_from_watchlist(wallet, synth_db)
        assert loaded_fp is not None

        parents = make_parents("insider_slicing")
        label, scores, _ = classify_high_frequency_wallet(parents, fingerprint=loaded_fp)
        insider_score = compute_insider_score(scores, wallet_address=wallet, db_conn=synth_db)

        assert label == "INSIDER_ALGO_SLICING"
        assert insider_score >= 0.6
        assert scores.size_entropy == pytest.approx(0.72, abs=1e-6)

        row = synth_db.execute(
            "SELECT inference_payload FROM insider_score_inference_log WHERE wallet_address=?",
            (wallet.lower()[:42],),
        ).fetchone()
        assert row is not None
        log_data = json.loads(row[0])
        assert "insider_score" in log_data

    def test_market_maker_no_fingerprint(self):
        parents = make_parents("market_maker")
        label, scores, _ = classify_high_frequency_wallet(parents, fingerprint=None)
        insider_score = compute_insider_score(scores)
        assert label == "UNCERTAIN_NOISE"
        assert insider_score < 0.35
