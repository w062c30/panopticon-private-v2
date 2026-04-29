# Panopticon 資料庫治理規範

## 1. Schema 與分層
- `raw_events`：append-only，儲存來源原始事件 payload。
- `strategy_decisions`：L3 決策鏈結論，必須關聯 `raw_events.event_id`。
- `execution_records`：L4 模擬執行結果，必須關聯 `strategy_decisions.decision_id`。
- `audit_log`：人工調整、覆寫、參數修訂歷程。

## 2. 關聯與完整性
- 禁止孤兒資料：外鍵約束啟用（SQLite `PRAGMA foreign_keys=ON`）。
- 核心數值限制：
  - 機率欄位 `0 <= p <= 1`
  - `latency_ms >= 0`
  - `kelly_fraction >= 0`
- 時間欄位一律 UTC ISO-8601。

## 3. 寫入規則
- 先驗證 schema，再寫入資料庫。
- `raw_events` 僅允許 INSERT，不得 UPDATE/DELETE。
- 去重鍵建議：`source + source_event_id + event_ts`（上游事件去重）。

## 4. 安全規則
- 禁止儲存 API key、私鑰、簽名原文。
- log 輸出需脫敏地址與憑證字串。
- 分離權限：
  - read-only：報表與 dashboard
  - write：事件寫入
  - admin：schema migration、清理封存

## 5. 保留與封存
- `raw_events` 至少保留 90 天。
- `strategy_decisions` / `execution_records` / `audit_log` 至少保留 365 天。
- 到期資料可壓縮封存，但必須可重建指定區間回放。

## 6. 回測一致性
- 回測僅可讀取當下事件時間之前資料，禁止看未來資料。
- 任何回測結果都要附：
  - `dataset_version`
  - `strategy_version`
  - `config_hash`

## 7. 災難恢復
- 每日快照、每小時增量備份（依實際部署調整）。
- 每週最少一次 restore drill，驗證 RTO/RPO。

## 8. Schema 變更加記錄（D82+）

新增欄位需同時更新本文件與 `EXPERIENCE_PLAYBOOK.md`。

| 日期 | Sprint | 表格 | 欄位 | 說明 |
|------|--------|------|------|------|
| 2026-04-29 | D82 | discovered_entities | insider_score REAL DEFAULT 0.0 | 由 analysis_worker 寫入；D82 補加以支援 CONSENSUS_SYNC metrics |

## 9. DB Migration Pattern（D84+）

所有欄位新增**必須**使用 `_add_column_if_missing` helper（`panopticon_py/db.py`）：

```python
self._add_column_if_missing(conn, "table_name", "column_name", "REAL DEFAULT 0.0")
```

**禁止**直接在 `_ensure_*_tables()` 內寫裸 `ALTER TABLE` 語句。
原因：`CREATE TABLE IF NOT EXISTS` 在 table 已存在時整段跳過，包括其後的 ALTER TABLE。Live DB 不會觸發，新建 DB 可能重複執行報錯。
參見：EXP-D83-001（EXPERIENCE_PLAYBOOK.md）
