from __future__ import annotations

from panopticon_py.hunting.trade_aggregate import ParentTrade


def make_parents(scenario: str) -> list[ParentTrade]:
    base_ts = 1_700_000_000_000

    if scenario == "insider_slicing":
        return [
            ParentTrade(
                taker="0xABC",
                side=1,
                volume=5000,
                first_ts_ms=base_ts + i * 50,
                last_ts_ms=base_ts + i * 50 + 10,
                child_count=3,
                market_id="MKT-1",
            )
            for i in range(12)
        ]

    if scenario == "market_maker":
        parents: list[ParentTrade] = []
        for i in range(10):
            side = 1 if i % 2 == 0 else -1
            parents.append(
                ParentTrade(
                    taker="0xDEF",
                    side=side,
                    volume=1000,
                    first_ts_ms=base_ts + i * 200,
                    last_ts_ms=base_ts + i * 200 + 5,
                    child_count=2,
                    market_id="MKT-2",
                )
            )
        return parents

    if scenario == "empty":
        return []

    if scenario == "single":
        return [
            ParentTrade(
                taker="0xGHI",
                side=1,
                volume=9000,
                first_ts_ms=base_ts,
                last_ts_ms=base_ts + 100,
                child_count=1,
                market_id="MKT-3",
            )
        ]

    if scenario == "uniform_gaps":
        return [
            ParentTrade(
                taker="0xJKL",
                side=1,
                volume=2000,
                first_ts_ms=base_ts + i * 1000,
                last_ts_ms=base_ts + i * 1000 + 10,
                child_count=2,
                market_id="MKT-4",
            )
            for i in range(10)
        ]

    return []
