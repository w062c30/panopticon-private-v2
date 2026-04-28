import unittest

from panopticon_py.liquidity_asymmetry import OrderBookSlice, bid_ask_imbalance, weighted_ask_entry_price


class LiquidityAsymmetryTests(unittest.TestCase):
    def test_weighted_ask_entry_uses_levels(self) -> None:
        book = OrderBookSlice(bid1=0.05, bid2=0.04, bid3=0.03, ask1=0.05, ask2=0.12, ask3=0.18)
        px = weighted_ask_entry_price(book)
        self.assertGreater(px, book.ask1)

    def test_bai_is_bounded(self) -> None:
        symmetric = OrderBookSlice(bid1=0.49, bid2=0.48, bid3=0.47, ask1=0.51, ask2=0.52, ask3=0.53)
        asymmetric = OrderBookSlice(bid1=0.05, bid2=0.04, bid3=0.03, ask1=0.15, ask2=0.16, ask3=0.17)
        self.assertLess(bid_ask_imbalance(symmetric), bid_ask_imbalance(asymmetric))
        self.assertGreaterEqual(bid_ask_imbalance(asymmetric), 0.0)
        self.assertLessEqual(bid_ask_imbalance(asymmetric), 1.0)


if __name__ == "__main__":
    unittest.main()
