from __future__ import annotations

import json
import sqlite3

import pytest

from panopticon_py.hunting.fingerprint_scrubber import compute_fingerprint
from panopticon_py.hunting.four_d_classifier import (
    FourDScores,
    compute_insider_score,
    load_fingerprint_from_watchlist,
    scores_from_parents,
)
from panopticon_py.hunting.trade_aggregate import ParentTrade

SYNTH_TRADES_ENOUGH = [
    {
        "size": 1000,
        "price": 0.85,
        "timestamp_seconds": 1_700_000_000 + i * 300,
        "category": "crypto" if i % 3 != 0 else "defi",
    }
    for i in range(20)
]

SYNTH_TRADES_SPARSE = [
    {
        "size": 500,
        "price": 0.70,
        "timestamp_seconds": 1_700_000_000 + i * 1000,
        "category": "unknown",
    }
    for i in range(3)
]


@pytest.fixture
def seam_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE wallet_watchlist (
            wallet_address TEXT PRIMARY KEY,
            score_components_json TEXT,
            last_seen_ts_utc TEXT DEFAULT '2026-05-09'
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


def _insider_parents() -> list[ParentTrade]:
    base_ts = 1_700_000_000_000
    offsets = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10000]
    return [
        ParentTrade(
            taker="0xabc",
            side=1,
            volume=5000,
            first_ts_ms=base_ts + offsets[i],
            last_ts_ms=base_ts + offsets[i] + 10,
            child_count=3,
            market_id="MKT-1",
        )
        for i in range(len(offsets))
    ]


class TestSeamARoundTrip:
    def test_full_trades_round_trip(self, seam_db):
        wallet = "0xseama0000000000000000000000000000000001"
        fp = compute_fingerprint(wallet, SYNTH_TRADES_ENOUGH)
        seam_db.execute(
            "INSERT INTO wallet_watchlist VALUES (?, ?, '2026-05-09')",
            (wallet, json.dumps(fp, separators=(",", ":"))),
        )
        seam_db.commit()

        loaded = load_fingerprint_from_watchlist(wallet, seam_db)
        assert loaded is not None
        assert "funding_source" in loaded
        assert loaded["funding_source"] == pytest.approx(0.0, abs=1e-9)
        assert isinstance(loaded["concentration"], dict)
        assert "max" in loaded["concentration"]

        s = scores_from_parents(_insider_parents(), fingerprint=loaded)
        score = compute_insider_score(s)
        assert 0.0 <= score <= 1.0

    def test_sparse_trades_propagates_insufficient_data(self, seam_db):
        wallet = "0xseama0000000000000000000000000000000002"
        fp = compute_fingerprint(wallet, SYNTH_TRADES_SPARSE)
        assert fp["insufficient_data"] is True

        seam_db.execute(
            "INSERT INTO wallet_watchlist VALUES (?, ?, '2026-05-09')",
            (wallet, json.dumps(fp)),
        )
        seam_db.commit()

        loaded = load_fingerprint_from_watchlist(wallet, seam_db)
        assert loaded is not None
        assert loaded["insufficient_data"] is True
        s = scores_from_parents(_insider_parents(), fingerprint=loaded)
        assert s.insufficient_data is True

        score_with_flag = compute_insider_score(s)
        score_without = compute_insider_score(
            FourDScores(
                idi=s.idi,
                burst=s.burst,
                taker_ratio=s.taker_ratio,
                size_entropy=s.size_entropy,
                concentration=s.concentration,
                funding_source=s.funding_source,
                insufficient_data=False,
            )
        )
        assert score_with_flag == pytest.approx(score_without * 0.5, abs=1e-6)

    def test_concentration_extra_keys_not_crash(self, seam_db):
        wallet = "0xseama0000000000000000000000000000000003"
        fp = {
            "size_entropy": 0.65,
            "timing_entropy": 0.55,
            "concentration": {
                "max": 0.72,
                "crypto": 0.72,
                "defi": 0.20,
                "other": 0.08,
                "all_unknown": False,
                "top3_share": 0.95,
            },
            "insufficient_data": False,
            "n_trades_sampled": 20,
        }
        seam_db.execute(
            "INSERT INTO wallet_watchlist VALUES (?, ?, '2026-05-09')",
            (wallet, json.dumps(fp)),
        )
        seam_db.commit()

        loaded = load_fingerprint_from_watchlist(wallet, seam_db)
        assert loaded is not None
        s = scores_from_parents([], fingerprint=loaded)
        assert s.concentration == pytest.approx(0.72, abs=1e-6)
