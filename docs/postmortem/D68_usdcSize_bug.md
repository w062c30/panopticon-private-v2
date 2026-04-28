# Post-Mortem: D68 usdcSize Field Assumption Bug

**Date**: 2026-04-28
**Sprint**: D68
**Severity**: HIGH — silently corrupted all USD volume data
**Status**: FIXED in D68g

---

## 事故摘要

D68 Phase 0 執行 10 分鐘監控後，報告 `Total USD volume = $0.00`，
實際應為 **$22,865.84**。

---

## 根本原因

```python
# polymarket_streams.py — BROKEN CODE
usdc_size = float(raw.get("usdcSize") or 0)
#                          ^^^^^^^^
# 假設 data-api.polymarket.com/trades 返回 usdcSize 欄位
# 實際上該欄位根本不存在於 response 中
# raw.get("usdcSize") → None → float(None or 0) → 0.0
```

**錯誤來源**：開發者根據欄位命名慣例（USDC + size → usdcSize）推測欄位名稱，
沒有先做 curl 驗證。

---

## 影響範圍

| 受影響資料 | 狀態 |
|-----------|------|
| wallet_activity D68 Phase 0 的 458 行 | 全部 usdc_size=0.0，已清除 |
| InsiderDetector L1 大額警報（>$200） | 全部失效（usdc_size 恆為 0） |
| USD volume 統計圖表 | 全部不可信 |
| 身份追蹤（proxyWallet） | 不受影響（不依賴 usdc_size） |

---

## 修復方案 (D68g)

```python
# polymarket_streams.py — FIXED CODE (verified 2026-04-28)
usdc_size = round(
    float(raw.get("size")  or 0) *
    float(raw.get("price") or 0),
    4
)
# size × price = correct USD amount
# both "size" and "price" CONFIRMED in API response
```

---

## 資料修復步驟

1. `wallet_activity_d68_backup` 備份了 458 污染行（保留用於調試）
2. `wallet_activity` 表清空（D69a purge）
3. D69 Phase 0 重新開始收集，所有新資料均符合標準

---

## 預防措施（新增至 PANOPTICON_CORE_LOGIC.md）

- **RULE-API-1**: 新端點先 curl，逐字確認欄位名稱
- **RULE-API-2**: 計算欄位從 raw 欄位推算，不依賴 API 預算欄位
- **RULE-API-3**: `raw.get()` 必須標注驗證日期
- **RULE-DATA-2**: 入庫前驗證 3 個必填欄位非零

---

## 早期發現信號（下次識別此類 bug 的指標）

1. Phase 0 結束後所有 USD 統計為 $0.00 或完全一致的值
2. InsiderDetector L1 從未觸發（大額警報門檻 $200 但所有 usdc_size=0）
3. `usdc_size` 分佈是 delta function（全部等於同一個值）

---

## 時間線

| 時間 | 事件 |
|------|------|
| D68 15:00 | Phase 0 開始，code 有 usdcSize bug |
| D68 15:10 | Phase 0 結束，報告 $0 volume |
| D68 15:15 | 發現 bug：curl 驗證確認 usdcSize 不存在 |
| D68 15:20 | D68g fix: `size × price`；Phase 0 rerun，$22,865 ✅ |
| D69 開始   | D69a purge 清除污染資料，D69 Phase 0 重新收集 |