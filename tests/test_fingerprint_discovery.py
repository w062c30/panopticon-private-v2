import unittest

from panopticon_py.hunting.fingerprint_scrubber import (
    WalletTradeSample,
    compute_idi,
    detect_kelly_violation,
    scrub_wallet_for_discovery,
)


class FingerprintDiscoveryTests(unittest.TestCase):
    def test_idi_market_maker_drop(self) -> None:
        hist = [
            WalletTradeSample(side=1, notional_usd=100, balance_before_usd=1000, ts_ms=1),
            WalletTradeSample(side=-1, notional_usd=95, balance_before_usd=900, ts_ms=2),
        ]
        idi = compute_idi(hist)
        self.assertLess(idi, 0.3)
        result = scrub_wallet_for_discovery("0x" + "2" * 40, hist)
        self.assertEqual(result.drop_tag, "MARKET_MAKER")

    def test_kelly_violation_drop(self) -> None:
        hist = [
            WalletTradeSample(side=1, notional_usd=600, balance_before_usd=1000, ts_ms=1),
            WalletTradeSample(side=1, notional_usd=100, balance_before_usd=900, ts_ms=2),
        ]
        self.assertTrue(detect_kelly_violation(hist))
        result = scrub_wallet_for_discovery("0x" + "3" * 40, hist)
        self.assertEqual(result.drop_tag, "DEGEN_GAMBLER")


if __name__ == "__main__":
    unittest.main()
