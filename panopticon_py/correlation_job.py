from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

from panopticon_py.correlation_rolling import align_series, pairwise_correlation_edges
from panopticon_py.market_data.clob_series import fetch_mid_series_clob, fetch_mid_series_stub

if TYPE_CHECKING:
    from panopticon_py.db import ShadowDB


def run_correlation_tick(db: ShadowDB) -> None:
    """Refresh correlation_edges from stub or CLOB token ids (best-effort)."""
    window = int(os.getenv("CORR_WINDOW_SEC", "300"))
    raw = os.getenv("CORR_MARKET_IDS", "").strip()
    if os.getenv("CORR_USE_DEMO_PAIR", "1").lower() in ("1", "true", "yes") and not raw:
        markets = ["demo-market", "demo-market-b"]
    else:
        markets = [x.strip() for x in raw.split(",") if x.strip()]
    if len(markets) < 2:
        return
    series: dict[str, list[float]] = {}
    for m in markets:
        token = os.getenv(f"CLOB_TOKEN_{m}")
        if token:
            hist = fetch_mid_series_clob(token)
            series[m] = hist if len(hist) >= 5 else fetch_mid_series_stub(m)
        else:
            series[m] = fetch_mid_series_stub(m)
    series = align_series(series)
    if len(series) < 2:
        return
    eps = float(os.getenv("CORR_EPSILON", "0.01"))
    edges = pairwise_correlation_edges(series, window_sec=window, epsilon=eps)
    if edges:
        db.upsert_correlation_edges([dict(e) for e in edges])


class CorrelationJobWorker:
    def __init__(self, db: ShadowDB, interval_sec: float = 30.0) -> None:
        self.db = db
        self.interval_sec = interval_sec
        self._running = False
        self._thread: threading.Thread | None = None

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

    def _loop(self) -> None:
        while self._running:
            try:
                run_correlation_tick(self.db)
            except Exception:
                pass
            time.sleep(self.interval_sec)
