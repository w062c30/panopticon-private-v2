from __future__ import annotations

import logging
import sqlite3

import pytest

import panopticon_py.hunting.bootstrap_seed as bs
from panopticon_py.hunting.four_d_classifier import classify_high_frequency_wallet
from panopticon_py.hunting.trade_aggregate import ParentTrade
from panopticon_py.rate_limit_governor import RateLimitGovernor


def make_parents(scenario: str) -> list[ParentTrade]:
    base_ts = 1_700_000_000_000
    if scenario == "insider_slicing":
        ts_offsets = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10000]
        return [
            ParentTrade("0xABC", 1, 5000, base_ts + ts_offsets[i], base_ts + ts_offsets[i] + 10, 3, "MKT-1")
            for i in range(len(ts_offsets))
        ]
    return []


class TestCP8EnvVarHardened:
    def test_invalid_idi_high_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("HUNT_IDI_HIGH", "abc")
        parents = make_parents("insider_slicing")
        with caplog.at_level(logging.WARNING, logger="panopticon_py.hunting.four_d_classifier"):
            label, _, _ = classify_high_frequency_wallet(parents)
        assert label in ("INSIDER_ALGO_SLICING", "POTENTIAL_INSIDER", "MARKET_MAKER_NOISE", "UNCERTAIN_NOISE")
        assert any("HUNT_IDI_HIGH" in r.message for r in caplog.records)

    def test_all_thresholds_invalid_still_classifies(self, monkeypatch):
        for k in ("HUNT_IDI_HIGH", "HUNT_IDI_LOW", "HUNT_TAKER_HIGH", "HUNT_TAKER_LOW", "HUNT_BURST_HIGH"):
            monkeypatch.setenv(k, "INVALID")
        label, _, _ = classify_high_frequency_wallet(make_parents("insider_slicing"))
        assert label is not None


class TestCPC3Hardened:
    def test_missing_cex_anonymized_warns_and_continues(self, monkeypatch, caplog):
        monkeypatch.setattr(bs, "fetch_wallet_erc20_transfers_capped", lambda w, **kw: [])
        monkeypatch.setattr(bs, "trace_funding_roots", lambda w, **kw: {"roots": []})

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE wallet_watchlist (wallet_address TEXT PRIMARY KEY, score_components_json TEXT)")
        conn.execute(
            "CREATE TABLE insider_score_inference_log (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_address TEXT, inference_payload TEXT, created_at TEXT)"
        )
        conn.commit()
        try:
            with caplog.at_level(logging.WARNING, logger="bootstrap_seed"):
                score, _ = bs._score_wallet("0x" + "a" * 40, RateLimitGovernor(), conn)
        finally:
            conn.close()
        assert isinstance(score, float)
        assert any("CP_C3" in r.message or "cex_anonymized" in r.message for r in caplog.records)

    def test_trace_non_dict_falls_back(self, monkeypatch):
        monkeypatch.setattr(bs, "fetch_wallet_erc20_transfers_capped", lambda w, **kw: [])
        monkeypatch.setattr(bs, "trace_funding_roots", lambda w, **kw: None)

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE wallet_watchlist (wallet_address TEXT PRIMARY KEY, score_components_json TEXT)")
        conn.execute(
            "CREATE TABLE insider_score_inference_log (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_address TEXT, inference_payload TEXT, created_at TEXT)"
        )
        conn.commit()
        try:
            score, meta = bs._score_wallet("0x" + "b" * 40, RateLimitGovernor(), conn)
        finally:
            conn.close()
        assert isinstance(score, float)
        assert isinstance(meta.get("trace"), dict)
