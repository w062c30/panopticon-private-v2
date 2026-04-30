# 👁️ Project Panopticon: Core Trading Logic & Architectural Invariants
# Version: 2.7
# Last Updated: 2026-04-30
# Changelog: v2.7 — D100 Kyle λ buffer bugfix（append_kyle_lambda_sample 重複定義合併 + flush guard bypass 監測）; EXPERIENCE_PLAYBOOK.md EXP-D100-001
# Changelog: v2.6 — D81 Identity Coverage Log + Transfer Entropy Cache; Invariant 4.2 修訂（背景計算白名單 + TE bool-only 約束）
# Changelog: v2.4 — D29 WS snapshot staleness fix (mc.on_ws_message); T1 KeyError(token_id) fix; T1 prefetch 3->5; NTP sync (ntplib); subscription cache guard; whale_scanner CLOB depth + thin-book signal; D30 kyle_lambda pending-price root-cause fix; PANOPTICON_WHALE default on
# Changelog: v2.3 — D26 hook wiring complete; D27 persistent WS (_ws_runner); D27 T1 startup init; D28 SKIP investigation (see notes); Phase 5 whale_scanner.py foundation added
# Changelog: v2.2 — Phase 1 WS Protocol: added app-level PING heartbeat (10s), tick_size_change handler, price_change book-snapshot handler, PONG filter
# Changelog: v2.1 — Added L1 Market Tiering (T1/T2/T3/T5); D14 T5 segmentation; D15 T2 resolved filters; SignalEvent.market_tier field

**[致 AI 開發代理 (To the AI Agent)]**
這份文件是本系統的「最高憲法」。你在修改、重構或最佳化任何程式碼時，**絕對不允許**破壞以下定義的理論基礎與架構不變式（Invariants）。任何違反這些原則的 PR 或程式碼產出，將被視為對系統風控的致命破壞。

---

## 🏛️ 核心哲學 (Core Philosophy)
本系統是一個針對 Polymarket 預測市場（含政治長線與 5 分鐘加密貨幣超短線）的高頻量化狙擊機器人。
* **過程大於結果 (Process over Outcome)**：我們不盲目跟隨利潤。我們利用香農信息論與圖譜演算法，只跟隨「具備機構級紀律或內線資訊的實體」。
* **防禦性悲觀主義 (Defensive Pessimism)**：在微秒級別的市場中，任何理論利潤都會被摩擦力（滑點、手續費、延遲）吃光。寧可錯過交易，絕不承擔未知的流動性風險。
* **寄生與降維打擊 (Parasitic Asymmetric Edge)**：我們不與做市商 (MM) 拼絕對硬體速度，我們利用「維度」優勢，捕捉他們演算法的延遲與破綻，作為「毒性流動性 (Toxic Flow)」進行狙擊。

---

## 🛑 第一定律：信號與感知不變式 (L1: Signal & Perception)
**目標：在噪音中捕捉真實的秩序降臨，絕對過濾假動作。**

* **[Invariant 1.1] 交易驅動的香農熵 (Trade-Conditioned Entropy)**：實時雷達判斷「異動」的唯一標準是香農熵 $H$ 的暴跌（$\Delta H < -4\sigma$）。**嚴禁**單獨使用報價變化（Quote-Tick）觸發信號，熵的計算必須嚴格綁定真實成交（Trade-Tick），以防禦 MM 撤單製造的假象。
* **[Invariant 1.2] 領先-滯後拓撲 (Lead-Lag Topology)**：針對 5 分鐘加密貨幣市場，必須監聽領先交易所（如 Hyperliquid）的訂單流不平衡 ($OFI$) 作為預判信號，並在 50~150 毫秒的空窗期內狙擊 Polymarket 的滯後報價 (Stale Quotes)。OFI 訊號必須通過 `asyncio.Queue[SignalEvent]` 進入 `signal_engine`，禁止繞過共識貝氏決策直接觸發執行。OFI → Polymarket 市場映射必須使用靜態 `OFI_MARKET_MAP` 配置表，禁止使用 Graph 推論或 ML 模型動態生成映射。
* **[Invariant 1.3] 狀態防污染 (Stale Buffer Flush)**：若 WebSocket 發生斷線重連，或 Tick 之間延遲 $> 500ms$，必須無條件清空當前 60 秒的雷達緩衝區。絕不允許在殘缺資料上發動攻擊。

