# Panopticon 長期數據收集計劃

> 版本：v1.0 — 2026-05-01（D120 系統穩定後啟動）
> 目標：連續運行所有服務，累積足夠 shadow trading 數據，達成 LIVE_TRADING 解鎖閾值並驗證信號品質。

---

## 一、LIVE 解鎖閾值（`check_shadow_readiness.py` 定義）

| 指標 | 閾值 | 數據來源 |
|------|------|----------|
| Shadow trades 總數 | ≥ 50 | `wallet_market_positions` |
| Win rate | ≥ 55% | `wallet_market_positions` |
| Average EV net | > 0 | `wallet_market_positions` |
| T5 signal pass rate | > 40% | `/api/t5-coverage` |

---

## 二、服務啟動順序

```bash
# 終端 1：Orchestrator（主進程）
python run_hft_orchestrator.py

# 終端 2：Backend API
uvicorn panopticon_py.api.app:app --host 0.0.0.0 --port 8001

# 瀏覽器 Dashboard
http://localhost:8001/dashboard/
```

**啟動後立即驗證（Zero-Trust）：**
```bash
curl http://localhost:8001/api/versions
# 期望：所有 version_match: true

curl http://localhost:8001/api/async-writer-health
# 期望：running=true, stale=false, queue_depth < 50

curl http://localhost:8001/api/t5-coverage
# 期望：有效數據回傳
```

---

## 三、數據收集階段規劃

### Phase 1：系統穩定驗證（第 1–3 天）

**目標：** D120 代碼庫在真實 API 流量下無崩潰、無靜默失效。

| 檢查點 | 頻率 | 健康指標 |
|--------|------|----------|
| `/api/async-writer-health` | 每 10 分鐘 | `running=true`, `queue_depth < 50` |
| `/api/versions` | 每 30 分鐘 | 所有 `version_match: true` |
| `data/async_writer_health.json` 時間戳 | 每 30 分鐘 | `written_at` 與當前時間差 < 60s |
| Orchestrator log | 每小時 | 無 `[ERROR]`、無 task crash |
| `pol_market_watchlist` 行數 | 每 2 小時 | 持續增長（T2-POL 市場被訂閱）|

**Phase 1 通過標準：**
- 連續 72 小時無 `[ERROR]` crash
- `async_writer_health.json` 每 30s 穩定更新
- POL markets 至少 5 個 `is_active=1`

---

### Phase 2：信號累積（第 4–14 天）

**目標：** 累積足夠 shadow trade 記錄供統計分析。

| 指標 | 每日目標 | 14 天累積目標 |
|------|---------|--------------|
| Shadow hits | ≥ 10 | ≥ 140 |
| T5 signals fired | ≥ 5 | ≥ 70 |
| T2-POL signals | ≥ 2 | ≥ 28 |
| Wallet observations | ≥ 50 | ≥ 700 |

**每日快照 SQL（每晚 23:55 執行）：**
```sql
SELECT
  date('now') as snapshot_date,
  (SELECT COUNT(*) FROM hunting_shadow_hits) as total_hits,
  (SELECT COUNT(*) FROM wallet_observations) as total_obs,
  (SELECT COUNT(*) FROM pol_market_watchlist WHERE is_active=1) as pol_active,
  (SELECT COUNT(*) FROM wallet_market_positions
   WHERE outcome IS NOT NULL) as resolved_trades;
```

**Phase 2 期間同步執行 D121 sprint**（修復 Debt-1 `_on_insider_alert` 裸 `sqlite3.connect`）。

---

### Phase 3：勝率驗證與 LIVE 解鎖評估（第 15–30 天）

**解鎖決策流程：**
1. `python scripts/check_shadow_readiness.py` 全部 GREEN
2. Architect 審查 `PANOPTICON_CORE_LOGIC.md` Invariant 1.4 合規
3. T5 pass rate > 40%（`/api/t5-coverage`）
4. Debt-1、Debt-2 已修復或風險已接受（記錄於 FEATURE_INDEX.md）
5. 操作者手動設定 `LIVE_TRADING=1`

---

## 四、Dashboard 監察重點

| 面板 | 觀察重點 |
|------|----------|
| System Health | 所有進程 version_match、heartbeat 新鮮度 |
| Async Writer | `running: true`、`queue_depth` 趨勢（應 < 100）|
| T5 Coverage | `pass_rate` 趨勢、`total_signals` 每日增量 |
| Hunting Hits | 每日新增 hits 數量、`market_tier` 分佈 |
| Wallet Obs | T2-POL vs T1 觀察比例 |

**告警閾值（需手動設定提醒）：**

