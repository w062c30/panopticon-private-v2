"""
TransferEntropyCache — 市場級轉移熵，基於 CLOB WS 匿名 tick 序列。
不需要 proxyWallet。符合 Invariant 4.2 背景計算白名單。

決策路徑唯一合法介面：.is_significant -> bool (O(1))
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque

import numpy as np

logger = logging.getLogger(__name__)

_HIGH_VOLUME_THRESHOLD = 800   # trade_ticks_60s > 此值時跳過本次重算
_TE_THRESHOLD = 0.05           # bits，高於此值視為顯著 Lead-Lag 關係
_COMPUTE_TIMEOUT_SEC = 8.0     # 執行緒池超時防護

# D81: Singleton instance — created once by orchestrator, reused by signal_engine
_te_cache_instance: "TransferEntropyCache | None" = None


def get_te_cache() -> "TransferEntropyCache":
    """Singleton getter — orchestrator calls this once; signal_engine reuses the same instance."""
    global _te_cache_instance
    if _te_cache_instance is None:
        _te_cache_instance = TransferEntropyCache()
    return _te_cache_instance


class TransferEntropyCache:
    """
    背景計算的市場級 Transfer Entropy。
    [Invariant 4.2] 合規：計算在 asyncio.to_thread() 中執行，
    event loop 在計算期間完全自由。
    """
    _WINDOW     = 300   # tick 滑動視窗大小
    _UPDATE_SEC = 15    # 重算週期（秒）

    def __init__(self) -> None:
        self._source_buf: Deque[float] = deque(maxlen=self._WINDOW)  # Hyperliquid
        self._target_buf: Deque[float] = deque(maxlen=self._WINDOW)  # Polymarket
        self._cached_te: float = 0.0
        self._significant: bool = False
        self._skip_counter: int = 0   # 高峰期跳過次數

    # ── O(1) event loop 介面（WS 回調調用）─────────────────────────────
    def push_source(self, price: float) -> None:
        """Hyperliquid tick 回調，O(1)，非阻塞。"""
        self._source_buf.append(price)

    def push_target(self, price: float) -> None:
        """Polymarket CLOB WS tick 回調，O(1)，非阻塞。"""
        self._target_buf.append(price)

    # ── 背景重算 task ────────────────────────────────────────────────────
    async def _recompute_loop(
        self,
        trade_ticks_getter=None   # 可選：注入 lambda 讀取 trade_ticks_60s
    ) -> None:
        """
        [Invariant 4.2 背景計算調度規範]
        1. await asyncio.sleep() 協作讓出 event loop
        2. O(1) 資料快照
        3. asyncio.to_thread() 執行緒隔離 — event loop 在計算期間完全自由
        4. O(1) 寫回標量快取
        """
        while True:
            await asyncio.sleep(self._UPDATE_SEC)

            # 高峰期跳過（T1 最忙時不增加 thread pool 壓力）
            if trade_ticks_getter is not None:
                ticks_60s = trade_ticks_getter()
                if ticks_60s > _HIGH_VOLUME_THRESHOLD:
                    self._skip_counter += 1
                    logger.debug(
                        "[TE] Skipped recompute (high volume ticks_60s=%d, skip_count=%d)",
                        ticks_60s, self._skip_counter
                    )
                    continue

            # 資料不足則跳過
            if len(self._source_buf) < 50 or len(self._target_buf) < 50:
                continue

            # O(1) 快照（在 event loop 內，必須在 to_thread 之前完成）
            src = list(self._source_buf)
            tgt = list(self._target_buf)

            try:
                # [Invariant 4.2] 計算在執行緒池，event loop 在此期間完全自由
                te_val = await asyncio.wait_for(
                    asyncio.to_thread(
                        TransferEntropyCache._compute_te,
                        np.array(src),
                        np.array(tgt),
                    ),
                    timeout=_COMPUTE_TIMEOUT_SEC,
                )
                # O(1) 寫回（Python GIL 保護標量賦值，無需鎖）
                self._cached_te  = te_val
                self._significant = te_val > _TE_THRESHOLD
                logger.debug("[TE] te=%.4f significant=%s", te_val, self._significant)
            except asyncio.TimeoutError:
                logger.warning("[TE] Compute timeout (>8s), retaining old cache")
            except Exception as exc:
                logger.warning("[TE] Compute error: %s", exc)

    @staticmethod
    def _compute_te(x: np.ndarray, y: np.ndarray, k: int = 1) -> float:
        """
        TE(X→Y) = H(Y_t | Y_{t-1}) - H(Y_t | Y_{t-1}, X_{t-1})
        Schreiber (2000) 分箱估計法，k=1 lag。
        純函式，無副作用，適合 to_thread()。
        """
        n    = min(len(x), len(y))
        bins = min(10, max(3, int(np.sqrt(n))))

        def _bin(arr: np.ndarray) -> np.ndarray:
            edges = np.histogram_bin_edges(arr, bins=bins)
            return np.digitize(arr, edges[:-1])  # 去掉最後一個 edge 避免越界

        y_t = _bin(y[1:n])
        y_l = _bin(y[:n-1])
        x_l = _bin(x[:n-1])

        def _cond_h(a: np.ndarray, b: np.ndarray) -> float:
            joint   = a * (bins + 2) + b
            _, j_cnt = np.unique(joint, return_counts=True)
            _, b_cnt = np.unique(b,     return_counts=True)
            p_j = j_cnt / j_cnt.sum()
            p_b = b_cnt / b_cnt.sum()
            return (
                -np.sum(p_j * np.log2(p_j + 1e-12))
                + np.sum(p_b * np.log2(p_b + 1e-12))
            )

        h_yt_given_yl   = _cond_h(y_t, y_l)
        joint_cond      = y_l * (bins + 2) + x_l
        h_yt_given_both = _cond_h(y_t, joint_cond)
        return float(max(0.0, h_yt_given_yl - h_yt_given_both))

    # ── 決策路徑唯一合法介面（O(1)）──────────────────────────────────────
    @property
    def is_significant(self) -> bool:
        """
        [Invariant 4.2] 決策路徑唯一合法讀取介面。
        返回布爾值，不返回連續浮點數（符合 Invariant 6.2 機率語意要求）。
        """
        return self._significant

    # ── 監控介面（前端/MetricsCollector 使用，不進入決策鏈）──────────────
    @property
    def cached_value(self) -> float:
        """供前端顯示。禁止在 signal_engine / fast_gate 讀取此屬性。"""
        return self._cached_te

    @property
    def skip_count(self) -> int:
        """高峰期跳過次數，用於監控。"""
        return self._skip_counter