* **[Invariant 1.4] 市場分层订阅 (Market Tiering)**：L1 雷達必須對不同類型市場實施分層訂閱，不可對所有 200 個市場套用相同策略：
  * **T1（5 分鐘加密）**：僅接受包含 `updown-5m` / `btc-up` 等關鍵字，到期時間 1-35 分鐘，成交量 ≥ $100 的市場。最高優先級，用於 Kyle λ 校準。
  * **T1 滾動預載**：採用動態 slug 規則預載 5 個視窗（current + 4），3 個資產共 30 個 token（6 tokens × 5 windows）。
  * **T1 訂閱抗抖動**：空 refresh 週期不得清空活躍訂閱；必須沿用最近一次成功快取，防止 `t1=0` 假性崩塌。
  * **T2（短期事件 3-30 天）**：排除演算法市場（`updown`/`5m` 等關鍵字）、體育分類、已解決/封閉市場、`bestBid ≥ 0.99` 或 `≤ 0.01` 的 near-certain 市場。成交量門檻 ≥ $5,000。最高 Smart Money Edge。
  * **T3（長線市場）**：現有邏輯，保留用於 L2 Discovery wallet_observations 數據源。
  * **T5（LIVE 體育 < 48 小時）**：僅接受體育分類、`active=True`、到期時間 ≤ 48 小時的市場。排除賽季/冠軍關鍵字（`champion`/`winner`/`world-cup-winner` 等）。信號進 `signal_engine` 時使用 `p_prior = 0.50`（保守基準，無財務 insider）。
  * **T5b（賽季冠軍市場）**：嚴禁訂閱 NBA Champion / FIFA World Cup 等長期賽季市場（錯誤的 `p_prior`，屬 Smart Money 市場，不屬 Shannon Entropy 有效範圍）。
  * **OFI 映射排除**：T2 / T3 / T4 / T5 市場不得加入 `OFI_MARKET_MAP`——它們的 Smart Money 信號來源是 L2 wallet_observations，不是 Hyperliquid OFI。

* **[Invariant 1.5] 時間戳合約 (Timestamp Contract)**：所有內部持久化時間戳（`ts_utc`/`*_ts_utc`/`created_at`）**必須**使用 RFC3339 UTC 毫秒字串，結尾為 `Z`（例如 `2026-04-25T11:35:00.765Z`）。外部 API 時間戳在 DB/內部合約邊界僅作正規化。Duration/TTL 時鐘保持單調遞增。代理**嚴禁**向內部表寫入 `+00:00` 或裸露的 epoch-ms/數字時間戳。

---

## 🛑 第二定律：發現與圖譜法證不變式 (L2: Discovery & Graph Forensics)
**目標：確保資料庫裡只有真正的 Smart Money，看穿機構的錢包矩陣。**

* **[Invariant 2.1] 雙軌資料源 (Dual-Track Harvesting)**：系統必須維持 Track A（微觀 CLOB Taker 抓取，捕捉新拋棄式錢包）與 Track B（宏觀排行榜 API 抓取，捕捉老錢）雙軌並行。
* **[Invariant 2.2] 實體圖譜折疊 (Graph Entity Folding)**：嚴禁將單一地址視為最終實體。必須透過圖論演算法 (`networkx`)，將「具備資金重合度」或「在 $<100ms$ 內同步吃單」的散碎免洗錢包，折疊聚類為單一 `HFT_FIRM_CLUSTER`。Graph 演算法的唯一決策層輸出是**獨立實體數量 $n$**（integer），此整數作為貝氏共識中的獨立觀測數使用。Graph 的相似度分數、嵌入向量或任何連續值輸出**禁止**直接進入貝氏公式或 L4 Gate。
* **[Invariant 2.3] 強制 4D 洗脫 (Mandatory 4D Scrubbing)**：所有候選錢包進入 `Seed_Whitelist` 前必須經過洗脫。
    * **做市商防禦**：`庫存單向性 (IDI) < 0.3` 必須被丟棄。
    * **賭徒防禦**：違背凱利公式（如單筆交易佔最大餘額 $> 50\%$），標記為 `DEGEN_GAMBLER` 並丟棄。
