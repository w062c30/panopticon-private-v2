# Hunting Phase 1b — 鏈上深度索引與 UMA 元遊戲

本文件為 **Phase 1b** 規劃（**不**阻塞 Moralis-only MVP 冷啟動）。

## 1. ConditionResolution 與極端價格門檻

- **目標**：還原 Polymarket CTF 在過去 90 天內「結算為 YES 且曾在極低價（例如 &lt;0.15）大量買入」的地址集合。
- **可行資料管線**（擇一或組合）：
  - **The Graph / Goldsky Subgraph**：事件 `ConditionResolution`、`OrderFilled` 等；需維護 subgraph endpoint 與 schema 版本成本。
  - **BigQuery 公開資料集**：適合離線批次；需 GCP 帳單與 SQL 維護。
  - **自建 `eth_getLogs`**：依 `POLYGON_RPC_URL` 拉 `ConditionResolution` topic；受 RPC 限額、歷史區塊範圍與索引器穩定性限制。
- **成本與風險**：深度分頁與大範圍 logs 易觸發限流；必須與 **Moralis 剪枝策略**（見主計畫）一致，採 **啟發式 Break**，避免 OOM / CU 耗盡。

## 2. UMA DVM / Voting Token 聯合監聽

- **動機**：極端倉位可能與 **預言機仲裁權** 結合（非僅 CTF 內價格內幕）。僅盯 CTF Exchange 會低估 **規則層** 風險。
- **建議**：
  - 索引 Polygon 上 **UMA Voting Token** 轉帳與（若可得）**投票合約** delegate / reveal 事件。
  - 與 Polymarket 大戶地址、種子圖譜 **≤2 跳** 地址做時間窗關聯；輸出 **`oracle_meta_risk`** 標籤供 L2 / 影子模式降權或審計。
- **合規**：標籤為 **風險與審計用途**；不宣稱鏈下司法意義；合約地址與 ABI 放配置，不硬編碼私鑰。

## 3. 與 MVP 的銜接

- Phase 1 MVP 仍以 **Moralis 可得的錢包活動 + 4D 矩陣 + Redis 種子** 為主。
- 本 Phase 1b 項目成熟後，可將種子分數與 **ConditionResolution** 證據鏈結，並把 **UMA 關聯度** 併入 `insider_score_snapshots` 的 `reasons_json` 或獨立欄位（需新一輪 schema 設計）。
