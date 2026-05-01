"""
D117-4: Arb Scanner — Paper Trade Mode
目標：WebSocket 監聽訂單簿，記錄符合打水條件的機會
本階段：只記錄，不下單

D117: Initial skeleton — paper mode only, no real execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PROCESS_VERSION = "v0.1.0-D117"

ARB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ARB_THRESHOLD = 0.97
ARB_MIN_DEPTH = 50
PAPER_MODE = True  # D117: enforced — no real orders


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class ArbOpportunity:
    ts: float
    market_id: str
    yes_price: float
    no_price: float
    total: float
    depth: float
    locked_profit_per_100: float
    paper_mode: bool = PAPER_MODE


@dataclass
class ArbScanner:
    paper_mode: bool = PAPER_MODE
    books: dict[str, dict[str, PriceLevel]] = field(
        default_factory=lambda: defaultdict(dict)
    opportunities_log: list[ArbOpportunity] = field(default_factory=list)
    _ws: object = field(default=None, init=False, repr=False)

    async def run(self, market_ids: list[str]) -> None:
        """
        Main entry point. Registers singleton, then runs the WebSocket listener loop.
        """
        from panopticon_py.utils.process_guard import acquire_singleton
        acquire_singleton("arb_scanner", PROCESS_VERSION)

        logger.info("[ARB] Starting ArbScanner paper_mode=%s", PAPER_MODE)
        while True:
            try:
                await self._connect_and_listen(market_ids)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ARB] WebSocket disconnected: %s, reconnecting in 5s…", e)
                await asyncio.sleep(5)

    async def _connect_and_listen(self, market_ids: list[str]) -> None:
        import websockets
        sub_msg = {"type": "subscribe", "markets": market_ids}
        async with websockets.connect(ARB_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            await ws.send(json.dumps(sub_msg))
            async for raw in ws:
                from panopticon_py.utils.process_guard import update_heartbeat
                update_heartbeat("arb_scanner")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._on_message(data)

    async def _on_message(self, data: dict) -> None:
        market_id = data.get("market_id")
        if not market_id or not isinstance(data, dict):
            return

        if "best_ask" in data:
            outcome = data.get("outcome", "YES")
            try:
                price = float(data["best_ask"])
                size = float(data.get("best_ask_size", 0))
            except (ValueError, TypeError):
                return
            self.books[market_id][outcome] = PriceLevel(price=price, size=size)

        await self._check_arb(market_id)

    async def _check_arb(self, market_id: str) -> None:
        book = self.books.get(market_id, {})
        yes = book.get("YES")
        no = book.get("NO")
        if not yes or not no:
            return

        total = yes.price + no.price
        min_depth = min(yes.size, no.size)

        if total < ARB_THRESHOLD and min_depth >= ARB_MIN_DEPTH:
            profit = round((1.0 - total) * 100, 4)
            opp = ArbOpportunity(
                ts=time.time(),
                market_id=market_id,
                yes_price=yes.price,
                no_price=no.price,
                total=total,
                depth=min_depth,
                locked_profit_per_100=profit,
                paper_mode=self.paper_mode,
            )
            self.opportunities_log.append(opp)
            mode_str = "[PAPER]" if self.paper_mode else "[LIVE]"
            logger.info(
                "[ARB_OPP] %s | YES=%.3f NO=%.3f total=%.3f profit/100shares=+$%.2f %s",
                market_id, yes.price, no.price, total, profit, mode_str,
            )


async def main() -> None:
    scanner = ArbScanner(paper_mode=PAPER_MODE)
    market_ids = os.getenv("ARB_MARKET_IDS", "").split(",")
    if not market_ids or market_ids == [""]:
        logger.warning("[ARB] No ARB_MARKET_IDS set; exiting.")
        return
    await scanner.run(market_ids)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    asyncio.run(main())
