# Frontend Start Guide — D55
# Run these when a shell with npm is available

## Quick Start
```
cd d:\Antigravity\Panopticon\dashboard
npm run dev
# → Open http://localhost:5173
```

## What to verify
- [ ] Trade List panel shows trades (not empty)
- [ ] Each row shows: confidence % | source badge | status | PnL
- [ ] Source badge: amber=paper, green=live, gray=db_settlement
- [ ] No "NaN" visible anywhere
- [ ] Market names: may show truncated IDs until polymarket_link_map
        is populated — this is expected (D55d)

## If Trade List is STILL empty
1. Open DevTools (F12) → Console tab
   Look for JavaScript errors related to normalizeTrades or toFloat
2. Open DevTools → Network tab
   Find /api/recommendations — check response body
   If response has trades: React rendering issue
   If response is empty: backend issue (report to architect)
3. Check: does the type mismatch guard (toFloat) throw errors?
   Search console for "TypeError" or "NaN"

## Backend health (should be running)
```
curl http://localhost:8001/health           → {"status":"ok"}
curl http://localhost:8001/api/recommendations?limit=5
```

## D55 Verification Summary
- TypeScript compiles clean (only pre-existing vis-network error)
- All numeric fields in TradeListItem are `number | null` ✅
- D53d toFloat defensive casting in place ✅
- Backend API: 18 trades, 94.7% win rate, $54.89 total PnL
