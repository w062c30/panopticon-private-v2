import unittest

from panopticon_py.hunting.consensus_radar import (
    ConsensusRadarConfig,
    detect_consensus_cluster,
    make_liquidity_ref_fn,
    monitor_basket_activity,
)
from panopticon_py.strategy.iron_rules import NonFilledTradeEvidenceError, assert_filled_trade_rows


def _filled_trade(
    *,
    wallet: str,
    market: str,
    side: str,
    n: float,
    ts: float,
    outcome: str = "YES",
) -> dict:
    return {
        "taker": wallet,
        "market_id": market,
        "side": side,
        "notional_usd": n,
        "timestamp": ts,
        "outcome_side": outcome,
        "status": "MATCHED",
    }


class TestConsensusRadar(unittest.TestCase):
    def test_monitor_basket_filters(self) -> None:
        basket = {"0x" + "aa" * 20}
        tr = [
            _filled_trade(wallet="0x" + "aa" * 20, market="m1", side="BUY", n=100, ts=1_000.0),
            _filled_trade(wallet="0x" + "bb" * 20, market="m1", side="BUY", n=100, ts=1_000.0),
        ]
        kept = monitor_basket_activity(basket, tr, now_ts_sec=1_000.0, cfg=ConsensusRadarConfig(time_window_sec=500))
        self.assertEqual(len(kept), 1)

    def test_assert_filled_rejects_open(self) -> None:
        with self.assertRaises(NonFilledTradeEvidenceError):
            assert_filled_trade_rows([{"status": "OPEN", "filled": True}])

    def test_temporal_decay_reduces_k_eff(self) -> None:
        ts0 = 1_000_000.0
        trs = []
        for i in range(3):
            trs.append(
                _filled_trade(
                    wallet=f"0x{'%040x' % (0x100 + i)}",
                    market="m1",
                    side="BUY",
                    n=10_000,
                    ts=ts0 + i * 10.0 if i < 2 else ts0 + 100_000.0,
                )
            )
        cfg = ConsensusRadarConfig(
            lambda_decay_per_sec=1e-3,
            min_distinct_wallets=3,
            abs_min_usd=1.0,
            ref_pct=0.001,
            hybrid_link_window_sec=0.0,
        )
        liq = make_liquidity_ref_fn({"m1": 1_000_000.0}, {}, cfg)
        sig = detect_consensus_cluster(trs, liquidity_ref_usd=liq, cfg=cfg)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertLess(sig.w_time, 1.0)
        self.assertLess(sig.k_eff, sig.k_hybrid)

    def test_net_conviction_cancel(self) -> None:
        """Large opposing NO flow vs small YES net -> cancel mode skips signal."""
        wyes = ["0x" + f"{i:040x}" for i in range(3)]
        trs = []
        for i, a in enumerate(wyes):
            trs.append(_filled_trade(wallet=a, market="m1", side="BUY", n=5000, ts=1_000_000 + i, outcome="YES"))
        trs.append(
            _filled_trade(
                wallet="0x" + "ff" * 20,
                market="m1",
                side="BUY",
                n=80_000,
                ts=1_000_010,
                outcome="NO",
            )
        )
        cfg = ConsensusRadarConfig(
            opposing_ratio_threshold=0.2,
            opposing_mode="cancel",
            abs_min_usd=1.0,
            ref_pct=0.0001,
            min_net_directional_usd=100.0,
            min_distinct_wallets=3,
            hybrid_link_window_sec=0.001,
        )
        liq = make_liquidity_ref_fn({"m1": 1e9}, {}, cfg)
        sig = detect_consensus_cluster(trs, liquidity_ref_usd=liq, cfg=cfg)
        self.assertIsNone(sig)

    def test_net_conviction_penalize(self) -> None:
        cfg = ConsensusRadarConfig(
            opposing_ratio_threshold=0.2,
            opposing_mode="penalize",
            opposing_penalty_factor=0.5,
            abs_min_usd=1.0,
            ref_pct=0.0001,
            min_distinct_wallets=3,
            hybrid_link_window_sec=0.0,
        )
        trs = [
            _filled_trade(wallet="0x" + "01" * 20, market="m2", side="BUY", n=8000, ts=2e6, outcome="YES"),
            _filled_trade(wallet="0x" + "02" * 20, market="m2", side="BUY", n=8000, ts=2e6 + 1, outcome="YES"),
            _filled_trade(wallet="0x" + "03" * 20, market="m2", side="BUY", n=8000, ts=2e6 + 2, outcome="YES"),
            _filled_trade(wallet="0x" + "04" * 20, market="m2", side="BUY", n=5000, ts=2e6 + 3, outcome="NO"),
        ]
        liq = make_liquidity_ref_fn({"m2": 500_000.0}, {}, cfg)
        sig = detect_consensus_cluster(trs, liquidity_ref_usd=liq, cfg=cfg)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertLess(sig.k_eff, sig.k_hybrid * sig.w_time + 1e-9)
