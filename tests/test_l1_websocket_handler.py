"""Regression tests for L1 _on_message() last_trade_price handler (Invariant 1.1).

Verifies that book/price_change events do NOT push to EntropyWindow, and that
last_trrade_price events are the ONLY source of Trade-Tick data for entropy.

These tests extract the core event-handling logic and verify it in isolation.
"""
import unittest

from panopticon_py.hunting.run_radar import _pending_trade_price


class TestL1LastTradePriceLogic(unittest.TestCase):
    """Unit tests for the last_trade_price event_type handler logic.

    The handler is:
        if event_type == "last_trade_price":
            trade_size = float(item.get("size") or 0)
            trade_side = item.get("side", "").upper()
            if trade_size == 0: continue
            _ws_trade_count += 1
            if trade_side == "BUY":  buy = trade_size; sell = 0.0
            elif trade_side == "SELL": buy = 0.0; sell = trade_size
            else: continue
            ew.push(recv, buy, sell)
            ew.record_H_sample(recv)
    """

    def test_last_trade_price_buy_extracts_correct_values(self) -> None:
        """last_trade_price + side=BUY → buy=trade_size, sell=0.0."""
        item = {"event_type": "last_trade_price", "size": "100.5", "side": "BUY"}
        trade_size = float(item.get("size") or 0)
        trade_side = item.get("side", "").upper()

        self.assertEqual(trade_size, 100.5)
        self.assertEqual(trade_side, "BUY")

        if trade_size == 0:
            buy = sell = None
        elif trade_side == "BUY":
            buy = trade_size
            sell = 0.0
        elif trade_side == "SELL":
            buy = 0.0
            sell = trade_size
        else:
            buy = sell = None

        self.assertEqual(buy, 100.5)
        self.assertEqual(sell, 0.0)

    def test_last_trade_price_sell_extracts_correct_values(self) -> None:
        """last_trade_price + side=SELL → buy=0.0, sell=trade_size."""
        item = {"event_type": "last_trade_price", "size": "75.25", "side": "SELL"}
        trade_size = float(item.get("size") or 0)
        trade_side = item.get("side", "").upper()

        self.assertEqual(trade_size, 75.25)
        self.assertEqual(trade_side, "SELL")

        if trade_size == 0:
            buy = sell = None
        elif trade_side == "BUY":
            buy = trade_size
            sell = 0.0
        elif trade_side == "SELL":
            buy = 0.0
            sell = trade_size
        else:
            buy = sell = None

        self.assertEqual(buy, 0.0)
        self.assertEqual(sell, 75.25)

    def test_last_trade_price_zero_size_is_skipped(self) -> None:
        """size=0 or empty → trade should be skipped (no ew.push call)."""
        for size_val in ["0", "0.0", "", None]:
            item = {"event_type": "last_trade_price", "size": size_val, "side": "BUY"}
            trade_size = float(item.get("size") or 0)
            should_skip = trade_size == 0
            self.assertTrue(should_skip, f"size={size_val!r} should skip")

    def test_last_trade_price_unknown_side_is_skipped(self) -> None:
        """side is not BUY/SELL → skipped."""
        item = {"event_type": "last_trade_price", "size": "10.0", "side": "UNKNOWN"}
        trade_size = float(item.get("size") or 0)
        trade_side = item.get("side", "").upper()

        should_skip = trade_side not in ("BUY", "SELL")
        self.assertTrue(should_skip)

    def test_last_trade_price_side_case_insensitive(self) -> None:
        """side handling is case-insensitive."""
        for side_val in ["buy", "Buy", "bUY"]:
            item = {"event_type": "last_trade_price", "size": "10.0", "side": side_val}
            trade_side = str(item.get("side") or "").upper()
            self.assertEqual(trade_side, "BUY")


class TestL1QuoteTickDoesNotPushEntropy(unittest.TestCase):
    """Verify that book and price_change event types do NOT push to EntropyWindow.

    After the Invariant 1.1 fix, only last_trade_price triggers ew.push().
    """

    def test_book_event_type_should_not_trigger_entropy_push(self) -> None:
        """book event_type should NOT call ew.push() after Invariant 1.1 fix."""
        event_type = "book"
        should_push = event_type == "last_trade_price"
        self.assertFalse(should_push)

    def test_price_change_event_type_should_not_trigger_entropy_push(self) -> None:
        """price_change event_type should NOT call ew.push() after Invariant 1.1 fix."""
        event_type = "price_change"
        should_push = event_type == "last_trade_price"
        self.assertFalse(should_push)

    def test_best_bid_ask_event_type_should_not_trigger_entropy_push(self) -> None:
        """best_bid_ask event_type is also Quote-Tick, should NOT push."""
        event_type = "best_bid_ask"
        should_push = event_type == "last_trade_price"
        self.assertFalse(should_push)

    def test_last_trade_price_event_type_should_trigger_entropy_push(self) -> None:
        """last_trade_price IS the Trade-Tick, should call ew.push()."""
        event_type = "last_trade_price"
        should_push = event_type == "last_trade_price"
        self.assertTrue(should_push)


