# `.cursorrules` / `AGENTS.md` 正式條文草案

## 可直接貼進 `.cursorrules`
```md
## Graphify isolation policy

- Graphify is strictly an engineering-assist tool for code navigation, debugging root-cause tracing, and test-gap discovery.
- Graphify outputs (`graphify-out/*`, `GRAPH_REPORT.md`, `graph.json`, graph HTML/SVG/GraphML exports) are HUMAN_READ_ONLY artifacts and must not be consumed by signal generation, model scoring, risk gates, or order execution code paths.
- Any attempt to route Graphify-derived data into trading decisions is a policy violation and must fail closed.

## Decision pipeline contract policy

- Any field entering the trading signal pipeline must be contract-governed and carry source metadata, timestamp, and version.
- Only whitelisted machine-readable sources may enter decision paths; ad-hoc summaries, manual notes, and visualization artifacts are blocked by default.

## Shadow committee isolation policy

- Committee score and disagreement metrics are allowed only as side-channel shadow observations.
- Shadow committee experiments must run under isolated experiment identifiers and separate reports.
- Shadow committee metrics must never change live or paper-trade actions, risk-gate outcomes, sizing, or execution behavior.
- Shadow experiment KPIs must never be merged with baseline paper-trade KPIs.
```

## 可直接貼進 `AGENTS.md`
```md
## Graphify 決策隔離（正式條文）
- Graphify 僅可用於工程輔助（coding/debug/測試缺口提示），不得介入交易決策。
- `graphify-out/*`、`GRAPH_REPORT.md`、`graph.json` 與任何圖譜推導內容一律視為 **HUMAN_READ_ONLY**，不得被 signal/risk/execution 程式路徑讀取。
- 若偵測到 Graphify 來源資料進入決策路徑，必須 fail-closed 並記錄審計事件。

## Committee Shadow 實驗隔離（正式條文）
- 允許新增 `committee_score` 與 `disagreement_index`，但只能作為旁路 shadow 觀測。
- 旁路觀測必須綁定獨立 `experiment_id`，且使用獨立報表/輸出檔，不得寫回主決策 KPI 聚合。
- 旁路觀測不得影響 `StrategyInput`、`decide()`、風控 gate、下單流程、倉位大小。
- 任一報表、儀表板或審查文件必須明確區分 **Baseline Paper Trade** 與 **Experiment Shadow** 數據，不得混算。
```