* **[Invariant 2.4] 本地快取優先 (Cache-First DAL)**：計算 4D 矩陣或圖譜關聯時，必須先查本地 SQLite/DB。資料不足時才允許呼叫外部 API (如 Moralis)。

---

## 🛑 第三定律：決策與資金不變式 (L3: Bayesian Engine & Kelly Sizing)
**目標：利用數學優勢下注，保證長期生存。**

* **[Invariant 3.1] 共識貝氏更新 (Consensus Bayesian Update)**：最終決策必須基於先驗機率與概似比（LR）相乘。同一事件中，發現的獨立高頻實體數量越多，勝率才允許指數級上升。此規則對**所有訊號來源一律適用，包括 OFI 快速路徑**，不存在任何豁免——OFI 路徑僅允許跳過 Leaderboard 歷史評分以縮短查詢窗口，但貝氏後驗計算本身不可省略。
* **[Invariant 3.2] 凱利倉位防護 (Kelly Criterion Guardrail)**：系統的建倉大小必須嚴格遵守 Fractional Kelly 公式 ($f^*$)。防禦任何情緒化或固定金額的 All-in 盲目重倉行為。

---

## 🛑 第四定律：實盤執行與幽靈流動性防禦 (L4: Execution Frictions)
**目標：保護本金，物理性消滅滑點、幽靈流動性與做市商陷阱。**

* **[Invariant 4.1] 真空與幽靈流動性過濾 (Ghost Liquidity Filter)**：
    * **Volume Floor**：若價格劇烈跳動但真實市價成交量小於安全底線，判定為 MM 撤單真空，直接丟棄信號。
    * **$\lambda$ 異常斷路器**：計算 Kyle's Lambda（$\Delta P / V_{trade}$，動態計算）。若 $V_{trade} = 0$ 或 $\lambda$ 趨近無限大，系統必須無條件鎖死扳機 (ABORT)。
* **[Invariant 4.2] 零延遲斷路器與背景計算調度邊界 (O(1) Circuit Breakers & Async Scheduling)**：
  主決策函式（`signal_engine._process_event()` 及所有 L4 Gate 調用棧）中只能進行 $O(1)$ 的讀取與相減。嚴禁在下單關鍵路徑上發出任何 I/O 請求，或執行計算複雜度超過 $O(1)$ 的算法。

  **背景預計算白名單（必須以 `asyncio.to_thread()` 或 `run_in_executor()` 實行執行緒隔離）：**

  | 指標 | 更新週期 | 快取形式 | 決策路徑讀取介面 |
  |-----|---------|---------|----------------|
  | `ping_ms` | 5s | `float` | 直接讀值 |
  | `fee_rate` | 30s | `float` | 直接讀值 |
  | `kyle_lambda_p75[asset_id]` | 60s | `dict[str, float]` | 直接讀值 |
  | `transfer_entropy_cache[market_pair]` | 15–30s | `bool`（閾值後） | **只讀 `.is_significant: bool`，禁讀 `.cached_value: float`** |

  **Transfer Entropy 合規使用條件：**
  * ✅ 允許：基於 CLOB WS 匿名 tick 序列的市場級 TE，在獨立背景 task 中以 `asyncio.to_thread()` 預計算，結果快取為布爾標誌，決策路徑僅 O(1) 讀取
  * ✅ 允許：基於 `wallet_observations` 的錢包級 TE，在路徑 B 的 `asyncio.to_thread()` 中計算，用於 L2 Discovery 離線評分（非 LR 直接輸入，符合 Invariant 6.2）
  * ❌ 禁止：任何 TE 積分、窗口求和、矩陣運算出現在 `signal_engine._process_event()` 或 `fast_gate.py` 調用棧內（即使以協程形式）
  * ❌ 禁止：TE `.cached_value`（`float`）直接作為貝氏 LR 的輸入（違反 Invariant 6.2——TE 不具備機率語意）；必須先通過閾值轉為布爾標誌，再轉換為整數 $n$（符合 Invariant 2.2）
  * ❌ 禁止：在 T1 高峰期（`trade_ticks_60s > 800`）縮短 TE 重算週期（反而增加 event loop 調度壓力）

  **背景計算調度規範（適用所有白名單項目）：**
  * 計算前必須在 event loop 內做 O(1) 資料快照（`list()` / `copy()`）
  * 計算函式本身必須是無副作用的純函式（pure function）
  * 使用 `asyncio.wait_for(timeout=8.0)` 防止執行緒池掛起
  * 所有快取以標量或淺層 dict 形式暴露，禁止暴露大型資料結構給決策路徑
  * `pending_entropy_signals` DB 輪詢模式（5s 延遲）屬於違反此 Invariant 的反模式，已廢棄，強制使用 `asyncio.Queue` 零延遲傳遞
