# Trade List Browser Verification — DevTools Guide
# For architect to run manually when browser/npm is available

## Setup
```powershell
cd d:\Antigravity\Panopticon\dashboard
npm run dev
# → Browser: http://localhost:5173
```

## DevTools Checks (F12 → open DevTools)

### Check 1: API response types
Network tab → XHR/Fetch → find /api/recommendations
Click → Response tab
Verify: confidence is 0.55 (number), NOT "0.55" (string)
Verify: exitPrice is null, NOT "None" (string)

### Check 2: Console errors
Console tab → filter "error"
If you see: "NaN" or "Cannot read properties of null"
→ React rendering bug, paste exact error to architect

### Check 3: React component state (if React DevTools installed)
Components → TradeListPanel → props → trades
Verify: each trade.confidence is typeof number

### Check 4: Visual confirmation
- [ ] Trade rows visible (not empty panel)
- [ ] Confidence column: "55.0%" format
- [ ] Source badge: amber background = paper
- [ ] PnL column: "$2.89" format (not NaN)
- [ ] Market name: may show truncated ID (expected until link_map populated)

### Check 5: RVF Metrics Panel (L5 共識錢包 section)
Hover "準備好共識的Market" → should show "{total} (top {ready})" e.g. "259 (top 10)"

## If Panel is STILL Empty
1. Check WebSocket race condition (see below)
2. Verify backend is running:
   curl http://localhost:8001/api/recommendations?limit=5
3. If backend returns empty: signal pipeline issue, report to architect
4. If backend returns data but panel empty: React data flow issue

## WebSocket Race Condition Check (D56b)
The liveAdapter.ts connects to the WebSocket on mount. If the WS message handler
resets trades to [] AFTER the REST fetch populates them, the list would clear.

Check for this pattern in liveAdapter.ts:
grep -n "trades.*=.*\[\]\|setTrades.*\[\]" dashboard/src/data/liveAdapter.ts

If the WS connect handler calls setTrades([]), the REST data (fetched on mount)
would be immediately overwritten with an empty array.

Current status: NOT FOUND in liveAdapter.ts (safe). If found in future,
report to architect before any fix — WS logic affects real-time data flow.
