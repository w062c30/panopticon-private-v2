# 團隊操作 SOP：用 Graphify 加速 Debug 且不越界

## 1. 適用範圍
- 本 SOP 僅用於工程效率提升（code navigation、debug root-cause、測試缺口盤點）。
- 不得將 Graphify 產物用於交易訊號、風控閘門、下單決策。

## 2. 角色與責任
- **開發者（Developer）**
  - 依本 SOP 使用 Graphify 快速定位問題與影響面。
  - 所有結論需回到原始碼與測試驗證，不可只憑圖譜輸出。
- **審查者（Reviewer）**
  - 檢查是否有 Graphify 越界（資料流進入 signal/risk/execution）。
  - 檢查 commit/PR 是否明確區分「工程分析」與「決策證據」。
- **值班/風控（Operator）**
  - 只接受合約化、白名單來源的決策輸入。
  - 若偵測 graphify 來源進入決策鏈，立即 fail-closed。

## 3. 日常操作流程（標準 7 步）
1. **定義問題**
   - 先寫一句問題陳述（例：`L3 action 出現異常 HOLD`）。
2. **限定範圍建圖**
   - 只對相關目錄建圖，避免全倉庫噪音（例如 `panopticon_py/strategy` + `panopticon_py/main_loop.py`）。
3. **先讀報告再搜碼**
   - 先看 `GRAPH_REPORT.md` 的社群、關聯、疑點，再回到實碼查證。
4. **形成假設**
   - 假設要可測試（例：`source whitelist 擋到了 friction_snapshot`）。
5. **原始碼驗證**
   - 以實碼 + 測試 + 日誌驗證，不使用圖譜輸出作最終判斷。
6. **修復與回歸**
   - 修改後跑最小必要測試，再跑關聯路徑測試。
7. **留痕與交接**
   - 在 PR/任務單記錄：
     - 問題陳述
     - Graphify 只作導航的證據
     - 最終以哪段實碼與哪個測試確認修復

## 4. 可做 / 不可做（強制）

### 可做
- 用 Graphify 找模組關係、呼叫鏈、潛在影響面。
- 用 Graphify 輔助定位測試缺口。
- 用 Graphify 輸出圖給人員審閱（Human-Read-Only）。

### 不可做
- 不可將 `graphify-out/*`、`GRAPH_REPORT.md`、`graph.json` 直接餵給策略引擎。
- 不可把 `INFERRED/AMBIGUOUS` 邊當成交易決策證據。
- 不可把 committee shadow 實驗數據與 baseline paper trade KPI 混算。

## 5. 資料與路徑隔離規範
- Graphify 輸出固定放在獨立路徑（建議 `data/visualization/` 或 `graphify-out/`）。
- 決策輸入只接受合約欄位，至少包含：`source`、`timestamp`、`version`。
- 來源必須在白名單；若來源含 `graphify|graph_report|graph_json|human_read_only`，必拒絕。

## 6. Committee Shadow 實驗規範
- 必須設定 `experiment_id`，並寫入獨立報表（JSONL/報表檔）。
- 僅供觀測：`committee_score`、`disagreement_index` 不可改變下單與風控結果。
- 實驗成果僅可作後續研究輸入，不可直接轉為主線 gate。

## 7. 每日檢查清單（5 分鐘）
- [ ] 今日 Graphify 產出是否只用於工程分析？
- [ ] 是否有任何 graphify 來源嘗試進入決策鏈？
- [ ] shadow 實驗是否帶有 `experiment_id`？
- [ ] baseline 與 experiment 報表是否分開？
- [ ] 今日修復是否有對應測試與可回放證據？

## 8. 事故處置（越界時）
- 立即停止相關流程（fail-closed）。
- 標記事故等級與影響範圍（signal/risk/execution）。
- 回退至最近穩定版本，保留審計紀錄。
- 24 小時內補 RCA（根因、修復、預防措施）。

## 9. 最小交付模板（PR 描述可直接貼）
```md
## Debug Context
- 問題：
- 影響範圍：

## Graphify Usage (Human-Read-Only)
- 用途：導航 / 根因追蹤 / 測試缺口
- 產物：GRAPH_REPORT.md / graph.json（僅參考）

## Verification
- 實碼驗證：
- 測試驗證：
- 決策鏈隔離檢查（source whitelist / contract）：

## Safety
- 無 graphify 資料進入 signal/risk/execution
- committee shadow 與 baseline KPI 未混算
```

## 10. Shadow Hydration 一鍵啟動（冷啟動）
- 單命令啟動：
  - `python scripts/start_shadow_hydration.py`
- 啟動器會同時拉起：
  - `panopticon_py.hunting.discovery_loop`
  - `panopticon_py.hunting.run_radar`
- 安全保護：
  - 若偵測 `LIVE_TRADING=true`，程序會 fail-fast 停止。
- 冷啟動週期策略：
  - 預設先用 2h 週期收集 48h
  - 達成 `Tier-1 >= 100` 或滿 48h 後，自動放寬為 6h