* **[Invariant 4.3] 淨期望值硬閘門 (Strict Net EV Gate)**：若扣除延遲滑點與手續費後的真實預期利潤 $EV_{net} \le 0$，拒絕交易。`fast_gate.py` 是系統唯一的 L4 實現，所有訊號來源（radar 或 ofi）必須經過同一套 Gate 參數，不得為任何來源建立獨立旁路。
* **[Invariant 4.4] 禁止市價單 (No Market Orders)**：送出至 Polymarket CLOB 的 EIP-712 簽名訂單，`orderType` 必須硬編碼為 **`FOK (Fill-Or-Kill)`**。絕對不允許訂單在未成交的情況下掛在簿子上成為對手盤的肉雞。

---

## 🛑 第五定律：系統狀態與運維不變式 (L5: Operations & Shadow Mode)
**目標：確保系統在資料不足時保持安靜，具備機構級的風控底線。**

* **[Invariant 5.1] 實盤解鎖閘門 (Go-Live Readiness)**：從 `Shadow Mode` 切換到 `Live Trading` 前，必須滿足統計顯著性（如 `trades >= 100` 且 `win_rate > 55%`）。嚴禁 AI 或系統單方面自動開啟實盤。所有交易記錄（含模擬）統一寫入 `execution_records` 表，以 `mode` 欄位區分 `'LIVE'` 與 `'PAPER'`，`paper_trades` 獨立表已廢棄。
* **[Invariant 5.2] 資金池隔離 (Inventory Isolation)**：單一事件的總曝險，無論勝率多高，絕對不得超過總資金池的固定百分比（如 25%），以防禦預言機 (Oracle) 被操縱的尾部風險。

---

## 🛑 第六定律：演算法邊界不變式 (L6: Algorithm Boundary)
**目標：確保 Graph 演算法與 ML 模型只在合法的感知預處理層運作，絕不污染決策鏈的機率語意。**

**理論基礎**：本系統的決策鏈建立在香農信息論的熵量化、貝氏機率論的可更新後驗、以及凱利準則的最優倉位三位一體之上。進入這條決策鏈的每一個數值必須具備**清晰的機率語意**，可追溯至真實 Trade-Tick 的觀測事實。Graph 分數和 ML 輸出是結構性相似度，不是機率密度，不具備機率語意，強行插入決策鏈將破壞整個數學框架的一致性。

* **[Invariant 6.1] Graph 演算法合法使用範圍 (Graph Scope)**：
    * ✅ **允許**：`graph_linker.py` 使用 `networkx` 進行實體折疊（Entity Folding），將多個錢包地址聚類為 `HFT_FIRM_CLUSTER`。其唯一的決策層輸出是**整數 $n$**（獨立實體數量），此整數輸入貝氏共識的 $\prod LR_i$ 計算。
    * ✅ **允許**：Graph 用於人類可視化（儀表板展示錢包關係），此用途不介入任何決策邏輯。
    * ❌ **禁止**：Graph 的相似度分數、PageRank 值、社群標籤等連續輸出直接作為貝氏 LR 的輸入或替代品。
    * ❌ **禁止**：使用 `correlation_edges` 或任何 Graph 推論結果進行跨交易所市場映射（如 OFI_MARKET_MAP）。

