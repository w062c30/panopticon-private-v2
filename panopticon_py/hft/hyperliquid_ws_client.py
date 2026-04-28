"""Hyperliquid Cross-Venue OFI Engine.

Emits UNDERLYING_SHOCK events when order flow imbalance on Hyperliquid
(Lead exchange) exceeds threshold within a 100ms rolling window, guarded by
real-volume floor per [Invariant 4.1] Ghost Liquidity Filter.

Multi-coin support: subscribes to BTC/USDT, BTC/USDC, ETH/USDT, ETH/USDC,
ETH/USD simultaneously.  Each coin has its own OFI window; any coin can fire
a SHOCK independently.

Timestamp alignment: Hyperliquid provides epoch_ms per trade.  All internal
comparisons use exchange-provided wall-clock ms, NOT monotonic recv time, so
cross-venue correlation with Polymarket is valid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Pair-mapping layer — user-facing pairs resolved to Hyperliquid coin symbols
# --------------------------------------------------------------------------- #
# User-facing pairs the HFT engine is asked to monitor.
# All collapse to Hyperliquid's coin subscription identifiers (BTC, ETH, ...).
HYPERLIQUID_SUPPORTED_PAIRS = ["BTCUSDT", "BTCUSDC", "ETHUSDT", "ETHUSDC", "ETHUSD"]

# Canonical Hyperliquid coin for each user-facing pair.
# Hyperliquid WS "coin" field = the base coin only (BTC, ETH).
PAIR_TO_COIN: dict[str, str] = {
    "BTCUSDT": "BTC",
    "BTCUSDC": "BTC",
    "ETHUSDT": "ETH",
    "ETHUSDC": "ETH",
    "ETHUSD":  "ETH",
}


def _resolve_pairs_from_env() -> list[str]:
    """
    Resolve user-requested pairs from HYPERLIQUID_PAIRS env var.
    Defaults to HYPERLIQUID_SUPPORTED_PAIRS.  Logs which pairs collapse
    to which Hyperliquid coin so the operator understands feed coverage.
    """
    raw = os.getenv(
        "HYPERLIQUID_PAIRS",
        ",".join(HYPERLIQUID_SUPPORTED_PAIRS),
    ).strip()
    requested = [p.strip().upper() for p in raw.split(",") if p.strip()]

    # Deduplicate coins after mapping
    coin_map: dict[str, list[str]] = {}
    for pair in requested:
        coin = PAIR_TO_COIN.get(pair)
        if coin is None:
            logger.warning("hl_ws_unknown_pair_ignored", extra={"pair": pair})
            continue
        coin_map.setdefault(coin, []).append(pair)

    resolved_coins = list(coin_map.keys())

    logger.info(
        "hl_ws_pair_resolve",
        extra={
            "user_requested_pairs": requested,
            "collapsed_coins": resolved_coins,
            "coin_to_pairs": coin_map,
        },
    )
    return resolved_coins


# --------------------------------------------------------------------------- #
#  Default coin list — env-overrideable via HYPERLIQUID_COINS (comma-separated)
# --------------------------------------------------------------------------- #
DEFAULT_COINS = ["BTC", "ETH"]


def _coins_from_env() -> list[str]:
    raw = os.getenv("HYPERLIQUID_COINS", ",".join(DEFAULT_COINS)).strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


# --------------------------------------------------------------------------- #
#  Hyperliquid WebSocket subscription payloads
# --------------------------------------------------------------------------- #

def _trade_subscription(coin: str) -> dict[str, Any]:
    return {
        "method": "subscribe",
        "subscription": {"type": "trades", "coin": coin},
    }


def _orderbook_subscription(coin: str, n_levels: int = 10) -> dict[str, Any]:
    return {
        "method": "subscribe",
        "subscription": {"type": "l2Book", "coin": coin, "nLevels": n_levels},
    }


# --------------------------------------------------------------------------- #
#  Core OFI rolling window — 100ms, Volume-Floor guarded
# --------------------------------------------------------------------------- #

@dataclass
class OFIWindow:
    """
    100ms rolling-order-flow-imbalance window with ghost-liquidity guard.

    Events inside the window are triples: (epoch_ms, side, notional_usd).
    OFI is the signed sum of notional by side (buy = +1, sell = -1).
    A shock fires only when |OFI| exceeds the imbalance threshold AND
    total notional crossing the window exceeds the volume floor —
    preventing MM quote-cancel spoofing from triggering a false signal.
    """
    window_ms: float = 100.0
    volume_floor_usd: float = 500.0
    imbalance_threshold: float = 0.60   # fraction of total notional that must be 1-sided
    _events: deque[tuple[float, str, float]] = field(default_factory=deque)
    _last_recv_mono: float | None = None

    def push(self, epoch_ms: float, side: str, notional_usd: float) -> bool:
        """
        Returns True if an UNDERLYING_SHOCK should fire.
        """
        # --- Stale-buffer flush: 500ms gap → lock triggers (Invariant 1.3) --- #
        recv_mono = time.monotonic()
        if self._last_recv_mono is not None:
            dt_mono = recv_mono - self._last_recv_mono
            if dt_mono > 5.0:   # 5000x real gap to detect reconnect-class stalls
                logger.warning(
                    "ofi_recv_gap_stale_flush",
                    extra={"dt_sec": round(dt_mono, 3), "epoch_ms": epoch_ms},
                )
                self._events.clear()
                self._last_recv_mono = recv_mono
                return False
        self._last_recv_mono = recv_mono

        # --- Evict events outside the 100ms window --- #
        cutoff = epoch_ms - self.window_ms
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        self._events.append((epoch_ms, side, max(0.0, notional_usd)))

        if len(self._events) < 2:
            return False

        # --- OFI computation --- #
        total = sum(e[2] for e in self._events)
        if total < self.volume_floor_usd:
            return False   # [Invariant 4.1] Ghost Liquidity — price moved but no real volume

        ofi = sum(e[2] * (1 if e[1] == "BUY" else -1) for e in self._events)
        imbalance = abs(ofi) / max(total, 1e-12)

        return imbalance >= self.imbalance_threshold

    def reset(self) -> None:
        self._events.clear()
        self._last_recv_mono = None


# --------------------------------------------------------------------------- #
#  UNDERLYING_SHOCK event payload
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class UnderlyingShock:
    """
    Emitted asynchronously when the Hyperliquid OFI engine detects a genuine
    market-shock condition.
    """
    shock_id: str
    hl_epoch_ms: float           # Hyperliquid exchange-provided wall-clock ms
    ofi_value: float             # net order-flow imbalance in window
    window_total_notional: float # USD notional in the 100ms window
    price_before: float          # mid-price at window start
    price_after: float            # mid-price at shock detection
    delta_t_ms: float             # time from first event in window to shock detection
    recv_ts_utc: str             # ISO-8601 UTC at receive time


# --------------------------------------------------------------------------- #
#  Hyperliquid OFI Engine
# --------------------------------------------------------------------------- #

class HyperliquidOFIEngine:
    """
    Connects to the Hyperliquid WebSocket, subscribes to MULTIPLE coin pairs
    (BTC, ETH, etc.) trades and L2 order-books, maintains independent 100ms
    OFI windows per coin, and fires UNDERLYING_SHOCK events through a user-provided
    async callback when any coin's imbalance exceeds threshold.

    Pair-mapping: user-facing pairs (``BTCUSDT``, ``BTCUSDC``, ``ETHUSDT``,
    ``ETHUSDC``, ``ETHUSD``) are resolved to Hyperliquid coin identifiers at
    startup.  Multiple pairs can map to the same coin (e.g. ``BTCUSDT`` and
    ``BTCUSDC`` both collapse to ``BTC``).  Set ``HYPERLIQUID_PAIRS`` env var
    to override the default pair set.

    Parameters
    ----------
    on_shock
        Async callback invoked with ``UnderlyingShock`` when a shock is detected.
        Must be a callable of type ``Callable[[UnderlyingShock], Coroutine]``.
    url
        WebSocket endpoint.  Defaults to the public Hyperliquid testnet endpoint.
    coins
        List of coin symbols to subscribe to (default: resolved from HYPERLIQUID_PAIRS).
        Override via HYPERLIQUID_COINS env var (comma-separated coin symbols).
    """

    def __init__(
        self,
        on_shock: Callable[[UnderlyingShock], Coroutine[Any, Any, None]],
        *,
        url: str | None = None,
        coins: list[str] | None = None,
    ) -> None:
        self._url = url or os.getenv(
            "HYPERLIQUID_WS_URL",
            "wss://api.hyperliquid-testnet.xyz/ws",
        ).strip()
        self._coins = coins or _resolve_pairs_from_env()
        self._on_shock = on_shock

        # Per-coin OFI windows — each coin tracks its own 100ms imbalance
        ofi_window_ms  = float(os.getenv("HFT_OFI_WINDOW_MS", "100.0"))
        vol_floor      = float(os.getenv("HFT_VOLUME_FLOOR_USD", "500.0"))
        imb_threshold  = float(os.getenv("HFT_OFI_THRESHOLD", "0.60"))

        self._ofi_by_coin: dict[str, OFIWindow] = {
            coin: OFIWindow(
                window_ms=ofi_window_ms,
                volume_floor_usd=vol_floor,
                imbalance_threshold=imb_threshold,
            )
            for coin in self._coins
        }

        self._close_event = asyncio.Event()
        self._running = False
        self._hl_recv_last: float | None = None
        self._price_windows: dict[str, deque[tuple[float, float]]] = {
            coin: deque(maxlen=100) for coin in self._coins
        }
        self._last_book_mids: dict[str, float] = {coin: 0.0 for coin in self._coins}

    # --------------------------------------------------------------------- #
    #  Public lifecycle
    # --------------------------------------------------------------------- #

    async def run(self) -> None:
        """Main receive loop — reconnects automatically on transient errors."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0   # reset on successful connection
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                logger.warning("hl_ws_reconnect_backoff", extra={"backoff_sec": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    def stop(self) -> None:
        self._running = False
        self._close_event.set()

    # --------------------------------------------------------------------- #
    #  Internal: WebSocket session
    # --------------------------------------------------------------------- #

    async def _connect(self) -> None:
        import websockets   # type: ignore[import-not-found]
        backoff_secs = float(os.getenv("HUNT_WS_BACKOFF_SEC", "3.0"))

        # Log resolved pair → coin map for operator clarity
        coin_to_pairs: dict[str, list[str]] = {}
        for pair, coin in PAIR_TO_COIN.items():
            if coin in self._coins:
                coin_to_pairs.setdefault(coin, []).append(pair)
        logger.info(
            "hl_ws_pair_map",
            extra={
                "url": self._url,
                "resolved_coins": self._coins,
                "coin_to_pairs": coin_to_pairs,
            },
        )

        async with websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as ws:
            # Subscribe to trades and L2 book for ALL configured coins
            for coin in self._coins:
                await ws.send(json.dumps(_trade_subscription(coin)))
                await ws.send(json.dumps(_orderbook_subscription(coin)))
                logger.info("hl_ws_subscribed_coin", extra={"coin": coin})

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    continue
                self._dispatch(msg)

    # --------------------------------------------------------------------- #
    #  Internal: message dispatch
    # --------------------------------------------------------------------- #

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route incoming message to trade or book handler for the specific coin."""
        sub = msg.get("subscription", {}) if isinstance(msg, dict) else {}
        sub_type = sub.get("type") if isinstance(sub, dict) else None
        coin = sub.get("coin") if isinstance(sub, dict) else None

        if sub_type == "trades" and coin and coin in self._ofi_by_coin:
            self._handle_trades(coin, msg.get("data") or msg)
        elif sub_type == "l2Book" and coin and coin in self._ofi_by_coin:
            self._handle_book(coin, msg.get("data") or msg)

    def _handle_trades(self, coin: str, data: Any) -> None:
        """Process one or more trade records for a specific coin.

        Each record carries ``p`` (price), ``s`` (side: "BUY"|"SELL"),
        ``sz`` (size in base units), ``epochMs`` (Hyperliquid epoch ms).
        ``ts`` (unix timestamp) and ``hash`` are also present.
        """
        if not isinstance(data, list):
            data = [data]

        ofi = self._ofi_by_coin[coin]
        price_window = self._price_windows[coin]
        last_book_mid = self._last_book_mids[coin]

        for trade in data:
            if not isinstance(trade, dict):
                continue

            try:
                epoch_ms = float(trade.get("epochMs") or trade.get("ts") or 0)
                price    = float(trade.get("p") or 0)
                side     = str(trade.get("s") or "BUY").upper()
                size     = float(trade.get("sz") or 0)
            except (TypeError, ValueError):
                continue

            if price <= 0 or size <= 0 or epoch_ms <= 0:
                continue

            notional = price * size

            # Update last-seen Hyperliquid epoch_ms for timestamp tracking
            self._hl_recv_last = epoch_ms

            # Update rolling mid-price window for this coin
            if last_book_mid > 0:
                price_window.append((epoch_ms, last_book_mid))

            # Push into OFI window
            try:
                shock = ofi.push(epoch_ms, side, notional)
            except Exception:
                logger.exception("ofi_push_error", extra={"coin": coin})
                continue

            if shock:
                self._emit_shock(coin, epoch_ms, notional)

    def _handle_book(self, coin: str, data: Any) -> None:
        """Process L2 book snapshot — update mid-price for shock metadata."""
        try:
            if isinstance(data, dict):
                bids = data.get("bids") or data.get("levels", {}).get("bids") or []
                asks = data.get("asks") or data.get("levels", {}).get("asks") or []
            elif isinstance(data, list):
                bids = []
                asks = []
                for item in data:
                    if isinstance(item, dict) and item.get("side") == "BUY":
                        bids.append(item)
                    elif isinstance(item, dict) and item.get("side") == "SELL":
                        asks.append(item)
            else:
                return

            if not bids or not asks:
                return

            best_bid = float(bids[0].get("px") or bids[0].get("price") or 0)
            best_ask = float(asks[0].get("px") or asks[0].get("price") or 0)
            if best_bid > 0 and best_ask > 0:
                self._last_book_mids[coin] = (best_bid + best_ask) / 2.0
        except (TypeError, ValueError, IndexError):
            pass

    # --------------------------------------------------------------------- #
    #  Internal: shock emission
    # --------------------------------------------------------------------- #

    def _emit_shock(self, coin: str, shock_epoch_ms: float, notional_at_shock: float) -> None:
        """Fire UNDERLYING_SHOCK asynchronously — non-blocking on hot path."""
        ofi = self._ofi_by_coin[coin]
        events = list(ofi._events)
        total  = sum(e[2] for e in events)
        ofi_value = sum(e[2] * (1 if e[1] == "BUY" else -1) for e in events)

        price_window = self._price_windows[coin]
        last_book_mid = self._last_book_mids[coin]

        # Price before: earliest mid in window, or last known book mid
        price_before = 0.0
        if price_window:
            price_before = price_window[0][1]
        elif last_book_mid > 0:
            price_before = last_book_mid

        # Price after: most recent book mid
        price_after = last_book_mid if last_book_mid > 0 else price_before

        delta_ms = 0.0
        if events:
            delta_ms = shock_epoch_ms - events[0][0]

        shock = UnderlyingShock(
            shock_id=f"hl_shock_{coin}_{int(shock_epoch_ms)}_{int(delta_ms)}",
            hl_epoch_ms=shock_epoch_ms,
            ofi_value=ofi_value,
            window_total_notional=total,
            price_before=price_before,
            price_after=price_after,
            delta_t_ms=delta_ms,
            recv_ts_utc=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        )

        logger.info(
            "HFT_UNDERLYING_SHOCK",
            extra={
                "shock_id": shock.shock_id,
                "coin": coin,
                "hl_epoch_ms": shock.hl_epoch_ms,
                "ofi_value": round(shock.ofi_value, 2),
                "window_notional": round(shock.window_total_notional, 2),
                "price_before": round(shock.price_before, 4),
                "price_after": round(shock.price_after, 4),
                "delta_t_ms": round(shock.delta_t_ms, 1),
            },
        )

        # Non-blocking async fire — hot path must not await here
        asyncio.create_task(self._safe_shock_cb(shock))

    async def _safe_shock_cb(self, shock: UnderlyingShock) -> None:
        """Invoke user callback with full exception trapping."""
        try:
            await self._on_shock(shock)
        except Exception:
            logger.exception("hft_shock_callback_error", extra={"shock_id": shock.shock_id})