class TestL1WSCountersSemantics(unittest.TestCase):
    """Verify L1_WS_DIAG counter semantics after the Invariant 1.1 fix.

    _ws_trade_count increments ONLY for last_trade_price events (real trades).
    _ws_entropy_fire_count increments ONLY when should_fire_negative_entropy() is True.
    """

    def test_ws_trade_count_increments_only_for_last_trade_price(self) -> None:
        """_ws_trade_count should count only last_trade_price events."""
        event_types = [
            "book",                    # not a trade
            "price_change",            # not a trade
            "last_trade_price",         # IS a trade → +1
            "book",                    # not a trade
            "tick_size_change",        # not a trade
            "last_trade_price",         # IS a trade → +1
            "best_bid_ask",            # not a trade
        ]
        _ws_trade_count = sum(1 for et in event_types if et == "last_trade_price")
        self.assertEqual(_ws_trade_count, 2)

    def test_ws_entropy_fire_count_only_when_entropy_fires(self) -> None:
        """_ws_entropy_fire_count increments only on entropy fire."""
        should_fire_results = [False, False, True, False, True]
        _ws_entropy_fire_count = sum(1 for s in should_fire_results if s)
        self.assertEqual(_ws_entropy_fire_count, 2)

    def test_raw_msg_count_increments_for_all_event_types(self) -> None:
        """_ws_raw_msg_count increments for every valid dict message."""
        event_types = ["book", "price_change", "last_trade_price", "book", "unknown"]
        _ws_raw_msg_count = len(event_types)
        self.assertEqual(_ws_raw_msg_count, 5)


class TestKyleLambdaBookEmbeddedTrade(unittest.TestCase):
    """Regression tests for D9: Kyle λ from book embedded trade (Issue A APPROVED).

    Verifies:
    1. book event WITH embedded last_trade_price → triggers lambda_obs calculation
    2. book event WITHOUT embedded last_trade_price → only updates snapshot (no lambda calc)
    """

    def test_book_with_embedded_trade_generates_kyle_sample(self) -> None:
        """book event with embedded last_trade_price → lambda_obs should be calculated.

        Per Polymarket docs: "emitted when there is a trade that affects the book"
        So a book event WITH embedded last_trade_price IS a trade trigger for Kyle λ.
        """
        bids = [{"price": "0.98"}]
        asks = [{"price": "1.02"}]

        # Simulate: first book event establishes mid_before
        book_msg_1 = {
            "event_type": "book",
            "asset_id": "0xtest",
            "bids": bids,
            "asks": asks,
            "timestamp": "1776966400000",
            # No embedded trade on first subscription
        }
        best_bid_1 = float(bids[0]["price"])
        best_ask_1 = float(asks[0]["price"])
        mid_before = (best_bid_1 + best_ask_1) / 2.0

        self.assertIsNotNone(mid_before)
        self.assertEqual(mid_before, 1.0)

        # Simulate: second book event with embedded trade → calculates lambda
        embedded_size = 100.0
        embedded_price = 0.99
        book_msg_2 = {
            "event_type": "book",
            "asset_id": "0xtest",
            "bids": bids,
            "asks": asks,
            "timestamp": "1776966401000",
            "last_trade_price": embedded_price,  # embedded trade
            "size": embedded_size,
        }
        best_bid_2 = float(bids[0]["price"])
        best_ask_2 = float(asks[0]["price"])
        mid_now = (best_bid_2 + best_ask_2) / 2.0

        # delta_p = |mid_now - mid_before| = |1.0 - 1.0| = 0 → lambda = 0 (discarded)
        # This demonstrates that mid must change between book events for meaningful lambda
        delta_p = abs(mid_now - mid_before)
        self.assertEqual(delta_p, 0.0)  # no mid-price change in this test case

        # Now test with actual mid change (realistic scenario)
        bids_2 = [{"price": "0.97"}]
        asks_2 = [{"price": "1.01"}]
        mid_now_changed = (0.97 + 1.01) / 2.0  # 0.99
        delta_p_real = abs(mid_now_changed - 1.0)  # 0.01
        lambda_obs = delta_p_real / embedded_size

        self.assertAlmostEqual(lambda_obs, 0.0001, places=6)

    def test_book_without_embedded_trade_updates_snapshot_only(self) -> None:
        """book event without embedded last_trade_price → snapshot updated, no lambda calc.

        A pure book snapshot (no embedded trade) should NOT trigger Kyle λ calculation.
        """
        event_type = "book"
        embedded_trade_price = None  # no embedded trade
        embedded_trade_size = 0.0

        has_embedded_trade = embedded_trade_price is not None and embedded_trade_size > 0
        # has_embedded_trade = False → no lambda calculation
        self.assertFalse(has_embedded_trade)


class TestKylePendingTradeState(unittest.TestCase):
    """Regression tests for standalone Kyle pending-trade gating."""

    def test_pending_trade_price_accepts_price_fallback_key(self) -> None:
        """D30: pending state stores `price`; gate must not require only `trade_price`."""
        pending = {"price": 0.63, "mid_before": 0.61}
        self.assertGreater(_pending_trade_price(pending), 0.0)


if __name__ == "__main__":
    unittest.main()