* **[Invariant 6.2] ML 模型合法使用範圍 (ML Scope)**：
    * ✅ **允許**：ML 模型用於離線分析、回測輔助、人類研究報告，產出結果不進入任何實時決策路徑。
    * ❌ **禁止**：ML 模型的打分輸出直接替代或疊加 `insider_score`，進而影響貝氏 LR 計算。
    * ❌ **禁止**：ML 模型的輸出直接觸發 `asyncio.Queue` 放入 `SignalEvent`。
    * ❌ **禁止**：ML 模型介入 L4 EV Gate 的任何輸入參數（p_adj、qty、avg_entry、fee 等）。

* **[Invariant 6.3] 決策鏈純淨性 (Decision Chain Purity)**：
    凡進入 L2/L3 貝氏計算、L4 EV Gate，或被封裝進 `SignalEvent` 的任何數值，必須滿足以下條件之一：
    1. 直接來源於真實 Trade-Tick 的統計彙總（如 Shannon Entropy z-score、wallet_observation 聚合值）
    2. 貝氏後驗計算的中間結果（如 p_prior、LR_i、posterior）
    3. Kelly 公式的確定性計算結果
    4. O(1) 讀取的背景快取確定值（如 ping_ms、fee_rate）
    
    任何違反以上條件的數值注入，均視為對本系統理論基礎的致命破壞。

---

## 🛡️ Architecture Rules — WebSocket & Identity (D68)

### RULE-ARCH-WS-1: WebSocket 強制使用規則
real-time 成交數據必須使用 WebSocket。
- CLOB WS: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- RTDS WS: `wss://ws-live-data.polymarket.com`
- **違禁**: GET /trades 在任何 loop/sleep 組合 = VIOLATION
- **例外**: data-api /trades 無 WebSocket 等效端點，4s poll 為合規替代方案

### RULE-ARCH-WS-2: 成交數據完整欄位
每筆 trade 記錄必須儲存以下所有欄位，缺少任何欄位 → NULL，不得丟棄整筆：
`proxyWallet, name, price, size, side, outcome, timestamp, transactionHash`

### RULE-ARCH-WS-3: 身份主鍵
`proxyWallet` 是唯一不可更改的用戶識別符（Polygon 地址）。
`name` / `pseudonym` 僅作顯示用，**不得**作為主鍵或 JOIN key。

### RULE-ARCH-WS-4: Insider Detection 端點
- 主要來源: `GET https://data-api.polymarket.com/trades`
- 用戶歷史: `GET https://data-api.polymarket.com/activity?user=0x...`
- 兩個端點均免認證，每筆成交包含 proxyWallet

---

## 🔴 Rules Added D68/D69 — API, Market, Data, Process

> Source: D68/D69 解難經驗歸檔。違反任何一條 = 立刻 STOP + ESCALATE。

---

### RULE-API-1: 新端點驗證先行（D68 usdcSize Bug 教訓）
**觸發條件**: 任何新端點第一次使用前
**要求**:
1. 先執行：`curl "<endpoint>?limit=3" | python3 -m json.tool`
2. 列印所有 key 名稱，逐字確認
3. 寫入 `docs/api_schema/polymarket_verified_YYYY-MM-DD.md`
4. 只有在 schema 文件中有記錄的欄位，才可以在代碼中 `raw.get("FIELD")`

**違禁**: 根據命名慣例推測欄位存在（如 `usdcSize` 被推測存在但實際不存在）

---

### RULE-API-2: 計算欄位不依賴 API 預算值（D68 usdcSize Bug 教訓）
**規則**: USD 金額、手續費等計算欄位必須從 verified raw 欄位自行計算
**標準實作**:
```python
# CORRECT — usdc_size from verified raw fields
usdc_size = round(float(raw.get("size") or 0) * float(raw.get("price") or 0), 4)
```
**違禁**: `float(raw.get("usdcSize") or 0)` 這類依賴可能不存在的計算欄位

---

### RULE-API-3: raw.get() 必須標注驗證來源
**規則**: 每個 `raw.get("FIELD")` 必須有 inline comment 標注來源和驗證日期
**格式**:
```python
proxy_wallet = raw.get("proxyWallet", "")  # verified: data-api /trades 2026-04-28
usdc_size    = ...                          # computed: size×price (usdcSize absent)
```

---

