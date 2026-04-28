import unittest

from panopticon_py.hunting.fingerprint_scrubber import (
    UncertainWalletState,
    evaluate_uncertain_bucket,
    scrub_candidates,
)
from panopticon_py.hunting.trade_aggregate import ParentTrade


class TestFingerprintScrubber(unittest.TestCase):
    def test_scrub_kelly_to_noise(self) -> None:
        addr = "0x" + "ab" * 20
        trades = [
            {"realized_pnl_usd": 100, "ts_ms": float(i), "side": "BUY", "size": 1.0, "market_id": "x"}
            for i in range(5)
        ]
        trades[0]["realized_pnl_usd"] = 10_000
        out = scrub_candidates([{"address": addr}], trades_by_address={addr: trades})
        self.assertEqual(out[0].label, "NOISE")

    def test_graduate_uncertain(self) -> None:
        addr = "0x" + "cd" * 20
        parents = [
            ParentTrade(addr, 1, 100.0, float(i), float(i) + 1, 1, "m1") for i in range(5)
        ]
        st = UncertainWalletState(
            address=addr,
            verified_profitable_trades=5,
            total_verified_trades=8,
            wins=6,
            last_trade_ts_utc="2026-01-15T00:00:00+00:00",
            parents_for_4d=parents,
        )
        transitions, remaining, arch = evaluate_uncertain_bucket(
            {addr: st},
            graduation_trades=5,
            eviction_min_trades=99,
            now_utc="2026-02-01T00:00:00+00:00",
        )
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].to_label, "SMART_MONEY_QUANT")
        self.assertEqual(remaining, {})

    def test_evict_low_win_rate(self) -> None:
        addr = "0x" + "ef" * 20
        st = UncertainWalletState(
            address=addr,
            verified_profitable_trades=0,
            total_verified_trades=20,
            wins=5,
            last_trade_ts_utc="2026-01-15T00:00:00+00:00",
            parents_for_4d=[],
        )
        transitions, remaining, _ = evaluate_uncertain_bucket(
            {addr: st},
            graduation_trades=99,
            eviction_min_trades=15,
            eviction_win_rate=0.4,
            now_utc="2026-02-01T00:00:00+00:00",
        )
        self.assertEqual(transitions[0].to_label, "NOISE")
        self.assertEqual(remaining, {})
