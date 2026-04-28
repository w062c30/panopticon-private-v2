"""
panopticon_py/ingestion/polymarket_streams.py
D68: Real-time Polymarket data streams.
Three-layer architecture per RULE-ARCH-WS-1/2/3.

Layer 1: CLOB WebSocket  — orderbook + last_trade_price (no user identity)
Layer 2: RTDS WebSocket  — BTC/ETH spot reference price
Layer 3: data-api REST   — trades with proxyWallet (4s poll, no auth needed)

Endpoint verification (official docs):
  CLOB WS: wss://ws-subscriptions-clob.polymarket.com/ws/market
  RTDS WS: wss://ws-live-data.polymarket.com
  Trades:  https://data-api.polymarket.com/trades
  Activity:https://data-api.polymarket.com/activity
"""

from __future__ import annotations

import json
import threading
import time
import datetime
import logging
import requests
import websocket
from dataclasses import dataclass, asdict, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# ── Data Models ────────────────────────────────────────────────────────────

@dataclass
class ClobTrade:
    """CLOB WS last_trade_price — price+size only, NO user identity."""
    asset_id:      str
    price:         float
    size:          float
    side:          str
    timestamp:     int
    fee_rate_bps:  str = ""
    trader_side:   str = ""
    received_at:   str = field(
        default_factory=lambda: datetime.datetime.utcnow().isoformat()
    )


@dataclass
class PolyTrade:
    """
    Official data-api /trades response schema.
    proxyWallet = primary identity key (RULE-ARCH-WS-3).
    All fields confirmed from official response schema:
    https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
    """
    proxy_wallet:        str    # ^0x[a-fA-F0-9]{40}$ — 唯一身份
    name:                str    # display name — 可更改，非主鍵
    pseudonym:           str    # system-assigned stable alias
    side:                str    # BUY | SELL
    outcome:             str    # "Up" | "Down" | "Yes" | "No"
    price:               float  # 0–1
    size:                float  # shares
    usdc_size:           float  # real USD amount (official: usdcSize)
    timestamp:           int    # Unix ms
    transaction_hash:    str    # Polygon tx hash — cross-ref with chain
    condition_id:        str    # ^0x[a-fA-F0-9]{64}$
    event_slug:          str    # "btc-updown-5m-1777355100"
    asset:               str    # ERC1155 token_id
    title:               str    # market title
    slug:                str    # market slug
    outcome_index:       int    = 0
    bio:                 str    = ""
    profile_image:       str    = ""
    profile_image_opt:   str    = ""


@dataclass
class CryptoPriceUpdate:
    """RTDS crypto_prices — BTC reference price from Binance."""
    symbol:    str    # "btcusdt"
    value:     float  # USD
    timestamp: int    # Unix ms


# ── Layer 1: CLOB WebSocket ────────────────────────────────────────────────

class ClobWebSocket:
    """
    wss://ws-subscriptions-clob.polymarket.com/ws/market
    Ref: https://docs.polymarket.com/api-reference/wss/market
    Provides: orderbook snapshots, price_change, last_trade_price.
    No auth needed. PING every 10s required.
    """

    WS_URL       = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    PING_INTERVAL = 10

    def __init__(
        self,
        on_trade: Optional[Callable[[ClobTrade], None]] = None,
        on_book:  Optional[Callable[[dict], None]] = None,
    ):
        self.on_trade = on_trade
        self.on_book  = on_book
        self._token_ids: list[str] = []
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = threading.Event()

    def subscribe(self, token_ids: list[str]):
        self._token_ids = token_ids

    def _on_open(self, ws):
        self._connected.set()
        logger.info("[CLOB-WS] connected")
        ws.send(json.dumps({
            "assets_ids": self._token_ids,
            "type": "Market"
        }))

    def _on_message(self, ws, raw: str):
        try:
            events = json.loads(raw)
        except Exception:
            return
        if not isinstance(events, list):
            events = [events]
        for ev in events:
            et = ev.get("event_type", "")
            if et == "book" and self.on_book:
                self.on_book(ev)
            elif et == "last_trade_price" and self.on_trade:
                self.on_trade(ClobTrade(
                    asset_id    = ev.get("asset_id", ""),
                    price       = float(ev.get("price", 0)),
                    size        = float(ev.get("size", 0)),
                    side        = ev.get("side", ""),
                    timestamp   = int(ev.get("timestamp", 0)),
                    fee_rate_bps= str(ev.get("fee_rate_bps", "")),
                    trader_side = ev.get("trader_side", ""),
                ))

    def _on_error(self, ws, error):
        logger.error("[CLOB-WS] %s", error)

    def _on_close(self, ws, code, msg):
        self._connected.clear()

    def _ping_loop(self):
        while self._connected.is_set():
            try:
                if self._ws:
                    self._ws.send("PING")
            except Exception:
                break
            time.sleep(self.PING_INTERVAL)

    def start(self):
        self._ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        t = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"reconnect": 5}, daemon=True
        )
        t.start()
        self._connected.wait(timeout=10)
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def stop(self):
        if self._ws:
            self._ws.close()


# ── Layer 2: RTDS WebSocket ────────────────────────────────────────────────

