# Post-Mortem: D81 Python Scope Chain Failure (UnboundLocalError)

**Date**: 2026-04-29
**Sprint**: D81
**Severity**: HIGH — 4 consecutive failed fix attempts before architect escalation
**Status**: FIXED (v1.1.18-D81)

---

## 事故摘要

4 次連續嘗試修復同一 Python `UnboundLocalError`，耗費 2+ sprints。
錯誤訊息會「游走」——每次修復後，錯誤出現在不同變數上。

---

## 根本原因

### Python 三層 scope 模型（靜態作用域）

Python 在**編譯時期**靜態分析 scope。只要函式本體內有任何一個 `X = ...` 賦值，
Python 就把 X 標記為**整個函式的 local**，包括賦值行之前的所有讀取。

### run_radar.py `_live_ticks` 的錯誤假設

```
層 1（模組層級）：heartbeat 相關純模組變數
  → 用 `global` 宣告
  → 例如：_last_ws_diag_log_ts, _live_loop_started, _d75_hb_last, _d77_tick_last

層 2（_live_ticks local）：所有 accumulator
  → 在函式開頭初始化
  → 例如：_evt_count, _entropy_eval_total, _entropy_locked_count

層 3（_on_message nonlocal）：透過 `nonlocal` 讀寫層 2
  → nested function 內用 `nonlocal` 宣告
  → 不可同時用 `global` + `nonlocal` 指向同一變數
```

### 四種破壞模式

1. **把 local 初始化移到模組層級** → `_on_message` 的 `nonlocal` 找不到 enclosing binding
2. **型別標注的模組層級宣告 + `global x`** → "annotated name can't be global"
3. **同一變數同時 `global` + `nonlocal`** → 宣告衝突
4. **刪除 _live_ticks 內的 local 初始化，只留模組層級** → `_on_message` 的 `nonlocal` 失效

---

## 失敗歷程

| 次數 | 做法 | 結果 |
|------|------|------|
| 1 | 將 `_evt_count` 等提到模組層級並加 `nonlocal` | `UnboundLocalError: _evt_count` |
| 2 | 移回 `_live_ticks` local，移除 `nonlocal` | `UnboundLocalError: _entropy_eval_total`（游走）|
| 3 | 加 `nonlocal` 在 `_on_message`，維持 `_live_ticks` local | `UnboundLocalError: _last_ws_diag_log_ts`（游走到 module 層）|
| 4 | 加 `global` 宣告 module 層變數，但保留型別標注 | `SyntaxError: annotated name can't be global` |

---

## 正確修復（D81 final）

```python
# run_radar.py — _live_ticks() 頂部
global _last_ws_diag_log_ts, _live_loop_started
global _d75_hb_last, _d77_tick_last, _d77_tick_n
global _d75_hb_trade_base, _d75_hb_entropy_base

# 層 2 accumulator：_live_ticks local，在這裡初始化
_evt_count = {"last_trade_price": 0, "book": 0, "price_change": 0, "other": 0}
_entropy_eval_total = 0
_entropy_locked_count = 0
_entropy_history_not_ready_count = 0
_entropy_z_ready_count = 0
_entropy_z_below_threshold_count = 0
_entropy_z_samples = []

async def _on_message(msg: dict | list) -> None:
    nonlocal _evt_count, _entropy_eval_total
    nonlocal _entropy_locked_count, _entropy_history_not_ready_count
    nonlocal _entropy_z_ready_count, _entropy_z_below_threshold_count
```

---

## 驗證方式

```bash
python -c "import py_compile; py_compile.compile('panopticon_py/hunting/run_radar.py')"
```
應無輸出（編譯成功）。

---

## 教訓

1. **先查 EXPERIENCE_PLAYBOOK** — Python scope 規則是已知的痛苦模式
2. **2 次失敗果斷上報** — 不應連續嘗試 4 次
3. **非 trivial fix 必須寫入 EXPERIENCE_PLAYBOOK** — 同樣的 scope 錯誤會在其他模組復發
4. **架構師裁定後才繼續** — 複雜 scope 問題需要靜態分析，開發者不應自行猜測

---

## 相關條目

- `EXPERIENCE_PLAYBOOK.md` → EXP-D81-001
- `.cursorrules` → "Escalate after 2 failed attempts"
- `AGENTS.md` → "Pre-fix Protocol"