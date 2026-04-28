"""
panopticon_py/ingestion/insider_detector.py
D68: Three-layer insider detection engine.

L1: Single large trade  (>$200 default)
L2: Rapid same-direction accumulation (3 trades / 3 min)
L3: Async historical win-rate check (>70% in 7 days)

Identity key: proxyWallet (RULE-ARCH-WS-3)
"""

import threading
import time
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from panopticon_py.ingestion.polymarket_streams import (
    PolyTrade, MarketTradePoller, fetch_wallet_history
)

logger = logging.getLogger(__name__)


@dataclass
class InsiderAlert:
    proxy_wallet:    str
    name:            str
    trigger:         str
    usd_size:        float
    outcome:         str
    timestamp:       int
    tx_hash:         str
    event_slug:      str
    condition_id:    str
    win_rate_7d:     float = 0.0
    total_vol_7d:    float = 0.0


class InsiderDetector:
    """
    Three-layer insider detection per Architect D68 ruling.

    L1: usd_size >= large_trade_usd  → immediate alert
    L2: same wallet, same outcome, >= rapid_count trades in rapid_window secs
    L3: async win-rate scan on wallet history (non-blocking)

    Usage:
        detector = InsiderDetector(
            condition_id="0xdd22...",
            on_alert=my_handler,
        )
        detector.start()
        time.sleep(600)
        detector.stop()
    """

    def __init__(
        self,
        condition_id:    str,
        on_alert:        Optional[Callable[[InsiderAlert], None]] = None,
        on_trade:        Optional[Callable[[PolyTrade], None]] = None,
        large_trade_usd: float = 200.0,
        rapid_window:    int   = 180,   # seconds
        rapid_count:     int   = 3,
        high_winrate:    float = 0.70,
        min_usd:         float = 10.0,
        poll_interval:   float = 4.0,
    ):
        self.condition_id    = condition_id
        self.on_alert        = on_alert
        self.on_trade        = on_trade
        self.large_trade_usd = large_trade_usd
        self.rapid_window    = rapid_window
        self.rapid_count     = rapid_count
        self.high_winrate    = high_winrate

        self._wallet_recent: dict[str, list[PolyTrade]] = {}
        self._alerted:       set[str] = set()

        self._poller = MarketTradePoller(
            condition_id  = condition_id,
            on_trade      = self._evaluate,
            min_usd       = min_usd,
            poll_interval = poll_interval,
        )

    def _evaluate(self, trade: PolyTrade):
        # L1
        if trade.usdc_size >= self.large_trade_usd:
            self._alert(trade, f"L1_LARGE ${trade.usdc_size:.0f}")

        # L2
        now_ms  = trade.timestamp
        recent  = self._wallet_recent.setdefault(trade.proxy_wallet, [])
        recent.append(trade)
        cutoff  = now_ms - self.rapid_window * 1000
        recent[:] = [t for t in recent if t.timestamp >= cutoff]
        same  = [t for t in recent if t.outcome == trade.outcome]
        if len(same) >= self.rapid_count:
            total = sum(t.usdc_size for t in same)
            self._alert(
                trade,
                f"L2_RAPID x{len(same)} {trade.outcome} ${total:.0f}"
            )

        # L3 async
        threading.Thread(
            target=self._check_history, args=(trade,), daemon=True
        ).start()

        # Forward to external on_trade hook if set
        if self.on_trade:
            try:
                self.on_trade(trade)
            except Exception as e:
                logger.warning("[INSIDER] on_trade hook error: %s", e)

    def _check_history(self, trade: PolyTrade):
        key = f"L3:{trade.proxy_wallet}"
        if key in self._alerted:
            return
        history = fetch_wallet_history(trade.proxy_wallet, limit=200)
        if len(history) < 5:
            return
        now_ms  = int(time.time() * 1000)
        week_ms = 7 * 24 * 3600 * 1000
        recent  = [t for t in history if now_ms - t.timestamp < week_ms]
        if len(recent) < 5:
            return
        buys  = [t for t in recent if t.side == "BUY"]
        sells = [t for t in recent if t.side == "SELL"]
        buy_vol  = sum(t.usdc_size for t in buys)
        sell_vol = sum(t.usdc_size for t in sells)
        if buy_vol == 0:
            return
        win_rate  = sell_vol / buy_vol
        total_vol = sum(t.usdc_size for t in recent)
        if win_rate >= self.high_winrate:
            alert = self._alert(
                trade,
                f"L3_WINRATE {win_rate:.0%} 7d=${total_vol:.0f}"
            )
            if alert:
                alert.win_rate_7d  = win_rate
                alert.total_vol_7d = total_vol

    def _alert(
        self, trade: PolyTrade, trigger: str
    ) -> Optional[InsiderAlert]:
        key = f"{trade.proxy_wallet}:{trigger[:25]}"
        if key in self._alerted:
            return None
        self._alerted.add(key)
        alert = InsiderAlert(
            proxy_wallet = trade.proxy_wallet,
            name         = trade.name or trade.pseudonym,
            trigger      = trigger,
            usd_size     = trade.usdc_size,
            outcome      = trade.outcome,
            timestamp    = trade.timestamp,
            tx_hash      = trade.transaction_hash,
            event_slug   = trade.event_slug,
            condition_id = trade.condition_id,
        )
        logger.warning(
            "INSIDER [%s] %s (%s...) $%.0f %s %s",
            trigger,
            alert.name or "anon",
            alert.proxy_wallet[:12],
            alert.usd_size,
            trade.side,
            trade.outcome,
        )
        if self.on_alert:
            self.on_alert(alert)
        return alert

    def start(self):
        self._poller.start()
        logger.info("[INSIDER] watching conditionId=%s...",
                    self.condition_id[:16])

    def stop(self):
        self._poller.stop()
