# Paper -> Live 晉級門檻與操作手冊

## 0. 核心治理前提
- Graphify 與可視化圖譜輸出僅供工程導航與人工審閱，不得進入交易決策鏈。
- 決策鏈輸入必須符合資料契約（`source`、`timestamp`、`version` 完整）且來源在白名單中。
- `committee_score` / `disagreement_index` 僅可作旁路 shadow 觀測；必須有獨立 `experiment_id`，且不得與 baseline paper trade KPI 混算。

## 1. 進入候選條件
- 連續蒐集至少 100 筆策略訊號，且至少 30 筆模擬成交。
- 連續 7 天運行中：
  - 無程序崩潰
  - 無 API 限流封禁
  - 無資料契約驗證失敗
- 關鍵風控硬閘有效：
  - `latency_ms > 200` 必拒單
  - `impact_pct > 2%` 必拒單

## 2. 指標門檻
- 預期 Sharpe Ratio >= 1.0
- Max Drawdown <= 15%
- 累積淨利為正，且不依賴單一極端交易
- 可追溯性檢查通過：任一交易可還原 `Prior -> Evidence -> Posterior -> Kelly -> Action`

## 3. 人工審查清單
- 核對 `config/api_capability_registry.json` 是否反映最新 API 限制。
- 核對 `NVIDIA_API_KEY` 僅經由環境變數注入。
- 核對 dashboard 指標與資料庫聚合結果一致。
- 核對最近 3 次 L5 參數調整是否有明確理由與結果回饋。

## 4. 灰度切換流程
1. 啟用 `LIVE_DRY_RUN=true`：只送出交易請求前檢核，不真正送單。
2. 觀察 48 小時，確認拒單率與風控觸發分佈合理。
3. 啟用 `LIVE_TRADING=true` 且設置資金上限（預設 1000 USD）。
4. 每日檢視 PnL 與 MDD；若連續 2 天異常則回退到 Paper。

## 5. 回退條件（任一觸發即回退）
- 單日 MDD 超過 8%
- API 錯誤率 > 5%
- L3/L4 決策鏈存在不可追溯交易
- 錯誤配置導致限流或下單失敗連續超過 10 次

## 6. A-E 分階段升級檢核
- **Phase A（研究與標註）**：資料集、target、feature lineage、資料品質監控完成。
- **Phase B（離線評估）**：PnL / Sharpe / Sortino / MaxDD / Turnover / Slippage sensitivity 達標，且完成 walk-forward / purged CV / regime split。
- **Phase C（影子交易）**：訊號與風控可完整回放，與 baseline 對照穩定。
- **Phase D（受控實盤）**：啟用 kill-switch、max position、max daily loss、max order rate、circuit breaker。
- **Phase E（全自動）**：再訓練有審批，漂移監控與異常降級流程上線。

## 7. 四類治理框架（最小落地）
- **技術治理**：模型/特徵/參數/資料快照版本鎖定。
- **風控治理**：實盤前清單、即時告警、人工接管與回退權限。
- **審計治理**：append-only 決策日誌，支持交易鏈路重演。
- **變更治理**：策略改版需經 shadow + staged rollout，禁止直上 full live。
