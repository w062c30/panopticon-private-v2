from __future__ import annotations

import asyncio

import pytest

from panopticon_py.execution.arb_scanner import ArbScanner


@pytest.mark.asyncio
async def test_on_message_accepts_market_key_without_market_id() -> None:
    scanner = ArbScanner()
    msg = {
        "market": "mkt-1",
        "asset_id": "asset-1",
        "best_ask": "0.42",
        "best_ask_size": "12",
        "outcome": "YES",
    }

    await scanner._on_message(msg)

    assert scanner._update_counter["asset-1"] == 1
    assert scanner.books["mkt-1"]["YES"].price == pytest.approx(0.42)
    assert scanner.books["mkt-1"]["YES"].size == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_stats_flush_loop_runs_without_ws_messages() -> None:
    scanner = ArbScanner()
    calls: list[int] = []

    async def _fake_flush() -> None:
        calls.append(1)
        if len(calls) >= 2:
            scanner._stop_event.set()

    scanner._flush_stats = _fake_flush  # type: ignore[assignment]
    await asyncio.wait_for(scanner._stats_flush_loop(interval_sec=0.01), timeout=0.2)
    assert len(calls) >= 2
