from __future__ import annotations

import sqlite3

import pytest

import panopticon_py.hunting.bootstrap_seed as bs
from panopticon_py.hunting.bootstrap_seed import _rows_to_synthetic_trades
from panopticon_py.hunting.trade_aggregate import aggregate_taker_sweeps
from panopticon_py.rate_limit_governor import RateLimitGovernor

WALLET = "0xc000000000000000000000000000000000000001"


def _make_erc20_rows(n_buy: int, n_sell: int, wallet: str):
    rows = []
    base_ts = 1_700_000_000
    for i in range(n_buy):
        rows.append(
            {
                "to_address": wallet,
                "from_address": "0xOTHER",
                "value": str(1000 * (10**6)),
                "token_decimals": 6,
                "token_symbol": "USDC",
                "block_timestamp": base_ts + i * 60,
            }
        )
    for j in range(n_sell):
        rows.append(
            {
                "to_address": "0xOTHER",
                "from_address": wallet,
                "value": str(800 * (10**6)),
                "token_decimals": 6,
                "token_symbol": "USDC",
                "block_timestamp": base_ts + (n_buy + j) * 60,
            }
        )
    return rows


class TestSeamCBootstrap:
    def test_buy_sell_side_mapping(self):
        rows = _make_erc20_rows(5, 3, WALLET)
        trades = _rows_to_synthetic_trades(WALLET, rows)
        buy_count = sum(1 for t in trades if t["side"] == "BUY")
        sell_count = sum(1 for t in trades if t["side"] == "SELL")
        assert buy_count == 5
        assert sell_count == 3

    def test_to_address_none_defaults_sell(self):
        rows = [
            {
                "to_address": None,
                "from_address": "0xOTHER",
                "value": "1000000",
                "token_decimals": 6,
                "token_symbol": "USDC",
                "block_timestamp": 1_700_000_000,
            }
        ]
        trades = _rows_to_synthetic_trades(WALLET, rows)
        assert trades[0]["side"] == "SELL"

    def test_aggregate_accepts_side_string(self):
        rows = _make_erc20_rows(10, 2, WALLET)
        trades = _rows_to_synthetic_trades(WALLET, rows)
        try:
            parents = aggregate_taker_sweeps(trades)
        except (TypeError, AttributeError, KeyError) as exc:
            pytest.fail(f"aggregate_taker_sweeps rejected side string: {exc}")
        assert isinstance(parents, list)

    def test_e2e_bootstrap_score_wallet_mock(self, monkeypatch):
        monkeypatch.setattr(bs, "fetch_wallet_erc20_transfers_capped", lambda w, **kw: _make_erc20_rows(10, 2, w))
        monkeypatch.setattr(bs, "trace_funding_roots", lambda w, **kw: {"cex_anonymized": False, "roots": []})

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE wallet_watchlist (wallet_address TEXT PRIMARY KEY, score_components_json TEXT)")
        conn.execute(
            "CREATE TABLE insider_score_inference_log (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_address TEXT, inference_payload TEXT, created_at TEXT)"
        )
        conn.commit()
        try:
            gov = RateLimitGovernor()
            score, meta = bs._score_wallet(WALLET, gov, conn)
        finally:
            conn.close()

        assert isinstance(score, float)
        assert "label" in meta
        assert "insider_score_5d" in meta
        assert 0.0 <= meta["insider_score_5d"] <= 1.0

    def test_e2e_bootstrap_trace_missing_key_crash(self, monkeypatch):
        monkeypatch.setattr(bs, "fetch_wallet_erc20_transfers_capped", lambda w, **kw: _make_erc20_rows(5, 1, w))
        monkeypatch.setattr(bs, "trace_funding_roots", lambda w, **kw: {"roots": []})

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE wallet_watchlist (wallet_address TEXT PRIMARY KEY, score_components_json TEXT)")
        conn.execute(
            "CREATE TABLE insider_score_inference_log (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_address TEXT, inference_payload TEXT, created_at TEXT)"
        )
        conn.commit()
        try:
            gov = RateLimitGovernor()
            with pytest.raises(KeyError):
                bs._score_wallet(WALLET, gov, conn)
        finally:
            conn.close()
