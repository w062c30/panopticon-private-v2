from __future__ import annotations

import statistics
import time

from panopticon_py.fast_gate import FastSignalInput, fast_execution_gate
from panopticon_py.friction_state import FrictionSnapshot


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    idx = max(0, min(len(data) - 1, int((p / 100.0) * (len(data) - 1))))
    return sorted(data)[idx]


def main() -> None:
    signal = FastSignalInput(
        p_prior=0.6,
        quote_price=0.49,
        payout=1.0,
        capital_in=0.49,
        order_size=75.0,
        delta_t_ms=120.0,
        gamma=0.001,
        slippage_tolerance=0.009,
        min_ev_threshold=0.0,
        daily_opp_cost=0.0005,
        days_to_resolution=2,
    )
    snapshot = FrictionSnapshot(
        network_ping_ms=120.0,
        current_base_fee=0.001,
        kyle_lambda=0.00001,
        gas_cost_estimate=0.2,
        api_health="ok",
        l2_timeout_ms=100.0,
        degraded=False,
        kelly_cap=0.25,
        last_update_ts=time.time(),
    )

    timings_us: list[float] = []
    for _ in range(2000):
        start = time.perf_counter_ns()
        fast_execution_gate(signal, snapshot)
        end = time.perf_counter_ns()
        timings_us.append((end - start) / 1000.0)

    p50 = percentile(timings_us, 50)
    p95 = percentile(timings_us, 95)
    p99 = percentile(timings_us, 99)
    avg = statistics.mean(timings_us)
    print(
        {
            "samples": len(timings_us),
            "avg_us": round(avg, 3),
            "p50_us": round(p50, 3),
            "p95_us": round(p95, 3),
            "p99_us": round(p99, 3),
        }
    )


if __name__ == "__main__":
    main()
