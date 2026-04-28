# 從輔助到自動交易：A-E 分階段升級

## Phase A — 研究與標註
- 建立標準資料集：行情、成交、深度、鏈上地址特徵、事件標註。
- 定義 target：`future_return`、`fill_adjusted_pnl`、`drawdown_risk`。
- 建立資料品質監控：缺值率、延遲、漂移（feature/label/execution drift）。

## Phase B — 離線模型與策略評估
- 模型起點：Logit / GBDT / 簡單時序模型（可解釋優先）。
- 主要指標：PnL、Sharpe、Sortino、MaxDD、Turnover、Slippage sensitivity。
- 驗證框架：walk-forward、purged CV、regime split（高低波動/流動性）。

## Phase C — 影子交易（不下單）
- 實時產生訊號與風控結果，但僅記錄。
- 必記錄欄位：`feature_snapshot`、`model_output`、`gate_result`、`theoretical_fill`、`latency`、`slippage_proxy`。
- 與 baseline 做 A/B 對照，且需跨市場 regime 檢驗。

## Phase D — 受控實盤（低風險）
- 低資金、低槓桿、白名單市場、限價優先。
- 風控硬閘：`kill_switch`、`max_position`、`max_daily_loss`、`max_order_rate`、`circuit_breaker`。
- 全鏈審計可回放：`feature -> model -> gate -> action -> execution`。

## Phase E — 全自動與持續監控
- 再訓練/再校準需審批，不可黑箱熱更新。
- 漂移監控常態化：feature/label/execution。
- 異常時自動降級至保守策略或停止交易。

## 穩定器（必備）
- 雙層決策：Alpha/Signal 層 + Risk/Execution Gate（否決權）。
- 不確定性管理：校準機率 + 信賴區間驅動倉位。
- 成本真實化：回測必含交易成本、衝擊成本、流動性限制。
- 制度化停機：連虧、滑點暴增、資料異常即停機。

## Committee Shadow 隔離原則
- 可新增 `committee_score` 與 `disagreement_index`，僅作旁路觀測。
- 必須使用獨立 `experiment_id` 與獨立報表。
- 不得改變 baseline paper trade 行為與 KPI。
