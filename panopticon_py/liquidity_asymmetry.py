from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderBookSlice:
    bid1: float
    bid2: float
    bid3: float
    ask1: float
    ask2: float
    ask3: float


def weighted_ask_entry_price(book: OrderBookSlice, weights: tuple[float, float, float] = (0.5, 0.3, 0.2)) -> float:
    asks = [book.ask1, book.ask2, book.ask3]
    return sum(w * p for w, p in zip(weights, asks))


def bid_ask_imbalance(book: OrderBookSlice) -> float:
    spread = max(book.ask1 - book.bid1, 1e-6)
    mid = (book.ask1 + book.bid1) / 2
    asymmetry = abs((book.ask1 - mid) - (mid - book.bid1)) / spread
    return max(0.0, min(1.0, asymmetry))


def exit_liquidity_pressure(book: OrderBookSlice) -> float:
    """Measure how harsh exit would be if forced to hit bid1."""
    return max(0.0, book.ask1 - book.bid1)
