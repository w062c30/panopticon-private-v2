# Graphify 可視化整合與程式指南（Human-Read-Only）

## 目的
- 參照本機 Graphify repo `D:\Antigravity\graphify-3` 的輸出模式，建立錢包關係 / insider fingerprint 可視化。
- 嚴格限制圖譜輸出為人工審閱用途，不介入任何交易決策流程。

## 參照來源（Graphify）
- Pipeline：`detect -> extract -> build_graph -> cluster -> analyze -> report -> export`
- 核心輸出：
  - `graphify-out/graph.html`（互動圖）
  - `graphify-out/GRAPH_REPORT.md`（文字審查報告）
  - `graphify-out/graph.json`（結構化圖資料）
- 關係信心分級：
  - `EXTRACTED`：來源明確
  - `INFERRED`：合理推斷
  - `AMBIGUOUS`：需人工覆核

## Panopticon 可視化資料模型（建議）
- **Node types**
  - `wallet`：地址節點
  - `market`：市場節點
  - `entity`：聚合實體（如 macro wallet group）
  - `fingerprint_cluster`：行為指紋群組
  - `event`：關鍵事件（resolution、large fill、abnormal flow）
- **Edge types**
  - `funded_by`
  - `co_traded_within_window`
  - `shared_router_path`
  - `temporal_fingerprint_similarity`
  - `resolved_event_exposure`
- **Edge confidence**
  - 僅允許 `EXTRACTED | INFERRED | AMBIGUOUS`
  - 若 `INFERRED`，必帶 `confidence_score`（0~1）

## Human-Read-Only 安全邊界
- 以下產物只可被報告層讀取，不可被策略程式讀取：
  - `graphify-out/*`
  - `GRAPH_REPORT.md`
  - `graph.json`
  - HTML/SVG/GraphML/Neo4j 匯出
- 若程式路徑來源標記為 `graphify`、`graph_report`、`graph_json`，必須 fail-closed。

## 實作建議（不覆蓋既有規則）
1. 僅使用 Graphify 建圖與匯出功能，不執行 `graphify * install` 類規則注入指令。
2. 在 Panopticon 內把圖譜輸出放到獨立資料夾（例如 `data/visualization/`），並註記 `HUMAN_READ_ONLY`。
3. Dashboard 顯示可視化時，標示「Not for decision use」。
4. 任何進入交易決策管線的欄位，必須通過 source whitelist + schema contract。

## Code Guide 檢查清單
- [ ] 圖譜資料輸出與決策輸入物理分離（不同檔案/資料路徑）
- [ ] 決策程式有來源白名單檢查
- [ ] 圖譜推論邊在 UI 明確標示信心級別
- [ ] 報表區分 baseline 與 experiment，不混算 KPI
