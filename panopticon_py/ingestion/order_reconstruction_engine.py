"""
Order Reconstruction Engine — D96
將 data-api 回傳的多筆 fills 重建為單一「Order」。

啟發式規則：
  同一 (taker_wallet, market_id, side)，
  fills 之間時間差 < ORDER_CLOSE_GAP_MS (30s)
  → 歸屬同一 order

Order type 推斷：
  fill_count=1                    → 'single'
  fill_count>=2, span<5s          → 'sweep'   (book sweep)
  fill_count>=5, span>=5s         → 'twap'    (algo execution)
  fill_count>=2, span>=5s, <5fills → 'iceberg'
"""

from __future__ import annotations

import time
from uuid import uuid4

from panopticon_py.time_utils import utc_now_rfc3339_ms

ORDER_CLOSE_GAP_MS = 30_000   # 30s 無新 fill → order 視為完成
JOIN_TOLERANCE_MS  = 3_000    # timestamp join 容忍度（±3s）


def try_match_or_open(raw: dict, db) -> str:
    """
    將一筆 data-api trade 歸屬到 open order 或開啟新 order。
    返回 order_id (str)。

     caller is responsible for db.conn.commit() after all calls.
    """
    wallet   = (raw.get("proxyWallet") or raw.get("taker_address") or "").lower()
    market   = raw.get("conditionId") or raw.get("asset") or raw.get("token_id") or ""
    side     = raw.get("side", "")
    price    = float(raw.get("price") or 0)
    size     = float(raw.get("size") or 0)
    ts_ms    = int(raw.get("timestamp") or 0)
    usdc     = round(size * price, 4)

    if not wallet or not market or usdc <= 0:
        return str(uuid4())  # degenerate case: return new id, caller discards

    cutoff_ms = ts_ms - ORDER_CLOSE_GAP_MS

    row = db.conn.execute("""
        SELECT order_id, total_size, fill_count, avg_price, first_fill_ts
        FROM order_reconstructions
        WHERE taker_wallet = ?
          AND market_id    = ?
          AND side         = ?
          AND is_complete  = 0
          AND last_fill_ts >= ?
        ORDER BY last_fill_ts DESC
        LIMIT 1
    """, (wallet, market, side, cutoff_ms)).fetchone()

    if row:
        order_id, cur_size, fill_count, cur_avg, first_ts = row
        new_total  = cur_size + usdc
        new_avg    = (cur_size * cur_avg + usdc * price) / new_total
        span_ms    = ts_ms - first_ts
        new_type   = _infer_type(fill_count + 1, span_ms)

        db.conn.execute("""
            UPDATE order_reconstructions
            SET total_size          = ?,
                fill_count          = ?,
                avg_price           = ?,
                last_fill_ts        = ?,
                order_type_inferred = ?
            WHERE order_id = ?
        """, (new_total, fill_count + 1, new_avg, ts_ms, new_type, order_id))
    else:
        order_id = str(uuid4())
        db.conn.execute("""
            INSERT INTO order_reconstructions
                (order_id, taker_wallet, market_id, side,
                 total_size, fill_count, avg_price,
                 first_fill_ts, last_fill_ts,
                 is_complete, order_type_inferred, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 'single', ?)
        """, (order_id, wallet, market, side,
              usdc, price, ts_ms, ts_ms,
              utc_now_rfc3339_ms()))

    return order_id


def close_stale_orders(db) -> int:
    """關閉超過 ORDER_CLOSE_GAP_MS 未更新的 open orders。返回關閉筆數。"""
    cutoff_ms = int(time.time() * 1000) - ORDER_CLOSE_GAP_MS
    cur = db.conn.execute("""
        UPDATE order_reconstructions
        SET is_complete = 1
        WHERE is_complete = 0
          AND last_fill_ts < ?
    """, (cutoff_ms,))
    db.conn.commit()
    return cur.rowcount


def _infer_type(fill_count: int, span_ms: int) -> str:
    if fill_count == 1:
        return "single"
    if span_ms < 5_000:
        return "sweep"
    if fill_count >= 5:
        return "twap"
    return "iceberg"