| 狀況 | 觸發條件 | 行動 |
|------|----------|------|
| Writer 停止 | `running: false` 持續 > 5 分鐘 | 重啟 orchestrator |
| Writer 過期 | `stale_sec > 120` | 檢查 orchestrator 是否卡死 |
| Signals 停止 | 24h 內 T5 signals = 0 | 檢查 Polymarket WS 連線 |
| DB 增長停止 | 24h 內 wallet_obs 增量 = 0 | 檢查 radar task |
| Version mismatch | 任何進程 `version_match: false` | 立即停服，確認版本後重啟 |

---

## 五、技術債清理時機

根據 D120 次期觀察點，以下 3 項技術債**在累積到 Phase 2 前建議處理**：

| ID | 問題 | 風險說明 | 建議 Sprint |
|----|------|----------|------------|
| Debt-1 | `_on_insider_alert` 裸 `sqlite3.connect`，無 WAL/busy_timeout | 長時間運行下可能 `database is locked` busy-fail，分析結果靜默丟失 | D121 |
| Debt-2 | `AsyncDBWriter.health()` 隱式契約，Stub 不同步 | health key 增加後 dashboard 結構不一致 | D121 |
| Debt-3 | `graph_engine` 局部 dead code 與 global 影子 | 不影響功能，但混淆下一位工程師 | D122 |

---

## 六、數據歸檔計劃

**每週日 00:00 執行：**
```python
import sqlite3, shutil, datetime
conn = sqlite3.connect('data/panopticon.db')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()
date = datetime.date.today().isoformat()
shutil.copy2('data/panopticon.db', f'data/backup/panopticon_{date}.db')
print(f'Backup: panopticon_{date}.db')
```

保留策略：最近 4 個週備份 + Phase 2 結束時的全量快照。

---

## 七、里程碑時間軸

```
2026-05-01  D120 代碼庫穩定，啟動 Phase 1
2026-05-04  Phase 1 通過（72h 無 crash）
2026-05-05  啟動 Phase 2（信號累積）+ D121 sprint（Debt-1 修復）
2026-05-14  Phase 2 中期檢查（hits > 70）
2026-05-18  Phase 2 結束，進入 Phase 3 評估
2026-05-25  目標：shadow trades ≥ 50, win_rate ≥ 55%
2026-05-31  LIVE_TRADING 解鎖評估會議
```

---

## 附：D101–D120 Coding Agent 踩坑速查表

> 此表濃縮自 `docs/AGENT_LESSONS_D101_D120.md`，供 coding agent 在 sprint 開始前快速自查。

| Rule ID | 一句話摘要 | 最常見違反場景 |
|---------|-----------|---------------|
| RULE-IMPORT-1 | `except Exception` 吞 `NameError`，函數體 import 缺失不報錯 | 新增 `json.dump()` 但忘記頂層 `import json` |
| RULE-IMPORT-2 | `python -c "import X"` 不測函數體路徑 | 只用 import check 作 handoff 驗證 |
| RULE-TIME-1 | 局部重實作 `utc_now_rfc3339_ms` 有跨秒 race | `_utc_now_rfc3339_ms()` 定義在 `main_async()` 內 |
| RULE-SQLITE-1 | `r[0]~r[N]` 位置索引靜默損壞 | DAL method 直接 `return r[0], r[1], ...` |
| RULE-SEMAPHORE-1 | 模組級 `asyncio.Semaphore` 在 3.9 會 `different loop` | `_sem = asyncio.Semaphore(2)` 在檔案頂層 |
| RULE-SEMAPHORE-2 | 持鎖 `sleep` 使 Semaphore 退化為 mutex | `async with sem: ... await sleep(0.5)` |
| RULE-API-1 | `tokens[0].get()` 無 `isinstance` 守衛崩潰 | Gamma API 回傳字串時 `AttributeError` |
| RULE-REENTRY-1 | `stop()` 無重入守衛，雙重 sentinel | 信號處理器與 `finally` 各呼叫一次 `stop()` |
| RULE-CONTRACT-1 | Stub 與原 class 隱式 dict 契約不同步 | `AsyncDBWriterStub.health()` key 結構漂移 |
| RULE-PATH-1 | 寫入方硬編碼路徑，讀取方用 env var | orchestrator 寫 `"data/health.json"`，backend 讀 env |
| RULE-DEAD-1 | 無 reader 的賦值是 dead code | `graph_engine = ...` 賦值後從未使用 |
| RULE-CLOSURE-1 | 閉包提升為模組級後局部變數 `NameError` | `_persist_health()` 引用 `db_writer` 局部變數 |
| RULE-WS-1 | WS 熱迴圈禁止 `json.loads(payload_json)` | handler 內反序列化字串欄位 |

_最後更新：2026-05-01 by Architect Agent（post-D120）_