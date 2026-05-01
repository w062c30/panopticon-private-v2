#!/usr/bin/env python3
"""
D107 驗收測試：確認 market_tier 正確寫入 execution_records。

執行方式：python scripts/verify_d107.py
前提：系統已運行至少 1 小時並處理過非 T3 信號。

此腳本僅用於驗收，不入 CI。
"""

from panopticon_py.db import ShadowDB


def main() -> int:
    db = ShadowDB()
    db.bootstrap()

    # 查詢最近 1 小時的 market_tier 分布
    rows = db.conn.execute("""
        SELECT
            market_tier,
            COUNT(*) AS cnt
        FROM execution_records
        WHERE created_ts_utc > datetime('now', '-1 hour')
        GROUP BY market_tier
        ORDER BY cnt DESC
    """).fetchall()

    print("=== execution_records market_tier 分布（最近 1 小時）===")
    if not rows:
        print("⚠️  無記錄（系統可能尚未運行或無信號）。跳過驗收。")
        return 0

    non_t3_found = False
    for r in rows:
        tier = dict(r)["market_tier"]
        cnt = dict(r)["cnt"]
        marker = " ← 非 t3" if tier != "t3" else ""
        print(f"  {tier:12s}  {cnt:6d}{marker}")
        if tier != "t3":
            non_t3_found = True

    print()
    if non_t3_found:
        print("✅ market_tier 多樣性確認通過 — 修復成功")
        return 0
    else:
        print("❌ 所有記錄仍為 t3 — market_tier 仍未被寫入！")
        print("   可能原因：信號引擎尚未重啟，或事件來源的 market_tier 仍是 t3")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())