### RULE-API-4: API Schema 文件化義務
**規則**: 每次 live curl 驗證後，必須更新 `docs/api_schema/` 對應文件
**必填欄位**:
- 端點 URL
- Auth 要求
- 每個 query param（名稱、類型、是否必填）
- 每個 response key（名稱、類型、是否穩定）
- ⚠️ Known Missing / Known Gotchas 章節

---

### RULE-MKT-1: 禁止用 Spread 判斷市場類型（D67 Root Cause）
**背景**: D67 用 `(ask - bid) > 0.85` 判斷 AMM，導致 BTC 5m 被誤殺
- BTC 5m 外部 AMM 報價：bid=0.01/ask=0.99（spread=0.98）
- BTC 5m 實際成交：0.30–0.70，458 trades/10min = 真實 CLOB 活動
- Spread 只反映做市商報價，不反映實際交易類型

**違禁**: `is_amm_market(bid, ask)` 任何基於 spread threshold 的實作

---

### RULE-MKT-2: 市場類型唯一有效判斷方式（D69 裁決）
**唯一有效方法**: 查詢最近 5 分鐘是否有實際 CLOB 成交
```python
def has_recent_clob_trades(token_id, lookback_secs=300) -> bool:
    # GET https://clob.polymarket.com/trades?token_id=...&limit=1
    # 有成交且 timestamp 在 lookback 內 → True（CLOB/hybrid）
    # 無成交 → False（可能純 AMM）
    ...
```
**市場分類**:
- `has_recent_clob_trades = True` → CLOB 或 hybrid → fetch_best_ask 正常返回
- `has_recent_clob_trades = False` → 可能 AMM → 再用 spread 作 fallback 判斷

---

### RULE-MKT-3: polymarket_link_map 最低完整度要求（D68d 教訓）
**背景**: D68d 診斷發現 link_map 只有 1 row，導致 signal engine 無市場可選
**要求**:
- Sprint 開始前確認 link_map 行數 > 10
- 每行必須有 `token_id`（非 NULL）
- `source` 欄位不得全為 `background_fetch`（需有主動 resolve 記錄）
- 每週至少 refresh 一次（市場到期後 token_id 失效）

---

### RULE-DATA-1: 資料污染發現後立即停止收集（D68 Phase 0 教訓）
**觸發**: 任何欄位出現恆定異常值（全為 0、全為 None、全相同）
**強制步驟**:
1. 立刻停止資料管線
2. 備份現有資料（`CREATE TABLE x_backup AS SELECT * FROM x`）
3. 清除污染資料（`DELETE FROM x`）
4. 修復 bug，驗證 fix（curl 驗證 → 打印 3 筆樣本確認非零）
5. 重新開始收集

**禁止**: 讓污染資料與乾淨資料混存

---

### RULE-DATA-2: wallet_activity 入庫前 3 欄位驗證（D68 usdcSize Bug 教訓）
**強制**: 下列 3 個欄位必須非零/非空才可 INSERT：
```python
assert proxy_wallet,     "proxy_wallet must not be empty"
assert usdc_size > 0,    "usdc_size must be > 0"
assert transaction_hash, "transaction_hash must not be empty"
# 三者任一失敗 → skip INSERT（log warning），不拋 exception
```
**目的**: 防止靜默 bug（欄位值錯誤但不報錯）汙染整個資料集

---

### RULE-DATA-3: 身份主鍵規範（RULE-ARCH-WS-3 延伸）
**規則**:
- `proxyWallet` = 唯一身份鍵（Polygon 地址，不可更改）
- `name`/`pseudonym` = 顯示用，可更改，**禁止**用作 JOIN key、dedup key、GROUP BY key
- 所有 insider 分析必須用 `proxy_wallet` 做 aggregation
- `transactionHash` = 去重鍵（UNIQUE constraint in DB）

---

### RULE-PROC-1: Kill 舊進程是強制第一步（非可選）
**規則**: 任何代碼變更、任何測試執行前，必須先 Kill 並確認所有舊進程死亡
**驗證方法**:
```python
# Kill
subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
time.sleep(2)
# Verify dead
r = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True)
assert str(pid) not in r.stdout, f"STILL ALIVE: PID={pid} — do not proceed"
```
**後果**: 不殺淨 → 舊代碼繼續運行 → 所有測試結果無效

