from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from panopticon_py.meta_learning import TradeRecord, summarize


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_reason_expectation_outcome(
    path: str,
    *,
    reason: str,
    expected: float,
    realized: float,
    posterior_probability: float,
    alpha: float,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts_utc": utc_now(),
        "reason": reason,
        "expected_ev": expected,
        "realized_pnl": realized,
        "posterior_probability": posterior_probability,
        "alpha": alpha,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def load_trade_records(path: str) -> list[TradeRecord]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[TradeRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        x = json.loads(line)
        out.append(
            TradeRecord(
                pnl=float(x["realized_pnl"]),
                reason=str(x["reason"]),
                expected_ev=float(x["expected_ev"]),
                posterior_probability=float(x["posterior_probability"]),
                alpha=float(x["alpha"]),
            )
        )
    return out


def optimize_every_10(path: str) -> dict[str, float | str]:
    trades = load_trade_records(path)
    if len(trades) < 10:
        return {"status": "skip", "reason": "less_than_10_trades"}
    recent = trades[-30:]
    summary = summarize(recent)
    return {
        "status": "ok",
        "recommended_alpha": summary.recommended_alpha,
        "sharpe": summary.sharpe,
        "max_drawdown": summary.max_drawdown,
        "net_profit": summary.net_profit,
    }


class PostTradeAttributionQueue:
    """Async attribution worker to avoid blocking critical path."""

    def __init__(self, out_path: str) -> None:
        self._queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._out_path = out_path
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def submit(self, payload: dict[str, object]) -> None:
        self._queue.put(payload)

    def _loop(self) -> None:
        p = Path(self._out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        while self._running:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            item["attribution_ts_utc"] = utc_now()
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    log_path = "data/reason_expectation_outcome.jsonl"
    result = optimize_every_10(log_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
