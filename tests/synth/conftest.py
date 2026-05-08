from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def synth_db():
    """In-memory DB with wallet_watchlist + insider_score_inference_log tables."""
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