class RtdsWebSocket:
    """
    wss://ws-live-data.polymarket.com
    Ref: https://docs.polymarket.com/market-data/websocket/rtds
    Provides: BTC/ETH spot prices (Binance source).
    IMPORTANT: Does NOT provide Polymarket trade activity.
    PING every 5s required (stricter than CLOB).
    """

    WS_URL        = "wss://ws-live-data.polymarket.com"
    PING_INTERVAL = 5

    def __init__(
        self,
        on_crypto_price: Optional[Callable[[CryptoPriceUpdate], None]] = None,
        symbols: list[str] = None,
    ):
        self.on_crypto_price = on_crypto_price
        self.symbols = symbols or ["btcusdt"]
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = threading.Event()

    def _on_open(self, ws):
        self._connected.set()
        logger.info("[RTDS-WS] connected")
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic":   "crypto_prices",
                "type":    "update",
                "filters": ",".join(self.symbols)
            }]
        }))

    def _on_message(self, ws, raw: str):
        if raw in ("PONG", ""):
            return
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if (msg.get("topic") == "crypto_prices"
                and msg.get("type") == "update"):
            p = msg.get("payload", {})
            if self.on_crypto_price:
                self.on_crypto_price(CryptoPriceUpdate(
                    symbol    = p.get("symbol", ""),
                    value     = float(p.get("value", 0)),
                    timestamp = int(p.get("timestamp", 0)),
                ))

    def _on_error(self, ws, error):
        logger.error("[RTDS-WS] %s", error)

    def _on_close(self, ws, code, msg):
        self._connected.clear()

    def _ping_loop(self):
        while self._connected.is_set():
            try:
                if self._ws:
                    self._ws.send("PING")
            except Exception:
                break
            time.sleep(self.PING_INTERVAL)

    def start(self):
        self._ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        t = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"reconnect": 5}, daemon=True
        )
        t.start()
        self._connected.wait(timeout=10)
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def stop(self):
        if self._ws:
            self._ws.close()


# ── Layer 3: data-api REST Trades Poller ──────────────────────────────────

class MarketTradePoller:
    """
    GET https://data-api.polymarket.com/trades
    No auth. Poll every 4s. Primary source for proxyWallet identity.

    Official response fields (all confirmed in docs):
      proxyWallet, side, asset, conditionId, size, price, timestamp,
      title, slug, icon, eventSlug, outcome, outcomeIndex,
      name, pseudonym, bio, profileImage, profileImageOptimized,
      transactionHash

    NOTE: This is NOT a WS violation (RULE-ARCH-WS-1 exception:
      data-api /trades has no WebSocket equivalent).
    """

    def __init__(
        self,
        condition_id: str,
        on_trade: Callable[[PolyTrade], None],
        poll_interval: float = 4.0,
        min_usd: float = 0.0,
        taker_only: bool = True,
    ):
        self.condition_id  = condition_id
        self.on_trade      = on_trade
        self.poll_interval = poll_interval
        self.min_usd       = min_usd
        self.taker_only    = taker_only
        self._seen:  set[str] = set()
        self._running = False

    def _fetch(self) -> list[dict]:
        params: dict = {
            "market":    self.condition_id,
            "limit":     100,
            "takerOnly": str(self.taker_only).lower(),
        }
        if self.min_usd > 0:
            params["filterType"]   = "CASH"
            params["filterAmount"] = self.min_usd
        try:
            r = requests.get(f"{DATA_API}/trades", params=params, timeout=5)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning("[TRADES] %s", e)
            return []

    @staticmethod
    def parse_trade(raw: dict) -> Optional[PolyTrade]:
        tx = raw.get("transactionHash", "")
        pw = raw.get("proxyWallet", "")
        if not tx or not pw:
            return None          # RULE-ARCH-WS-2
        return PolyTrade(
            proxy_wallet      = pw,
            name              = raw.get("name", ""),
            pseudonym         = raw.get("pseudonym", ""),
            side              = raw.get("side", ""),
            outcome           = raw.get("outcome", ""),
            price             = float(raw.get("price") or 0),
            size              = float(raw.get("size")  or 0),
            usdc_size         = round(float(raw.get("size") or 0) * float(raw.get("price") or 0), 4),
            timestamp         = int(raw.get("timestamp") or 0),
            transaction_hash  = tx,
            condition_id      = raw.get("conditionId", ""),
            event_slug        = raw.get("eventSlug", ""),
            asset             = raw.get("asset", ""),
            title             = raw.get("title", ""),
            slug              = raw.get("slug", ""),
            outcome_index     = int(raw.get("outcomeIndex") or 0),
            bio               = raw.get("bio", ""),
            profile_image     = raw.get("profileImage", ""),
            profile_image_opt = raw.get("profileImageOptimized", ""),
        )

    def _loop(self):
        while self._running:
            for raw in self._fetch():
                t = self.parse_trade(raw)
                if t and t.transaction_hash not in self._seen:
                    self._seen.add(t.transaction_hash)
                    if len(self._seen) > 50_000:
                        self._seen = set(list(self._seen)[-25_000:])
                    self.on_trade(t)
            time.sleep(self.poll_interval)

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False


def fetch_wallet_history(
    proxy_wallet: str,
    condition_id: Optional[str] = None,
    limit: int = 500,
) -> list[PolyTrade]:
    """
    One-shot: pull full trade history for a wallet.
    GET https://data-api.polymarket.com/activity?user=0x...
    Ref: https://docs.polymarket.com/api-reference/core/get-user-activity
    """
    params: dict = {
        "user":  proxy_wallet,
        "limit": min(limit, 500),
        "type":  "TRADE",
    }
    if condition_id:
        params["market"] = condition_id
    try:
        r = requests.get(f"{DATA_API}/activity", params=params, timeout=10)
        r.raise_for_status()
        raw_list = r.json()
        if not isinstance(raw_list, list):
            return []
    except Exception as e:
        logger.error("[ACTIVITY] %s", e)
        return []
    return [t for t in (MarketTradePoller.parse_trade(r) for r in raw_list)
            if t is not None]
