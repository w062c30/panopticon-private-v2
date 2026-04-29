# Panopticon 現時掃描與決策流程完整報告（基於現行程式碼）

> 本報告**只根據目前 repo 已落地程式碼**撰寫，不包含未實作設計假設。

> ⚠️ [SUPERSEDED - D55] 本文件反映 `main_loop.py` 架構（2026-04-23），現行系統已重構為 `run_hft_orchestrator.py` + `run_radar.py`。本文件保留作歷史參考，不反映 D55 以後的架構變更。

## 1. 報告範圍與方法

本報告覆蓋以下模組與流程：

- 主循環與事件落庫：`panopticon_py/main_loop.py`
- 策略核心（Bayesian + EV + Kelly）：`panopticon_py/strategy/decide_core.py`
- 叢集風險引擎：`panopticon_py/strategy/bayesian_engine.py`
- 共識雷達：`panopticon_py/hunting/consensus_radar.py`
- 指紋清洗與不確定桶生命週期：`panopticon_py/hunting/fingerprint_scrubber.py`
- 語意路由：`panopticon_py/hunting/semantic_router.py`
- LLM HTTP 薄層：`panopticon_py/llm_backend.py`
- 語意路由 daemon：`scripts/run_semantic_router.py`

分析方式：

1. 讀取程式主路徑與函式行為。
2. 將資料流拆解為「掃描 → 分析 → 處理 → 儲存 → 決策」。
3. 提取現行公式與風控條件。
4. 列出現況可行點與疑問點。

---

## 2. 系統整體理念（現碼）

系統是「多層訊號 + 風控閘門 + 可審計決策」架構，核心目標：

- 先把市場與錢包行為轉為結構化證據。
- 用 Bayesian 更新與 Kelly 倉位把訊號轉為可執行決策。
- 在執行前施加 friction、cluster cap、unknown market 保守規則。
- 全程寫入事件/決策/執行資料，保留回放能力。

---

## 3. 端到端流程：由掃描到交易決策

## 3.1 掃描層 A：市場語意掃描（Market_Semantic_Router）

入口：`scripts/run_semantic_router.py`

流程：

1. 週期輪詢 Gamma API `/markets`。
2. 抽取 `market_id`、`title`、`description`、`tags`。
3. 呼叫 `nvidia_extract_market_semantics(...)`（`semantic_router.py`）。
4. 寫入 `data/cluster_mapping.json`（atomic write）。

語意抽取輸出（每個 market）：

- `cluster_id`（由 `Parent_Theme` 映射）
- `internal_direction`（`Directional_Vector`）
- `entities`
- `updated_ts_utc`

關鍵保護：

- 若 LLM timeout、回非 JSON、schema 驗證失敗，fallback 到：
  - `{"Parent_Theme":"UNKNOWN_CLUSTER","Entities":[],"Directional_Vector":1}`
- 檔案寫入採 `tempfile + os.replace`，避免半寫狀態被讀取。

## 3.2 掃描層 B：微觀與認知訊號

入口：`main_loop.py`

- L1：微觀市場訊號（例如 `delta_h`、`ofi`、book slice、延遲）。
- L2：認知訊號（trust/sentiment/external_event_score/timeout degrade）。
- L2 LLM 呼叫經 `llm_backend.py`（Phase 1: NVIDIA）。

## 3.3 分析層：fast gate + strategy core

1. `fast_execution_gate(...)`（main loop 內）先做 friction / 時效 / EV 可行性。
2. 若 gate 未 abort，交由 `decide_core.decide(...)` 做最終 BUY/HOLD。

## 3.4 風控層：cluster exposure 與 unknown fallback

入口：`bayesian_engine.py`

- `check_cluster_exposure_limit(...)`：
  - 計算 cluster `Net_Delta`
  - 超 cap 拒絕
  - 若為 hedge 且可降低 `|Net_Delta|`，允許例外
- 未知市場：
  - `UNKNOWN_CLUSTER` 或 `largest_cluster_rho1`
  - `unknown_bucket_5pct` 時套 5% 嚴格上限
- 缺失相關係數 `rho` 預設 `1.0`（不是 0）

## 3.5 執行與儲存

`main_loop.py` 會將結果寫入 DB：

- raw events（L1/L2/L3）
- strategy decisions
- execution records
- reservation
- pending chain reconcile
- position

語意叢集映射則由 `semantic_router.py` 寫到 `cluster_mapping.json`，供 bayesian 非同步讀取。

---

## 4. 理論、公式與其對應程式

## 4.1 Bayesian 更新（`decide_core.py`）

\[
\text{odds}_{prior}=\frac{p}{1-p},\quad
\text{odds}_{post}=\text{odds}_{prior}\cdot LR,\quad
p_{post}=\frac{\text{odds}_{post}}{1+\text{odds}_{post}}
\]

對應：`bayesian_update(...)`

## 4.2 Fractional Kelly（`decide_core.py`）

\[
b=\frac{1-price}{price},\quad q=1-p,\quad
f_{raw}=\frac{bp-q}{b},\quad
f=\max(0,\alpha \cdot f_{raw})
\]

對應：`fractional_kelly(...)`

## 4.3 EV（含摩擦）

\[
EV_{gross}=p(1-entry)-(1-p)entry
\]

\[
EV_{net}=EV_{gross}-(fee+slippage+micro\_cost)
\]

對應：`ev_net(...)`

## 4.4 共識雷達（`consensus_radar.py`）

時間衰減：
\[
w_{time}=e^{-\lambda \Delta t}
\]

有效共識：
\[
k_{eff}=k_{hybrid}\cdot w_{time}
\]

