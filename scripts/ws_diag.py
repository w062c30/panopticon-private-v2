"""
Standalone Polymarket WS diagnostic.
Tests raw WS connection INDEPENDENTLY of run_radar.py.
Run: python scripts/ws_diag.py
Expected: receive at least 1 message within 30s
"""
import asyncio
import json
import time
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ws_diag")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def _heartbeat(ws, interval: float = 10.0):
    """Send application-level PING every N seconds per Polymarket WS spec."""
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"type": "PING"}))
            logger.info("[DIAG] sent PING at %s", time.strftime("%H:%M:%S"))
        except Exception:
            break


async def diag():
    # Step 1: fetch 3 active token IDs via httpx
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 5},
            )
            resp.raise_for_status()
            markets = resp.json()
    except Exception as e:
        logger.error("[DIAG] Failed to fetch markets: %s", e)
        return

    token_ids = []
    for m in markets:
        # Gamma API returns clobTokenIds directly on the market object
        raw_tids = m.get("clobTokenIds") or []
        if isinstance(raw_tids, str):
            try:
                raw_tids = json.loads(raw_tids)
            except json.JSONDecodeError:
                raw_tids = []
        if not isinstance(raw_tids, list):
            raw_tids = [raw_tids]
        for tid in raw_tids:
            if tid:
                token_ids.append(str(tid))
        if len(token_ids) >= 3:
            break

    if not token_ids:
        logger.warning("[DIAG] No token IDs found in markets response")
        return

    logger.info("[DIAG] Using token_ids: %s", token_ids[:3])

    # Step 2: connect and subscribe
    import websockets

    async with websockets.connect(
        WS_URL,
        ping_interval=None,  # we handle PING manually at app level
        close_timeout=10,
    ) as ws:
        ping_task = asyncio.create_task(_heartbeat(ws, interval=10.0))
        try:
            sub = {
                "assets_ids": token_ids[:3],
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub))
            logger.info("[DIAG] Subscribed at %s", time.strftime("%H:%M:%S"))
            logger.info("[DIAG] Subscribe payload: %s", json.dumps(sub))
            logger.info("[DIAG] Waiting for messages (30s timeout)...")

            t_start = time.time()
            msg_count = 0
            async for raw in ws:
                elapsed = time.time() - t_start

                # Handle PONG
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("[DIAG] non-JSON: %s", raw[:100])
                    continue

                if isinstance(msg, dict) and msg.get("type") == "PONG":
                    logger.info("[DIAG] recv PONG at +%.1fs", elapsed)
                    continue

                if isinstance(msg, dict):
                    event_type = msg.get("event_type", "?")
                    logger.info(
                        "[DIAG] msg#%d event_type=%s keys=%s at +%.1fs",
                        msg_count + 1,
                        event_type,
                        list(msg.keys()),
                        elapsed,
                    )
                    msg_count += 1
                elif isinstance(msg, list):
                    for item in msg:
                        if isinstance(item, dict):
                            event_type = item.get("event_type", "?")
                            logger.info(
                                "[DIAG] msg#%d event_type=%s keys=%s at +%.1fs (batch item)",
                                msg_count + 1,
                                event_type,
                                list(item.keys()),
                                elapsed,
                            )
                            msg_count += 1

                if msg_count >= 5 or elapsed > 30:
                    break

            if msg_count == 0:
                logger.error("[DIAG] ZERO messages received — WS subscription not delivering")
            else:
                logger.info(
                    "[DIAG] %d messages received — WS is functional",
                    msg_count,
                )
                try:
                    logger.info("[DIAG] First msg sample: %s", json.dumps(msg, indent=2)[:500])
                except NameError:
                    pass
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(diag())
