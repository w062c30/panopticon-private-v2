"""Minimal async WebSocket helper for CLOB (best-effort; URL from env)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


def default_clob_ws_url() -> str:
    return os.getenv(
        "CLOB_WS_URL",
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    ).strip()


async def _ws_heartbeat(ws, interval: float = 10.0) -> None:
    """Send application-level PING every N seconds per Polymarket WS spec."""
    while True:
        try:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"type": "PING"}))
        except asyncio.CancelledError:
            break
        except Exception:
            break


async def stream_json_messages(
    url: str | None = None,
    *,
    subscribe_payload: dict[str, Any] | None = None,
    close_event: asyncio.Event | None = None,
    on_open: Callable[[], None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Yield parsed JSON objects from a WebSocket; skip non-dict frames.

    If close_event is set, the loop exits cleanly when the event is set
    (instead of raising CancelledError on the caller).
    """
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError("pip install websockets") from e

    u = url or default_clob_ws_url()
    async with websockets.connect(u, ping_interval=20, ping_timeout=20) as ws:
        ping_task = asyncio.create_task(_ws_heartbeat(ws, interval=10.0))
        try:
            if subscribe_payload:
                payload_str = json.dumps(subscribe_payload)
                logger.info("[WS] Sending subscribe payload: %s", payload_str[:200])
                await ws.send(payload_str)
            # Fire on_open callback after connection + subscription are established
            logger.info("[WS] on_open callback about to fire, on_open=%s", on_open)
            if on_open:
                try:
                    on_open()
                    logger.info("[WS] on_open callback fired successfully")
                except Exception as exc:
                    logger.warning("[WS] on_open callback error: %s", exc)

            while True:
                if close_event is not None and close_event.is_set():
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # Normal: no message yet
                except asyncio.CancelledError:
                    raise
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("[WS] non-JSON frame dropped: %s", raw[:100])
                    continue
                if isinstance(msg, dict):
                    # Per Polymarket WS spec: server responds to PING with PONG.
                    # Filter it out — it carries no market data.
                    if msg.get("type") == "PONG":
                        continue
                    logger.debug("[WS] recv dict keys=%s event_type=%s",
                                 list(msg.keys()), msg.get("event_type", ""))
                    yield msg
                elif isinstance(msg, list):
                    for item in msg:
                        if isinstance(item, dict):
                            logger.debug("[WS] recv list item event_type=%s", item.get("event_type", ""))
                            yield item
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass


async def run_ws_loop(
    on_message: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    url: str | None = None,
    subscribe_payload: dict[str, Any] | None = None,
    on_reconnect: Callable[[], Awaitable[None]] | None = None,
    on_connect_cb: Callable[[], None] | None = None,
    on_disconnect_cb: Callable[[], None] | None = None,
    close_event: asyncio.Event | None = None,
) -> None:
    """
    Reconnect loop; invokes ``on_reconnect`` before each (re)connect attempt.

    Exits cleanly when close_event is set, without raising CancelledError.
    """
    # Wrapper: fire on_connect_cb when WS opens, then pass it to stream as on_open
    _on_open = on_connect_cb
    while True:
        if close_event is not None and close_event.is_set():
            break
        try:
            if on_reconnect:
                import inspect as _inspect
                if _inspect.iscoroutinefunction(on_reconnect):
                    await on_reconnect()
                elif callable(on_reconnect):
                    on_reconnect()
            async for msg in stream_json_messages(
                url, subscribe_payload=subscribe_payload, close_event=close_event, on_open=_on_open
            ):
                _on_open = None  # only fire on first connection, not on reconnect
                await on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if close_event is not None and close_event.is_set():
                break
            backoff = min(30.0, float(os.getenv("HUNT_WS_BACKOFF_SEC", "3")))
            import traceback as _tb
            tb_str = _tb.format_exc()
            logger.warning("[WS] Connection error\n%s", tb_str[:500])
            if on_disconnect_cb:
                try:
                    on_disconnect_cb()
                except Exception:
                    pass
            await asyncio.sleep(backoff)
            _on_open = on_connect_cb  # reset so on_connect_cb fires on next successful connect