---

### RULE-PROC-2: RTDS 連接 ≠ 有資料（D68 Phase 0 教訓）
**背景**: D68 Phase 0 RTDS WS 顯示 connected 但 0 price ticks in 10 min
**規則**:
- RTDS 連接成功只代表 TCP 連接建立，不代表訂閱生效
- 在 handoff 中報告 "RTDS running ✅" 必須附上實際 tick count
- 如 tick count = 0，必須在 Open Questions 列出，不可標記為 ✅
- RTDS 訂閱格式需在每個 sprint 開始時用 curl/wscat 驗證

---

## 📚 Polymarket API 快速參考卡（D68/D69 驗證）

| 需求 | 端點 | Auth | 身份資料 |
|------|------|------|---------|
| 市場成交（含用戶身份） | `data-api.polymarket.com/trades` | ❌ 不需要 | ✅ proxyWallet |
| 指定錢包歷史 | `data-api.polymarket.com/activity?user=0x...` | ❌ 不需要 | ✅ proxyWallet |
| 實時成交價（無身份） | CLOB WS `ws-subscriptions-clob.polymarket.com` | ❌ 不需要 | ❌ 無 |
| BTC/ETH 參考價 | RTDS WS `ws-live-data.polymarket.com` | ❌ 不需要 | ❌ 無 |
| 市場 conditionId/tokenId | `gamma-api.polymarket.com/markets?slug=...` | ❌ 不需要 | ❌ 無 |
| Polygon tx 驗證 | `polygonscan.com/tx/<txHash>` | 可選 | ✅ 手動查 |

**重要**: Polygon API 非必要。txHash 已在 data-api 回傳，polygonscan 只作人工審計用。

---

## 📊 Monitoring Health Targets

* `elapsed_since_last_ws_msg < 15s`（D29 前曾觀察到 ~153s staleness）
* `trade_ticks_60s > 500`（BTC 5m 活躍窗口目標）
* `book_events_60s > 300`
* `kyle_lambda_samples/10min > 0`（D30 後關鍵恢復指標）
* `whale_alerts/hour`：啟用初期僅觀測，不設硬門檻

---

## 📐 架構不變式摘要表

| Invariant | 定律 | 一句話描述 |
|-----------|------|-----------|
| 1.1 | L1 | 熵必須綁定 Trade-Tick，禁用 Quote-Tick |
| 1.2 | L1 | OFI 必須進 Queue，靜態映射表，禁 ML/Graph 映射 |
| 1.3 | L1 | WS 斷線或延遲 >500ms 必須 flush 緩衝區 |
| 1.4 | L1 | 市場分層訂閱（T1/T2/T3/T5）；T5 用 p_prior=0.50；T2 排除已解決/near-certain 市場 |
| 1.5 | L1 | 所有內部時間戳用 RFC3339 UTC Z；外部 API 時間戳在邊界正規化 |
| 2.1 | L2 | 雙軌資料源並行 |
| 2.2 | L2 | Graph 折疊輸出整數 n，禁連續分數進入決策鏈 |
| 2.3 | L2 | IDI < 0.3 丟棄；賭徒行為丟棄 |
| 2.4 | L2 | 本地快取優先，不足才呼叫外部 API |
| 3.1 | L3 | 所有訊號來源必須執行貝氏後驗，OFI 無豁免 |
| 3.2 | L3 | Fractional Kelly 強制倉位上限 |
| 4.1 | L4 | Kyle's λ 動態計算；Volume Floor；幽靈流動性 ABORT |
| 4.2 | L4 | 決策路徑 O(1) 只讀；禁 DB polling；禁 I/O；背景計算須 to_thread() 執行緒隔離；TE 只讀 bool |
| 4.3 | L4 | EV_net ≤ 0 → GATE_ABORT；fast_gate.py 唯一 Gate |
| 4.4 | L4 | 強制 FOK，禁市價單 |
| 5.1 | L5 | 實盤需統計顯著性解鎖；紀錄統一進 execution_records |
| 5.2 | L5 | 單一事件曝險 ≤ 25% 資金池 |
| 6.1 | L6 | Graph 