動態流動性門檻：
\[
threshold=\max(ABS\_MIN,\ ref\_notional\cdot pct)
\]

對向流懲罰：
- 若 `conflict_ratio` 超閾值：取消或懲罰 `k_eff`

## 4.5 叢集風險（`bayesian_engine.py`）

\[
Net\_Delta=\sum_i position_i\cdot \rho_i
\]

規則：

- `|Net_Delta|` 超 cap 拒絕新風險
- 若新單可令 `|Net_Delta_after| < |Net_Delta_before|`，視為 hedge exception 可放行
- unknown market 可套 5% 上限

---

## 5. 模組職責清單（現碼）

## 5.1 `panopticon_py/llm_backend.py`

- 提供 NVIDIA chat completion HTTP 呼叫
- `safe` 版本把錯誤轉 `None`，上層做 fallback

## 5.2 `panopticon_py/hunting/semantic_router.py`

- 實作語意抽取 prompt + 回應驗證
- 管理 `cluster_mapping.json` 讀寫
- 提供給 bayesian 的 mapping 載入函式

## 5.3 `scripts/run_semantic_router.py`

- 以 daemon 方式輪詢新市場
- 去重（用既有 mapping keys）
- 將語意結果寫入 cluster mapping

## 5.4 `panopticon_py/strategy/decide_core.py`

- 核心數學：Bayesian / EV / Kelly / 動作決策

## 5.5 `panopticon_py/strategy/bayesian_engine.py`

- 叢集敞口限制
- unknown market fallback
- posterior cap 與 size helper

## 5.6 `panopticon_py/hunting/consensus_radar.py`

- filled-only 過濾
- hybrid 去重
- 時間衰減 + 流動性門檻 + 對向流處理

## 5.7 `panopticon_py/hunting/fingerprint_scrubber.py`

- Kelly violation / one-hit 判別
- 4D 行為分類
- `WATCHLIST_UNCERTAIN` 週期升降級（graduate / evict / archive）

---

## 6. 由訊號到交易決策：現碼順序

以下係 `main_loop.py` 內的實際決策順序：

1. 啟動 worker（friction/cognitive/DB writer/reconcile）
2. 生成 L1 與 L2 event
3. 計算 gate input（book、latency、impact、p_prior 等）
4. `fast_execution_gate` 先判斷是否 ABORT
5. 進 `decide_core.decide` 輸出 `BUY` 或 `HOLD`
6. 若 BUY：
   - 建 execution/reservation
   - 可經 TS bridge 送單（或 paper tx）
   - 寫 pending chain / position
7. 若 HOLD：
   - 寫 skipped execution 審計

---

## 7. 已落地測試與可驗證性

語意路由相關測試（`tests/test_semantic_router.py`）覆蓋：

- 無 key fallback
- 合法 JSON 解析
- markdown code fence 清理
- 非法方向值 fallback
- atomic write roundtrip
- cluster mapping 載入
- gamma market id 解析
- row merge

---

## 8. 困難、風險與疑問（只按現碼）

## 8.1 已觀察困難

1. NVIDIA 回覆可能 timeout 或非 JSON  
   - 現碼會 fallback，不會中斷流程
   - 但 `UNKNOWN_CLUSTER` 佔比可能偏高，語意價值下降

2. Gamma 欄位可能變動  
   - 有多鍵容錯（`conditionId` / `id`）
   - 仍需定期 smoke test API shape

3. `cluster_mapping.json` 為單寫者假設  
   - 當前可行
   - 若未來多寫者應轉 SQLite/DB transaction

## 8.2 架構疑問（需產品/量化決策）

1. `semantic_router` 與 `main_loop` 的實時耦合程度  
   - 模組已完成，但主 loop demo 流程未完全串入 consensus/semantic 的「交易前必經」路徑

2. fallback 容忍門檻  
   - 需定義可接受 fallback rate（例如 5%-10%）

3. 叢集關聯係數來源  
   - 目前支持注入 matrix；需明確資料來源與更新頻率

---

## 9. 結論

就現有程式碼，Panopticon 已具備：

- 語意掃描與落盤
- 多層訊號與風控數學核心
- 叢集風險與 unknown fallback
- 可審計儲存路徑

最關鍵待持續優化點：

- 提升 LLM 回傳結構化穩定度（降低 fallback）
- 確保語意/共識/叢集風控在 live decision path 的整合深度
- 以首日運營監控驗證錯誤率與資料品質

---

## 10. 附錄：主要參考程式檔

- [`panopticon_py/main_loop.py`](d:\Antigravity\Panopticon\panopticon_py\main_loop.py)
- [`panopticon_py/strategy/decide_core.py`](d:\Antigravity\Panopticon\panopticon_py\strategy\decide_core.py)
- [`panopticon_py/strategy/bayesian_engine.py`](d:\Antigravity\Panopticon\panopticon_py\strategy\bayesian_engine.py)
- [`panopticon_py/hunting/consensus_radar.py`](d:\Antigravity\Panopticon\panopticon_py\hunting\consensus_radar.py)
- [`panopticon_py/hunting/fingerprint_scrubber.py`](d:\Antigravity\Panopticon\panopticon_py\hunting\fingerprint_scrubber.py)
- [`panopticon_py/hunting/semantic_router.py`](d:\Antigravity\Panopticon\panopticon_py\hunting\semantic_router.py)
- [`panopticon_py/llm_backend.py`](d:\Antigravity\Panopticon\panopticon_py\llm_backend.py)
- [`scripts/run_semantic_router.py`](d:\Antigravity\Panopticon\scripts\run_semantic_router.py)
